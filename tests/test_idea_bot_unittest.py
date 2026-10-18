import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from narrative_collector.idea_bot import _finish, _reserve, candidates, format_card, send_telegram
from narrative_collector.x_first import _query_due


class IdeaBotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "collector.sqlite3"
        self.now = datetime.now(timezone.utc)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE x_first_posts (
                  post_id TEXT PRIMARY KEY, created_at TEXT, first_observed_at TEXT,
                  text TEXT, author_username TEXT);
                CREATE TABLE launch_source_metadata (source_post_id TEXT);
                CREATE TABLE launches (mint TEXT, name TEXT, symbol TEXT, launched_at TEXT);
            """)

    def add_post(self, post_id, text, age=5, discovery_lag=1):
        observed = self.now - timedelta(minutes=age)
        created = observed - timedelta(minutes=discovery_lag)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO x_first_posts VALUES (?,?,?,?,?)",
                         (post_id, created.isoformat(), observed.isoformat(), text, "tester"))

    def test_local_duplicate_and_promo_filter(self):
        text = "The dancing capybara has taken over the internet today and everyone is sharing it"
        self.add_post("1", text)
        self.add_post("2", text + " $CAPY")
        self.add_post("3", text)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO launch_source_metadata VALUES ('3')")
        ideas = candidates(self.db, self.now, 60, 5)
        self.assertEqual([idea.post_id for idea in ideas], ["1"])
        self.assertIn("не проверен", format_card(ideas[0]))

    def test_stale_and_late_discovery_are_not_candidates(self):
        text = "A brand new internet animal joke is spreading rapidly across the whole web today"
        self.add_post("old", text, age=90)
        self.add_post("late", text, discovery_lag=40)
        self.assertEqual(candidates(self.db, self.now, 60, 5), [])

    def test_name_and_ticker_matches_are_local_warnings(self):
        self.add_post("1", "Dancing capybara surprises the audience with a very funny little performance")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO launches VALUES (?,?,?,?)",
                         ("mint", "Dancing Capybara", "DANCING", self.now.isoformat()))
        idea = candidates(self.db, self.now, 60, 5)[0]
        self.assertEqual((idea.local_name_matches, idea.local_ticker_matches), (1, 1))

    def test_repeated_copy_gets_one_draft(self):
        text = "Dancing capybara surprises the audience with a very funny little performance"
        self.add_post("1", text, age=5)
        self.add_post("2", text, age=6)
        self.assertEqual(len(candidates(self.db, self.now, 60, 5)), 1)

    def test_delivery_state_deduplicates_and_releases_failures(self):
        path = Path(self.temp.name) / "state.sqlite3"
        self.assertTrue(_reserve(path, "one"))
        self.assertFalse(_reserve(path, "one"))
        _finish(path, "one", False)
        self.assertTrue(_reserve(path, "one"))
        _finish(path, "one", True)
        self.assertFalse(_reserve(path, "one"))

    def test_telegram_request_uses_post_without_markup(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self, *_):
                return b'{"ok":true,"result":{}}'

        with patch("urllib.request.urlopen", return_value=Response()) as mocked:
            send_telegram("secret", "123", "test")
        request = mocked.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertIn(b'"chat_id": "123"', request.data)
        self.assertNotIn(b'"parse_mode"', request.data)

    def test_x_first_timer_boundary_is_due(self):
        last = self.now - timedelta(minutes=9, seconds=45)
        self.assertTrue(_query_due(self.now, last, 10))
        self.assertFalse(_query_due(self.now, self.now - timedelta(minutes=5), 10))


if __name__ == "__main__":
    unittest.main()
