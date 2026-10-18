import json

from narrative_collector.source_metadata import SourceMetadataWorker, cid_from_uri, post_link
from narrative_collector.storage import Launch, Migration, Store


CID = "bafkreicrkuloewexpsuspvnke3ksnjc3trtoyvbrk44574f7xt5wb5yom4"


def test_uri_is_reduced_to_cid_and_untrusted_host_is_never_called():
    assert cid_from_uri(f"ipfs://{CID}") == CID
    assert cid_from_uri(f"https://any.example/ipfs/{CID}?x=1") == CID
    assert cid_from_uri("http://localhost/private") is None
    assert cid_from_uri("file:///etc/passwd") is None
    assert post_link({"twitter": "https://x.com/person/status/123456789/photo/1"}) == (
        "123456789", "https://x.com/i/status/123456789"
    )
    assert post_link({"twitter": "https://x.com/person"}) == (None, None)


def test_source_worker_only_processes_migrated_launches_after_run_start(tmp_path):
    store = Store(tmp_path / "collector.sqlite3")
    store.initialize()
    store.save_launch(Launch("before", "2026-10-01T00:00:00+00:00", raw={"uri": f"ipfs://{CID}"}))
    store.save_migration(Migration("before", "2026-10-01T01:00:00+00:00"))
    store.save_launch(Launch("not_migrated", "2026-10-10T00:00:00+00:00", raw={"uri": f"ipfs://{CID}"}))
    store.save_launch(Launch("eligible", "2026-10-10T00:00:00+00:00", raw={"uri": f"ipfs://{CID}"}))
    store.save_migration(Migration("eligible", "2026-10-10T01:00:00+00:00"))
    seen = []

    def fake_fetch(cid):
        seen.append(cid)
        return {"description": "a claimed meme", "twitter": "https://twitter.com/a/status/123"}

    worker = SourceMetadataWorker(store, fake_fetch)
    assert worker.process("2026-10-09T00:00:00+00:00") == {
        "processed": 1, "with_post_link": 1, "fetch_errors": 0
    }
    assert worker.process("2026-10-09T00:00:00+00:00")["processed"] == 0
    assert seen == [CID]
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM launch_source_metadata WHERE mint='eligible'").fetchone()
        assert row["status"] == "ok"
        assert row["description"] == "a claimed meme"
        assert row["source_post_id"] == "123"
        assert json.loads(row["raw_json"])["description"] == "a claimed meme"
        assert conn.execute("SELECT COUNT(*) FROM launch_source_metadata").fetchone()[0] == 1


def test_missing_or_non_ipfs_uri_records_coverage_without_network(tmp_path):
    store = Store(tmp_path / "collector.sqlite3")
    store.initialize()
    for mint, uri in (("missing", None), ("bad", "http://127.0.0.1/private")):
        store.save_launch(Launch(mint, "2026-10-10T00:00:00+00:00", raw={"uri": uri}))
        store.save_migration(Migration(mint, "2026-10-10T01:00:00+00:00"))

    def no_network(_):
        raise AssertionError("network should not be called")

    result = SourceMetadataWorker(store, no_network).process("2026-10-09T00:00:00+00:00")
    assert result["processed"] == 2
    assert result["fetch_errors"] == 0
    with store.connection() as conn:
        assert {row["status"] for row in conn.execute("SELECT status FROM launch_source_metadata")} == {
            "missing_uri", "non_ipfs_uri"
        }


def test_transient_gateway_error_retries_at_most_three_times(tmp_path):
    store = Store(tmp_path / "collector.sqlite3")
    store.initialize()
    store.save_launch(Launch("retry", "2026-10-10T00:00:00+00:00", raw={"uri": f"ipfs://{CID}"}))
    store.save_migration(Migration("retry", "2026-10-10T01:00:00+00:00"))
    calls = []

    def fail(_):
        calls.append(1)
        raise OSError("temporary")

    worker = SourceMetadataWorker(store, fail)
    for attempt in range(1, 4):
        assert worker.process("2026-10-09T00:00:00+00:00")["fetch_errors"] == 1
        with store.connection() as conn:
            row = conn.execute("SELECT status,attempts FROM launch_source_metadata").fetchone()
            assert row["status"] == "fetch_error"
            assert row["attempts"] == attempt
            conn.execute("UPDATE launch_source_metadata SET fetched_at='2020-01-01T00:00:00+00:00'")
    assert worker.process("2026-10-09T00:00:00+00:00")["processed"] == 0
    assert len(calls) == 3
