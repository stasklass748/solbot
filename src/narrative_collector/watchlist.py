"""Shared, bounded X-account watchlist managed by the two Telegram reviewers."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MAX_ACCOUNTS = 1000
MAX_FILE_BYTES = 64 * 1024
HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}\Z")
PROFILE = re.compile(r"https://(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})/?\Z", re.I)


def normalize_handle(value: str) -> str:
    value = value.strip()
    match = PROFILE.fullmatch(value)
    if match:
        value = match.group(1)
    else:
        value = value.removeprefix("@")
    if not HANDLE.fullmatch(value):
        raise ValueError(f"Некорректный X-аккаунт: {value[:30]}")
    return value.lower()


def parse_txt(data: bytes) -> list[str]:
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("Нужен непустой TXT-файл не больше 64 КБ")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Сохраните TXT-файл в кодировке UTF-8") from None
    handles: list[str] = []
    seen: set[str] = set()
    for line_no, line in enumerate(content.splitlines(), 1):
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        try:
            handle = normalize_handle(item)
        except ValueError:
            raise ValueError(f"Строка {line_no}: ожидается @handle или ссылка x.com/handle") from None
        if handle not in seen:
            handles.append(handle)
            seen.add(handle)
        if len(handles) > MAX_ACCOUNTS:
            raise ValueError(f"В файле должно быть не больше {MAX_ACCOUNTS} аккаунтов")
    if not handles:
        raise ValueError("В TXT-файле нет X-аккаунтов")
    return handles


def ensure_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS x_watchlist_accounts (
        handle TEXT PRIMARY KEY, added_at TEXT NOT NULL, added_by TEXT NOT NULL
    )""")


def add_accounts(db: sqlite3.Connection, handles: list[str], user_id: str) -> tuple[int, int]:
    ensure_schema(db)
    existing = {row[0] for row in db.execute("SELECT handle FROM x_watchlist_accounts")}
    new = [handle for handle in handles if handle not in existing]
    if len(existing) + len(new) > MAX_ACCOUNTS:
        raise ValueError(f"Общий лимит — {MAX_ACCOUNTS} аккаунтов; сейчас добавлено {len(existing)}")
    now = datetime.now(timezone.utc).isoformat()
    with db:
        db.executemany("INSERT INTO x_watchlist_accounts VALUES (?,?,?)",
                       [(handle, now, user_id) for handle in new])
    return len(new), len(existing) + len(new)


def remove_account(db: sqlite3.Connection, handle: str) -> bool:
    ensure_schema(db)
    with db:
        return db.execute("DELETE FROM x_watchlist_accounts WHERE handle=?",
                          (normalize_handle(handle),)).rowcount == 1


def list_accounts(db: sqlite3.Connection) -> list[str]:
    ensure_schema(db)
    return [row[0] for row in db.execute("SELECT handle FROM x_watchlist_accounts ORDER BY handle")]


def read_accounts(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='x_watchlist_accounts'").fetchone() is None:
            return []
        return [(str(row[0]), str(row[1])) for row in db.execute(
            "SELECT handle,added_at FROM x_watchlist_accounts ORDER BY handle")]
    finally:
        db.close()
