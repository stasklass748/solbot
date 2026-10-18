import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from narrative_collector.storage import Store, _social_stats
from narrative_collector.x_first import XFirstWorker


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def search_page(self, query):
        self.calls.append(query)
        return self.payload


def make_settings(tmp_path: Path, end: datetime):
    path = tmp_path / "queries.json"
    path.write_text(json.dumps([{"label": "culture", "query": "viral meme lang:en",
                                 "interval_minutes": 0}]), encoding="utf-8")
    return SimpleNamespace(
        x_first_enabled=True,
        twitterapi_io_key="test-key",
        x_first_run_id="test-run",
        x_first_end_at=end.isoformat(),
        x_first_queries_path=path,
        x_first_credit_cap=1000,
        social_weekly_credit_cap=2000,
    )


def test_x_first_stores_tokenless_posts_and_deduplicates(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.initialize()
    now = datetime.now(timezone.utc)
    settings = make_settings(tmp_path, now + timedelta(hours=2))
    payload = {"tweets": [{
        "id": "post-1", "text": "funny viral animal", "createdAt": "Tue Oct 06 10:00:00 +0000 2026",
        "author": {"id": "author-1", "userName": "someone"}, "likeCount": 5,
    }], "has_next_page": False}
    client = FakeClient(payload)
    first = XFirstWorker(settings, store, client).process()
    second = XFirstWorker(settings, store, client).process()
    assert first["new_posts"] == 1
    assert second["new_posts"] == 0
    assert store.summary()["x_first_searches"] == 2
    assert store.summary()["x_first_posts"] == 1
    assert store.summary()["x_first_post_observations"] == 2
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM x_first_posts").fetchone()
        assert row["created_at"] == "2026-10-06T10:00:00+00:00"
        assert row["text"] == "funny viral animal"


def test_x_first_ignores_old_end_time_but_halts_at_credit_cap(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.initialize()
    now = datetime.now(timezone.utc)
    settings = make_settings(tmp_path, now - timedelta(seconds=1))
    client = FakeClient({"tweets": []})
    assert XFirstWorker(settings, store, client).process()["status"] == "ok"
    assert len(client.calls) == 1
    store.add_usage("twitterapi_xfirst", "test-run", 715)
    assert XFirstWorker(settings, store, client).process()["status"] == "budget_exhausted"
    assert len(client.calls) == 1


def test_x_first_zero_caps_do_not_stop_search_after_old_usage(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.initialize()
    now = datetime.now(timezone.utc)
    settings = make_settings(tmp_path, now)
    settings.x_first_credit_cap = 0
    settings.social_weekly_credit_cap = 0
    store.add_usage("twitterapi_xfirst", "test-run", 100000)
    week = (now.date() - timedelta(days=now.weekday())).isoformat()
    store.add_usage("twitterapi", week, 1000000)
    result = XFirstWorker(settings, store, FakeClient({"tweets": []})).process()
    assert result["status"] == "ok"
    assert result["searches"] == 1


def test_twitterapi_date_stats_are_parsed():
    result = _social_stats(
        [{"id": "1", "createdAt": "Tue Oct 06 09:00:00 +0000 2026", "author": {"id": "a"}}],
        "2026-10-06T10:00:00+00:00", "2026-10-06T11:00:00+00:00",
    )
    assert result["pre_launch_tweets"] == 1
    assert result["unique_authors"] == 1


def test_x_first_paginates_with_cursor_but_stops_at_page_cap(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.initialize()
    now = datetime.now(timezone.utc)
    settings = make_settings(tmp_path, now + timedelta(hours=2))
    settings.x_first_queries_path.write_text(json.dumps([{
        "label": "crypto", "query": "memecoin lang:en", "interval_minutes": 0, "max_pages": 2
    }]), encoding="utf-8")

    class PagedClient:
        def __init__(self):
            self.calls = []

        def search_page(self, query, cursor=None):
            self.calls.append((query, cursor))
            return {"tweets": [{"id": str(len(self.calls)), "text": "meme"}],
                    "has_next_page": True, "next_cursor": f"page-{len(self.calls)}"}

    client = PagedClient()
    result = XFirstWorker(settings, store, client).process()
    assert result["searches"] == 2
    assert result["new_posts"] == 2
    assert client.calls == [("memecoin lang:en", None), ("memecoin lang:en", "page-1")]
