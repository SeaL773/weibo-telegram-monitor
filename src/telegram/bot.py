"""Telegram Bot API interactions."""

import io
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

from src.core.config import TG_BOT_TOKEN, TG_CHAT_ID, TG_CAPTION_MAX
from src.utils.LoggerManager import logger as log


# ---------------------------------------------------------------------------
# TG alert with cooldown
# ---------------------------------------------------------------------------

_alert_cooldowns = {}
ALERT_COOLDOWN = 3600  # 1 hour between same type alerts


def _alert_tg(alert_type: str, text: str):
    """Send alert to TG with cooldown per type."""
    now = time.time()
    if now - _alert_cooldowns.get(alert_type, 0) < ALERT_COOLDOWN:
        return None
    result = tg_send(text)
    if result:
        _alert_cooldowns[alert_type] = now
    return result


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


def _file_values(files: Any) -> Iterable[Any]:
    """Yield file values from httpx's mapping or sequence form."""
    if files is None:
        return ()
    if hasattr(files, "values"):
        return files.values()
    return (item[1] for item in files)


def _capture_file_positions(files: Any) -> list[tuple[Any, int]]:
    """Remember initial positions so multipart bodies can be retried safely."""
    positions = []
    seen = set()
    for value in _file_values(files):
        file_obj = value[1] if isinstance(value, tuple) and len(value) >= 2 else value
        if id(file_obj) in seen or not hasattr(file_obj, "seek") or not hasattr(file_obj, "tell"):
            continue
        try:
            positions.append((file_obj, file_obj.tell()))
            seen.add(id(file_obj))
        except (OSError, ValueError):
            continue
    return positions


def _rewind_files(positions: list[tuple[Any, int]]) -> None:
    for file_obj, position in positions:
        file_obj.seek(position)


def _retry_delay(response_data: dict[str, Any]) -> float:
    parameters = response_data.get("parameters")
    value = parameters.get("retry_after", 5) if isinstance(parameters, dict) else 5
    try:
        return max(float(value), 0) + 1
    except (TypeError, ValueError):
        return 6


def _tg_post(
    method: str,
    *,
    json: Any = None,
    data: Any = None,
    files: Any = None,
    retries: int = 3,
):
    """POST to Telegram, retrying transient and malformed responses.

    ``json`` is used for normal Bot API methods. ``data`` is form-encoded when
    supplied alone and is combined with ``files`` for multipart requests.
    """
    if retries < 1:
        raise ValueError("retries must be at least 1")
    if json is not None and (data is not None or files is not None):
        raise ValueError("json cannot be combined with data or files")

    client = _get_tg_client()
    file_positions = _capture_file_positions(files)

    for attempt in range(retries):
        is_last_attempt = attempt == retries - 1
        try:
            _rewind_files(file_positions)
            request_kwargs = {}
            if json is not None:
                request_kwargs["json"] = json
            if data is not None:
                request_kwargs["data"] = data
            if files is not None:
                request_kwargs["files"] = files
            r = client.post(f"{TG_API}/{method}", **request_kwargs)
        except Exception as e:
            # httpx exceptions can contain the request URL, which embeds the bot token.
            log.error(f"TG {method} transport error: {type(e).__name__}")
            if not is_last_attempt:
                time.sleep(3)
            continue

        try:
            resp = r.json()
        except (ValueError, TypeError):
            log.error(f"TG {method} returned non-JSON response (HTTP {r.status_code})")
            if not is_last_attempt:
                time.sleep(3)
            continue

        if not isinstance(resp, dict):
            log.error(f"TG {method} returned an invalid JSON payload (HTTP {r.status_code})")
            if not is_last_attempt:
                time.sleep(3)
            continue

        error_code = resp.get("error_code")
        if r.status_code == 429 or error_code == 429:
            delay = _retry_delay(resp)
            log.warning(f"TG 429, retrying in {delay:g}s...")
            if not is_last_attempt:
                time.sleep(delay)
            continue

        if r.status_code >= 500 or (
            isinstance(error_code, int) and error_code >= 500
        ):
            log.warning(f"TG {method} server error (HTTP {r.status_code})")
            if not is_last_attempt:
                time.sleep(3)
            continue

        if resp.get("ok"):
            return resp.get("result")

        log.error(
            f"TG {method} failed (HTTP {r.status_code}, error_code={error_code})"
        )
        return None

    return None


def tg_send(text: str, parse_mode="HTML"):
    time.sleep(1)
    return _tg_post("sendMessage", json={
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    })


def tg_send_reply(text: str, reply_to_message_id: int, parse_mode="HTML"):
    """Send a TG message as a reply to an existing message."""
    time.sleep(1)
    return _tg_post("sendMessage", json={
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
    return _tg_post("sendPhoto", json=payload)


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
    return _tg_post("sendAnimation", json=payload)


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
