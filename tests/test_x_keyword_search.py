from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from narrative_collector.storage import Store
from narrative_collector.x_keyword_search import (
    MAX_QUERY_LENGTH, _state, build_queries, load_terms, process, renew_run, start_run,
)


class FakeClient:
    def __init__(self):
        self.queries = []

    def search_page(self, query):
        self.queries.append(query)
        return {"tweets": [{"id": str(123 + len(self.queries)),
                            "createdAt": "2026-10-16T10:00:00Z",
                            "text": "A new little character appears",
                            "author": {"userName": "someone"}}],
                "has_next_page": False}


class KeywordSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.terms = root / "terms.txt"
        self.terms.write_text("DOG\nCAT\nDOG\nNEW MASCOT\nTESLA ROADSTER\n", encoding="utf-8")
        self.db = root / "collector.sqlite3"
        self.state = root / "state.sqlite3"
        self.store = Store(self.db)
        self.store.initialize()
        self.settings = SimpleNamespace(database_path=self.db, twitterapi_io_key="key",
                                        social_weekly_credit_cap=10000)
        self.now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)

    def test_all_supplied_unique_terms_go_into_bounded_queries(self):
        terms = load_terms(self.terms)
        queries = build_queries(terms)
        self.assertEqual(len(terms), 4)
        self.assertEqual(sum(query.count('"') // 2 for query in queries), 4)
        self.assertTrue(all(len(query) <= MAX_QUERY_LENGTH for query in queries))
        full_list = Path(__file__).parents[1] / "x_keyword_terms.txt"
        supplied = load_terms(full_list)
        all_queries = build_queries(supplied)
        self.assertGreaterEqual(len(supplied), 3)
        self.assertEqual(sum(query.count('"') // 2 for query in all_queries), len(supplied))
        self.assertTrue(all(len(query) <= MAX_QUERY_LENGTH for query in all_queries))

    def test_explicit_start_pacing_and_credit_cap(self):
        client = FakeClient()
        self.assertEqual(process(self.settings, self.terms, self.state,
                                 now=self.now, client=client, store=self.store)["status"], "not_started")
        self.assertEqual(client.queries, [])
        started = start_run(self.state, self.terms, self.now, credit_cap=300)
        self.assertEqual(started["terms"], 4)
        with self.assertRaises(ValueError):
            start_run(self.state, self.terms, self.now, credit_cap=300)
        first = process(self.settings, self.terms, self.state,
                        now=self.now, client=client, store=self.store)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["credits_used"], 15)
        self.assertEqual(len(client.queries), 1)
        self.assertEqual(process(self.settings, self.terms, self.state,
                                 now=self.now + timedelta(minutes=1), client=client,
                                 store=self.store)["status"], "not_due")
        with closing(_state(self.state)) as state:
            self.assertEqual(tuple(state.execute("SELECT next_batch, credits_used FROM keyword_run").fetchone()), (1, 15))
        self.assertEqual(self.store.weekly_usage("twitterapi", "2026-10-12"), 15)

    def test_three_minute_interval_and_unlimited_default_cap(self):
        self.terms.write_text("\n".join(f"PHRASE {i}" for i in range(30)), encoding="utf-8")
        started = start_run(self.state, self.terms, self.now)
        self.assertEqual(started["credit_cap"], 0)
        client = FakeClient()
        process(self.settings, self.terms, self.state, now=self.now,
                client=client, store=self.store)
        early = process(self.settings, self.terms, self.state,
                        now=self.now + timedelta(minutes=2, seconds=29),
                        client=client, store=self.store)
        self.assertEqual(early["status"], "not_due")
        due = process(self.settings, self.terms, self.state,
                      now=self.now + timedelta(minutes=3), client=client, store=self.store)
        self.assertEqual(due["status"], "ok")
        self.assertEqual(len(client.queries), 2)

    def test_zero_caps_keep_searching_after_old_budget_was_exhausted(self):
        start_run(self.state, self.terms, self.now, credit_cap=0)
        self.settings.social_weekly_credit_cap = 0
        with closing(_state(self.state)) as state, state:
            state.execute("UPDATE keyword_run SET credits_used=100000 WHERE id=1")
        self.store.add_usage("twitterapi", "2026-10-12", 1000000)
        result = process(self.settings, self.terms, self.state,
                         now=self.now, client=FakeClient(), store=self.store)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["credits_used"], 100015)

    def test_changed_terms_refused_even_after_old_end_time(self):
        start_run(self.state, self.terms, self.now)
        self.terms.write_text("OTHER\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            process(self.settings, self.terms, self.state, now=self.now,
                    client=FakeClient(), store=self.store)
        with self.assertRaises(ValueError):
            process(self.settings, self.terms, self.state,
                    now=self.now + timedelta(hours=25), client=FakeClient(), store=self.store)

    def test_old_end_time_does_not_stop_unsearched_keyword_batches(self):
        self.terms.write_text("\n".join(f"PHRASE {i}" for i in range(30)), encoding="utf-8")
        start_run(self.state, self.terms, self.now)
        client = FakeClient()
        result = process(self.settings, self.terms, self.state,
                         now=self.now + timedelta(hours=25), client=client, store=self.store)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(client.queries), 1)

    def test_keyword_batches_repeat_without_resetting_credit_usage(self):
        started = start_run(self.state, self.terms, self.now, credit_cap=600)
        self.assertEqual(started["batches"], 1)
        client = FakeClient()
        first = process(self.settings, self.terms, self.state,
                        now=self.now, client=client, store=self.store)
        second = process(self.settings, self.terms, self.state,
                         now=self.now + timedelta(minutes=3), client=client, store=self.store)
        self.assertEqual((first["cycle"], second["cycle"]), (1, 2))
        self.assertEqual((first["batch"], second["batch"]), (1, 1))
        self.assertEqual(second["credits_used"], 30)
        with closing(_state(self.state)) as state:
            self.assertEqual(state.execute("SELECT COUNT(*) FROM keyword_requests").fetchone()[0], 2)
            self.assertEqual(state.execute("SELECT next_batch FROM keyword_run").fetchone()[0], 2)

    def test_explicit_renewal_continues_without_repeating_batches(self):
        self.terms.write_text("\n".join(f"PHRASE {i}" for i in range(30)), encoding="utf-8")
        started = start_run(self.state, self.terms, self.now, credit_cap=300)
        self.assertGreater(started["batches"], 1)
        client = FakeClient()
        process(self.settings, self.terms, self.state, now=self.now, client=client, store=self.store)
        with self.assertRaises(ValueError):
            renew_run(self.state, self.terms, self.now + timedelta(hours=1))
        renewed = renew_run(self.state, self.terms, self.now + timedelta(hours=25))
        self.assertEqual(renewed["next_batch"], 1)
        second = process(self.settings, self.terms, self.state,
                         now=self.now + timedelta(hours=25), client=client, store=self.store)
        self.assertEqual(second["batch"], 2)
        self.assertNotEqual(client.queries[0], client.queries[1])


if __name__ == "__main__":
    unittest.main()
