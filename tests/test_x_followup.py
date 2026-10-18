from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from narrative_collector.storage import Store
from narrative_collector.x_followup import XFollowupWorker


def settings(now):
    return SimpleNamespace(
        x_first_run_id="run-1", x_first_end_at=(now + timedelta(days=1)).isoformat(),
        twitterapi_io_key="fake", x_first_followup_sample_percent=100,
        x_first_credit_cap=1000, social_weekly_credit_cap=2000,
    )


def test_1h_followup_uses_exact_id_and_stores_observation(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.initialize()
    now = datetime.now(timezone.utc)
    observed = (now - timedelta(minutes=70)).isoformat()
    store.save_x_first_search("run-1", "meme", "meme", {
        "tweets": [{"id": "123", "text": "original", "likeCount": 1}]
    }, 15, observed, followup_sample_percent=100)

    class FakeClient:
        def __init__(self):
            self.ids = []

        def tweets_by_ids(self, ids):
            self.ids = ids
            return [{"id": "123", "likeCount": 25, "viewCount": 1000}]

    client = FakeClient()
    result = XFollowupWorker(settings(now), store, client).process()
    assert result["lookups"] == 1
    assert result["observations"] == 1
    assert result["credits"] == 15
    assert client.ids == ["123"]
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM x_followup_observations").fetchone()
        assert row["window_minutes"] == 60
        assert row["likes"] == 25
        assert row["status"] == "ok"


def test_late_window_is_marked_missed_without_a_paid_lookup(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.initialize()
    now = datetime.now(timezone.utc)
    observed = (now - timedelta(hours=3)).isoformat()
    store.save_x_first_search("run-1", "meme", "meme", {"tweets": [{"id": "456"}]},
                              15, observed, followup_sample_percent=100)

    class NoCallClient:
        def tweets_by_ids(self, _):
            raise AssertionError("a missed window must not spend credits")

    result = XFollowupWorker(settings(now), store, NoCallClient()).process()
    assert result["missed_windows"] == 1
    assert result["lookups"] == 0
    with store.connection() as conn:
        row = conn.execute("SELECT status FROM x_followup_observations").fetchone()
        assert row["status"] == "missed_window"
