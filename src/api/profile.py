"""Weibo user profile and follow list monitoring."""

import random
from src.utils.LoggerManager import logger as log
import time
from typing import Dict, List, Optional

import httpx

from src.api.client import _check_response



def get_user_info(client: httpx.Client, uid: str) -> Optional[Dict]:
    """Fetch user info from desktop API."""
    url = "https://weibo.com/ajax/profile/info"
    params = {"uid": uid}
    try:
        r = client.get(url, params=params)
    except Exception as e:
        log.error(f"Failed to fetch user info for {uid}: {e}")
        return None

    data = _check_response(r, uid, context="profile/info")
    if data is None:
        return None
    return data.get("data", {}).get("user", {})


def get_follow_list(client: httpx.Client, uid: str) -> List[Dict]:
    """Fetch the list of users this UID follows. Returns list of {id, screen_name}."""
    url = "https://weibo.com/ajax/friendships/friends"
    all_follows = []
    page = 1
    while page <= 20:  # Safety limit
        params = {"uid": uid, "page": page}
        try:
            r = client.get(url, params=params)
        except Exception as e:
            log.error(f"Failed to fetch follow list for {uid} page {page}: {e}")
            break

        data = _check_response(r, uid, context="friendships/friends")
        if data is None:
            return None

        users = data.get("users", [])
        if not users:
            break
        for u in users:
            all_follows.append({
                "id": str(u.get("id", "")),
                "screen_name": u.get("screen_name", ""),
            })
        if len(users) < 20:
            break
        page += 1
        time.sleep(random.uniform(1, 2))
    return all_follows


def check_profile_changes(client: httpx.Client, uid: str, state: Dict) -> List[str]:
    """Check for profile changes. Returns list of change descriptions."""
    user_info = get_user_info(client, uid)
    if not user_info:
        return []

    stored = state.get("profiles", {}).get(uid, {})
    if not stored:
        # First time: save and skip
        state.setdefault("profiles", {})[uid] = {
            "screen_name": user_info.get("screen_name", ""),
            "description": user_info.get("description", ""),
            "avatar_hd": user_info.get("avatar_hd", ""),
        }
        return []

    changes = []
    new_name = user_info.get("screen_name", "")
    old_name = stored.get("screen_name", "")
    if new_name and old_name and new_name != old_name:
        changes.append(f"昵称变更: {old_name} → {new_name}")

    new_desc = user_info.get("description", "")
    old_desc = stored.get("description", "")
    if new_desc != old_desc:
        changes.append(f"简介变更: {old_desc or '(空)'} → {new_desc or '(空)'}")

    new_avatar = user_info.get("avatar_hd", "")
    old_avatar = stored.get("avatar_hd", "")
    if new_avatar and old_avatar and new_avatar != old_avatar:
        changes.append("头像已更换")

    # Update stored profile
    if changes:
        state.setdefault("profiles", {})[uid] = {
            "screen_name": new_name,
            "description": new_desc,
            "avatar_hd": new_avatar,
        }

    return changes


def check_follow_changes(client: httpx.Client, uid: str, state: Dict) -> tuple:
    """Check for follow list changes. Returns (new_follows, unfollows)."""
    current_follows = get_follow_list(client, uid)
    if current_follows is None:
        log.warning(f"Follow list fetch failed for {uid}, skipping comparison")
        return [], []
    if not current_follows:
        return [], []

    stored_key = f"follows_{uid}"
    stored_follows = state.get(stored_key, [])

    if not stored_follows:
        # First time: save and skip
        state[stored_key] = current_follows
        log.info(f"Cached {len(current_follows)} follows for {uid}")
        return [], []

    old_ids = {f["id"]: f["screen_name"] for f in stored_follows}
    new_ids = {f["id"]: f["screen_name"] for f in current_follows}

    new_follows = []
    for fid, name in new_ids.items():
        if fid not in old_ids:
            new_follows.append(name)

    unfollows = []
    for fid, name in old_ids.items():
        if fid not in new_ids:
            unfollows.append(name)

    if new_follows or unfollows:
        state[stored_key] = current_follows

    return new_follows, unfollows
