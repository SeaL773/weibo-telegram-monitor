"""Weibo post fetching and data extraction."""

import random
import time
from typing import Dict, List, Optional

import httpx

from src.api.client import _check_response
from src.utils.LoggerManager import logger as log


def fetch_user_posts(client: httpx.Client, uid: str, max_pages: int = 1) -> List[Dict]:
    """Fetch user posts via desktop weibo.com API. Returns list of post dicts.
    Use max_pages > 1 for initial archiving (fetch all history)."""
    url = "https://weibo.com/ajax/statuses/mymblog"
    all_posts = []

    for page in range(1, max_pages + 1):
        params = {"uid": uid, "page": page, "feature": 0}
        try:
            r = client.get(url, params=params)
        except Exception as e:
            log.error(f"API request failed for {uid} page {page}: {e}")
            break

        data = _check_response(r, uid, context="mymblog")
        if data is None:
            break

        posts = data.get("data", {}).get("list", [])
        if not posts:
            break
        all_posts.extend(posts)
        log.info(f"Fetched page {page} for {uid}: {len(posts)} posts (total: {len(all_posts)})")

        if max_pages > 1 and page < max_pages:
            time.sleep(random.uniform(1, 2))

    return all_posts


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
