"""Main monitoring loop, signal handling, and entry point."""

import os
import random
import signal
import threading
import time
import traceback
from pathlib import Path

from src.core.config import (
    POLL_INTERVAL,
    POLL_JITTER,
    FIRST_RUN_MODE,
    FIRST_RUN_PAGES,
    MAX_SEEN_IDS,
    DATA_DIR,
    VIDEOS_DIR,
    FOLLOW_CHECK_INTERVAL,
    COOKIE_REFRESH_INTERVAL,
    get_user_tag,
)
from src.core.utils import html_escape, now_et
from src.core.push import push_post, _clean_hashtag
from src.core.comments import poll_active_posts
from src.api.client import (
    load_cookie_store,
    make_api_client,
    parse_targets,
)
from src.telegram.bot import _alert_tg
from src.api.posts import fetch_user_posts, extract_post_id, is_like_collection
from src.api.profile import (
    get_user_info,
    check_profile_changes,
    check_follow_changes,
)
from src.archive.state import load_state, save_state
from src.telegram.bot import tg_send, get_tg_client_ref
from src.screenshot.browser import (
    cleanup_playwright,
    configure_cookie_store,
    refresh_weibo_session,
)
from src.utils.LoggerManager import logger as log

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_shutdown_requested = False
_consecutive_failures = 0

_HEARTBEAT = Path('/tmp/healthcheck')
_WATCHDOG_TIMEOUT = 900


def _watchdog():
    while True:
        time.sleep(60)
        try:
            if _HEARTBEAT.exists() and time.time() - _HEARTBEAT.stat().st_mtime > _WATCHDOG_TIMEOUT:
                log.critical(f"Watchdog: no heartbeat for >{_WATCHDOG_TIMEOUT}s, forcing exit")
                os._exit(1)
        except Exception:
            pass


def main():
    global _consecutive_failures, _shutdown_requested

    def _handle_signal(sig, frame):
        global _shutdown_requested
        log.info(f"Received signal {sig}, shutting down gracefully...")
        _shutdown_requested = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("Starting Weibo Telegram Monitor")

    target_uids = parse_targets()
    cookie_store = load_cookie_store()
    cookie_dict = cookie_store.cookie_dict()

    if not cookie_dict:
        log.error("No cookies found! Set WEIBO_COOKIES or restore data/auth_state.json")
        import sys
        sys.exit(1)

    log.info(f"Monitoring UIDs: {', '.join(target_uids)}")
    log.info(f"Poll interval: {POLL_INTERVAL}±{POLL_JITTER}s")
    log.info(f"First run mode: {FIRST_RUN_MODE}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

    state = load_state()
    configure_cookie_store(cookie_store)
    api_client = make_api_client(cookie_dict, cookie_store)

    # Ensure seen_ids has entries for all targets
    for uid in target_uids:
        if uid not in state.get("seen_ids", {}):
            if "seen_ids" not in state:
                state["seen_ids"] = {}
            state["seen_ids"][uid] = []

    is_first_run = {}
    for uid in target_uids:
        is_first_run[uid] = len(state["seen_ids"].get(uid, [])) == 0

    threading.Thread(target=_watchdog, daemon=True).start()
    cycle = 0
    next_cookie_refresh = 0.0

    while True:
        try:
            cycle += 1

            log.info(f"=== Cycle {cycle} ===")

            # Normal Ajax polling does not run the first-party page/SSO flow.
            # Periodically load the homepage and persist any rotated cookies.
            if time.time() >= next_cookie_refresh:
                refreshed = refresh_weibo_session()
                if hasattr(api_client, "sync_from_store"):
                    api_client.sync_from_store()
                refresh_jitter = min(max(COOKIE_REFRESH_INTERVAL // 6, 60), 3600)
                if refreshed:
                    next_cookie_refresh = (
                        time.time()
                        + COOKIE_REFRESH_INTERVAL
                        + random.randint(-refresh_jitter, refresh_jitter)
                    )
                else:
                    # Avoid retrying a browser/auth failure on every poll cycle.
                    next_cookie_refresh = time.time() + min(1800, COOKIE_REFRESH_INTERVAL)

            for uid in target_uids:
                try:
                    log.info(f"Checking UID {uid}...")

                    # Fetch user info on first run
                    if is_first_run.get(uid) and uid not in state.get("profiles", {}):
                        user_info = get_user_info(api_client, uid)
                        if user_info:
                            state.setdefault("profiles", {})[uid] = {
                                "screen_name": user_info.get("screen_name", ""),

                                "description": user_info.get("description", ""),
                            }
                            log.info(f"Cached profile for {uid}: {user_info.get('screen_name', '')}")

                    # Fetch posts (fetch all pages on first run to archive all IDs)
                    pages = FIRST_RUN_PAGES if is_first_run.get(uid) else 1
                    posts = fetch_user_posts(api_client, uid, max_pages=pages)
                    if not posts:
                        log.info(f"No posts found for {uid}")
                        continue

                    seen = list(state["seen_ids"].get(uid, []))
                    seen_set = set(seen)  # O(1) lookup
                    new_posts = []
                    for post in posts:
                        mid = extract_post_id(post)
                        if mid and mid not in seen_set:
                            if is_like_collection(post):
                                seen.append(mid)
                                seen_set.add(mid)
                                continue
                            new_posts.append(post)

                    # Sort oldest first
                    new_posts.sort(key=lambda p: int(extract_post_id(p)))

                    if is_first_run.get(uid):
                        if FIRST_RUN_MODE == "archive":
                            log.info(f"First run (archive): recording {len(new_posts)} post IDs for {uid}")
                            for post in new_posts:
                                mid = extract_post_id(post)
                                if mid and mid not in seen_set:
                                    seen.append(mid)
                                    seen_set.add(mid)
                            state["seen_ids"][uid] = seen[-MAX_SEEN_IDS:]
                            save_state(state)
                            is_first_run[uid] = False
                            continue
                        else:
                            log.info(f"First run (push): pushing {len(new_posts)} posts for {uid}")

                    if new_posts:
                        log.info(f"Found {len(new_posts)} new posts for {uid}")
                    else:
                        log.info(f"No new posts for {uid}")

                    for post in new_posts:
                        mid = extract_post_id(post)
                        if not mid:
                            continue
                        try:
                            push_post(post, uid, cookie_dict, state=state)
                        except Exception as e:
                            log.error(f"Failed to push post {mid}: {e}")
                            traceback.print_exc()
                        if mid not in seen_set:
                            seen.append(mid)
                            seen_set.add(mid)
                        state["seen_ids"][uid] = seen[-MAX_SEEN_IDS:]
                        save_state(state)

                    is_first_run[uid] = False

                    # --- Profile changes ---
                    try:
                        username = state.get("profiles", {}).get(uid, {}).get("screen_name", uid)
                        tag_name = get_user_tag(uid, _clean_hashtag(username))
                        changes = check_profile_changes(api_client, uid, state)
                        if changes:
                            text = f"#微博 #资料变更 #{tag_name}\n" + "\n".join(html_escape(c) for c in changes)
                            tg_send(text)
                            save_state(state)
                            log.info(f"Profile changes for {uid}: {changes}")
                    except Exception as e:
                        log.error(f"Profile check failed for {uid}: {e}")

                    # --- Follow list changes (throttled) ---
                    last_follow_check = state.get(f"last_follow_check_{uid}", 0)
                    if time.time() - last_follow_check >= FOLLOW_CHECK_INTERVAL:
                        try:
                            username = state.get("profiles", {}).get(uid, {}).get("screen_name", uid)
                            tag_name = get_user_tag(uid, _clean_hashtag(username))
                            new_follows, unfollows = check_follow_changes(api_client, uid, state)
                            if new_follows:
                                names = ", ".join(html_escape(n) for n in new_follows)
                                tg_send(f"#微博 #新关注 #{tag_name}\n{names}")
                                save_state(state)
                                log.info(f"New follows for {uid}: {new_follows}")
                            if unfollows:
                                names = ", ".join(html_escape(n) for n in unfollows)
                                tg_send(f"#微博 #取关 #{tag_name}\n{names}")
                                save_state(state)
                                log.info(f"Unfollows for {uid}: {unfollows}")
                            state[f"last_follow_check_{uid}"] = int(time.time())
                            save_state(state)
                        except Exception as e:
                            log.error(f"Follow check failed for {uid}: {e}")

                except Exception as e:
                    log.error(f"Error checking UID {uid}: {e}")
                    traceback.print_exc()

                # Small delay between users
                if len(target_uids) > 1:
                    time.sleep(random.uniform(2, 5))

            # --- Poll active posts for comments/edits ---
            try:
                poll_active_posts(api_client, state)
            except Exception as e:
                log.error(f"poll_active_posts failed: {e}")
                traceback.print_exc()

            state["last_check"] = now_et().isoformat()
            save_state(state)
            _consecutive_failures = 0

        except Exception as e:
            _consecutive_failures += 1
            log.error(f"Cycle {cycle} failed ({_consecutive_failures}x): {e}")
            traceback.print_exc()

            err_str = str(e).lower()
            # Detect auth/cookie issues
            if any(k in err_str for k in ["401", "403", "unauthorized", "forbidden", "login"]):
                _alert_tg("cookie",
                    f"Weibo Telegram Monitor Cookie失效/被风控\n"
                    f"Error: {html_escape(type(e).__name__)}\n"
                    f"连续失败: {_consecutive_failures}次\n\n"
                    f"请更新WEIBO_COOKIES")
            elif any(k in err_str for k in ["429", "418", "rate", "频繁"]):
                _alert_tg("rate",
                    f"Weibo Telegram Monitor 请求限速\n"
                    f"Error: {html_escape(type(e).__name__)}\n"
                    f"连续失败: {_consecutive_failures}次\n\n"
                    f"监控将自动退避")
            elif _consecutive_failures >= 3:
                _alert_tg("error",
                    f"Weibo Telegram Monitor 连续失败\n"
                    f"连续失败: {_consecutive_failures}次\n"
                    f"Error: {html_escape(str(e)[:200])}\n\n"
                    f"请检查监控状态")

        if _shutdown_requested:
            break

        _HEARTBEAT.write_text('')

        # Sleep with jitter + backoff on failures
        sleep_time = POLL_INTERVAL + random.randint(-POLL_JITTER, POLL_JITTER)
        if _consecutive_failures > 0:
            # Exponential backoff: 2x, 4x, 8x... up to 1 hour max
            backoff = min(sleep_time * (2 ** _consecutive_failures), 3600)
            log.warning(f"Backoff: sleeping {backoff:.0f}s due to {_consecutive_failures} consecutive failures")
            sleep_time = int(backoff)
        sleep_time = max(60, sleep_time)
        log.info(f"Sleeping {sleep_time}s...")

        # Sleep in small increments to respond to shutdown quickly
        for _ in range(sleep_time):
            if _shutdown_requested:
                break
            time.sleep(1)

    # Graceful cleanup
    log.info("Shutting down...")
    cleanup_playwright()
    try:
        _tg_client = get_tg_client_ref()
        if _tg_client and not _tg_client.is_closed:
            _tg_client.close()
    except Exception as e:
        log.debug(f"Closing TG client on shutdown: {e}")
    log.info("Weibo Telegram Monitor stopped")
