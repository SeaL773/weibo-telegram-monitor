"""Offline tests for the durable delivery outbox."""

import tempfile
import unittest
from pathlib import Path

from src.archive.outbox import DeliveryOutbox


class OutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "outbox.db"
        self.now = 1_000.0
        self.outbox = DeliveryOutbox(self.path, now=lambda: self.now)

    def tearDown(self) -> None:
        self.outbox.close()
        self.temp_dir.cleanup()

    def test_enqueue_is_idempotent_and_survives_restart(self) -> None:
        self.assertTrue(self.outbox.enqueue("post:1:2", "post", {"mid": "2"}))
        self.assertFalse(self.outbox.enqueue("post:1:2", "post", {"mid": "changed"}))
        self.outbox.close()

        self.outbox = DeliveryOutbox(self.path, now=lambda: self.now)
        event = self.outbox.pending()[0]
        self.assertEqual(event.event_key, "post:1:2")
        self.assertEqual(event.payload, {"mid": "2"})

    def test_failed_delivery_uses_bounded_backoff(self) -> None:
        self.outbox.enqueue("message:1", "message", {"text": "test"})

        self.assertEqual(self.outbox.mark_failed("message:1", "TimeoutError"), 1_030.0)
        self.assertEqual(self.outbox.pending(), [])
        self.now = 1_030.0
        self.assertEqual(self.outbox.pending()[0].attempts, 1)

    def test_delivered_event_retains_key_but_erases_payload(self) -> None:
        self.outbox.enqueue("message:1", "message", {"text": "private"})
        self.outbox.mark_delivered("message:1")

        self.assertEqual(self.outbox.pending(), [])
        self.assertFalse(self.outbox.enqueue("message:1", "message", {"text": "again"}))

    def test_corrupt_payload_is_dead_lettered(self) -> None:
        self.outbox.enqueue("message:bad", "message", {"text": "test"})
        with self.outbox._connection:
            self.outbox._connection.execute(
                "UPDATE delivery_outbox SET payload_json = 'not-json' WHERE event_key = ?",
                ("message:bad",),
            )

        self.assertEqual(self.outbox.pending(), [])
        self.assertEqual(self.outbox.pending_count(), 0)


if __name__ == "__main__":
    unittest.main()
