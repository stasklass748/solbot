import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from narrative_collector.telegram_ui import card_keyboard, parse_chat_ids, poll_once, send_card
from narrative_collector.watchlist import add_accounts


class TelegramInterfaceTests(unittest.TestCase):
    def test_deployment_uses_one_persistent_telegram_polling_process(self):
        root = Path(__file__).resolve().parents[1]
        service = (root / "systemd/meme-idea-telegram.service").read_text(encoding="utf-8")
        starter = (root / "start_live_ideas.sh").read_text(encoding="utf-8")
        self.assertIn("Type=simple", service)
        self.assertIn("--continuous", service)
        self.assertIn("Restart=always", service)
        self.assertIn("systemctl disable --now meme-idea-telegram.timer", starter)
        self.assertIn("meme-idea-telegram.service", starter)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state.sqlite3"
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute("CREATE TABLE alerts (alert_key TEXT PRIMARY KEY, day TEXT, status TEXT, "
                       "created_at TEXT, payload_json TEXT)")
            db.execute("INSERT INTO alerts VALUES (?,?,?,?,?)", (
                "alert-1", "2026-10-16", "sent", "2026-10-16T12:00:00+00:00",
                json.dumps({"source_post_ids": ["123"],
                            "post_url": "https://x.com/alice/status/123",
                            "card_text": "Mochi X карточка",
                            "model": {"coin_name": "Mochi"}}),
            ))
            db.execute("CREATE TABLE alert_deliveries (alert_key TEXT, chat_id TEXT, "
                       "status TEXT, updated_at TEXT, PRIMARY KEY(alert_key,chat_id))")
            db.executemany("INSERT INTO alert_deliveries VALUES (?,?,?,?)", [
                ("alert-1", "42", "sent", "2026-10-16T12:00:00+00:00"),
                ("alert-1", "43", "sent", "2026-10-16T12:00:00+00:00"),
            ])

    def test_parse_two_private_chat_ids(self):
        self.assertEqual(parse_chat_ids("42,43,42"), ("42", "43"))
        with self.assertRaises(ValueError):
            parse_chat_ids("42,not-an-id")

    def test_long_poll_timeout_and_skipped_automatic_diagnostic(self):
        calls = []

        def api(_token, method, payload):
            calls.append((method, payload))
            return [] if method == "getUpdates" else True

        result = poll_once(self.state, "fake", "42", set(), api,
                           diagnostics=lambda: self.fail("automatic diagnostic should be skipped"),
                           poll_timeout=20, notify_diagnostics=False)
        self.assertEqual(result["updates"], 0)
        self.assertEqual(calls[0][1]["timeout"], 20)
        with self.assertRaises(ValueError):
            poll_once(self.state, "fake", "42", set(), api, poll_timeout=21)

    def test_keyboard_has_source_and_review_actions(self):
        keyboard = card_keyboard("123", "https://x.com/alice/status/123")
        self.assertEqual(keyboard["inline_keyboard"][0][0]["url"],
                         "https://x.com/alice/status/123")
        self.assertEqual({button["callback_data"] for button in keyboard["inline_keyboard"][1]},
                         {"vote:work:123", "vote:skip:123", "vote:save:123"})
        with self.assertRaises(ValueError):
            card_keyboard("123", "https://evil.example/")

    def test_send_card_posts_plain_text_and_keyboard(self):
        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self, *_):
                return b'{"ok":true,"result":{"message_id":1}}'

        with patch("urllib.request.urlopen", return_value=Reply()) as mocked:
            result = send_card("123:abc", "42", "<untrusted> & post", "123",
                               "https://x.com/alice/status/123")
        self.assertEqual(result["message_id"], 1)
        request = mocked.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        payload = json.loads(request.data)
        self.assertEqual(payload["text"], "<untrusted> & post")
        self.assertNotIn("parse_mode", payload)
        self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["url"],
                         "https://x.com/alice/status/123")

    def test_private_chat_review_saved_and_idempotent_cursor(self):
        calls = []
        update = {"update_id": 8, "callback_query": {
            "id": "callback-1", "from": {"id": 42}, "data": "vote:save:123",
            "message": {"message_id": 5, "chat": {"id": 42}},
        }}

        def api(_token, method, payload):
            calls.append((method, payload))
            if method == "getUpdates":
                return [update] if payload["offset"] <= 8 else []
            return True

        self.assertEqual(poll_once(self.state, "token", "42", set(), api)["handled"], 1)
        self.assertEqual(poll_once(self.state, "token", "42", set(), api)["updates"], 0)
        with closing(sqlite3.connect(self.state)) as db:
            self.assertEqual(db.execute("SELECT action FROM telegram_feedback_votes "
                                        "WHERE post_id='123' AND reviewer_id='42'").fetchone()[0],
                             "save")
        self.assertTrue(any(method == "answerCallbackQuery" for method, _ in calls))
        self.assertTrue(any(method == "editMessageReplyMarkup" for method, _ in calls))

    def test_unauthorized_reviewer_cannot_change_feedback(self):
        updates = [{"update_id": 1, "callback_query": {
            "id": "cb", "from": {"id": 77}, "data": "vote:work:123",
            "message": {"message_id": 5, "chat": {"id": -100}},
        }}]

        def api(_token, method, _payload):
            return updates if method == "getUpdates" else True

        self.assertEqual(poll_once(self.state, "token", "-100", {"42"}, api)["handled"], 0)
        with closing(sqlite3.connect(self.state)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM telegram_feedback_votes").fetchone()[0], 0)

    def test_two_friends_can_review_independently(self):
        updates = [
            {"update_id": 1, "callback_query": {"id": "a", "from": {"id": 42},
                                                 "data": "vote:save:123",
                                                 "message": {"message_id": 5, "chat": {"id": 42}}}},
            {"update_id": 2, "callback_query": {"id": "b", "from": {"id": 43},
                                                 "data": "vote:work:123",
                                                 "message": {"message_id": 6, "chat": {"id": 43}}}},
        ]

        def api(_token, method, _payload):
            return updates if method == "getUpdates" else True

        self.assertEqual(poll_once(self.state, "token", "42,43", set(), api)["handled"], 2)
        with closing(sqlite3.connect(self.state)) as db:
            rows = db.execute("SELECT reviewer_id,action FROM telegram_feedback_votes "
                              "ORDER BY reviewer_id").fetchall()
            self.assertEqual(rows, [("42", "save"), ("43", "work")])

    def test_start_menu_button_opens_latest(self):
        calls = []
        updates = [
            {"update_id": 1, "message": {"chat": {"id": 42}, "from": {"id": 42},
                                         "text": "/start"}},
            {"update_id": 2, "callback_query": {"id": "cb", "from": {"id": 42},
                                                 "data": "menu:latest",
                                                 "message": {"message_id": 1, "chat": {"id": 42}}}},
        ]

        def api(_token, method, payload):
            calls.append((method, payload))
            return updates if method == "getUpdates" else True

        self.assertEqual(poll_once(self.state, "token", "42", set(), api)["handled"], 2)
        sent = [data for method, data in calls if method == "sendMessage"]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
                         "menu:latest")
        self.assertIn("/card 123", sent[1]["text"])

    def test_latest_stats_and_saved_commands(self):
        responses = []
        updates = [
            {"update_id": i, "message": {"chat": {"id": 42}, "from": {"id": 42},
                                         "text": command}}
            for i, command in enumerate(("/latest", "/stats", "/saved", "/card 123"), 1)
        ]

        def api(_token, method, payload):
            if method == "getUpdates":
                return updates
            if method == "sendMessage":
                responses.append(payload["text"])
            return True

        self.assertEqual(poll_once(self.state, "token", "42", set(), api)["handled"], 4)
        self.assertIn("https://x.com/alice/status/123", responses[0])
        self.assertIn("/card 123", responses[0])
        self.assertIn("карточек 1", responses[1])
        self.assertIn("Сохранённых карточек пока нет", responses[2])
        self.assertEqual(responses[3], "Mochi X карточка")

    def test_txt_upload_is_shared_but_unauthorized_upload_is_ignored(self):
        calls = []
        updates = [
            {"update_id": 1, "message": {"chat": {"id": 99}, "from": {"id": 99},
                                          "document": {"file_name": "watch.txt", "file_id": "evil", "file_size": 12}}},
            {"update_id": 2, "message": {"chat": {"id": 42}, "from": {"id": 42},
                                          "document": {"file_name": "watch.txt", "file_id": "file1", "file_size": 28}}},
            {"update_id": 3, "message": {"chat": {"id": 43}, "from": {"id": 43},
                                          "text": "/watchlist"}},
        ]

        def api(_token, method, payload):
            calls.append((method, payload))
            if method == "getUpdates":
                return updates
            if method == "getFile":
                self.assertEqual(payload["file_id"], "file1")
                return {"file_path": "documents/watch.txt"}
            return True

        result = poll_once(self.state, "token", "42,43", set(), api,
                           lambda _token, _path: b"@elonmusk\nhttps://x.com/Solana\n")
        self.assertEqual(result["handled"], 2)
        sent = [payload for method, payload in calls if method == "sendMessage"]
        self.assertEqual([payload["chat_id"] for payload in sent], ["42", "43"])
        self.assertIn("@elonmusk", sent[1]["text"])
        self.assertIn("@solana", sent[1]["text"])
        with closing(sqlite3.connect(self.state)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM x_watchlist_accounts").fetchone()[0], 2)

    def test_thousand_account_list_is_paginated_for_telegram(self):
        with closing(sqlite3.connect(self.state)) as db:
            add_accounts(db, [f"acct{i:04d}" for i in range(1000)], "42")
        updates = [{"update_id": 1, "message": {"chat": {"id": 42}, "from": {"id": 42},
                                              "text": "/watchlist 20"}}]
        replies = []

        def api(_token, method, payload):
            if method == "getUpdates":
                return updates
            if method == "sendMessage":
                replies.append(payload["text"])
            return True

        self.assertEqual(poll_once(self.state, "token", "42", set(), api)["handled"], 1)
        self.assertEqual(len(replies), 1)
        self.assertIn("1000/1000", replies[0])
        self.assertIn("стр. 20/20", replies[0])
        self.assertIn("@acct0999", replies[0])
        self.assertNotIn("@acct0000", replies[0])
        self.assertLess(len(replies[0]), 4000)

    def test_upload_rejects_non_txt_without_getfile(self):
        updates = [{"update_id": 1, "message": {"chat": {"id": 42}, "from": {"id": 42},
                                              "document": {"file_name": "watch.pdf", "file_id": "file1"}}}]
        calls = []

        def api(_token, method, payload):
            calls.append((method, payload))
            return updates if method == "getUpdates" else True

        poll_once(self.state, "token", "42", set(), api)
        self.assertFalse(any(method == "getFile" for method, _ in calls))
        self.assertIn("Список не добавлен", [payload["text"] for method, payload in calls
                                          if method == "sendMessage"][0])


if __name__ == "__main__":
    unittest.main()
