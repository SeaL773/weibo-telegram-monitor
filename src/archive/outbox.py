"""Durable SQLite outbox for Telegram delivery work."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List


@dataclass(frozen=True)
class OutboxEvent:
    event_key: str
    kind: str
    payload: Dict[str, Any]
    attempts: int


class DeliveryOutbox:
    """Persist delivery intent before business checkpoints advance."""

    def __init__(self, path: Path, now: Callable[[], float] = time.time):
        self.path = path
        self._now = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), timeout=30)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS delivery_outbox (
                event_key TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                delivered_at REAL,
                last_error TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS delivery_outbox_pending
            ON delivery_outbox(status, next_attempt_at, created_at)
            """
        )
        self._connection.commit()

    def enqueue(self, event_key: str, kind: str, payload: Dict[str, Any]) -> bool:
        """Insert an idempotent delivery intent. Return True only when newly inserted."""
        if not event_key or not kind:
            raise ValueError("event_key and kind are required")
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO delivery_outbox
                    (event_key, kind, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (event_key, kind, serialized, self._now()),
            )
        return cursor.rowcount == 1

    def pending(self, limit: int = 20) -> List[OutboxEvent]:
        rows = self._connection.execute(
            """
            SELECT event_key, kind, payload_json, attempts
            FROM delivery_outbox
            WHERE status = 'pending' AND next_attempt_at <= ?
            ORDER BY created_at, event_key
            LIMIT ?
            """,
            (self._now(), max(1, int(limit))),
        ).fetchall()
        events = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
                if not isinstance(payload, dict):
                    raise ValueError("payload root must be an object")
            except (TypeError, ValueError, json.JSONDecodeError):
                with self._connection:
                    self._connection.execute(
                        """
                        UPDATE delivery_outbox
                        SET status = 'dead_letter', payload_json = '{}',
                            last_error = 'CorruptPayload'
                        WHERE event_key = ?
                        """,
                        (row["event_key"],),
                    )
                continue
            events.append(
                OutboxEvent(
                    event_key=row["event_key"],
                    kind=row["kind"],
                    payload=payload,
                    attempts=row["attempts"],
                )
            )
        return events

    def mark_delivered(self, event_key: str) -> None:
        """Keep the idempotency key while removing delivered personal payload data."""
        with self._connection:
            self._connection.execute(
                """
                UPDATE delivery_outbox
                SET status = 'delivered', delivered_at = ?, payload_json = '{}',
                    last_error = NULL
                WHERE event_key = ?
                """,
                (self._now(), event_key),
            )

    def mark_failed(self, event_key: str, error_type: str) -> float:
        row = self._connection.execute(
            "SELECT attempts FROM delivery_outbox WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        attempts = (int(row["attempts"]) if row else 0) + 1
        retry_delay = min(30 * (2 ** min(attempts - 1, 7)), 3600)
        next_attempt = self._now() + retry_delay
        with self._connection:
            self._connection.execute(
                """
                UPDATE delivery_outbox
                SET attempts = ?, next_attempt_at = ?, last_error = ?
                WHERE event_key = ? AND status = 'pending'
                """,
                (attempts, next_attempt, error_type[:120], event_key),
            )
        return next_attempt

    def pending_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM delivery_outbox WHERE status = 'pending'"
        ).fetchone()
        return int(row["count"])

    def prune_delivered(self, retention_days: int = 30) -> int:
        cutoff = self._now() - max(1, retention_days) * 86400
        with self._connection:
            cursor = self._connection.execute(
                """
                DELETE FROM delivery_outbox
                WHERE status = 'delivered' AND delivered_at < ?
                """,
                (cutoff,),
            )
        return cursor.rowcount

    def close(self) -> None:
        self._connection.close()
