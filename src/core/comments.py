"""Comment and edit monitoring for active posts."""

import random
from src.utils.LoggerManager import logger as log
import re
import time
from dataclasses import dataclass
from typing import Dict

import httpx

from src.core.config import COMMENT_MONITOR_HOURS
from src.core.utils import html_escape
from src.core.push import _clean_hashtag
from src.api.client import _check_response
from src.archive.outbox import DeliveryOutbox
from src.archive.state import save_state
from src.archive.storage import is_allowed_media_url



@dataclass(frozen=True)
class CommentPollResult:
    activity: bool
    complete: bool
    state_changed: bool


def _extract_comment_pic_urls(comment: dict) -> list:
    """Extract picture URLs from a weibo comment.

    Pictures are in url_struct[].pic_infos → {pid: {large: {url: ...}}}.
    Returns list of large image URLs.
    """
    urls = []
    for us in comment.get("url_struct", []):
        pic_infos = us.get("pic_infos", {})
        for pid, info in pic_infos.items():
            large_url = (info.get("large") or {}).get("url", "")
            if large_url:
                urls.append(large_url)
    return urls


def _get_poll_interval_seconds(age_seconds: int) -> int:
    """Return the poll interval based on how old the post is."""
    age_hours = age_seconds / 3600
    if age_hours < 2:
        return 10 * 60   # every 10 min
    elif age_hours < 6:
        return 15 * 60   # every 15 min
    elif age_hours < 12:
        return 20 * 60   # every 20 min
    else:
        return 30 * 60   # every 30 min


def _should_poll_now(active_post: Dict) -> bool:
    """Decide if it's time to poll this active post based on adaptive intervals."""
    now_ts = int(time.time())
    age = now_ts - active_post["created_at"]
    interval = _get_poll_interval_seconds(age)
    last_poll_time = active_post.get("last_poll_time", active_post["created_at"])
    return (now_ts - last_poll_time) >= interval


def check_active_post_comments(
    api_client: httpx.Client,
    mid: str,
    active_post: Dict,
    state: Dict,
    outbox: DeliveryOutbox,
) -> CommentPollResult:
    """Check comments and edits for a single active post.
    Returns activity and completeness without treating API failures as a poll."""
    uid = active_post["uid"]
    tg_message_id = active_post["tg_message_id"]
    username = state.get("profiles", {}).get(uid, {}).get("screen_name", uid)
    clean_name = _clean_hashtag(username)
    tag = f"#微博 #{clean_name}"
    activity_found = False
    state_changed = False
    comments_complete = False
    edit_complete = False

    # --- 1. Check comments ---
    try:
        url = "https://weibo.com/ajax/statuses/buildComments"
        params = {
            "id": mid,
            "is_reload": 1,
            "is_show_bulletin": 2,
            "is_mix": 0,
            "count": 20,
            "flow": 0,
        }
        r = api_client.get(url, params=params)
        data = _check_response(r, uid, context="buildComments")
        if data is not None:
            comments = data.get("data", [])
            if not isinstance(comments, list):
                raise ValueError("Malformed comments payload")
            comments_complete = True
            seen_ids = set(active_post.get("seen_comment_ids", []))
            uid_int = int(uid)

            for comment in comments:
                comment_user = comment.get("user", {})
                comment_uid = comment_user.get("id", 0)
                if comment_uid != uid_int:
                    continue  # Not from our monitored user

                cid = str(comment.get("id", ""))
                if not cid or cid in seen_ids:
                    continue  # Already seen

                # New comment from monitored user
                comment_text = comment.get("text_raw", "") or comment.get("text", "")
                comment_text_clean = re.sub(r'<[^>]+>', '', comment_text).strip()

                # Check if it's a reply to someone
                reply_comment = comment.get("reply_comment")
                if reply_comment:
                    reply_user = reply_comment.get("user", {})
                    reply_name = reply_user.get("screen_name", "某人")
                    reply_text = reply_comment.get("text_raw", "") or reply_comment.get("text", "")
                    reply_text_clean = re.sub(r'<[^>]+>', '', reply_text).strip()
                    # Truncate long reply text
                    if len(reply_text_clean) > 100:
                        reply_text_clean = reply_text_clean[:100] + "..."

                    msg = (
                        f"{tag}\n"
                        f"回复了 @{html_escape(reply_name)}：\n"
                        f"&gt; {html_escape(reply_name)}说：{html_escape(reply_text_clean)}\n"
                        f"{html_escape(comment_text_clean)}"
                    )
                else:
                    # Direct comment (not a reply)
                    msg = (
                        f"{tag}\n"
                        f"评论了自己的微博：\n"
                        f"{html_escape(comment_text_clean)}"
                    )

                # Extract comment pictures
                pic_urls = [
                    url
                    for url in _extract_comment_pic_urls(comment)
                    if is_allowed_media_url(url)
                ]
                if pic_urls:
                    method = "photo" if len(pic_urls) == 1 else "media_group"
                else:
                    method = "reply"
                outbox.enqueue(
                    f"comment:{mid}:{cid}",
                    "telegram",
                    {
                        "method": method,
                        "text": msg,
                        "reply_to": tg_message_id,
                        "photo_urls": pic_urls,
                    },
                )
                seen_ids.add(cid)
                activity_found = True
                state_changed = True
                log.info(f"Pushed comment {cid} for post {mid}")

            active_post["seen_comment_ids"] = list(seen_ids)
    except Exception as e:
        log.error(f"Failed to check comments for post {mid}: {e}")

    # --- 2. Check for post edits ---
    try:
        url = f"https://weibo.com/ajax/statuses/show?id={mid}"
        r = api_client.get(url)
        data = _check_response(r, uid, context="statuses-show-edit-check")
        if data is not None:
            edit_complete = True
            new_edit_count = data.get("edit_count", 0)
            stored_edit_count = active_post.get("edit_count", 0)
            if new_edit_count > stored_edit_count:
                msg = f"{tag}\n编辑了微博内容"
                outbox.enqueue(
                    f"edit:{mid}:{new_edit_count}",
                    "telegram",
                    {
                        "method": "reply",
                        "text": msg,
                        "reply_to": tg_message_id,
                    },
                )
                active_post["edit_count"] = new_edit_count
                activity_found = True
                state_changed = True
                log.info(f"Post {mid} was edited (edit_count {stored_edit_count} → {new_edit_count})")
    except Exception as e:
        log.error(f"Failed to check post edit for {mid}: {e}")

    return CommentPollResult(
        activity=activity_found,
        complete=comments_complete and edit_complete,
        state_changed=state_changed,
    )


def poll_active_posts(
    api_client: httpx.Client,
    state: Dict,
    outbox: DeliveryOutbox,
):
    """Poll all active posts for comments/edits. Called once per main loop cycle."""
    active_posts = state.get("active_posts", {})
    if not active_posts:
        return

    now_ts = int(time.time())
    expired_mids = []
    polled = 0
    failures = 0
    state_dirty = False

    for mid, ap in list(active_posts.items()):
        # Check expiration
        if now_ts >= ap.get("expires_at", 0):
            expired_mids.append(mid)
            continue

        # Check if it's time to poll this post
        if not _should_poll_now(ap):
            continue

        log.info(f"Polling comments for post {mid} (poll #{ap.get('poll_count', 0) + 1})")
        result = check_active_post_comments(api_client, mid, ap, state, outbox)

        if result.complete:
            ap["poll_count"] = ap.get("poll_count", 0) + 1
            ap["last_poll_time"] = now_ts
            state_dirty = True
            polled += 1
        else:
            failures += 1
            log.warning(f"Comment poll for post {mid} was incomplete; retrying later")
        state_dirty = state_dirty or result.state_changed

        if result.activity:
            # Reset the 24h sliding window
            ap["last_activity"] = now_ts
            ap["expires_at"] = now_ts + COMMENT_MONITOR_HOURS * 3600
            log.info(f"Activity on post {mid}, reset expiry to {COMMENT_MONITOR_HOURS}h from now")

        # Small delay between API calls to avoid rate limiting
        time.sleep(random.uniform(2, 4))

    # Clean up expired posts
    for mid in expired_mids:
        del active_posts[mid]
        state_dirty = True
        log.info(f"Post {mid} expired from comment monitoring")

    if state_dirty:
        save_state(state)
        if polled > 0:
            log.info(f"Polled {polled} active posts, {len(expired_mids)} expired, {len(active_posts)} remaining")
    return failures
