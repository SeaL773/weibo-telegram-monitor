"""Post processing and push to Telegram."""

import re
from src.utils.LoggerManager import logger as log
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from src.core.config import COMMENT_MONITOR_HOURS, ET
from src.core.utils import html_escape, mid_to_base62
from src.core.config import get_user_tag
from src.api.posts import (
    extract_image_urls,
    extract_post_id,
    extract_video_url,
    get_post_username,
    has_video,
    is_retweet,
)
from src.telegram.bot import (
    tg_send,
    tg_send_animation_url,
    tg_send_media_group_url,
    tg_send_photo_bytes,
    tg_send_photo_url,
)
from src.screenshot.browser import screenshot_post
from src.archive.storage import archive_post, download_video, send_video, is_allowed_media_url



@dataclass(frozen=True)
class DeliveryResult:
    primary_delivered: bool
    message_id: Optional[int] = None
    optional_failures: List[str] = field(default_factory=list)


def _clean_hashtag(name: str) -> str:
    """Strip characters invalid in TG hashtags (keep letters, digits, underscores, CJK)."""
    return re.sub(r'[^\w\u4e00-\u9fff\u3400-\u4dbf]', '', name)


def build_caption(post: Dict, uid: str) -> str:
    """Build TG caption with tags. Differentiates original vs retweet."""
    username = get_post_username(post)
    mid = extract_post_id(post)
    mid_b62 = mid_to_base62(mid)
    weibo_url = f"https://weibo.com/{uid}/{mid_b62}"

    tag_name = get_user_tag(uid, _clean_hashtag(username))

    if is_retweet(post):
        sub_tag = "#转发"
    else:
        sub_tag = "#新微博"

    # Parse created_at timestamp (e.g. "Tue Mar 04 20:18:32 +0800 2026")
    ts_str = ""
    created_at = post.get("created_at", "")
    if created_at:
        try:
            dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
            ts_str = dt.astimezone(ET).strftime("%Y-%m-%d %H:%M") + " ET"
        except Exception as e:
            log.debug(f"Parsing created_at timestamp: {e}")
            ts_str = created_at

    parts = [f"#微博 {sub_tag} #{tag_name}"]
    if ts_str:
        parts.append(ts_str)
    parts.append(f'<a href="{weibo_url}">查看原文</a>')
    return "\n".join(parts)


def push_post(
    post: Dict,
    uid: str,
    cookie_dict: Dict,
    state: Optional[Dict] = None,
) -> DeliveryResult:
    """Deliver a post and report whether its primary notification was acknowledged."""
    mid = extract_post_id(post)
    caption = build_caption(post, uid)
    tg_message_id = None
    optional_failures: List[str] = []

    # Screenshot the post
    log.info(f"Taking screenshot of post {mid}...")
    time.sleep(1)
    screenshot = screenshot_post(mid, uid=uid)

    # Send screenshot as photo
    if screenshot:
        result = tg_send_photo_bytes(screenshot, caption)
        if result:
            tg_message_id = result.get("message_id")
        else:
            log.warning(f"Screenshot delivery failed for {mid}, sending text fallback")

    if not tg_message_id:
        if not screenshot:
            log.warning(f"Screenshot failed for {mid}, sending text only")
        text = post.get("text_raw", "") or post.get("text", "")
        clean_text = re.sub(r'<[^>]+>', '', text)[:500]
        result = tg_send(f"{caption}\n\n{html_escape(clean_text)}")
        if result:
            tg_message_id = result.get("message_id")

    if not tg_message_id:
        try:
            archive_post(post, uid, screenshot_bytes=screenshot, cookie_dict=cookie_dict)
        except Exception as e:
            log.warning(f"Archive failed for undelivered post {mid}: {type(e).__name__}")
        return DeliveryResult(primary_delivered=False)

    # Optional work must never turn an acknowledged primary notification into
    # a retry of the whole post, which would create duplicate Telegram posts.
    try:
        photo_urls, gif_urls = extract_image_urls(post)
        photo_urls = [url for url in photo_urls if is_allowed_media_url(url)]
        gif_urls = [url for url in gif_urls if is_allowed_media_url(url)]
        if photo_urls:
            log.info(f"Sending {len(photo_urls)} image(s) for post {mid}")
            if len(photo_urls) == 1:
                if not tg_send_photo_url(photo_urls[0]):
                    optional_failures.append("photo")
            elif not tg_send_media_group_url(photo_urls):
                optional_failures.append("photo_group")
        for gif_url in gif_urls:
            log.info(f"Sending GIF for post {mid}")
            if not tg_send_animation_url(gif_url):
                optional_failures.append("gif")
    except Exception as e:
        optional_failures.append("image_processing")
        log.warning(f"Optional image delivery failed for {mid}: {type(e).__name__}")

    # Handle video
    try:
        if has_video(post):
            video_url = extract_video_url(post)
            if video_url:
                log.info(f"Downloading video for post {mid}...")
                time.sleep(1)
                video_path = download_video(video_url, mid, cookie_dict)
                if video_path:
                    if not send_video(video_path, caption):
                        optional_failures.append("video")
                else:
                    optional_failures.append("video_download")
            else:
                log.warning(f"Video detected but URL not found for {mid}")
    except Exception as e:
        optional_failures.append("video_processing")
        log.warning(f"Optional video delivery failed for {mid}: {type(e).__name__}")

    # Archive locally
    try:
        archive_post(post, uid, screenshot_bytes=screenshot, cookie_dict=cookie_dict)
    except Exception as e:
        log.warning(f"Archive failed for {mid}: {e}")

    # Register for comment monitoring
    if state is not None and tg_message_id:
        try:
            now_ts = int(time.time())
            state.setdefault("active_posts", {})[mid] = {
                "uid": uid,
                "tg_message_id": tg_message_id,
                "edit_count": post.get("edit_count", 0),
                "seen_comment_ids": [],
                "last_activity": now_ts,
                "expires_at": now_ts + COMMENT_MONITOR_HOURS * 3600,
                "created_at": now_ts,
                "poll_count": 0,
            }
            log.info(
                f"Registered post {mid} for comment monitoring (tg_msg={tg_message_id})"
            )
        except Exception as e:
            optional_failures.append("comment_registration")
            log.warning(f"Comment registration failed for {mid}: {type(e).__name__}")

    time.sleep(1)
    return DeliveryResult(
        primary_delivered=True,
        message_id=tg_message_id,
        optional_failures=optional_failures,
    )
