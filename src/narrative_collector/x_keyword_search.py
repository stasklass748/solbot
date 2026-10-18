"""Rotating search of user-supplied phrases through X.

All unique phrases are searched cyclically. A positive optional credit cap
stops requests when exhausted; zero disables that internal cap.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .providers import TwitterApiIoClient
from .storage import Store

MAX_PAGE_CREDITS = 300
QUERY_SUFFIX = " lang:en -filter:retweets -filter:replies"
MAX_QUERY_LENGTH = 400
MAX_TERMS_PER_QUERY = 12
MIN_INTERVAL = timedelta(minutes=3)
TIMER_SLACK = timedelta(seconds=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_terms(path: Path) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        term = " ".join(raw.strip().split())
        if not term or term.startswith("#"):
            continue
        if len(term) > 80 or '"' in term or "\n" in term or "\r" in term:
            raise ValueError("Invalid keyword phrase in lexicon")
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            terms.append(term)
    if not terms:
        raise ValueError("Keyword lexicon is empty")
    return terms


def build_queries(terms: list[str]) -> list[str]:
    # Stable dispersion ensures a capped pilot samples every part of the huge
    # user list, not only its first animal/crypto-themed block.
    ordered = sorted(terms, key=lambda value: hashlib.sha256(value.casefold().encode()).digest())
    batches: list[str] = []
    current: list[str] = []
    for term in ordered:
        quoted = f'"{term}"'
        candidate = current + [quoted]
        query = "(" + " OR ".join(candidate) + ")" + QUERY_SUFFIX
        if current and (len(query) > MAX_QUERY_LENGTH or len(candidate) > MAX_TERMS_PER_QUERY):
            batches.append("(" + " OR ".join(current) + ")" + QUERY_SUFFIX)
            current = [quoted]
        else:
            current = candidate
    if current:
        batches.append("(" + " OR ".join(current) + ")" + QUERY_SUFFIX)
    if any(len(query) > MAX_QUERY_LENGTH for query in batches):
        raise ValueError("A keyword batch exceeds the X query length limit")
    return batches


def _state(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    state = sqlite3.connect(path, timeout=10)
    state.row_factory = sqlite3.Row
    state.execute("PRAGMA busy_timeout=10000")
    state.executescript("""
      CREATE TABLE IF NOT EXISTS keyword_run (
        id INTEGER PRIMARY KEY CHECK(id=1), run_id TEXT NOT NULL,
        started_at TEXT NOT NULL, ends_at TEXT NOT NULL,
        terms_sha256 TEXT NOT NULL, terms_count INTEGER NOT NULL,
        batches_total INTEGER NOT NULL, next_batch INTEGER NOT NULL DEFAULT 0,
        credit_cap INTEGER NOT NULL, credits_used INTEGER NOT NULL DEFAULT 0,
        last_request_at TEXT);
      CREATE TABLE IF NOT EXISTS keyword_requests (
        batch_index INTEGER PRIMARY KEY, requested_at TEXT NOT NULL,
        query TEXT NOT NULL, status TEXT NOT NULL,
        credits INTEGER NOT NULL, returned_tweets INTEGER NOT NULL DEFAULT 0,
        fresh_tweets INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS keyword_run_history (
        run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ends_at TEXT NOT NULL,
        credit_cap INTEGER NOT NULL, credits_used INTEGER NOT NULL,
        next_batch INTEGER NOT NULL);
    """)
    return state


def start_run(path: Path, terms_path: Path, now: datetime | None = None,
              credit_cap: int = 0) -> dict:
    now = now or _now()
    if credit_cap != 0 and not 300 <= credit_cap <= 100_000:
        raise ValueError("Keyword cap must be 0 or 300..100000 credits")
    terms = load_terms(terms_path)
    batches = build_queries(terms)
    digest = hashlib.sha256(terms_path.read_bytes()).hexdigest()
    with closing(_state(path)) as state, state:
        if state.execute("SELECT 1 FROM keyword_run WHERE id=1").fetchone():
            raise ValueError("Keyword run already exists; cannot reset its budget")
        run_id = "keywords-" + now.strftime("%Y%m%dT%H%M%SZ")
        state.execute("INSERT INTO keyword_run VALUES (1,?,?,?,?,?,?,0,?,0,NULL)",
                      (run_id, now.isoformat(), (now + timedelta(hours=24)).isoformat(),
                       digest, len(terms), len(batches), credit_cap))
        return {"run_id": run_id, "ends_at": (now + timedelta(hours=24)).isoformat(),
                "terms": len(terms), "batches": len(batches), "credit_cap": credit_cap}


def renew_run(path: Path, terms_path: Path, now: datetime | None = None,
              credit_cap: int = 0) -> dict:
    """Explicit later pilot; preserve the batch cursor and prior budget audit."""
    now = now or _now()
    if credit_cap != 0 and not 300 <= credit_cap <= 100_000:
        raise ValueError("Keyword cap must be 0 or 300..100000 credits")
    with closing(_state(path)) as state, state:
        row = state.execute("SELECT * FROM keyword_run WHERE id=1").fetchone()
        if not row or now < datetime.fromisoformat(row["ends_at"]):
            raise ValueError("Previous 24h run must expire before explicit renewal")
        if hashlib.sha256(terms_path.read_bytes()).hexdigest() != row["terms_sha256"]:
            raise ValueError("Cannot renew with a changed keyword lexicon")
        state.execute("INSERT INTO keyword_run_history VALUES (?,?,?,?,?,?)",
                      (row["run_id"], row["started_at"], row["ends_at"], row["credit_cap"],
                       row["credits_used"], row["next_batch"]))
        run_id = "keywords-" + now.strftime("%Y%m%dT%H%M%SZ")
        state.execute("UPDATE keyword_run SET run_id=?,started_at=?,ends_at=?,credit_cap=?,"
                      "credits_used=0,last_request_at=NULL WHERE id=1",
                      (run_id, now.isoformat(), (now + timedelta(hours=24)).isoformat(), credit_cap))
        return {"run_id": run_id, "ends_at": (now + timedelta(hours=24)).isoformat(),
                "next_batch": row["next_batch"], "batches": row["batches_total"],
                "credit_cap": credit_cap}


def process(settings: Settings, terms_path: Path, state_path: Path,
            now: datetime | None = None, client: object | None = None,
            store: Store | None = None) -> dict:
    now = now or _now()
    with closing(_state(state_path)) as state:
        run = state.execute("SELECT * FROM keyword_run WHERE id=1").fetchone()
        if not run:
            return {"status": "not_started"}
        if (run["last_request_at"] and
                now + TIMER_SLACK < datetime.fromisoformat(run["last_request_at"]) + MIN_INTERVAL):
            return {"status": "not_due", "credits_used": run["credits_used"]}
        if run["credit_cap"] > 0 and run["credits_used"] + MAX_PAGE_CREDITS > run["credit_cap"]:
            return {"status": "budget_exhausted", "credits_used": run["credits_used"]}
        if hashlib.sha256(terms_path.read_bytes()).hexdigest() != run["terms_sha256"]:
            raise ValueError("Keyword lexicon changed during bounded run")
        queries = build_queries(load_terms(terms_path))
        if len(queries) != run["batches_total"]:
            raise ValueError("Keyword batch count changed during bounded run")
        if not settings.twitterapi_io_key:
            return {"status": "missing_x_key"}
        store = store or Store(settings.database_path)
        week = (now.date() - timedelta(days=now.weekday())).isoformat()
        if (settings.social_weekly_credit_cap > 0 and
                store.weekly_usage("twitterapi", week) + MAX_PAGE_CREDITS > settings.social_weekly_credit_cap):
            return {"status": "shared_weekly_budget_exhausted"}
        request_index = run["next_batch"]
        index = request_index % len(queries)
        cycle = request_index // len(queries) + 1
        query = queries[index]
        # Reserve the worst-case page before the network call. Concurrent X
        # workers see the reservation through the shared api_usage table.
        with state:
            state.execute("UPDATE keyword_run SET next_batch=next_batch+1, "
                          "credits_used=credits_used+?, last_request_at=? WHERE id=1",
                          (MAX_PAGE_CREDITS, now.isoformat()))
            state.execute("INSERT INTO keyword_requests VALUES (?,? ,?,'reserved',?,0,0)",
                          (request_index, now.isoformat(), query, MAX_PAGE_CREDITS))
        store.add_usage("twitterapi", week, MAX_PAGE_CREDITS)
        lookup = client or TwitterApiIoClient(settings.twitterapi_io_key, 20)
        try:
            payload = lookup.search_page(query)
            tweets = TwitterApiIoClient._tweets(payload)
            if len(tweets) > 20:
                raise ValueError("X search returned more than 20 posts")
            charged = max(15, len(tweets) * 15)
            count, fresh = store.save_x_first_search(
                run["run_id"], f"keyword:{index}", query, payload, charged,
                now.isoformat(), 0)
        except Exception as exc:
            with state:
                state.execute("UPDATE keyword_requests SET status=? WHERE batch_index=?",
                              ("error:" + type(exc).__name__[:50], request_index))
            return {"status": "search_error", "batch": index + 1, "cycle": cycle,
                    "credits_used": run["credits_used"] + MAX_PAGE_CREDITS,
                    "error_type": type(exc).__name__}
        refund = MAX_PAGE_CREDITS - charged
        with state:
            state.execute("UPDATE keyword_run SET credits_used=credits_used-? WHERE id=1", (refund,))
            state.execute("UPDATE keyword_requests SET status='ok',credits=?,returned_tweets=?,fresh_tweets=? "
                          "WHERE batch_index=?", (charged, count, fresh, request_index))
        if refund:
            store.add_usage("twitterapi", week, -refund)
        return {"status": "ok", "batch": index + 1, "cycle": cycle,
                "batches_total": len(queries),
                "returned_tweets": count, "fresh_tweets": fresh,
                "credits_used": run["credits_used"] + charged,
                "credit_cap": run["credit_cap"]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded X search across supplied phrases")
    parser.add_argument("--env-dir", default=".")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--start", action="store_true")
    mode.add_argument("--renew", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--status", action="store_true")
    args = parser.parse_args()
    root = Path(args.env_dir).resolve()
    settings = Settings.from_environment(root)
    state_path = root / "data/x-keyword-state.sqlite3"
    terms_path = root / "x_keyword_terms.txt"
    if args.start:
        result = start_run(state_path, terms_path)
    elif args.renew:
        result = renew_run(state_path, terms_path)
    elif args.execute:
        result = process(settings, terms_path, state_path)
    else:
        with closing(_state(state_path)) as state:
            run = state.execute("SELECT * FROM keyword_run WHERE id=1").fetchone()
            result = {"run": dict(run) if run else None,
                      "requests": state.execute("SELECT COUNT(*) FROM keyword_requests").fetchone()[0],
                      "posts": state.execute("SELECT COALESCE(SUM(fresh_tweets),0) FROM keyword_requests").fetchone()[0]}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
