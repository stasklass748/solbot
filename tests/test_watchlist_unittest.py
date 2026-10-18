import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from narrative_collector.storage import Store
from narrative_collector.watchlist import add_accounts, parse_txt, remove_account
from narrative_collector.x_first import XFirstWorker


class WatchlistTests(unittest.TestCase):
    def test_parse_utf8_txt_and_reject_bad_lines(self):
        self.assertEqual(parse_txt("\ufeff@ElonMusk\nhttps://x.com/Solana\n@elonmusk\n".encode()),
                         ["elonmusk", "solana"])
        with self.assertRaises(ValueError):
            parse_txt(b"https://evil.example/account\n")
        with self.assertRaises(ValueError):
            parse_txt("".join(f"@too_many_{i:04d}\n" for i in range(1001)).encode())

    def test_accepts_1000_accounts_but_not_1001(self):
        payload = "".join(f"@acct{i:04d}\n" for i in range(1000)).encode()
        self.assertEqual(len(parse_txt(payload)), 1000)
        with self.assertRaisesRegex(ValueError, "1000"):
            parse_txt(payload + b"@overflow\n")
        with closing(sqlite3.connect(":memory:")) as db:
            self.assertEqual(add_accounts(db, parse_txt(payload), "42"), (1000, 1000))
            self.assertEqual(add_accounts(db, ["acct0001"], "43"), (0, 1000))

    def test_watch_batches_rotate_across_run_ids(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            root = Path(temp)
            store = Store(root / "collector.sqlite3")
            store.initialize()
            with closing(sqlite3.connect(root / "idea-bot-state.sqlite3")) as state:
                add_accounts(state, [f"acct{i:02d}" for i in range(10)], "42")
            config_file = root / "queries.json"
            config_file.write_text(json.dumps([{"label": "culture", "query": "viral meme"}]),
                                   encoding="utf-8")
            settings = SimpleNamespace(x_first_enabled=True, twitterapi_io_key="fake",
                                       x_first_run_id="first-run",
                                       x_first_end_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                                       x_first_queries_path=config_file,
                                       x_first_credit_cap=1000, social_weekly_credit_cap=2000,
                                       database_path=root / "collector.sqlite3")

            class Client:
                def __init__(self):
                    self.calls = []

                def search_page(self, query):
                    self.calls.append(query)
                    return {"tweets": []}

            client = Client()
            XFirstWorker(settings, store, client).process()
            settings.x_first_run_id = "second-run"
            XFirstWorker(settings, store, client).process()
            first_batch = client.calls[1]
            second_batch = client.calls[3]
            self.assertNotEqual(first_batch, second_batch)
            self.assertEqual(first_batch.count("from:"), 5)
            self.assertEqual(second_batch.count("from:"), 5)

    def test_watch_search_uses_shared_cap_and_marks_matching_author(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            root = Path(temp)
            store = Store(root / "collector.sqlite3")
            store.initialize()
            state_path = root / "idea-bot-state.sqlite3"
            with closing(sqlite3.connect(state_path)) as state:
                add_accounts(state, ["elonmusk", "solana"], "42")
                self.assertTrue(remove_account(state, "@solana"))
            config_file = root / "queries.json"
            config_file.write_text(json.dumps([{"label": "culture", "query": "viral meme",
                                                "interval_minutes": 0}]), encoding="utf-8")
            settings = SimpleNamespace(x_first_enabled=True, twitterapi_io_key="fake",
                                       x_first_run_id="test-run",
                                       x_first_end_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                                       x_first_queries_path=config_file,
                                       x_first_credit_cap=600, social_weekly_credit_cap=1000,
                                       database_path=root / "collector.sqlite3")

            class Client:
                def __init__(self):
                    self.calls = []

                def search_page(self, query):
                    self.calls.append(query)
                    if "from:elonmusk" not in query:
                        return {"tweets": []}
                    return {"tweets": [
                        {"id": "101", "text": "A new Roadster is here", "createdAt": datetime.now(timezone.utc).isoformat(),
                         "author": {"userName": "ElonMusk"}},
                        {"id": "102", "text": "unrelated", "createdAt": datetime.now(timezone.utc).isoformat(),
                         "author": {"userName": "someone"}},
                    ]}

            client = Client()
            result = XFirstWorker(settings, store, client).process()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["new_posts"], 1)
            self.assertTrue(client.calls[1].startswith("(from:elonmusk)"))
            with store.connection() as conn:
                self.assertEqual(conn.execute("SELECT handle FROM x_watchlist_hits WHERE post_id='101'").fetchone()[0],
                                 "elonmusk")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM x_first_posts WHERE post_id='102'").fetchone()[0], 0)
            self.assertEqual(store.weekly_usage("twitterapi_xfirst", "test-run"), 45)


if __name__ == "__main__":
    unittest.main()
