import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from narrative_collector import chatgpt_plan
from narrative_collector.ai_analyzer import ChatGPTPlanAnalyst, Config, Post


class PlanCredentialsTests(unittest.TestCase):
    def test_host_id_is_stable_and_separate_from_tokens(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            credentials = Path(directory) / "credentials.json"
            first = chatgpt_plan.ensure_host_id(credentials)
            self.assertEqual(first, chatgpt_plan.ensure_host_id(credentials))
            self.assertFalse(credentials.exists())
            self.assertEqual(first, chatgpt_plan._host_id(credentials))

    def test_refresh_replaces_both_rotating_tokens(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            credentials = Path(directory) / "credentials.json"
            chatgpt_plan._save(credentials, {
                "version": 1, "active_client_id": "oaiapp_test",
                "accounts": {"oaiapp_test": {
                    "client_id": "oaiapp_test", "subject": "person",
                    "scopes": ["chatgpt.tokens.use.direct", "offline_access"],
                    "access_token": "old-access", "refresh_token": "old-refresh",
                    "saved_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
                    "expires_in": 3600,
                }},
            })
            with patch.object(chatgpt_plan, "_form_post", return_value={
                "access_token": "new-access", "refresh_token": "new-refresh",
                "expires_in": 3600,
            }) as exchange:
                self.assertEqual(chatgpt_plan.access_token(credentials), "new-access")
            self.assertEqual(exchange.call_args.args[1]["client_id"], "oaiapp_test")
            record = chatgpt_plan._read(credentials)["accounts"]["oaiapp_test"]
            self.assertEqual(record["refresh_token"], "new-refresh")
            self.assertEqual(record["access_token"], "new-access")

    def test_missing_plan_grant_is_rejected(self):
        with self.assertRaises(ValueError):
            chatgpt_plan._require_grant({
                "scope": "openid offline_access", "access_token": "a",
                "refresh_token": "b", "id_token": "c",
            })

    def test_plan_config_does_not_need_api_key(self):
        config = Config(Path("collector.db"), Path("state.db"), "", "tg", "42",
                        auth_mode="chatgpt_plan", chatgpt_credentials=Path("private.json"))
        config.validate()


class FakeStream(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class PlanInferenceTests(unittest.TestCase):
    def test_stream_requires_completion_and_uses_oauth_contract(self):
        now = datetime.now(timezone.utc)
        post = Post("123", now, now, "author", "A funny concept", 1, 9000)
        proposal = {"post_id": "123", "interesting": False, "theme": "other",
                    "narrative": "", "coin_name": "", "ticker": "", "description": "",
                    "reason": "No clear hook", "confidence": "low"}
        frame = json.dumps({"type": "response.output_text.delta",
                            "delta": json.dumps(proposal)})
        done = json.dumps({"type": "response.completed", "response": {"usage": {"input_tokens": 7}}})
        stream = FakeStream((f"data: {frame}\n\ndata: {done}\n\ndata: [DONE]\n").encode())
        with patch.object(chatgpt_plan, "access_token", return_value="oauth-access"), patch(
            "narrative_collector.ai_analyzer.urllib.request.urlopen", return_value=stream
        ) as opened:
            analyst = ChatGPTPlanAnalyst(Path("private.json"), "gpt-6.1-sol")
            self.assertEqual(analyst.analyze(post), proposal)
        request = opened.call_args.args[0]
        body = json.loads(request.data)
        self.assertTrue(body["stream"])
        self.assertFalse(body["store"])
        self.assertIsInstance(body["input"], list)
        self.assertEqual(analyst.last_usage, {"input_tokens": 7})

    def test_truncated_stream_is_not_accepted(self):
        now = datetime.now(timezone.utc)
        post = Post("123", now, now, "author", "A funny concept", 1, 9000)
        with patch.object(chatgpt_plan, "access_token", return_value="oauth-access"), patch(
            "narrative_collector.ai_analyzer.urllib.request.urlopen",
            return_value=FakeStream(b'data: {"type":"response.output_text.delta","delta":"{}"}\n'),
        ):
            with self.assertRaisesRegex(RuntimeError, "without response.completed"):
                ChatGPTPlanAnalyst(Path("private.json"), "gpt-6.1-sol").analyze(post)


if __name__ == "__main__":
    unittest.main()
