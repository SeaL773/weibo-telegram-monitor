"""Post archiving and video download/send."""

import io
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlsplit

import httpx

from src.core.config import (
    ARCHIVE_DIR,
    VIDEOS_DIR,
    MOBILE_UA,
    DESKTOP_UA,
    ET,
    MAX_IMAGE_BYTES,
    MAX_VIDEO_BYTES,
)
from src.api.posts import extract_post_id, get_post_username, is_retweet
from src.telegram.bot import tg_send_video_file
from src.telegram.pyrogram import pyro_send_video
from src.utils.LoggerManager import logger as log

# ---------------------------------------------------------------------------
# Reusable media client
# ---------------------------------------------------------------------------
_media_client: Optional[httpx.Client] = None
_ALLOWED_MEDIA_DOMAINS = ("sinaimg.cn", "weibocdn.com", "weibo.com", "sina.com.cn")
_IMAGE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


def is_allowed_media_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return parts.scheme == "https" and any(
            host == domain or host.endswith(f".{domain}")
            for domain in _ALLOWED_MEDIA_DOMAINS
        )
    except Exception:
        return False


def _validate_media_request(request: httpx.Request) -> None:
    if not is_allowed_media_url(str(request.url)):
        raise ValueError("Blocked media URL outside the approved Weibo CDN domains")


def _safe_component(value: object) -> str:
    component = re.sub(r"[^A-Za-z0-9_-]", "_", str(value))[:128]
    if not component:
        raise ValueError("Empty path component")
    return component


def get_media_client() -> httpx.Client:
    """Return a reusable httpx client for media downloads."""
    global _media_client
    if _media_client is None or _media_client.is_closed:
        _media_client = httpx.Client(
            timeout=120,
            follow_redirects=True,
            headers={
                "User-Agent": DESKTOP_UA,
                "Referer": "https://weibo.com/",
            },
            event_hooks={"request": [_validate_media_request]},
        )
    return _media_client


def archive_post(post: Dict, uid: str, screenshot_bytes: Optional[io.BytesIO] = None,
                 cookie_dict: Optional[Dict] = None):
    """Archive post data, screenshot, and media locally."""
    mid = extract_post_id(post)
    username = get_post_username(post)
    safe_mid = _safe_component(mid)
    uid_dir = ARCHIVE_DIR / _safe_component(uid)
    ss_dir = uid_dir / "screenshots"
    media_dir = uid_dir / "media"
    for d in [uid_dir, ss_dir, media_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # Determine category
    if is_retweet(post):
        cat = "retweets"
    else:
        cat = "posts"

    # Build archive entry
    entry = {
        "id": mid,
        "text_raw": post.get("text_raw", ""),
        "text": post.get("text", ""),
        "created_at": post.get("created_at", ""),
        "type": cat.rstrip("s"),
        "is_retweet": is_retweet(post),
        "username": username,
        "archived_at": datetime.now(ET).isoformat(),
        "screenshot": None,
        "local_media": [],
    }

    # Save retweeted post info
    if is_retweet(post):
        rt = post.get("retweeted_status", {})
        entry["rt_source"] = {
            "id": str(rt.get("id", "")),
            "username": rt.get("user", {}).get("screen_name", ""),
            "text_raw": rt.get("text_raw", ""),
        }

    # Save screenshot
    if screenshot_bytes:
        ss_path = ss_dir / f"{safe_mid}.png"
        try:
            screenshot_bytes.seek(0)
            ss_path.write_bytes(screenshot_bytes.read())
            screenshot_bytes.seek(0)
            entry["screenshot"] = f"screenshots/{mid}.png"
        except Exception as e:
            log.warning(f"Failed to save screenshot for {mid}: {e}")

    # Download images
    pics = post.get("pic_ids", [])
    pic_infos = post.get("pic_infos", {})
    for i, pic_id in enumerate(pics):
        info = pic_infos.get(pic_id, {})
        # Try largest version
        url = ""
        for size in ["original", "large", "mw2000", "thumbnail"]:
            img = info.get(size, {})
            url = img.get("url", "")
            if url:
                break
        if not url:
            continue
        if not is_allowed_media_url(url):
            log.warning("Skipped image outside approved Weibo CDN domains")
            continue
        fpath = None
        try:
            headers = {"User-Agent": DESKTOP_UA, "Referer": "https://weibo.com/"}
            with get_media_client().stream("GET", url, headers=headers, timeout=30) as r:
                r.raise_for_status()
                content_type = r.headers.get("content-type", "").split(";", 1)[0].lower()
                ext = _IMAGE_EXTENSIONS.get(content_type)
                if not ext:
                    raise ValueError(f"Unsupported image content type: {content_type or 'unknown'}")
                content_length = int(r.headers.get("content-length", "0") or 0)
                if content_length > MAX_IMAGE_BYTES:
                    raise ValueError("Image exceeds MAX_IMAGE_BYTES")
                fname = f"{safe_mid}_{i}.{ext}"
                fpath = media_dir / fname
                total = 0
                with fpath.open("wb") as output:
                    for chunk in r.iter_bytes(chunk_size=65536):
                        total += len(chunk)
                        if total > MAX_IMAGE_BYTES:
                            raise ValueError("Image exceeds MAX_IMAGE_BYTES")
                        output.write(chunk)
            entry["local_media"].append(f"media/{fname}")
            log.info(f"Archived image: {fname} ({total} bytes)")
        except Exception as e:
            if fpath:
                fpath.unlink(missing_ok=True)
            log.warning(f"Failed to download image: {type(e).__name__}")

    # Save to JSON archive
    json_path = uid_dir / f"{cat}.json"
    existing = []
    if json_path.exists():
        try:
            existing = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.debug(f"Failed to parse archive JSON {json_path}: {e}")
            existing = []
    if not any(e.get("id") == mid for e in existing):
        existing.append(entry)
        json_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(f"Archived post {mid} to {cat}.json")


def download_video(url: str, mid: str, cookie_dict: Dict) -> Optional[str]:
    """Download video to data/videos/ using streaming to avoid OOM. Returns local file path or None."""
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    video_path = str(VIDEOS_DIR / f"{_safe_component(mid)}.mp4")

    if not is_allowed_media_url(url):
        log.warning("Skipped video outside approved Weibo CDN domains")
        return None

    try:
        client = get_media_client()
        with client.stream("GET", url, headers={
            "User-Agent": MOBILE_UA,
            "Referer": "https://m.weibo.cn/",
        }) as r:
            r.raise_for_status()
            content_type = r.headers.get("content-type", "").split(";", 1)[0].lower()
            if not (content_type.startswith("video/") or content_type == "application/octet-stream"):
                raise ValueError(f"Unsupported video content type: {content_type or 'unknown'}")
            content_length = int(r.headers.get("content-length", "0") or 0)
            if content_length > MAX_VIDEO_BYTES:
                raise ValueError("Video exceeds MAX_VIDEO_BYTES")
            total = 0
            with open(video_path, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=65536):
                    f.write(chunk)
                    total += len(chunk)
                    if total > MAX_VIDEO_BYTES:
                        raise ValueError("Video exceeds MAX_VIDEO_BYTES")
        size_mb = total / (1024 * 1024)
        log.info(f"Video downloaded: {mid}.mp4 ({size_mb:.1f} MB)")
        return video_path
    except Exception as e:
        log.error(f"Video download failed for {mid}: {e}")
        try:
            Path(video_path).unlink(missing_ok=True)
        except Exception as e:
            log.debug(f"Cleanup failed video file {video_path}: {e}")
        return None


def send_video(video_path: str, caption: str):
    """Send video via Pyrogram MTProto (>=50MB) or Bot API (<50MB)."""
    file_size = Path(video_path).stat().st_size
    size_mb = file_size / (1024 * 1024)

    sent = False
    if size_mb >= 50:
        log.info(f"Sending video via MTProto ({size_mb:.1f} MB)...")
        sent = pyro_send_video(video_path, caption)

    if not sent:
        if size_mb >= 50:
            log.warning(f"MTProto failed, trying Bot API for {size_mb:.1f}MB video...")
        else:
            log.info(f"Sending video via Bot API ({size_mb:.1f} MB)...")
        result = tg_send_video_file(video_path, caption)
        if not result:
            log.warning(f"Failed to send video via Bot API: {video_path}")

    # Cleanup
    try:
        Path(video_path).unlink()
        thumb = video_path.rsplit(".", 1)[0] + "_thumb.jpg"
        if Path(thumb).exists():
            Path(thumb).unlink()
    except Exception as e:
        log.debug(f"Cleanup video/thumb files: {e}")
