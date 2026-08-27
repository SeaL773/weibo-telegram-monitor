"""Delivery worker for durable outbox events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional

from src.archive.outbox import DeliveryOutbox, OutboxEvent
from src.archive.state import save_state
from src.core.push import push_post
from src.telegram.bot import (
    tg_send,
    tg_send_media_group_url,
    tg_send_photo_url,
    tg_send_reply,
)
from src.utils.LoggerManager import logger as log


@dataclass(frozen=True)
class DeliveryStats:
    delivered: int = 0
    failed: int = 0


def _deliver_telegram_event(event: OutboxEvent):
    payload = event.payload
    method = payload.get("method", "message")
    text = str(payload.get("text", ""))
    reply_to = payload.get("reply_to")
    if method == "message":
        return tg_send(text)
    if method == "reply":
        return tg_send_reply(text, int(reply_to))
    if method == "photo":
        urls = payload.get("photo_urls", [])
        if not isinstance(urls, list) or not urls:
            return None
        return tg_send_photo_url(urls[0], caption=text, reply_to=reply_to)
    if method == "media_group":
        urls = payload.get("photo_urls", [])
        if not isinstance(urls, list) or not urls:
            return None
        return tg_send_media_group_url(urls, caption=text, reply_to=reply_to)
    raise ValueError(f"Unsupported Telegram outbox method: {method}")


def process_outbox(
    outbox: DeliveryOutbox,
    state: Dict,
    cookie_dict: Dict[str, str],
    limit: int = 20,
    progress: Optional[Callable[[], None]] = None,
) -> DeliveryStats:
    """Attempt pending events once, preserving failures for bounded backoff."""
    delivered = 0
    failed = 0
    for event in outbox.pending(limit=limit):
        if progress:
            progress()
        success = False
        try:
            if event.kind == "post":
                result = push_post(
                    event.payload["post"],
                    str(event.payload["uid"]),
                    cookie_dict,
                    state=state,
                )
                if result.primary_delivered:
                    save_state(state)
                    success = True
                    if result.optional_failures:
                        log.warning(
                            f"Post {event.event_key} delivered with optional failures: "
                            f"{','.join(result.optional_failures)}"
                        )
            elif event.kind == "telegram":
                success = bool(_deliver_telegram_event(event))
            else:
                raise ValueError(f"Unsupported outbox kind: {event.kind}")
        except Exception as exc:
            log.error(
                f"Outbox delivery failed for {event.event_key}: {type(exc).__name__}"
            )

        if success:
            outbox.mark_delivered(event.event_key)
            delivered += 1
        else:
            next_attempt = outbox.mark_failed(event.event_key, "DeliveryFailed")
            log.warning(
                f"Outbox event {event.event_key} retained for retry at {int(next_attempt)}"
            )
            failed += 1

        if progress:
            progress()

    outbox.prune_delivered(retention_days=30)
    return DeliveryStats(delivered=delivered, failed=failed)
