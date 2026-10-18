import json
import io
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from narrative_collector.ai_analyzer import Config, INSTRUCTIONS, OpenAIAPIError, OpenAIAnalyst, Post, run


class FakeAnalyst:
    def __init__(self, proposal):
        self.proposal = proposal
        self.calls = 0

    def analyze(self, post):
        self.calls += 1
        return self.proposal


def proposal(post_id):
    return {
        "post_id": post_id,
        "interesting": True,
        "theme": "animal_character",
        "narrative": "Dancing capybara called Mochi",
        "coin_name": "Dancing Mochi",
        "ticker": "MOCHI",
        "description": "A playful capybara meme inspired by the original X posts.",
        "reason": "The post itself describes a recognisable dancing animal character.",
        "confidence": "high",
    }


class AnalyzerTests(unittest.TestCase):
    def test_meme_hook_eval_fixture_and_prompt(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "narrative_eval.jsonl"
        cases = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([case["human_label"] for case in cases],
                         ["manual_review_candidate", "reject", "reject"])
        self.assertIn("same day as the Tesla roadster demo", cases[0]["text"])
        self.assertIn("Poorsh", cases[0]["text"])
        self.assertIn("timely rivalry", INSTRUCTIONS)
        self.assertIn("short coined nickname/pun", INSTRUCTIONS)
        self.assertIn("Do not assume a short pun is recycled", INSTRUCTIONS)
        self.assertIn("reject investment promotion", INSTRUCTIONS)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "collector.sqlite3"
        self.state = Path(self.temp.name) / "state.sqlite3"
        self.now = datetime.now(timezone.utc)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE x_first_posts (
                  post_id TEXT PRIMARY KEY, created_at TEXT, first_observed_at TEXT,
                  author_username TEXT, text TEXT, raw_json TEXT);
                CREATE TABLE launch_source_metadata (source_post_id TEXT);
                CREATE TABLE launches (mint TEXT, name TEXT, symbol TEXT, launched_at TEXT);
            """)
        self.config = Config(self.db, self.state, "fake-openai", "fake-tg", "123",
                             min_views=0)

    def add_post(self, post_id, author, likes=6, views=800, text=None):
        observed = self.now - timedelta(minutes=5)
        created = observed - timedelta(minutes=2)
        text = text or f"The dancing capybara named Mochi is making everyone laugh in this adorable new clip {post_id}"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO x_first_posts VALUES (?,?,?,?,?,?)",
                         (post_id, created.isoformat(), observed.isoformat(), author, text,
                          json.dumps({"likeCount": likes, "viewCount": views})))

    def test_eligible_ai_alert_sent_once(self):
        self.add_post("101", "alice", likes=30, views=4000)
        analyst = FakeAnalyst(proposal("101"))
        messages = []
        result = run(self.config, self.now, analyst,
                     lambda token, chat, msg, post_id, url: messages.append(msg))
        self.assertEqual((result["ai_calls"], result["sent"]), (1, 1))
        self.assertEqual(analyst.calls, 1)
        self.assertIn("https://x.com/alice/status/101", messages[0])
        self.assertIn("4,000 просмотров", messages[0])
        self.assertIn("Тема для исследования: животное / персонаж", messages[0])
        self.assertIn("Stonk.fun НЕ выполнена", messages[0])
        self.assertEqual(run(self.config, self.now, analyst,
                             lambda *_: self.fail("duplicate send"))["status"], "no_candidates")

    def test_hallucinated_id_rejected(self):
        self.add_post("101", "alice", likes=30)
        result = run(self.config, self.now, FakeAnalyst(proposal("fabricated")),
                     lambda *_: self.fail("should not send"))
        self.assertEqual(result["sent"], 0)

    def test_each_post_gets_its_own_ai_call(self):
        self.add_post("101", "alice", likes=30, views=4000)
        self.add_post("102", "bob", likes=35, views=5000,
                      text="A curious otter wearing a red hat is making the whole internet laugh in a new clip")

        class PerPostAnalyst:
            def __init__(self):
                self.ids = []

            def analyze(self, post):
                self.ids.append(post.post_id)
                value = proposal(post.post_id)
                value["coin_name"] = f"Dancing Mochi {post.post_id}"
                value["ticker"] = f"MO{post.post_id}"
                return value

        analyst = PerPostAnalyst()
        result = run(self.config, self.now, analyst, lambda *_: None)
        self.assertEqual(result["ai_calls"], 2)
        self.assertEqual(set(analyst.ids), {"101", "102"})
        with closing(sqlite3.connect(self.state)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ai_batches").fetchone()[0], 2)

    def test_same_draft_name_from_different_sources_is_not_silently_hidden(self):
        self.add_post("101", "alice", likes=30, views=4000)
        self.add_post("102", "bob", likes=35, views=5000,
                      text="A newly unveiled robot with a funny red hat dances across the stage")

        class PerPostAnalyst:
            def analyze(self, post):
                return proposal(post.post_id)

        sent = []
        result = run(self.config, self.now, PerPostAnalyst(),
                     lambda _token, _chat, _message, post_id, _url: sent.append(post_id))
        self.assertEqual(result["sent"], 2)
        self.assertEqual(set(sent), {"101", "102"})

    def test_one_card_is_sent_to_both_chat_ids(self):
        self.add_post("101", "alice", likes=30, views=4000)
        config = Config(self.db, self.state, "key", "token", "42,43", min_views=0)
        recipients = []
        result = run(config, self.now, FakeAnalyst(proposal("101")),
                     lambda _token, chat, *_: recipients.append(chat))
        self.assertEqual(recipients, ["42", "43"])
        self.assertEqual(result["sent"], 1)
        with closing(sqlite3.connect(self.state)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM alerts").fetchone()[0], "sent")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alert_deliveries "
                                          "WHERE status='sent'").fetchone()[0], 2)

    def test_partial_delivery_is_recorded_without_duplicate_retry(self):
        self.add_post("101", "alice", likes=30, views=4000)
        config = Config(self.db, self.state, "key", "token", "42,43", min_views=0)

        def sender(_token, chat, *_):
            if chat == "43":
                raise RuntimeError("simulated timeout")

        with patch("narrative_collector.ai_analyzer.LOGGER.exception"):
            result = run(config, self.now, FakeAnalyst(proposal("101")), sender)
        self.assertEqual((result["sent"], result["delivery_errors"]), (1, 1))
        with closing(sqlite3.connect(self.state)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM alerts").fetchone()[0], "partial")
            self.assertEqual(conn.execute("SELECT status FROM alert_deliveries "
                                          "WHERE chat_id='43'").fetchone()[0], "uncertain")

    def test_copied_text_produces_at_most_one_draft(self):
        copied = "Mochi the capybara dancing in the same little clip is all over the internet today"
        self.add_post("101", "alice", likes=6, views=900, text=copied)
        self.add_post("102", "bob", likes=6, views=900, text=copied)
        class PerPostAnalyst:
            def __init__(self):
                self.calls = 0

            def analyze(self, post):
                self.calls += 1
                return proposal(post.post_id)

        analyst = PerPostAnalyst()
        messages = []
        result = run(self.config, self.now, analyst,
                     lambda _token, _chat, message, *_: messages.append(message))
        self.assertEqual(result["ai_calls"], 1)
        self.assertEqual(analyst.calls, 1)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(messages), 1)

    def test_low_initial_engagement_can_still_produce_an_early_card(self):
        self.add_post("101", "alice", likes=3, views=300)
        messages = []
        result = run(self.config, self.now, FakeAnalyst(proposal("101")),
                     lambda _token, _chat, message, *_: messages.append(message))
        self.assertEqual(result["sent"], 1)
        self.assertIn("300 просмотров", messages[0])
        self.assertIn("Ранний сигнал", messages[0])

    def test_below_discovery_prefilter_still_skips_ai(self):
        self.add_post("101", "alice", likes=0, views=0)
        analyst = FakeAnalyst(proposal("101"))
        result = run(self.config, self.now, analyst,
                     lambda *_: self.fail("should not send"))
        self.assertEqual(result["sent"], 0)
        self.assertEqual(analyst.calls, 0)

    def test_five_thousand_views_required_even_for_watched_accounts(self):
        self.add_post("4999", "Solana", likes=100, views=4999,
                      text="A new dancing otter mascot appears in the latest Solana clip")
        self.add_post("5000", "bob", likes=2, views=5000,
                      text="A robot with bright blue ears is dancing on stage in a new clip")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE x_watchlist_hits "
                         "(post_id TEXT PRIMARY KEY, handle TEXT, observed_at TEXT)")
            conn.execute("INSERT INTO x_watchlist_hits VALUES (?,?,?)",
                         ("4999", "solana", self.now.isoformat()))
        config = replace(self.config, min_views=5000)

        class RecordingAnalyst:
            def __init__(self):
                self.ids = []

            def analyze(self, post):
                self.ids.append(post.post_id)
                return proposal(post.post_id)

        analyst = RecordingAnalyst()
        result = run(config, self.now, analyst, lambda *_: None)
        self.assertEqual(result["ai_calls"], 1)
        self.assertEqual(analyst.ids, ["5000"])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM x_first_posts").fetchone()[0], 2)

    def test_missing_views_do_not_reach_ai_at_five_thousand(self):
        self.add_post("missing", "alice", likes=100, views=0)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE x_first_posts SET raw_json=? WHERE post_id='missing'",
                         (json.dumps({"likeCount": 100}),))
        analyst = FakeAnalyst(proposal("missing"))
        result = run(replace(self.config, min_views=5000), self.now, analyst,
                     lambda *_: self.fail("should not send"))
        self.assertEqual(result["status"], "no_candidates")
        self.assertEqual(analyst.calls, 0)

    def test_later_observation_can_reach_view_threshold(self):
        self.add_post("later", "alice", likes=2, views=100,
                      text="A dancing capybara named Mochi appears in a playful new clip")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE x_first_searches (
                  id INTEGER PRIMARY KEY, query_label TEXT, observed_at TEXT);
                CREATE TABLE x_first_post_observations (
                  search_id INTEGER, post_id TEXT, likes INTEGER, views INTEGER,
                  PRIMARY KEY(search_id, post_id));
            """)
            conn.execute("INSERT INTO x_first_searches VALUES (?,?,?)",
                         (1, "discovery", (self.now - timedelta(minutes=1)).isoformat()))
            conn.execute("INSERT INTO x_first_post_observations VALUES (?,?,?,?)",
                         (1, "later", 32, 6200))
        messages = []
        result = run(replace(self.config, min_views=5000), self.now,
                     FakeAnalyst(proposal("later")),
                     lambda _token, _chat, message, *_: messages.append(message))
        self.assertEqual(result["sent"], 1)
        self.assertIn("6,200 просмотров", messages[0])

    def test_old_end_time_does_not_stop_ai_review(self):
        self.add_post("101", "alice", likes=30, views=5000)
        config = replace(self.config, end_at=(self.now - timedelta(hours=1)).isoformat(),
                         min_views=5000)
        analyst = FakeAnalyst(proposal("101"))
        result = run(config, self.now, analyst, lambda *_: None)
        self.assertEqual(result["ai_calls"], 1)

    def test_existing_name_or_ticker_warns_instead_of_hiding_idea(self):
        self.add_post("102", "bob", likes=30, views=4000)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO launches VALUES (?,?,?,?)",
                         ("mint", "Other Name", "MOCHI", self.now.isoformat()))
        messages = []
        result = run(self.config, self.now, FakeAnalyst(proposal("102")),
                     lambda _token, _chat, message, *_: messages.append(message))
        self.assertEqual(result["sent"], 1)
        self.assertIn("имя или тикер уже встречались", messages[0])

    def test_exact_source_post_already_used_is_suppressed(self):
        self.add_post("101", "alice", likes=30, views=4000)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO launch_source_metadata VALUES (?)", ("101",))
        result = run(self.config, self.now, FakeAnalyst(proposal("101")),
                     lambda *_: self.fail("should not send"))
        self.assertEqual(result["sent"], 0)

    def test_daily_ai_call_cap(self):
        self.add_post("101", "alice", likes=30)
        capped = Config(self.db, self.state, "key", "token", "42", max_ai_calls_per_day=1,
                        min_views=0)
        run(capped, self.now, FakeAnalyst(proposal("101")), lambda *_: None)
        self.add_post("102", "bob", likes=30)
        analyst = FakeAnalyst(proposal("102"))
        result = run(capped, self.now, analyst, lambda *_: None)
        self.assertEqual(result["status"], "ai_daily_cap")
        self.assertEqual(analyst.calls, 0)

    def test_zero_ai_daily_cap_means_unlimited_with_accounting(self):
        self.add_post("101", "alice", likes=30, views=5000)
        config = replace(self.config, max_ai_calls_per_day=0, min_views=5000)
        with closing(sqlite3.connect(self.state)) as conn, conn:
            conn.execute("CREATE TABLE ai_call_budget (day TEXT PRIMARY KEY, calls INTEGER NOT NULL)")
            conn.execute("INSERT INTO ai_call_budget VALUES (?,?)", (self.now.date().isoformat(), 999))
        result = run(config, self.now, FakeAnalyst(proposal("101")), lambda *_: None)
        self.assertEqual(result["ai_calls"], 1)
        with closing(sqlite3.connect(self.state)) as conn:
            self.assertEqual(conn.execute("SELECT calls FROM ai_call_budget").fetchone()[0], 1000)

    def test_zero_alert_daily_cap_allows_more_cards_after_existing_daily_count(self):
        self.add_post("101", "alice", likes=30, views=5000)
        config = replace(self.config, max_alerts_per_day=0, min_views=5000)
        with closing(sqlite3.connect(self.state)) as conn, conn:
            conn.execute("CREATE TABLE alerts (alert_key TEXT PRIMARY KEY, day TEXT, "
                         "status TEXT, created_at TEXT, payload_json TEXT)")
            conn.executemany("INSERT INTO alerts VALUES (?,?,?,?,?)", [
                (f"old-{index}", self.now.date().isoformat(), "sent",
                 self.now.isoformat(), "{}") for index in range(10)
            ])
        result = run(config, self.now, FakeAnalyst(proposal("101")), lambda *_: None)
        self.assertEqual(result["sent"], 1)
        with closing(sqlite3.connect(self.state)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alerts WHERE day=?",
                                          (self.now.date().isoformat(),)).fetchone()[0], 11)

    def test_positive_alert_daily_cap_still_stops_new_cards(self):
        self.add_post("101", "alice", likes=30, views=5000)
        config = replace(self.config, max_alerts_per_day=1, min_views=5000)
        with closing(sqlite3.connect(self.state)) as conn, conn:
            conn.execute("CREATE TABLE alerts (alert_key TEXT PRIMARY KEY, day TEXT, "
                         "status TEXT, created_at TEXT, payload_json TEXT)")
            conn.execute("INSERT INTO alerts VALUES (?,?,?,?,?)",
                         ("old", self.now.date().isoformat(), "sent", self.now.isoformat(), "{}"))
        result = run(config, self.now, FakeAnalyst(proposal("101")),
                     lambda *_: self.fail("daily cap should prevent delivery"))
        self.assertEqual(result["sent"], 0)

    def test_openai_429_enters_cooldown_without_minute_by_minute_retries(self):
        self.add_post("101", "alice", likes=30, views=5000)

        class RateLimited:
            calls = 0

            def analyze(self, _post):
                self.calls += 1
                raise OpenAIAPIError(429, "rate_limit_exceeded", 600)

        analyst = RateLimited()
        config = replace(self.config, min_views=5000)
        first = run(config, self.now, analyst, lambda *_: None)
        second = run(config, self.now + timedelta(minutes=1), analyst, lambda *_: None)
        self.assertEqual(first["status"], "openai_cooldown")
        self.assertEqual(second["status"], "openai_cooldown")
        self.assertEqual(analyst.calls, 1)

    def test_openai_quota_429_requires_manual_resolution(self):
        self.add_post("101", "alice", likes=30, views=5000)

        class QuotaExhausted:
            calls = 0

            def analyze(self, _post):
                self.calls += 1
                raise OpenAIAPIError(429, "credit_balance_exhausted")

        analyst = QuotaExhausted()
        config = replace(self.config, min_views=5000)
        first = run(config, self.now, analyst, lambda *_: None)
        second = run(config, self.now + timedelta(days=1), analyst, lambda *_: None)
        self.assertEqual(first["status"], "openai_action_required")
        self.assertEqual(second["status"], "openai_action_required")
        self.assertEqual(analyst.calls, 1)

    def test_keyword_post_precedes_watched_account_when_ai_batch_is_full(self):
        self.add_post("keyword", "alice", likes=4, views=5000,
                      text="A playful blue robot is dancing through the new city parade")
        self.add_post("watched", "Solana", likes=300, views=100000,
                      text="A new little fox mascot is dancing in the Solana animation")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE x_watchlist_hits "
                         "(post_id TEXT PRIMARY KEY, handle TEXT, observed_at TEXT)")
            conn.execute("INSERT INTO x_watchlist_hits VALUES (?,?,?)",
                         ("watched", "solana", self.now.isoformat()))
            conn.execute("CREATE TABLE x_first_searches "
                         "(id INTEGER PRIMARY KEY, query_label TEXT, observed_at TEXT)")
            conn.execute("CREATE TABLE x_first_post_observations "
                         "(search_id INTEGER, post_id TEXT, likes INTEGER, views INTEGER)")
            conn.execute("INSERT INTO x_first_searches VALUES (1,?,?)",
                         ("keyword:7", self.now.isoformat()))
            conn.execute("INSERT INTO x_first_post_observations (search_id,post_id) "
                         "VALUES (1,?)", ("keyword",))

        class RecordingAnalyst:
            def __init__(self):
                self.ids = []

            def analyze(self, post):
                self.ids.append(post.post_id)
                return proposal(post.post_id)

        analyst = RecordingAnalyst()
        config = replace(self.config, max_posts_per_run=1, min_views=5000)
        result = run(config, self.now, analyst, lambda *_: None)
        self.assertEqual(result["ai_calls"], 1)
        self.assertEqual(analyst.ids, ["keyword"])

    def test_openai_request_uses_schema_and_parses_reply(self):
        response = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(proposal("101"))}]}]}

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self, *_):
                return json.dumps(response).encode()

        with patch("urllib.request.urlopen", return_value=Reply()) as mocked:
            analyst = OpenAIAnalyst("secret")
            self.assertEqual(analyst.analyze(Post("101", self.now, self.now, "alice",
                                                  "A playful animal moment on X", 25, 3500))["post_id"], "101")
        body = json.loads(mocked.call_args.args[0].data)
        self.assertEqual(body["model"], "gpt-6-luna")
        self.assertNotIn("max_output_tokens", body)
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertIn("theme", body["text"]["format"]["schema"]["required"])
        self.assertFalse(body["store"])

    def test_openai_http_429_exposes_only_safe_code_and_retry_delay(self):
        response = urllib.error.HTTPError(
            "https://api.openai.com/v1/responses", 429, "Too Many Requests",
            {"Retry-After": "600"},
            io.BytesIO(json.dumps({"error": {"code": "credit_balance_exhausted",
                                            "message": "private provider message"}}).encode()),
        )
        with patch("urllib.request.urlopen", side_effect=response):
            with self.assertRaises(OpenAIAPIError) as caught:
                OpenAIAnalyst("secret").analyze(
                    Post("101", self.now, self.now, "alice", "A playful animal moment", 2, 5000))
        self.assertEqual((caught.exception.status, caught.exception.code,
                          caught.exception.retry_after), (429, "credit_balance_exhausted", 600))
        self.assertNotIn("private provider message", str(caught.exception))

    def test_watched_image_post_can_be_reviewed_before_engagement(self):
        self.add_post("777", "Solana", likes=0, views=0, text="🐶")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE x_watchlist_hits (post_id TEXT PRIMARY KEY, handle TEXT, observed_at TEXT)")
            conn.execute("INSERT INTO x_watchlist_hits VALUES (?,?,?)", ("777", "solana", self.now.isoformat()))
            conn.execute("UPDATE x_first_posts SET raw_json=? WHERE post_id='777'", (
                json.dumps({"likeCount": 0, "viewCount": 0, "media": [
                    {"media_url_https": "https://pbs.twimg.com/media/puppy.jpg"}]}),))

        class ImageAnalyst(FakeAnalyst):
            def analyze(self, post):
                self.post = post
                return super().analyze(post)

        analyst = ImageAnalyst(proposal("777"))
        messages = []
        result = run(self.config, self.now, analyst,
                     lambda _token, _chat, message, *_: messages.append(message))
        self.assertEqual(result["sent"], 1)
        self.assertEqual(analyst.post.watchlist_handle, "solana")
        self.assertEqual(analyst.post.image_url, "https://pbs.twimg.com/media/puppy.jpg")
        self.assertIn("отслеживаемый @solana", messages[0])

    def test_watched_token_announcement_reaches_ai_but_not_an_auto_signal(self):
        self.add_post("778", "account", likes=0, views=0,
                      text="The new $TRUMP token launches today")
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE x_watchlist_hits (post_id TEXT PRIMARY KEY, handle TEXT, observed_at TEXT)")
            conn.execute("INSERT INTO x_watchlist_hits VALUES (?,?,?)", ("778", "account", self.now.isoformat()))
        analyst = FakeAnalyst(proposal("778") | {"interesting": False, "confidence": "low"})
        result = run(self.config, self.now, analyst, lambda *_: self.fail("model rejected it"))
        self.assertEqual(analyst.calls, 1)
        self.assertEqual(result["sent"], 0)

    def test_openai_watched_image_request_uses_low_detail_vision(self):
        response = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(proposal("777"))}]}]}

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self, *_):
                return json.dumps(response).encode()

        post = Post("777", self.now, self.now, "Solana", "🐶", 0, 0,
                    "solana", "https://pbs.twimg.com/media/puppy.jpg")
        with patch("urllib.request.urlopen", return_value=Reply()) as mocked:
            OpenAIAnalyst("secret").analyze(post)
        body = json.loads(mocked.call_args.args[0].data)
        self.assertEqual(body["input"][0]["content"][1], {
            "type": "input_image", "image_url": post.image_url, "detail": "low"})


if __name__ == "__main__":
    unittest.main()
