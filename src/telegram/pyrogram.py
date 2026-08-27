"""Pyrogram MTProto for large video uploads."""

import asyncio
import json
import subprocess
from src.utils.LoggerManager import logger as log
from pathlib import Path
from typing import Optional

from pyrogram import Client as PyroClient
from pyrogram import enums as PyroEnums
import pyrogram.utils

from src.core.config import TG_API_ID, TG_API_HASH, TG_BOT_TOKEN, TG_CHAT_ID, DATA_DIR


# ---------------------------------------------------------------------------
# Pyrogram monkey-patch for peer_type
# ---------------------------------------------------------------------------

_orig_get_peer_type = pyrogram.utils.get_peer_type


def _patched_get_peer_type(peer_id: int) -> str:
    try:
        return _orig_get_peer_type(peer_id)
    except ValueError:
        if peer_id < 0:
            return "chat"
        raise


pyrogram.utils.get_peer_type = _patched_get_peer_type

# ---------------------------------------------------------------------------
# Pyrogram client
# ---------------------------------------------------------------------------

_pyro_client = None


def get_pyro_client():
    global _pyro_client
    if not TG_API_ID or not TG_API_HASH:
        return None
    if _pyro_client is None:
        _pyro_client = PyroClient(
            "weibo_monitor_bot",
            api_id=TG_API_ID,
            api_hash=TG_API_HASH,
            bot_token=TG_BOT_TOKEN,
            workdir=str(DATA_DIR),
            no_updates=True,
        )
    return _pyro_client


def _get_video_dimensions(video_path: str) -> tuple:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_streams", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration",
                "-show_entries", "format=duration",
                video_path,
            ],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        stream = data.get("streams", [{}])[0]
        w = int(stream.get("width", 0))
        h = int(stream.get("height", 0))
        dur = float(stream.get("duration", 0) or data.get("format", {}).get("duration", 0))
        return w, h, int(dur)
    except Exception as e:
        log.debug(f"ffprobe video dimensions: {e}")
        return 0, 0, 0


def _generate_thumbnail(video_path: str) -> Optional[str]:
    thumb_path = video_path.rsplit(".", 1)[0] + "_thumb.jpg"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", video_path,
                "-ss", "00:00:03", "-frames:v", "1", "-q:v", "2",
                thumb_path,
            ],
            capture_output=True, timeout=30,
        )
        if Path(thumb_path).exists() and Path(thumb_path).stat().st_size > 0:
            return thumb_path
    except Exception as e:
        log.debug(f"ffmpeg thumbnail generation: {e}")
    return None


def pyro_send_video(video_path: str, caption: str = "") -> bool:
    """Send video via Pyrogram MTProto (up to 2GB)."""
    pyro = get_pyro_client()
    if not pyro:
        return False
    try:
        loop = asyncio.new_event_loop()

        async def _send():
            async with pyro:
                chat_id = int(TG_CHAT_ID)
                try:
                    await pyro.storage.update_peers([(abs(chat_id), 0, "group", "", "")])
                except Exception as e:
                    log.debug(f"Pyrogram update_peers: {e}")
                w, h, dur = _get_video_dimensions(video_path)
                thumb = _generate_thumbnail(video_path)
                kwargs = {
                    "chat_id": chat_id,
                    "video": video_path,
                    "caption": caption,
                    "parse_mode": PyroEnums.ParseMode.HTML,
                    "supports_streaming": True,
                }
                if w and h:
                    kwargs["width"] = w
                    kwargs["height"] = h
                if dur:
                    kwargs["duration"] = dur
                if thumb:
                    kwargs["thumb"] = thumb
                await pyro.send_video(**kwargs)

        loop.run_until_complete(_send())
        loop.close()
        log.info(f"[pyro] video sent: {Path(video_path).name}")
        return True
    except Exception as e:
        log.error(f"[pyro] send_video failed: {e}")
        return False
