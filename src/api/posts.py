"""Weibo post fetching and data extraction."""

import random
import time
from dataclasses import dataclass
from typing import Collection, Dict, Generic, List, Optional, TypeVar

import httpx

from src.api.client import WeiboApiError, _check_response
from src.utils.LoggerManager import logger as log


T = TypeVar("T")


@dataclass(frozen=True)
class FetchResult(Generic[T]):
    """Items fetched together with whether they form a complete snapshot.

    ``complete=False`` means callers may inspect ``items`` for diagnostics or
    best-effort processing, but must not use them to replace a stored snapshot.
    ``truncated=True`` specifically means a configured page limit was reached.
    """

    items: List[T]
    complete: bool
    pages_fetched: int
    reason: str
    truncated: bool = False


def fetch_user_posts(
    client: httpx.Client,
    uid: str,
    max_pages: int = 1,
    stop_ids: Optional[Collection[str]] = None,
) -> FetchResult[Dict]:
    """Fetch user posts via the desktop API with explicit completeness.

    When ``stop_ids`` is provided, pagination is complete only after reaching
    a previously seen post or an empty page. This prevents bursts larger than
    one page from being silently skipped by a regular polling cycle.
    """
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")

    url = "https://weibo.com/ajax/statuses/mymblog"
    all_posts: List[Dict] = []
    pages_fetched = 0
    high_watermarks = {str(item) for item in (stop_ids or []) if str(item)}

    for page in range(1, max_pages + 1):
        params = {"uid": uid, "page": page, "feature": 0}
        try:
            r = client.get(url, params=params)
        except Exception as e:
            log.error(f"API request failed for {uid} page {page}: {e}")
            return FetchResult(
                items=all_posts,
                complete=False,
                pages_fetched=pages_fetched,
                reason="request_error",
            )

        try:
            data = _check_response(
                r,
                uid,
                context="mymblog",
                raise_errors=True,
            )
        except WeiboApiError as exc:
            return FetchResult(
                items=all_posts,
                complete=False,
                pages_fetched=pages_fetched,
                reason=exc.kind,
            )

        payload = data.get("data")
        if not isinstance(payload, dict) or not isinstance(payload.get("list"), list):
            log.error(f"Malformed post list for {uid} page {page}")
            return FetchResult(
                items=all_posts,
                complete=False,
                pages_fetched=pages_fetched,
                reason="invalid_payload",
            )

        posts = payload["list"]
        pages_fetched += 1
        if not posts:
            return FetchResult(
                items=all_posts,
                complete=True,
                pages_fetched=pages_fetched,
                reason="exhausted",
            )
        all_posts.extend(posts)
        log.info(f"Fetched page {page} for {uid}: {len(posts)} posts (total: {len(all_posts)})")

        if high_watermarks and any(
            str(post.get("id", "") or post.get("mid", "")) in high_watermarks
            for post in posts
        ):
            return FetchResult(
                items=all_posts,
                complete=True,
                pages_fetched=pages_fetched,
                reason="high_watermark",
            )

        if max_pages == 1 and stop_ids is None:
            return FetchResult(
                items=all_posts,
                complete=True,
                pages_fetched=pages_fetched,
                reason="requested_window",
            )

        if page < max_pages:
            time.sleep(random.uniform(1, 2))

    return FetchResult(
        items=all_posts,
        complete=False,
        pages_fetched=pages_fetched,
        reason="page_limit",
        truncated=True,
    )


def extract_post_id(post: Dict) -> str:
    """Extract the post mid (message ID)."""
    return str(post.get("id", "") or post.get("mid", ""))


def extract_video_url(post: Dict) -> Optional[str]:
    """Extract video URL from post data."""
    page_info = post.get("page_info", {})

    if page_info.get("type") == "video":
        media_info = page_info.get("media_info", {})
        for key in ["mp4_720p_mp4", "mp4_hd_url", "mp4_sd_url", "stream_url"]:
            url = media_info.get(key, "")
            if url:
                return url
        urls = page_info.get("urls", {})
        if urls:
            for key in sorted(urls.keys(), reverse=True):
                if urls[key]:
                    return urls[key]

    # Also check mix_media_info for video posts
    mix_media = post.get("mix_media_info", {})
    if mix_media:
        items = mix_media.get("items", [])
        for item in items:
            if item.get("type") == "video":
                data = item.get("data", {})
                media_info = data.get("media_info", {})
                for key in ["mp4_720p_mp4", "mp4_hd_url", "mp4_sd_url", "stream_url"]:
                    url = media_info.get(key, "")
                    if url:
                        return url

    return None


def has_video(post: Dict) -> bool:
    """Check if post has video."""
    page_info = post.get("page_info", {})
    if page_info.get("type") == "video":
        return True
    mix_media = post.get("mix_media_info", {})
    if mix_media:
        for item in mix_media.get("items", []):
            if item.get("type") == "video":
                return True
    return False


def get_post_username(post: Dict) -> str:
    """Get the display name from a post."""
    user = post.get("user", {})
    return user.get("screen_name", "")


def is_retweet(post: Dict) -> bool:
    """Check if post is a retweet/repost."""
    return bool(post.get("retweeted_status"))


def extract_image_urls(post: Dict) -> tuple[list[str], list[str]]:
    """Extract image URLs from post. Returns (photo_urls, gif_urls)."""
    photos = []
    gifs = []
    pics = post.get("pic_ids", [])
    pic_infos = post.get("pic_infos", {})
    for pic_id in pics:
        info = pic_infos.get(pic_id, {})
        url = ""
        for size in ["original", "large", "mw2000", "thumbnail"]:
            img = info.get(size, {})
            url = img.get("url", "")
            if url:
                break
        if not url:
            continue
        if url.lower().split("?")[0].endswith(".gif"):
            gifs.append(url)
        else:
            photos.append(url)
    return photos, gifs


def is_like_collection(post: Dict) -> bool:
    """Weibo aggregates user's liked posts into fake timeline entries with title like '他X-XX赞过的微博(N)'."""
    title = post.get("title")
    if isinstance(title, dict):
        return "赞过的微博" in title.get("text", "")
    return False
