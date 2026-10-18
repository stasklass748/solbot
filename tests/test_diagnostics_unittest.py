import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from narrative_collector.diagnostics import DiagnosticConfig, snapshot
from narrative_collector.telegram_ui import poll_once


NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
RUN = "20261016T110000Z"


def healthy(unit):
    if unit.endswith(".timer") or unit == "meme-idea-telegram.service":
        return {"ActiveState": "active", "Result": "success"}
    return {"ActiveState": "inactive", "Result": "success", "ExecMainStatus": "0"}


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.collector = root / "collector.sqlite3"
        self.state = root / "state.sqlite3"
        self.config = DiagnosticConfig(self.collector, self.state, RUN,
                                       "2026-10-16T13:00:00Z", 5000, 300, 5)
        with closing(sqlite3.connect(self.collector)) as db, db:
            db.executescript("""
                CREATE TABLE api_usage (period_start TEXT, provider TEXT, units INTEGER);
                CREATE TABLE x_first_searches (run_id TEXT, observed_at TEXT);
                CREATE TABLE x_first_posts (first_observed_at TEXT);
            """)
            db.execute("INSERT INTO api_usage VALUES (?,?,?)", (RUN, "twitterapi_xfirst", 600))
            db.execute("INSERT INTO x_first_searches VALUES (?,?)",
                       (RUN, (NOW - timedelta(minutes=3)).isoformat()))
            db.execute("INSERT INTO x_first_posts VALUES (?)",
                       ((NOW - timedelta(minutes=2)).isoformat(),))
        with closing(sqlite3.connect(self.state)) as db, db:
            db.executescript("""
                CREATE TABLE ai_batches (id INTEGER PRIMARY KEY, analyzed_at TEXT, proposals_json TEXT);
                CREATE TABLE ai_call_budget (day TEXT, calls INTEGER);
                CREATE TABLE alerts (created_at TEXT, day TEXT);
                CREATE TABLE x_watchlist_accounts (handle TEXT);
            """)
            db.execute("INSERT INTO ai_batches (analyzed_at,proposals_json) VALUES (?,?)",
                       ((NOW - timedelta(minutes=1)).isoformat(),
                        json.dumps([{"interesting": False, "confidence": "low"}])))
            db.execute("INSERT INTO ai_call_budget VALUES (?,?)", (NOW.date().isoformat(), 1))

    def test_current_pipeline_explains_rejected_posts_without_provider_calls(self):
        text, problems = snapshot(self.config, NOW, healthy)
        self.assertEqual(problems, ())
        self.assertIn("≈600/5000", text)
        self.assertIn("новых постов за час: 1", text)
        self.assertIn("ИИ не увидел достаточно сильной идеи", text)
        self.assertIn("Карточки: 0 за час, 0/5", text)

    def test_unlimited_ai_calls_are_reported_without_false_cap_warning(self):
        config = DiagnosticConfig(self.collector, self.state, RUN,
                                  "2026-10-16T13:00:00Z", 5000, 0, 0)
        text, problems = snapshot(config, NOW, healthy)
        self.assertIn("ИИ-вызовы сегодня: 1 (без дневного лимита)", text)
        self.assertIn("Карточки: 0 за час, 0 сегодня (без дневного лимита)", text)
        self.assertNotIn("достигнут дневной лимит ИИ", text)
        self.assertNotIn("достигнут дневной лимит карточек", text)
        self.assertEqual(problems, ())

    def test_unlimited_cards_do_not_trigger_false_warning_after_existing_cards(self):
        with closing(sqlite3.connect(self.state)) as db, db:
            for _ in range(10):
                db.execute("INSERT INTO alerts VALUES (?,?)",
                           ((NOW - timedelta(minutes=5)).isoformat(), NOW.date().isoformat()))
        config = DiagnosticConfig(self.collector, self.state, RUN,
                                  "2026-10-16T13:00:00Z", 5000, 0, 0)
        text, problems = snapshot(config, NOW, healthy)
        self.assertIn("Карточки: 10 за час, 10 сегодня (без дневного лимита)", text)
        self.assertNotIn("достигнут дневной лимит карточек", text)
        self.assertEqual(problems, ())

    def test_keyword_progress_and_cap_are_visible(self):
        path = self.state.parent / "x-keyword-state.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE keyword_run "
                       "(id INTEGER PRIMARY KEY,next_batch INTEGER,batches_total INTEGER,"
                       "credits_used INTEGER,credit_cap INTEGER)")
            db.execute("INSERT INTO keyword_run VALUES (1,8,7,900,1000)")
        text, _ = snapshot(self.config, NOW, healthy)
        self.assertIn("Поиск по словам: круг 2, группа 2/7", text)
        self.assertIn("остановлен лимитом X-кредитов", text)

    def test_zero_x_caps_show_usage_without_false_exhaustion(self):
        path = self.state.parent / "x-keyword-state.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE keyword_run "
                       "(id INTEGER PRIMARY KEY,next_batch INTEGER,batches_total INTEGER,"
                       "credits_used INTEGER,credit_cap INTEGER)")
            db.execute("INSERT INTO keyword_run VALUES (1,335,363,99900,0)")
        config = DiagnosticConfig(self.collector, self.state, RUN,
                                  "2026-10-16T13:00:00Z", 0, 0, 0, 0)
        text, _ = snapshot(config, NOW, healthy)
        self.assertIn("внутренние лимиты кредитов отключены", text)
        self.assertIn("X-кредиты ≈99900 (без внутреннего лимита)", text)
        self.assertNotIn("остановлен лимитом X-кредитов", text)

    def test_openai_quota_block_is_reported_without_exposing_response_body(self):
        with closing(sqlite3.connect(self.state)) as db, db:
            db.execute("CREATE TABLE ai_provider_pause "
                       "(id INTEGER PRIMARY KEY,status TEXT,error_code TEXT,"
                       "resume_at TEXT,failures INTEGER)")
            db.execute("INSERT INTO ai_provider_pause VALUES (1,?,?,?,1)",
                       ("requires_action", "credit_balance_exhausted", None))
        text, _ = snapshot(self.config, NOW, healthy)
        self.assertIn("нужна проверка квоты/оплаты", text)
        self.assertIn("credit_balance_exhausted", text)
        self.assertNotIn("Посты найдены, но ИИ их пока не разобрал", text)

    def test_old_end_time_does_not_end_credit_capped_run(self):
        text, problems = snapshot(self.config, NOW + timedelta(hours=2), healthy)
        self.assertIn("без ограничения по времени", text)
        self.assertNotIn("завершён по времени", text)
        self.assertIn("x_search_stale", problems)

    def test_failed_ai_service_is_reported_without_raw_log(self):
        def failed_ai(unit):
            if unit == "meme-idea-analyzer.service":
                return {"ActiveState": "failed", "Result": "exit-code", "ExecMainStatus": "1"}
            return healthy(unit)

        text, problems = snapshot(self.config, NOW, failed_ai)
        self.assertIn("задание ИИ: ОШИБКА (exit-code, код 1)", text)
        self.assertIn("meme-idea-analyzer.service:exit-code", problems)

    def test_internal_weekly_cap_explains_no_new_cards(self):
        week = (NOW.date() - timedelta(days=NOW.weekday())).isoformat()
        with closing(sqlite3.connect(self.collector)) as db, db:
            db.execute("INSERT INTO api_usage VALUES (?,?,?)", (week, "twitterapi", 799900))
        config = DiagnosticConfig(self.collector, self.state, RUN,
                                  "2026-10-16T13:00:00Z", 5000, 300, 5, 800000)
        text, problems = snapshot(config, NOW, healthy)
        self.assertIn("X-кредиты недели: ≈799900/800000", text)
        self.assertIn("внутренний лимит X исчерпан", text)
        self.assertEqual(problems, ())

    def test_telegram_command_button_access_and_notice_dedup(self):
        updates = [
            {"update_id": 1, "message": {"chat": {"id": 42}, "from": {"id": 42},
                                         "text": "/diagnostics"}},
            {"update_id": 2, "callback_query": {"id": "cb", "from": {"id": 43},
                                                  "data": "menu:diagnostics",
                                                  "message": {"message_id": 1, "chat": {"id": 43}}}},
            {"update_id": 3, "message": {"chat": {"id": 99}, "from": {"id": 99},
                                         "text": "/diagnostics"}},
        ]
        sent = []

        def api(_token, method, payload):
            if method == "getUpdates":
                return updates if payload["offset"] <= 1 else []
            if method == "sendMessage":
                sent.append(payload)
            return True

        def diagnostic():
            return "🩺 Причина: тестовая ошибка", ("x_search_stale", "ai_failed")

        result = poll_once(self.state, "fake", "42,43", set(), api,
                           diagnostics=diagnostic)
        self.assertEqual(result["handled"], 2)
        self.assertEqual(len([p for p in sent if "Причина" in p["text"]]), 2)
        self.assertEqual(len([p for p in sent if "⚠️" in p["text"]]), 2)
        self.assertTrue(all("X-поиск давно не обновлялся" in p["text"]
                            for p in sent if "⚠️" in p["text"]))
        self.assertNotIn("99", [str(p["chat_id"]) for p in sent])
        poll_once(self.state, "fake", "42,43", set(), api, diagnostics=diagnostic)
        self.assertEqual(len([p for p in sent if "⚠️" in p["text"]]), 2)

    def test_expired_callback_does_not_block_menu_or_cursor(self):
        update = {"update_id": 1, "callback_query": {
            "id": "expired", "from": {"id": 42}, "data": "menu:diagnostics",
            "message": {"message_id": 1, "chat": {"id": 42}},
        }}
        calls = []

        def api(_token, method, payload):
            calls.append(method)
            if method == "getUpdates":
                return [update] if payload["offset"] <= 1 else []
            if method == "answerCallbackQuery":
                raise RuntimeError("Telegram answerCallbackQuery HTTP 400")
            return True

        result = poll_once(self.state, "fake", "42", set(), api,
                           diagnostics=lambda: ("🩺 Всё работает", ()))
        self.assertEqual(result, {"updates": 1, "handled": 1})
        self.assertEqual(calls[:3], ["getUpdates", "answerCallbackQuery", "sendMessage"])
        self.assertEqual(poll_once(self.state, "fake", "42", set(), api,
                                   diagnostics=lambda: ("🩺 Всё работает", ()))['updates'], 0)

    def test_diagnostic_exception_keeps_bot_responsive(self):
        update = {"update_id": 1, "message": {
            "chat": {"id": 42}, "from": {"id": 42}, "text": "/diagnostics",
        }}
        sent = []
        runs = []

        def api(_token, method, payload):
            if method == "getUpdates":
                return [update] if payload["offset"] <= 1 else []
            if method == "sendMessage":
                sent.append(payload["text"])
            return True

        def diagnostic():
            runs.append(True)
            raise TypeError("unexpected diagnostic schema")

        result = poll_once(self.state, "fake", "42", set(), api,
                           diagnostics=diagnostic)
        self.assertEqual(result, {"updates": 1, "handled": 1})
        self.assertEqual(len(runs), 1)
        self.assertIn("Диагностика сейчас недоступна", sent[0])


if __name__ == "__main__":
    unittest.main()
