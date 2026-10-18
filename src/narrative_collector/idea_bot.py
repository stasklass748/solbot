"""Conservative X-post idea cards; no trade execution or paid API calls.

The local collector database is read-only. Dry-run is the default, and
Telegram delivery requires an explicit --send plus credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import load_dotenv

URL = re.compile(r"https?://\S+", re.IGNORECASE)
WORDS = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'-]*")
ADDRESS = re.compile(r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])")
PROMO = re.compile(
    r"\$[A-Za-z]{2,12}\b|\b(?:contract address|buy now|presale|airdrop|"
    r"pump\.fun|dexscreener|gmgn|axiom\.trade)\b|\bca\s*:",
    re.IGNORECASE,
)
STOP = {"the", "this", "that", "with", "from", "what", "when", "where", "your", "have",
        "just", "about", "today", "there", "their", "would", "could", "should", "video",
        "people", "really", "after", "before", "these", "those", "first", "every", "watch",
        "my", "you", "and", "for", "are", "its", "into", "has", "was", "how", "why",
        "это", "как", "что", "вот", "для", "или", "они", "его", "она", "уже"}


@dataclass(frozen=True)
class Idea:
    post_id: str
    post_url: str
    created_at: str
    first_observed_at: str
    author: str
    text: str
    name: str
    ticker: str
    description: str
    local_name_matches: int
    local_ticker_matches: int


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _draft(text: str) -> tuple[str, str, str] | None:
    clean = URL.sub("", text)
    clean = re.sub(r"@[A-Za-z0-9_]+", "", clean)
    words = [word.strip("'-") for word in WORDS.findall(clean)]
    useful = [word for word in words if len(word) >= 3 and word.casefold() not in STOP]
    if not useful:
        return None
    name = " ".join(useful[:2]).title()[:40]
    latin = [word for word in useful if word.isascii()]
    ticker = (latin[0] if latin else "".join(word[0] for word in useful[:4])).upper()[:10]
    if len(ticker) < 2:
        return None
    excerpt = " ".join(clean.split())[:180]
    description = f"Черновик мем-идеи по X-посту: {excerpt}"
    return name, ticker, description


def candidates(db_path: Path, now: datetime, max_age_minutes: int, limit: int,
               discovery_lag_minutes: int = 30, immutable_snapshot: bool = False,
               only_post_ids: set[str] | None = None) -> list[Idea]:
    if max_age_minutes < 1 or limit < 1 or discovery_lag_minutes < 0:
        raise ValueError("age, limit and discovery lag must be positive")
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    if immutable_snapshot:
        # Only for a completed export: immutable ignores any live WAL file.
        uri += "&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cutoff = (now - timedelta(minutes=max_age_minutes)).isoformat()
        query = ("SELECT post_id, created_at, first_observed_at, text, author_username "
                 "FROM x_first_posts WHERE first_observed_at >= ? AND text IS NOT NULL")
        params: list[str] = [cutoff]
        if only_post_ids is not None:
            query += " AND post_id IN (" + ",".join("?" for _ in only_post_ids) + ")"
            params.extend(sorted(only_post_ids))
        query += " ORDER BY first_observed_at DESC LIMIT 500"
        rows = conn.execute(query, params).fetchall()
        used = {row[0] for row in conn.execute(
            "SELECT DISTINCT source_post_id FROM launch_source_metadata WHERE source_post_id IS NOT NULL"
        )}
        result: list[Idea] = []
        seen_drafts: set[tuple[str, str]] = set()
        for row in rows:
            if len(result) >= limit:
                break
            post_id = row["post_id"]
            raw_text = row["text"] or ""
            if post_id in used or len(raw_text.strip()) < 40 or PROMO.search(raw_text) or ADDRESS.search(raw_text):
                continue
            try:
                created = _utc(row["created_at"])
                observed = _utc(row["first_observed_at"])
            except (TypeError, ValueError):
                continue
            if created > observed + timedelta(minutes=2) or observed - created > timedelta(minutes=discovery_lag_minutes):
                continue
            draft = _draft(raw_text)
            if draft is None:
                continue
            name, ticker, description = draft
            draft_key = (name.casefold(), ticker.casefold())
            if draft_key in seen_drafts:
                continue
            seen_drafts.add(draft_key)
            since = (now - timedelta(days=2)).isoformat()
            matches = conn.execute(
                "SELECT lower(name)=lower(?) AS same_name, lower(symbol)=lower(?) AS same_symbol "
                "FROM launches WHERE launched_at >= ? AND "
                "(lower(name)=lower(?) OR lower(symbol)=lower(?))",
                (name, ticker, since, name, ticker),
            ).fetchall()
            result.append(Idea(
                post_id=post_id,
                post_url=f"https://x.com/{row['author_username'] or 'i'}/status/{post_id}",
                created_at=created.isoformat(), first_observed_at=observed.isoformat(),
                author=row["author_username"] or "unknown", text=" ".join(raw_text.split())[:500],
                name=name, ticker=ticker, description=description,
                local_name_matches=sum(int(m[0]) for m in matches),
                local_ticker_matches=sum(int(m[1]) for m in matches),
            ))
        return result
    finally:
        conn.close()


def format_card(idea: Idea) -> str:
    return (
        "Идея для ручной проверки — НЕ торговый сигнал\n"
        f"X: {idea.post_url}\n"
        f"Опубликовано: {idea.created_at}\n"
        f"Первое наблюдение: {idea.first_observed_at}\n"
        f"Автор: @{idea.author}\n"
        f"Пост: {idea.text}\n\n"
        f"Черновик имени: {idea.name}\n"
        f"Черновик тикера: {idea.ticker}\n"
        f"Черновик описания: {idea.description}\n\n"
        f"Похожие имена/тикеры среди локально увиденных запусков за 48ч: "
        f"{idea.local_name_matches}/{idea.local_ticker_matches}\n"
        "Полная уникальность Pump.fun: не проверена; Stonk.fun: не проверен.\n"
        "Связь нарратива с прибыльностью: не доказана."
    )


def send_telegram(token: str, chat_id: str, message: str) -> None:
    if not token or not chat_id:
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": message,
                         "link_preview_options": {"is_disabled": True}}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram HTTP {exc.code}; credentials and chat permissions need checking") from None
    except urllib.error.URLError:
        raise RuntimeError("Telegram network error") from None
    if not payload.get("ok"):
        raise RuntimeError("Telegram rejected sendMessage; credentials and chat permissions need checking")


def _reserve(state_path: Path, post_id: str) -> bool:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(state_path)) as conn:
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS delivery "
                         "(post_id TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL)")
            cursor = conn.execute(
                "INSERT OR IGNORE INTO delivery VALUES (?, 'pending', ?)",
                (post_id, datetime.now(timezone.utc).isoformat()),
            )
            return cursor.rowcount == 1


def _finish(state_path: Path, post_id: str, success: bool) -> None:
    with closing(sqlite3.connect(state_path)) as conn:
        with conn:
            if success:
                conn.execute("UPDATE delivery SET status='sent', updated_at=? WHERE post_id=?",
                             (datetime.now(timezone.utc).isoformat(), post_id))
            else:
                conn.execute("DELETE FROM delivery WHERE post_id=? AND status='pending'", (post_id,))


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only X idea cards; Telegram sending is opt-in")
    parser.add_argument("--env-dir", default=".", help="directory with the collector .env")
    parser.add_argument("--db", help="collector SQLite path; defaults to DATABASE_PATH")
    parser.add_argument("--state", help="separate delivery state SQLite path")
    parser.add_argument("--max-age-minutes", type=int, default=60)
    parser.add_argument("--max-discovery-lag-minutes", type=int, default=30)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--send", action="store_true", help="actually send to TELEGRAM_CHAT_ID")
    parser.add_argument("--auto", action="store_true",
                        help="AI-analyze fresh X posts and automatically send eligible alerts")
    parser.add_argument("--approve-post-id", action="append", default=[],
                        help="exact X post ID manually approved for sending; repeat if needed")
    parser.add_argument("--immutable-snapshot", action="store_true",
                        help="read a completed export without SQLite sidecar files; preview only")
    args = parser.parse_args()
    if args.auto and args.send:
        parser.error("--auto and --send are separate modes")
    if not 1 <= args.limit <= 10:
        parser.error("--limit must be in 1..10")
    env_dir = Path(args.env_dir).resolve()
    load_dotenv(env_dir / ".env")
    db = Path(args.db or os.environ.get("DATABASE_PATH", str(env_dir / "data/collector.sqlite3")))
    state = Path(args.state or env_dir / "data/idea-bot-state.sqlite3")
    if args.auto:
        if os.environ.get("AI_BOT_AUTO_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}:
            parser.error("Set AI_BOT_AUTO_ENABLED=true explicitly before --auto")
        if args.immutable_snapshot:
            parser.error("--auto requires the live collector DB, not an immutable export")
        from .ai_analyzer import Config, run

        try:
            config = Config(
                collector_db=db,
                state_db=state,
                openai_key=os.environ.get("OPENAI_API_KEY", ""),
                telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
                telegram_chat_id=os.environ.get("TELEGRAM_CHAT_IDS") or os.environ.get("TELEGRAM_CHAT_ID", ""),
                model=os.environ.get("OPENAI_MODEL", "gpt-6-luna"),
                run_id=os.environ.get("X_FIRST_RUN_ID", ""),
                end_at=os.environ.get("X_FIRST_END_AT", ""),
                max_posts_per_run=int(os.environ.get("AI_MAX_POSTS_PER_RUN", "5")),
                max_ai_calls_per_day=int(os.environ.get("AI_MAX_CALLS_PER_DAY", "0")),
                max_alerts_per_day=int(os.environ.get("AI_MAX_ALERTS_PER_DAY", "0")),
                min_views=int(os.environ.get("AI_MIN_VIEWS", "5000")),
                auth_mode=os.environ.get("AI_AUTH_MODE", "api_key"),
                chatgpt_credentials=(Path(os.environ["CHATGPT_CREDENTIALS_FILE"])
                                     if os.environ.get("CHATGPT_CREDENTIALS_FILE") else None),
            )
            print(json.dumps(run(config), ensure_ascii=False))
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
            parser.exit(1, f"AI analyzer stopped: {exc}\n")
        return
    if args.send and args.max_age_minutes > 60:
        parser.error("--send refuses posts older than 60 minutes")
    if args.send and args.immutable_snapshot:
        parser.error("--send refuses an immutable export; use the live collector DB")
    if args.send and not args.approve_post_id:
        parser.error("--send requires at least one manually approved --approve-post-id")
    if args.send and len(set(args.approve_post_id)) > args.limit:
        parser.error("number of approved post IDs exceeds --limit")
    if args.send and not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        parser.error("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID locally before --send")
    try:
        ideas = candidates(db, datetime.now(timezone.utc), args.max_age_minutes,
                           args.limit, args.max_discovery_lag_minutes, args.immutable_snapshot,
                           set(args.approve_post_id) if args.send else None)
    except (sqlite3.Error, OSError, ValueError) as exc:
        parser.exit(2, f"Cannot inspect collector DB: {exc}\n")
    if not ideas:
        print("No fresh, eligible X ideas in the local collector DB. No API call made.")
        return
    if args.send:
        approved = set(args.approve_post_id)
        ideas = [idea for idea in ideas if idea.post_id in approved]
        if not ideas:
            parser.error("approved post ID is not in the current fresh candidate batch")
    if not args.send:
        print(f"DRY RUN: {len(ideas)} draft idea(s); no Telegram/API call made.")
        for idea in ideas:
            print("\n" + format_card(idea) + "\n")
        return
    sent = 0
    for idea in ideas:
        if not _reserve(state, idea.post_id):
            continue
        try:
            send_telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"], format_card(idea))
        except (RuntimeError, ValueError) as exc:
            _finish(state, idea.post_id, False)
            parser.exit(1, f"Telegram delivery stopped: {exc}\n")
        _finish(state, idea.post_id, True)
        sent += 1
    print(f"Sent {sent} draft idea(s). No token was launched or traded.")


if __name__ == "__main__":
    main()
