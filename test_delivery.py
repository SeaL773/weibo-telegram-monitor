"""Offline tests for outbox delivery dispatch."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("TG_BOT_TOKEN", "test-token")
os.environ.setdefault("TG_CHAT_ID", "123456")

from src.archive.outbox import DeliveryOutbox
from src.core.delivery import process_outbox
from src.core.push import DeliveryResult


class DeliveryWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.now = 1_000.0
        self.outbox = DeliveryOutbox(
            Path(self.temp_dir.name) / "outbox.db",
            now=lambda: self.now,
        )

    def tearDown(self) -> None:
        self.outbox.close()
        self.temp_dir.cleanup()

    def test_failed_message_remains_pending_for_retry(self) -> None:
        self.outbox.enqueue(
            "profile:1:change",
            "telegram",
            {"method": "message", "text": "changed"},
        )

        with patch("src.core.delivery.tg_send", return_value=None):
            stats = process_outbox(self.outbox, {}, {}, limit=1)

        self.assertEqual(stats.failed, 1)
        self.assertEqual(self.outbox.pending_count(), 1)
        self.assertEqual(self.outbox.pending(), [])
        self.now = 1_030.0
        self.assertEqual(len(self.outbox.pending()), 1)

    def test_successful_message_is_acknowledged(self) -> None:
        self.outbox.enqueue(
            "profile:1:change",
            "telegram",
            {"method": "message", "text": "changed"},
        )

        with patch("src.core.delivery.tg_send", return_value={"message_id": 7}):
            stats = process_outbox(self.outbox, {}, {}, limit=1)

        self.assertEqual(stats.delivered, 1)
        self.assertEqual(self.outbox.pending_count(), 0)

    def test_post_state_failure_does_not_acknowledge_event(self) -> None:
        self.outbox.enqueue(
            "post:1:2",
            "post",
            {"uid": "1", "post": {"id": "2"}},
        )

        with (
            patch(
                "src.core.delivery.push_post",
                return_value=DeliveryResult(primary_delivered=True, message_id=7),
            ),
            patch("src.core.delivery.save_state", side_effect=OSError("disk full")),
        ):
            stats = process_outbox(self.outbox, {}, {}, limit=1)

        self.assertEqual(stats.failed, 1)
        self.assertEqual(self.outbox.pending_count(), 1)


if __name__ == "__main__":
    unittest.main()
