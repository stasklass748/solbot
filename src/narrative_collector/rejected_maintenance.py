"""Restore archived AI decisions and clear only their Telegram list view."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def restore_and_clear(state_path: Path) -> dict[str, str | int]:
    if not state_path.is_file():
        raise FileNotFoundError(f"Idea state database not found: {state_path}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    backup_path = state_path.with_name(f"{state_path.stem}-before-rejected-repair-{stamp}.sqlite3")
    with closing(sqlite3.connect(state_path, timeout=30)) as db:
        db.execute("PRAGMA busy_timeout=30000")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_batches'").fetchone() is None:
            raise RuntimeError("ai_batches table is missing; refusing to modify state")
        with closing(sqlite3.connect(backup_path)) as backup:
            db.backup(backup)
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS rejected_view_state ("
                       "id INTEGER PRIMARY KEY CHECK(id=1), cleared_through_id INTEGER NOT NULL)")
            restored = 0
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='ai_batches_rejected_archive'").fetchone():
                before = db.total_changes
                db.execute("INSERT OR IGNORE INTO ai_batches "
                           "(id,analyzed_at,post_ids_json,proposals_json,usage_json) "
                           "SELECT id,analyzed_at,post_ids_json,proposals_json,usage_json "
                           "FROM ai_batches_rejected_archive")
                restored = db.total_changes - before
            through = int(db.execute("SELECT COALESCE(MAX(id),0) FROM ai_batches").fetchone()[0])
            db.execute("INSERT INTO rejected_view_state VALUES (1,?) "
                       "ON CONFLICT(id) DO UPDATE SET cleared_through_id=excluded.cleared_through_id",
                       (through,))
        return {"restored_decisions": restored, "hidden_through_batch_id": through,
                "backup": str(backup_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-dir", required=True)
    args = parser.parse_args()
    state_path = Path(args.env_dir).resolve() / "data/idea-bot-state.sqlite3"
    print(json.dumps(restore_and_clear(state_path), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
