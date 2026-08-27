"""Telegram Bot API interactions."""

import io
import time
from src.utils.LoggerManager import logger as log
from pathlib import Path
from typing import Optional

import httpx

from src.core.config import TG_BOT_TOKEN, TG_CHAT_ID, TG_CAPTION_MAX


# ---------------------------------------------------------------------------
# TG alert with cooldown
# ---------------------------------------------------------------------------

_alert_cooldowns = {}
ALERT_COOLDOWN = 3600  # 1 hour between same type alerts


def _alert_tg(alert_type: str, text: str):
    """Send alert to TG with cooldown per type."""
    now = time.time()
    if now - _alert_cooldowns.get(alert_type, 0) < ALERT_COOLDOWN:
        return
    _alert_cooldowns[alert_type] = now
    tg_send(text)


TG_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
_tg_client: Optional[httpx.Client] = None


def _get_tg_client() -> httpx.Client:
    """Reuse a long-lived httpx client for TG API calls."""
    global _tg_client
    if _tg_client is None or _tg_client.is_closed:
        _tg_client = httpx.Client(timeout=60)
    return _tg_client


def get_tg_client_ref():
    """Return the raw _tg_client for cleanup purposes."""
    return _tg_client


def _tg_post(method: str, payload=None, files=None, data=None, retries=3):
    """POST to Telegram Bot API with 429 retry."""
    client = _get_tg_client()
    for attempt in range(retries):
        try:
            if files:
                r = client.post(f"{TG_API}/{method}", data=data or {}, files=files)
            else:
                r = client.post(f"{TG_API}/{method}", json=payload)
            resp = r.json()
            if resp.get("ok"):
                return resp.get("result")
            if resp.get("error_code") == 429:
                wait = resp.get("parameters", {}).get("retry_after", 5)
                log.warning(f"TG 429, retrying in {wait}s...")
                time.sleep(wait + 1)
                continue
            log.error(f"TG {method} failed: {resp}")
            return None
        except Exception as e:
            # httpx exceptions can contain the request URL, which embeds the bot token.
            log.error(f"TG {method} error: {type(e).__name__}")
            if attempt < retries - 1:
                time.sleep(3)
    return None


def tg_send(text: str, parse_mode="HTML"):
    time.sleep(1)
    return _tg_post("sendMessage", {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    })


def tg_send_reply(text: str, reply_to_message_id: int, parse_mode="HTML"):
    """Send a TG message as a reply to an existing message."""
    time.sleep(1)
    return _tg_post("sendMessage", {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
        "reply_to_message_id": reply_to_message_id,
    })


def tg_send_photo_bytes(photo_bytes: io.BytesIO, caption: str = ""):
    """Send a photo from bytes via Bot API."""
    time.sleep(1)
    photo_bytes.seek(0)
    return _tg_post(
        "sendPhoto",
        files={"photo": ("screenshot.png", photo_bytes, "image/png")},
        data={
            "chat_id": TG_CHAT_ID,
            "caption": caption[:TG_CAPTION_MAX],
            "parse_mode": "HTML",
        },
    )


def tg_send_photo_url(photo_url: str, caption: str = "", reply_to: int = None):
    """Send a photo by URL, optionally as a reply."""
    time.sleep(1)
    payload = {
        "chat_id": TG_CHAT_ID,
        "photo": photo_url,
        "parse_mode": "HTML",
    }
    if caption:
        payload["caption"] = caption[:TG_CAPTION_MAX]
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    return _tg_post("sendPhoto", payload)


def tg_send_media_group_url(photo_urls: list, caption: str = "", reply_to: int = None):
    """Send multiple photos by URL as an album, optionally as a reply."""
    import json as _json
    if not photo_urls:
        return None
    time.sleep(1)
    batch = photo_urls[:10]
    media = []
    for i, url in enumerate(batch):
        m = {"type": "photo", "media": url}
        if i == 0 and caption:
            m["caption"] = caption[:TG_CAPTION_MAX]
            m["parse_mode"] = "HTML"
        media.append(m)
    payload = {
        "chat_id": TG_CHAT_ID,
        "media": _json.dumps(media),
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    return _tg_post("sendMediaGroup", data=payload)


def tg_send_animation_url(animation_url: str, caption: str = ""):
    time.sleep(1)
    payload = {
        "chat_id": TG_CHAT_ID,
        "animation": animation_url,
        "parse_mode": "HTML",
    }
    if caption:
        payload["caption"] = caption[:TG_CAPTION_MAX]
    return _tg_post("sendAnimation", payload)


def tg_send_video_file(video_path: str, caption: str = ""):
    """Send video via Bot API (< 50MB)."""
    time.sleep(1)
    with open(video_path, "rb") as f:
        return _tg_post(
            "sendVideo",
            files={"video": (Path(video_path).name, f, "video/mp4")},
            data={
                "chat_id": TG_CHAT_ID,
                "caption": caption[:TG_CAPTION_MAX],
                "parse_mode": "HTML",
            },
        )
