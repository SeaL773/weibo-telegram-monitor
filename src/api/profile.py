"""Weibo user profile and follow list monitoring."""

import random
from src.utils.LoggerManager import logger as log
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx

from src.api.client import WeiboApiError, _check_response
from src.api.posts import FetchResult


@dataclass(frozen=True)
class ProfileDelta:
    snapshot: Dict
    changes: List[str]


@dataclass(frozen=True)
class FollowDelta:
    snapshot: List[Dict]
    new_follows: List[str]
    unfollows: List[str]



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


def get_follow_list(client: httpx.Client, uid: str) -> FetchResult[Dict]:
    """Fetch follows, returning items only as a snapshot when complete."""
    url = "https://weibo.com/ajax/friendships/friends"
    all_follows: List[Dict] = []
    page = 1
    pages_fetched = 0
    while page <= 20:  # Safety limit
        params = {"uid": uid, "page": page}
        try:
            r = client.get(url, params=params)
        except Exception as e:
            log.error(f"Failed to fetch follow list for {uid} page {page}: {e}")
            return FetchResult(
                items=all_follows,
                complete=False,
                pages_fetched=pages_fetched,
                reason="request_error",
            )

        try:
            data = _check_response(
                r,
                uid,
                context="friendships/friends",
                raise_errors=True,
            )
        except WeiboApiError as exc:
            return FetchResult(
                items=all_follows,
                complete=False,
                pages_fetched=pages_fetched,
                reason=exc.kind,
            )

        users = data.get("users")
        if not isinstance(users, list):
            log.error(f"Malformed follow list for {uid} page {page}")
            return FetchResult(
                items=all_follows,
                complete=False,
                pages_fetched=pages_fetched,
                reason="invalid_payload",
            )

        pages_fetched += 1
        if not users:
            return FetchResult(
                items=all_follows,
                complete=True,
                pages_fetched=pages_fetched,
                reason="exhausted",
            )
        for u in users:
            if not isinstance(u, dict) or not str(u.get("id", "")):
                log.error(f"Malformed follow entry for {uid} page {page}")
                return FetchResult(
                    items=all_follows,
                    complete=False,
                    pages_fetched=pages_fetched,
                    reason="invalid_payload",
                )
            all_follows.append({
                "id": str(u.get("id", "")),
                "screen_name": u.get("screen_name", ""),
            })
        if len(users) < 20:
            return FetchResult(
                items=all_follows,
                complete=True,
                pages_fetched=pages_fetched,
                reason="exhausted",
            )
        page += 1
        if page <= 20:
            time.sleep(random.uniform(1, 2))

    return FetchResult(
        items=all_follows,
        complete=False,
        pages_fetched=pages_fetched,
        reason="page_limit",
        truncated=True,
    )


def detect_profile_changes(
    client: httpx.Client,
    uid: str,
    stored: Dict,
) -> Optional[ProfileDelta]:
    """Return an immutable profile delta without committing caller state."""
    user_info = get_user_info(client, uid)
    if not user_info:
        return None

    snapshot = {
        "screen_name": user_info.get("screen_name", ""),
        "description": user_info.get("description", ""),
        "avatar_hd": user_info.get("avatar_hd", ""),
    }
    if not stored:
        return ProfileDelta(snapshot=snapshot, changes=[])

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

    return ProfileDelta(snapshot=snapshot, changes=changes)


def check_profile_changes(client: httpx.Client, uid: str, state: Dict) -> List[str]:
    """Compatibility wrapper that detects and commits a complete snapshot."""
    delta = detect_profile_changes(
        client,
        uid,
        state.get("profiles", {}).get(uid, {}),
    )
    if delta is None:
        return []
    state.setdefault("profiles", {})[uid] = delta.snapshot
    return delta.changes

def detect_follow_changes(
    client: httpx.Client,
    uid: str,
    stored_follows: Optional[List[Dict]],
) -> Optional[FollowDelta]:
    """Return a complete follow delta without mutating caller state."""
    result = get_follow_list(client, uid)
    if not result.complete:
        log.warning(
            f"Follow list fetch incomplete for {uid} "
            f"({result.reason}, {len(result.items)} partial items), skipping comparison"
        )
        return None

    current_follows = result.items
    if stored_follows is None:
        return FollowDelta(current_follows, [], [])
    if not isinstance(stored_follows, list):
        log.warning(f"Invalid stored follow snapshot for {uid}, replacing baseline")
        return FollowDelta(current_follows, [], [])

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

    return FollowDelta(current_follows, new_follows, unfollows)


def check_follow_changes(client: httpx.Client, uid: str, state: Dict) -> tuple:
    """Compatibility wrapper that detects and commits a complete snapshot."""
    stored_key = f"follows_{uid}"
    is_initial_snapshot = stored_key not in state
    delta = detect_follow_changes(client, uid, state.get(stored_key))
    if delta is None:
        return [], []
    state[stored_key] = delta.snapshot
    if is_initial_snapshot:
        log.info(f"Cached {len(delta.snapshot)} follows for {uid}")
    return delta.new_follows, delta.unfollows
