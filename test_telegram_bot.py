"""Offline tests for Telegram request encoding and retry behavior."""

import io
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("TG_BOT_TOKEN", "test-token")
os.environ.setdefault("TG_CHAT_ID", "123456")
if "pytz" not in sys.modules:
    try:
        __import__("pytz")
    except ImportError:
        sys.modules["pytz"] = types.SimpleNamespace(timezone=lambda _name: timezone.utc)

from src.telegram import bot  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self.payload = payload
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


class FakeClient:
    def __init__(self, responses, consume_files=False):
        self.responses = iter(responses)
        self.consume_files = consume_files
        self.calls = []
        self.file_reads = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.consume_files:
            reads = {}
            for field, value in kwargs.get("files", {}).items():
                file_obj = value[1] if isinstance(value, tuple) else value
                reads[field] = file_obj.read()
            self.file_reads.append(reads)
        return next(self.responses)


class TelegramBotTests(unittest.TestCase):
    def setUp(self):
        bot._alert_cooldowns.clear()

    def test_send_media_group_uses_form_data(self):
        client = FakeClient([FakeResponse(payload={"ok": True, "result": [{"message_id": 1}]})])

        with patch.object(bot, "_get_tg_client", return_value=client), patch.object(
            bot.time, "sleep"
        ):
            result = bot.tg_send_media_group_url(
                ["https://example.test/1.jpg", "https://example.test/2.jpg"],
                caption="caption",
                reply_to=42,
            )

        self.assertEqual(result, [{"message_id": 1}])
        _, kwargs = client.calls[0]
        self.assertNotIn("json", kwargs)
        self.assertNotIn("files", kwargs)
        self.assertEqual(kwargs["data"]["chat_id"], bot.TG_CHAT_ID)
        self.assertEqual(kwargs["data"]["reply_to_message_id"], 42)
        self.assertEqual(json.loads(kwargs["data"]["media"])[0]["caption"], "caption")

    def test_json_payload_is_sent_as_json(self):
        client = FakeClient([FakeResponse(payload={"ok": True, "result": {"message_id": 7}})])

        with patch.object(bot, "_get_tg_client", return_value=client):
            result = bot._tg_post("sendMessage", json={"text": "hello"})

        self.assertEqual(result, {"message_id": 7})
        self.assertEqual(client.calls[0][1], {"json": {"text": "hello"}})

    def test_429_retries_and_rewinds_bytesio(self):
        client = FakeClient(
            [
                FakeResponse(
                    status_code=429,
                    payload={
                        "ok": False,
                        "error_code": 429,
                        "parameters": {"retry_after": 2},
                    },
                ),
                FakeResponse(payload={"ok": True, "result": {"message_id": 8}}),
            ],
            consume_files=True,
        )
        photo = io.BytesIO(b"complete-image")

        with patch.object(bot, "_get_tg_client", return_value=client), patch.object(
            bot.time, "sleep"
        ) as sleep:
            result = bot._tg_post(
                "sendPhoto",
                data={"chat_id": "123456"},
                files={"photo": ("photo.png", photo, "image/png")},
                retries=2,
            )

        self.assertEqual(result, {"message_id": 8})
        self.assertEqual(client.file_reads, [
            {"photo": b"complete-image"},
            {"photo": b"complete-image"},
        ])
        sleep.assert_called_once_with(3.0)

    def test_5xx_retries_and_rewinds_real_file(self):
        client = FakeClient(
            [
                FakeResponse(status_code=503, payload={"ok": False, "error_code": 503}),
                FakeResponse(payload={"ok": True, "result": {"message_id": 9}}),
            ],
            consume_files=True,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "video.mp4"
            path.write_bytes(b"complete-video")
            with path.open("rb") as video, patch.object(
                bot, "_get_tg_client", return_value=client
            ), patch.object(bot.time, "sleep") as sleep:
                result = bot._tg_post(
                    "sendVideo",
                    data={"chat_id": "123456"},
                    files={"video": ("video.mp4", video, "video/mp4")},
                    retries=2,
                )

        self.assertEqual(result, {"message_id": 9})
        self.assertEqual(client.file_reads, [
            {"video": b"complete-video"},
            {"video": b"complete-video"},
        ])
        sleep.assert_called_once_with(3)

    def test_non_json_response_is_retried(self):
        client = FakeClient([
            FakeResponse(status_code=502, json_error=ValueError("not json")),
            FakeResponse(payload={"ok": True, "result": {"message_id": 10}}),
        ])

        with patch.object(bot, "_get_tg_client", return_value=client), patch.object(
            bot.time, "sleep"
        ) as sleep:
            result = bot._tg_post("sendMessage", json={"text": "hello"}, retries=2)

        self.assertEqual(result, {"message_id": 10})
        self.assertEqual(len(client.calls), 2)
        sleep.assert_called_once_with(3)

    def test_non_retryable_error_returns_immediately(self):
        client = FakeClient([
            FakeResponse(status_code=400, payload={"ok": False, "error_code": 400}),
        ])

        with patch.object(bot, "_get_tg_client", return_value=client), patch.object(
            bot.time, "sleep"
        ) as sleep:
            result = bot._tg_post("sendMessage", json={"text": "hello"})

        self.assertIsNone(result)
        self.assertEqual(len(client.calls), 1)
        sleep.assert_not_called()

    def test_alert_cooldown_is_recorded_only_after_success(self):
        with patch.object(bot.time, "time", return_value=10_000), patch.object(
            bot, "tg_send", side_effect=[None, {"message_id": 11}]
        ) as send:
            self.assertIsNone(bot._alert_tg("cookie", "first"))
            self.assertNotIn("cookie", bot._alert_cooldowns)

            self.assertEqual(bot._alert_tg("cookie", "second"), {"message_id": 11})
            self.assertEqual(bot._alert_cooldowns["cookie"], 10_000)

            self.assertIsNone(bot._alert_tg("cookie", "third"))

        self.assertEqual(send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
