"""Read-only, bounded health report for the live idea pipeline.

No provider API is called, and raw journal/error text is never sent to Telegram.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


UNITS = (
    "meme-x-first.timer",
    "meme-x-first.service",
    "meme-idea-analyzer.timer",
    "meme-idea-analyzer.service",
    "meme-idea-telegram.service",
)
KEYWORD_TIMER = "meme-x-keywords.timer"
LABELS = {
    "meme-x-first.timer": "поиск X",
    "meme-x-first.service": "задание поиска X",
    "meme-idea-analyzer.timer": "ИИ-анализ",
    "meme-idea-analyzer.service": "задание ИИ",
    "meme-idea-telegram.service": "меню Telegram",
}


@dataclass(frozen=True)
class DiagnosticConfig:
    collector_db: Path
    state_db: Path
    run_id: str
    end_at: str
    x_cap: int
    ai_daily_cap: int
    alert_daily_cap: int
    weekly_x_cap: int = 0


def systemd_state(unit: str) -> dict[str, str]:
    if unit not in UNITS and unit != KEYWORD_TIMER:
        raise ValueError("Unexpected systemd unit")
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "-p", "ActiveState", "-p", "Result",
             "-p", "ExecMainStatus", "--no-pager"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"ActiveState": "unknown", "Result": "unknown"}
    if result.returncode:
        return {"ActiveState": "unknown", "Result": "unknown"}
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def _ro(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    db.execute("PRAGMA busy_timeout=3000")
    return db


def _scalar(db: sqlite3.Connection, sql: str, args: tuple = ()) -> object:
    try:
        row = db.execute(sql, args).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise


def _time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.astimezone(timezone.utc) if value.tzinfo else None


def _ago(raw: str | None, now: datetime) -> str:
    value = _time(raw)
    if not value:
        return "нет данных"
    minutes = max(0, int((now - value).total_seconds() // 60))
    return "только что" if minutes == 0 else f"{minutes} мин назад"


def snapshot(config: DiagnosticConfig, now: datetime | None = None,
             unit_reader: Callable[[str], dict[str, str]] = systemd_state,
             ) -> tuple[str, tuple[str, ...]]:
    """Return safe Telegram text and stable problem codes for automatic notices."""
    now = now or datetime.now(timezone.utc)
    try:
        start = datetime.strptime(config.run_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        start = None
    active_run = bool(config.run_id)
    lines = ["🩺 Диагностика · без вызовов X/OpenAI", ""]
    failures: list[str] = []
    if not config.run_id:
        lines.append("Прогон X: не настроен. Нужен явный запуск с лимитом.")
    else:
        lines.append("Прогон X: без ограничения по времени; внутренние лимиты кредитов отключены."
                     if not config.x_cap and not config.weekly_x_cap else
                     "Прогон X: без ограничения по времени; действуют лимиты кредитов.")

    states = {unit: unit_reader(unit) for unit in UNITS}
    for unit in UNITS:
        state = states[unit].get("ActiveState", "unknown")
        result = states[unit].get("Result", "unknown")
        if unit.endswith(".timer"):
            label = "работает" if state == "active" else "выключен" if state == "inactive" else state
            lines.append(f"{LABELS[unit]}: {label}.")
            if active_run and state in {"inactive", "failed"}:
                failures.append(unit + ":" + state)
        elif unit == "meme-idea-telegram.service" and state != "active":
            lines.append(f"{LABELS[unit]}: не работает ({state}).")
            if active_run:
                failures.append(unit + ":" + state)
        elif state == "failed" or result not in {"success", "unknown", ""}:
            lines.append(f"{LABELS[unit]}: ОШИБКА ({result}, код {states[unit].get('ExecMainStatus', '?')}).")
            if active_run:
                failures.append(unit + ":" + result)

    used = weekly_used = 0
    last_search = None
    searches = posts_hour = 0
    database_problem = ""
    try:
        with closing(_ro(config.collector_db)) as db:
            used = int(_scalar(db, "SELECT units FROM api_usage WHERE period_start=? AND "
                               "provider='twitterapi_xfirst'", (config.run_id,)) or 0)
            week = (now.date() - timedelta(days=now.weekday())).isoformat()
            weekly_used = int(_scalar(db, "SELECT units FROM api_usage WHERE period_start=? AND "
                                      "provider='twitterapi'", (week,)) or 0)
            row = db.execute("SELECT COUNT(*),MAX(observed_at) FROM x_first_searches WHERE run_id=?",
                             (config.run_id,)).fetchone()
            searches, last_search = int(row[0]), row[1]
            hour = now - timedelta(hours=1)
            if start and start > hour:
                hour = start
            posts_hour = int(_scalar(db, "SELECT COUNT(*) FROM x_first_posts "
                                     "WHERE first_observed_at>=?", (hour.isoformat(),)) or 0)
    except (OSError, sqlite3.Error) as exc:
        database_problem = "База коллектора недоступна: " + type(exc).__name__
        failures.append("collector_db")
    if database_problem:
        lines.append(database_problem + ".")
    else:
        run_usage = f"≈{used}/{config.x_cap}" if config.x_cap else f"≈{used} (без внутреннего лимита)"
        week_usage = (f"≈{weekly_used}/{config.weekly_x_cap}" if config.weekly_x_cap
                      else f"≈{weekly_used} (без внутреннего лимита)")
        lines.append(f"X-кредиты прогона: {run_usage} (внутренний учёт).")
        lines.append(f"X-кредиты недели: {week_usage} (внутренний учёт).")
        lines.append(f"X-поиск: {searches} запросов, последний {_ago(last_search, now)}; "
                     f"новых постов за час: {posts_hour}.")
        if active_run and ((config.x_cap > 0 and used + 300 > config.x_cap) or
                           (config.weekly_x_cap > 0 and weekly_used + 300 > config.weekly_x_cap)):
            lines.append("Причина тишины: внутренний лимит X исчерпан или почти исчерпан.")
        elif active_run and states["meme-x-first.timer"].get("ActiveState") == "active":
            last = _time(last_search)
            if last and now - last > timedelta(minutes=25):
                lines.append("Внимание: X-поиск давно не записывал результат; проверьте службу.")
                failures.append("x_search_stale")
            elif not last and start and now - start > timedelta(minutes=25):
                lines.append("Внимание: X-поиск ещё ни разу не записал результат.")
                failures.append("x_search_missing")

    keyword_state_path = config.state_db.parent / "x-keyword-state.sqlite3"
    if keyword_state_path.exists():
        try:
            with closing(_ro(keyword_state_path)) as keyword_db:
                row = keyword_db.execute(
                    "SELECT next_batch,batches_total,credits_used,credit_cap FROM keyword_run WHERE id=1"
                ).fetchone()
            if row and row[1]:
                position, total, keyword_used, keyword_cap = map(int, row)
                timer_state = unit_reader(KEYWORD_TIMER).get("ActiveState", "unknown")
                keyword_usage = (f"≈{keyword_used}/{keyword_cap}" if keyword_cap
                                 else f"≈{keyword_used} (без внутреннего лимита)")
                lines.append(f"Поиск по словам: круг {position // total + 1}, "
                             f"группа {position % total + 1}/{total}; "
                             f"X-кредиты {keyword_usage}; таймер {timer_state}.")
                if keyword_cap > 0 and keyword_used + 300 > keyword_cap:
                    lines.append("Поиск по словам остановлен лимитом X-кредитов; бюджет не сбрасывается автоматически.")
            else:
                lines.append("Поиск по словам: прогон не настроен.")
        except (OSError, sqlite3.Error):
            lines.append("Поиск по словам: состояние недоступно.")
    else:
        lines.append("Поиск по словам: не настроен.")

    analyzed = interesting = 0
    last_analysis = last_card = None
    ai_calls = cards_today = cards_hour = watch_count = 0
    provider_pause: tuple[str, str, str | None] | None = None
    state_problem = ""
    try:
        with closing(_ro(config.state_db)) as db:
            cutoff = (now - timedelta(hours=1)).isoformat()
            try:
                rows = db.execute("SELECT analyzed_at,proposals_json FROM ai_batches "
                                  "WHERE analyzed_at>=? ORDER BY id DESC LIMIT 5000", (cutoff,)).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
                rows = []
            analyzed = len(rows)
            last_analysis = _scalar(db, "SELECT MAX(analyzed_at) FROM ai_batches")
            for _, raw in rows:
                try:
                    proposals = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                interesting += sum(1 for item in proposals if isinstance(item, dict) and
                                   item.get("interesting") is True and item.get("confidence") == "high")
            today = now.date().isoformat()
            ai_calls = int(_scalar(db, "SELECT calls FROM ai_call_budget WHERE day=?", (today,)) or 0)
            cards_today = int(_scalar(db, "SELECT COUNT(*) FROM alerts WHERE day=?", (today,)) or 0)
            cards_hour = int(_scalar(db, "SELECT COUNT(*) FROM alerts WHERE created_at>=?",
                                     (cutoff,)) or 0)
            last_card = _scalar(db, "SELECT MAX(created_at) FROM alerts")
            watch_count = int(_scalar(db, "SELECT COUNT(*) FROM x_watchlist_accounts") or 0)
            try:
                provider_pause = db.execute(
                    "SELECT status,error_code,resume_at FROM ai_provider_pause WHERE id=1"
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
    except (OSError, sqlite3.Error) as exc:
        state_problem = "База ИИ/бота недоступна: " + type(exc).__name__
        failures.append("idea_state_db")
    if state_problem:
        lines.append(state_problem + ".")
    else:
        lines.append(f"TXT-список: {watch_count} аккаунтов.")
        lines.append(f"ИИ за час: разобрал {analyzed}, одобрил с высокой уверенностью {interesting}; "
                     f"последний раз {_ago(last_analysis, now)}.")
        ai_limit = str(config.ai_daily_cap) if config.ai_daily_cap else "без дневного лимита"
        card_limit = (f"/{config.alert_daily_cap} сегодня" if config.alert_daily_cap
                      else " сегодня (без дневного лимита)")
        lines.append(f"ИИ-вызовы сегодня: {ai_calls} ({ai_limit}). "
                     f"Карточки: {cards_hour} за час, {cards_today}{card_limit}, "
                     f"последняя {_ago(last_card, now)}.")
        if provider_pause and provider_pause[0] == "requires_action":
            lines.append(f"OpenAI: запросы остановлены — нужна проверка квоты/оплаты "
                         f"в кабинете API (код {provider_pause[1]}).")
        elif provider_pause and provider_pause[0] == "cooldown" and provider_pause[2] and (
                now < datetime.fromisoformat(provider_pause[2])):
            lines.append(f"OpenAI: пауза после HTTP 429/503 до {provider_pause[2]} "
                         f"(код {provider_pause[1]}).")
        elif active_run and config.ai_daily_cap and ai_calls >= config.ai_daily_cap:
            lines.append("Причина тишины: достигнут дневной лимит ИИ.")
        elif active_run and config.alert_daily_cap and cards_today >= config.alert_daily_cap:
            lines.append("Причина тишины: достигнут дневной лимит карточек.")
        elif active_run and searches and not posts_hour:
            lines.append("Пока нет новых постов для анализа; карточка не гарантирована.")
        elif active_run and posts_hour and not analyzed:
            lines.append("Посты найдены, но ИИ их пока не разобрал: возможно, "
                         "они не прошли предварительные фильтры или ждут очереди.")
        elif active_run and analyzed and not interesting:
            lines.append("Новые посты были, но ИИ не увидел достаточно сильной идеи.")
        elif active_run and interesting and not cards_hour:
            lines.append("ИИ находил идеи, но карточки не прошли дополнительные проверки "
                         "(свежесть, повторы или формат).")
    if failures:
        lines.append("Есть проблема: см. строки выше. Подробности: "
                     "journalctl -u meme-x-first.service -u meme-idea-analyzer.service -n 30 --no-pager")
    else:
        lines.append("Если карточек нет, это не обязательно ошибка: фильтры не обещают результат.")
    return "\n".join(lines)[:3900], tuple(sorted(set(failures)))
