from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from narrative_collector.migration_learning import _state, draft_filters, process, start_run
from narrative_collector.storage import Store


class FakeX:
    def __init__(self):
        self.calls = []

    def tweets_by_ids(self, ids):
        self.calls.append(ids)
        return [{"id": item, "text": "A distinctive blue comet dog appears",
                 "createdAt": "2026-10-16T10:00:00Z",
                 "author": {"userName": "example"}} for item in ids]


def fake_review(text):
    assert "success" not in text.lower()
    return {"theme": "animal_character", "hook": "blue comet dog",
            "specificity": "distinctive", "promotion": False, "reason": "specific image"}


class MigrationLearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.collector = root / "collector.sqlite3"
        self.state = root / "learning.sqlite3"
        Store(self.collector).initialize()
        with closing(sqlite3.connect(self.collector)) as db, db:
            db.execute("INSERT INTO migrations VALUES (?,?,?,?,?,?)",
                       ("mint1", "2026-10-16T11:00:00+00:00", None, "Dog", "DOG", "{}"))
            db.execute("INSERT INTO launches VALUES (?,?,?,?,?,?)",
                       ("mint1", "2026-10-16T10:05:00+00:00", "creator", "Dog", "DOG", "{}"))
            db.execute("INSERT INTO launch_source_metadata VALUES (?,?,?,?,?,?,?,?,?,?)",
                       ("mint1", "ipfs://cid", "cid", "2026-10-16T11:10:00+00:00", "ok",
                        "", "123456", "https://x.com/i/status/123456", "{}", 1))
            db.execute("INSERT INTO token_labels VALUES (?,?,?,?,?,?,?,?,?)",
                       ("mint1", "2026-10-16T12:00:00+00:00", 1, 1, 2, 3,
                        "success", "low", "{}"))

    def test_no_paid_calls_until_start_and_capped(self):
        now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        x = FakeX()
        result = process(self.collector, self.state, "x-key", "ai-key", now=now,
                         x_client=x, reviewer=fake_review)
        self.assertEqual(result["status"], "not_started")
        self.assertEqual(x.calls, [])
        with closing(_state(self.state)) as state:
            start_run(state, now, 1, 1)
            with self.assertRaises(ValueError):
                start_run(state, now, 10, 50)
        result = process(self.collector, self.state, "x-key", "ai-key", now=now,
                         x_client=x, reviewer=fake_review)
        self.assertEqual((result["x_requests_total"], result["ai_calls_total"]), (1, 1))
        self.assertEqual(result["reviews_saved"], 1)
        self.assertEqual(x.calls, [["123456"]])
        again = process(self.collector, self.state, "x-key", "ai-key", now=now,
                        x_client=x, reviewer=fake_review)
        self.assertEqual((again["x_requests_total"], again["ai_calls_total"]), (1, 1))
        with closing(sqlite3.connect(self.state)) as state:
            row = state.execute("SELECT theme, prelaunch, provenance, outcome FROM narrative_reviews").fetchone()
        self.assertEqual(row, ("animal_character", 1, "creator_supplied_metadata", "success"))
        self.assertEqual(process(self.collector, self.state, "x-key", "ai-key",
                                 now=now + timedelta(hours=25))["status"], "expired")

    def test_draft_never_activates_filters(self):
        now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
        with closing(_state(self.state)) as state:
            start_run(state, now)
        process(self.collector, self.state, "x-key", "ai-key", now=now,
                x_client=FakeX(), reviewer=fake_review)
        draft = draft_filters(self.state, now)
        self.assertFalse(draft["active"])
        self.assertEqual(draft["evidence"]["nonmigrated_controls"], 0)
        with closing(sqlite3.connect(self.state)) as state:
            self.assertEqual(state.execute("SELECT active FROM filter_drafts").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
