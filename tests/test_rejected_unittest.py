import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from narrative_collector.ai_analyzer import INSTRUCTIONS
from narrative_collector.rejected_maintenance import restore_and_clear
from narrative_collector.telegram_ui import poll_once


class RejectedIdeasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = root / "idea-bot-state.sqlite3"
        self.collector = root / "collector.sqlite3"
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute("CREATE TABLE ai_batches (id INTEGER PRIMARY KEY, analyzed_at TEXT, "
                       "post_ids_json TEXT, proposals_json TEXT, usage_json TEXT)")
            for post_id in range(101, 108):
                proposal = {
                    "post_id": str(post_id), "interesting": post_id == 106,
                    "confidence": "high" if post_id == 106 else "low",
                    "reason": "Слишком общий инфоповод" if post_id == 107 else "",
                }
                db.execute("INSERT INTO ai_batches VALUES (?,?,?,?,?)", (
                    post_id, "2026-10-16T12:00:00+00:00", json.dumps([str(post_id)]),
                    json.dumps([proposal], ensure_ascii=False), "{}",
                ))
        with closing(sqlite3.connect(self.collector)) as db, db:
            db.execute("CREATE TABLE x_first_posts (post_id TEXT PRIMARY KEY, "
                       "author_username TEXT, text TEXT, raw_json TEXT)")
            for post_id in range(101, 108):
                db.execute("INSERT INTO x_first_posts VALUES (?,?,?,?)", (
                    str(post_id), "SpaceX", f"Пост о космической собаке {post_id}",
                    json.dumps({"viewCount": 2100, "likeCount": 44}),
                ))

    def test_rejected_command_and_button_show_original_posts_without_new_ai_calls(self):
        updates = [
            {"update_id": 1, "message": {"chat": {"id": 42}, "from": {"id": 42},
                                       "text": "/rejected"}},
            {"update_id": 2, "callback_query": {"id": "cb", "from": {"id": 43},
                                                  "data": "menu:rejected",
                                                  "message": {"message_id": 1, "chat": {"id": 43}}}},
            {"update_id": 3, "message": {"chat": {"id": 99}, "from": {"id": 99},
                                       "text": "/rejected"}},
        ]
        calls = []

        def api(_token, method, payload):
            calls.append((method, payload))
            return updates if method == "getUpdates" else True

        result = poll_once(self.state, "fake", "42,43", set(), api,
                           collector_db_path=self.collector)
        self.assertEqual(result["handled"], 2)
        replies = [payload for method, payload in calls if method == "sendMessage"]
        self.assertEqual({str(reply["chat_id"]) for reply in replies}, {"42", "43"})
        self.assertIn("https://x.com/SpaceX/status/107", replies[0]["text"])
        self.assertIn("Пост о космической собаке 107", replies[0]["text"])
        self.assertIn("Слишком общий инфоповод", replies[0]["text"])
        self.assertIn("Для старого разбора причина не сохранена", replies[0]["text"])
        self.assertNotIn("/status/106", replies[0]["text"])
        self.assertIn("/rejected 2", replies[0]["text"])
        self.assertFalse(any(method not in {"getUpdates", "answerCallbackQuery", "sendMessage"}
                             for method, _ in calls))

    def test_second_page_and_bad_input(self):
        updates = [
            {"update_id": 1, "message": {"chat": {"id": 42}, "from": {"id": 42},
                                       "text": "/rejected 2"}},
            {"update_id": 2, "message": {"chat": {"id": 42}, "from": {"id": 42},
                                       "text": "/rejected " + "9" * 5000}},
        ]
        replies = []

        def api(_token, method, payload):
            if method == "getUpdates":
                return updates
            if method == "sendMessage":
                replies.append(payload["text"])
            return True

        self.assertEqual(poll_once(self.state, "fake", "42", set(), api,
                                   collector_db_path=self.collector)["handled"], 2)
        self.assertIn("/status/101", replies[0])
        self.assertIn("страницы 1–10", replies[1])

    def test_future_rejection_prompt_requests_reason(self):
        self.assertIn("Even when rejecting a post", INSTRUCTIONS)
        self.assertIn("one concise Russian sentence in reason", INSTRUCTIONS)

    def test_clear_command_hides_old_rejects_without_erasing_diagnostics(self):
        updates = [
            {"update_id": 1, "message": {"chat": {"id": 42}, "from": {"id": 42},
                                         "text": "/clear_rejected"}},
            {"update_id": 2, "message": {"chat": {"id": 42}, "from": {"id": 42},
                                         "text": "/clear_rejected confirm"}},
            {"update_id": 3, "message": {"chat": {"id": 43}, "from": {"id": 43},
                                         "text": "/rejected"}},
        ]
        replies = []

        def api(_token, method, payload):
            if method == "getUpdates":
                return updates
            if method == "sendMessage":
                replies.append(payload["text"])
            return True

        self.assertEqual(poll_once(self.state, "fake", "42,43", set(), api,
                                   collector_db_path=self.collector)["handled"], 3)
        self.assertIn("confirm", replies[0])
        self.assertIn("Старые отказы скрыты", replies[1])
        self.assertIn("нет отклонённых", replies[2])
        with closing(sqlite3.connect(self.state)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_batches").fetchone()[0], 7)
            self.assertEqual(db.execute("SELECT cleared_through_id FROM rejected_view_state").fetchone()[0], 107)

    def test_repair_restores_archived_decisions_and_hides_them(self):
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute("CREATE TABLE ai_batches_rejected_archive AS SELECT * FROM ai_batches WHERE 0")
            db.execute("INSERT INTO ai_batches_rejected_archive SELECT * FROM ai_batches WHERE id=107")
            db.execute("DELETE FROM ai_batches WHERE id=107")
        result = restore_and_clear(self.state)
        self.assertEqual(result["restored_decisions"], 1)
        self.assertEqual(result["hidden_through_batch_id"], 107)
        self.assertTrue(Path(result["backup"]).is_file())
        with closing(sqlite3.connect(self.state)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ai_batches").fetchone()[0], 7)
            db.execute("INSERT INTO ai_batches VALUES (?,?,?,?,?)", (
                108, "2026-10-16T13:00:00+00:00", json.dumps(["108"]),
                json.dumps([{"post_id": "108", "interesting": False, "confidence": "low"}]), "{}"))
            db.commit()
        result = restore_and_clear(self.state)
        self.assertEqual(result["restored_decisions"], 0)
        self.assertEqual(result["hidden_through_batch_id"], 108)


if __name__ == "__main__":
    unittest.main()
