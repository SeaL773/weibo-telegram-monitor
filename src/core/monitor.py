"""Main monitoring loop, scheduling, delivery outbox, and lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import random
import signal
import threading
import time
import traceback
import fcntl
from pathlib import Path

from src.api.client import load_cookie_store, make_api_client, parse_targets
from src.api.posts import extract_post_id, fetch_user_posts, is_like_collection
from src.api.profile import detect_follow_changes, detect_profile_changes
from src.archive.outbox import DeliveryOutbox
from src.archive.state import load_state, save_state
from src.archive.storage import close_media_client
from src.core.comments import poll_active_posts
from src.core.config import (
    COOKIE_REFRESH_INTERVAL,
    DATA_DIR,
    FIRST_RUN_MODE,
    FIRST_RUN_PAGES,
    FOLLOW_CHECK_INTERVAL,
    HEALTH_TIMEOUT,
    MAX_SEEN_IDS,
    OUTBOX_BATCH_SIZE,
    OUTBOX_FILE,
    POLL_INTERVAL,
    POLL_JITTER,
    POLL_MAX_PAGES,
    VIDEOS_DIR,
    get_user_tag,
)
from src.core.delivery import process_outbox
from src.core.push import _clean_hashtag
from src.core.utils import html_escape, now_et
from src.screenshot.browser import (
    cleanup_playwright,
    configure_cookie_store,
    refresh_weibo_session,
)
from src.telegram.bot import _alert_tg, get_tg_client_ref
from src.utils.LoggerManager import logger as log


_HEARTBEAT = Path("/tmp/healthcheck")
_shutdown_event = threading.Event()
_consecutive_failures = 0


class ShutdownRequested(BaseException):
    """Escape nested retry/error handlers and reach the application cleanup."""


def _touch_heartbeat() -> None:
    try:
        _HEARTBEAT.touch(exist_ok=True)
    except OSError as exc:
        log.warning(f"Could not update health heartbeat: {type(exc).__name__}")


def _watchdog() -> None:
    while not _shutdown_event.wait(30):
        try:
            age = time.time() - _HEARTBEAT.stat().st_mtime
            if age > HEALTH_TIMEOUT:
                log.critical(
                    f"Watchdog: no monitor progress for {int(age)}s; requesting shutdown"
                )
                os.kill(os.getpid(), signal.SIGTERM)
                return
        except FileNotFoundError:
            _touch_heartbeat()
        except OSError as exc:
            log.warning(f"Watchdog check failed: {type(exc).__name__}")


def _interruptible_sleep(seconds: int) -> None:
    next_heartbeat = time.monotonic()
    deadline = time.monotonic() + max(0, seconds)
    while not _shutdown_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if time.monotonic() >= next_heartbeat:
            _touch_heartbeat()
            next_heartbeat = time.monotonic() + 30
        _shutdown_event.wait(min(1, remaining))


def _event_digest(payload) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:20]


def _tag_for(uid: str, state: dict) -> str:
    username = state.get("profiles", {}).get(uid, {}).get("screen_name", uid)
    configured = get_user_tag(uid, username)
    return _clean_hashtag(configured) or _clean_hashtag(uid) or "User"


def main():
    global _consecutive_failures

    api_client = None
    outbox = None
    watchdog_thread = None
    lock_file = None
    _shutdown_event.clear()

    def _handle_signal(sig, _frame):
        log.info(f"Received signal {sig}, shutting down gracefully...")
        _shutdown_event.set()
        raise ShutdownRequested()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        log.info("Starting Weibo Telegram Monitor")
        target_uids = parse_targets()
        cookie_store = load_cookie_store()
        cookie_dict = cookie_store.cookie_dict()
        if not cookie_dict:
            raise RuntimeError(
                "No cookies found; set WEIBO_COOKIES or restore data/auth_state.json"
            )

        log.info(f"Monitoring UIDs: {', '.join(target_uids)}")
        log.info(f"Poll interval: {POLL_INTERVAL}±{POLL_JITTER}s")
        log.info(f"First run mode: {FIRST_RUN_MODE}")

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        lock_file = (DATA_DIR / ".monitor.lock").open("a+")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "Another monitor process is already using this data directory"
            ) from exc
        VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
        state = load_state()
        state.setdefault("seen_ids", {})
        initialized_targets = {
            str(uid) for uid in state.get("initialized_targets", [])
        }
        for uid in target_uids:
            state["seen_ids"].setdefault(uid, [])
            if state["seen_ids"][uid]:
                initialized_targets.add(uid)
        state["initialized_targets"] = sorted(initialized_targets)

        configure_cookie_store(cookie_store)
        api_client = make_api_client(cookie_dict, cookie_store)
        outbox = DeliveryOutbox(OUTBOX_FILE)

        _touch_heartbeat()
        watchdog_thread = threading.Thread(
            target=_watchdog,
            name="monitor-watchdog",
            daemon=True,
        )
        watchdog_thread.start()

        cycle = 0
        next_cookie_refresh = 0.0

        while not _shutdown_event.is_set():
            cycle += 1
            cycle_failures = []
            try:
                log.info(f"=== Cycle {cycle} ===")
                _touch_heartbeat()

                pending_stats = process_outbox(
                    outbox,
                    state,
                    cookie_dict,
                    limit=OUTBOX_BATCH_SIZE,
                    progress=_touch_heartbeat,
                )
                if pending_stats.delivered or pending_stats.failed:
                    log.info(
                        f"Outbox before polling: {pending_stats.delivered} delivered, "
                        f"{pending_stats.failed} retained"
                    )

                if time.time() >= next_cookie_refresh:
                    _touch_heartbeat()
                    refreshed = refresh_weibo_session(target_uids[0] if target_uids else "")
                    api_client.sync_from_store()
                    refresh_jitter = min(max(COOKIE_REFRESH_INTERVAL // 6, 60), 3600)
                    if refreshed:
                        next_cookie_refresh = (
                            time.time()
                            + COOKIE_REFRESH_INTERVAL
                            + random.randint(-refresh_jitter, refresh_jitter)
                        )
                    else:
                        cycle_failures.append("cookie_refresh")
                        next_cookie_refresh = time.time() + min(
                            1800,
                            COOKIE_REFRESH_INTERVAL,
                        )

                for uid in target_uids:
                    if _shutdown_event.is_set():
                        break
                    _touch_heartbeat()
                    try:
                        first_run = uid not in initialized_targets
                        pages = FIRST_RUN_PAGES if first_run else POLL_MAX_PAGES
                        known_ids = None if first_run else state["seen_ids"].get(uid, [])
                        result = fetch_user_posts(
                            api_client,
                            uid,
                            max_pages=pages,
                            stop_ids=known_ids,
                        )
                        if not result.complete and not result.truncated:
                            cycle_failures.append(f"{uid}:{result.reason}")
                            log.error(
                                f"Post fetch incomplete for {uid}: {result.reason} "
                                f"after {result.pages_fetched} page(s)"
                            )
                            continue
                        if result.truncated and not first_run:
                            cycle_failures.append(f"{uid}:poll_page_limit")
                            log.error(
                                f"Post polling for {uid} reached the {POLL_MAX_PAGES}-page "
                                "safety limit before a known post; checkpoints were not advanced"
                            )
                            continue
                        if result.truncated:
                            log.warning(
                                f"First-run history for {uid} reached the "
                                f"{FIRST_RUN_PAGES}-page safety limit"
                            )

                        posts = result.items
                        seen = list(state["seen_ids"].get(uid, []))
                        seen_set = set(seen)
                        new_posts = []
                        for post in posts:
                            mid = extract_post_id(post)
                            if not mid or mid in seen_set:
                                continue
                            if is_like_collection(post):
                                seen.append(mid)
                                seen_set.add(mid)
                            else:
                                new_posts.append(post)
                                seen_set.add(mid)
                        new_posts.sort(
                            key=lambda post: int(extract_post_id(post))
                            if extract_post_id(post).isdigit()
                            else 0
                        )

                        state_dirty = False
                        if first_run and FIRST_RUN_MODE == "archive":
                            log.info(
                                f"First run (archive): recording {len(new_posts)} "
                                f"post IDs for {uid}"
                            )
                            for post in new_posts:
                                mid = extract_post_id(post)
                                seen.append(mid)
                                seen_set.add(mid)
                            new_posts = []
                        elif first_run:
                            log.info(
                                f"First run (push): queueing {len(new_posts)} posts for {uid}"
                            )

                        for post in new_posts:
                            mid = extract_post_id(post)
                            outbox.enqueue(
                                f"post:{uid}:{mid}",
                                "post",
                                {"uid": uid, "post": post},
                            )
                            seen.append(mid)
                            seen_set.add(mid)

                        state["seen_ids"][uid] = seen[-MAX_SEEN_IDS:]
                        initialized_targets.add(uid)
                        state["initialized_targets"] = sorted(initialized_targets)
                        state_dirty = True

                        profile_delta = detect_profile_changes(
                            api_client,
                            uid,
                            state.get("profiles", {}).get(uid, {}),
                        )
                        if profile_delta is not None:
                            if profile_delta.changes:
                                text = (
                                    f"#微博 #资料变更 #{_tag_for(uid, state)}\n"
                                    + "\n".join(
                                        html_escape(change)
                                        for change in profile_delta.changes
                                    )
                                )
                                outbox.enqueue(
                                    f"profile:{uid}:{_event_digest(profile_delta.snapshot)}",
                                    "telegram",
                                    {"method": "message", "text": text},
                                )
                            state.setdefault("profiles", {})[uid] = profile_delta.snapshot
                            state_dirty = True
                        else:
                            cycle_failures.append(f"{uid}:profile_incomplete")

                        last_follow_check = state.get(f"last_follow_check_{uid}", 0)
                        if time.time() - last_follow_check >= FOLLOW_CHECK_INTERVAL:
                            stored_key = f"follows_{uid}"
                            follow_delta = detect_follow_changes(
                                api_client,
                                uid,
                                state.get(stored_key),
                            )
                            if follow_delta is not None:
                                messages = []
                                tag = _tag_for(uid, state)
                                if follow_delta.new_follows:
                                    names = ", ".join(
                                        html_escape(name)
                                        for name in follow_delta.new_follows
                                    )
                                    messages.append(f"#微博 #新关注 #{tag}\n{names}")
                                if follow_delta.unfollows:
                                    names = ", ".join(
                                        html_escape(name)
                                        for name in follow_delta.unfollows
                                    )
                                    messages.append(f"#微博 #取关 #{tag}\n{names}")
                                if messages:
                                    outbox.enqueue(
                                        f"follow:{uid}:{_event_digest(follow_delta.snapshot)}",
                                        "telegram",
                                        {
                                            "method": "message",
                                            "text": "\n\n".join(messages),
                                        },
                                    )
                                state[stored_key] = follow_delta.snapshot
                                state[f"last_follow_check_{uid}"] = int(time.time())
                                state_dirty = True
                            else:
                                state[f"last_follow_check_{uid}"] = int(time.time())
                                state_dirty = True
                                cycle_failures.append(f"{uid}:follow_incomplete")

                        if state_dirty:
                            save_state(state)
                    except Exception as exc:
                        cycle_failures.append(f"{uid}:{type(exc).__name__}")
                        log.error(f"Error checking UID {uid}: {type(exc).__name__}")
                        traceback.print_exc()

                    if len(target_uids) > 1:
                        _interruptible_sleep(random.randint(2, 5))

                try:
                    comment_failures = poll_active_posts(api_client, state, outbox)
                    if comment_failures:
                        cycle_failures.append(f"comments:{comment_failures}_incomplete")
                except Exception as exc:
                    cycle_failures.append(f"comments:{type(exc).__name__}")
                    log.error(f"poll_active_posts failed: {type(exc).__name__}")
                    traceback.print_exc()

                delivery_stats = process_outbox(
                    outbox,
                    state,
                    cookie_dict,
                    limit=OUTBOX_BATCH_SIZE,
                    progress=_touch_heartbeat,
                )
                if delivery_stats.delivered or delivery_stats.failed:
                    log.info(
                        f"Outbox after polling: {delivery_stats.delivered} delivered, "
                        f"{delivery_stats.failed} retained, {outbox.pending_count()} pending"
                    )

                state["last_check"] = now_et().isoformat()
                save_state(state)
                _touch_heartbeat()
            except Exception as exc:
                cycle_failures.append(type(exc).__name__)
                log.error(f"Cycle {cycle} failed: {type(exc).__name__}")
                traceback.print_exc()

            if cycle_failures:
                _consecutive_failures += 1
                log.warning(
                    f"Cycle {cycle} had failures ({_consecutive_failures}x): "
                    f"{', '.join(cycle_failures[:5])}"
                )
                if _consecutive_failures >= 3:
                    _alert_tg(
                        "error",
                        "Weibo Telegram Monitor 连续失败\n"
                        f"连续失败: {_consecutive_failures}次\n"
                        f"类型: {html_escape(', '.join(cycle_failures[:3]))}\n\n"
                        "请检查监控状态",
                    )
            else:
                _consecutive_failures = 0

            if _shutdown_event.is_set():
                break
            sleep_time = max(
                60,
                POLL_INTERVAL + random.randint(-POLL_JITTER, POLL_JITTER),
            )
            if _consecutive_failures > 0:
                sleep_time = min(
                    sleep_time * (2 ** min(_consecutive_failures, 6)),
                    3600,
                )
                log.warning(
                    f"Backoff: sleeping {sleep_time}s after "
                    f"{_consecutive_failures} failed cycle(s)"
                )
            else:
                log.info(f"Sleeping {sleep_time}s...")
            _interruptible_sleep(int(sleep_time))

    except ShutdownRequested:
        pass
    finally:
        _shutdown_event.set()
        log.info("Shutting down...")
        if outbox is not None:
            try:
                outbox.close()
            except Exception as exc:
                log.debug(f"Closing outbox: {type(exc).__name__}")
        if api_client is not None:
            try:
                api_client.close()
            except Exception as exc:
                log.debug(f"Closing Weibo client: {type(exc).__name__}")
        try:
            close_media_client()
        except Exception as exc:
            log.debug(f"Closing media client: {type(exc).__name__}")
        try:
            cleanup_playwright()
        except Exception as exc:
            log.debug(f"Closing Playwright: {type(exc).__name__}")
        try:
            tg_client = get_tg_client_ref()
            if tg_client and not tg_client.is_closed:
                tg_client.close()
        except Exception as exc:
            log.debug(f"Closing Telegram client: {type(exc).__name__}")
        if watchdog_thread and watchdog_thread.is_alive():
            watchdog_thread.join(timeout=2)
        if lock_file is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
            except OSError as exc:
                log.debug(f"Releasing process lock: {type(exc).__name__}")
        log.info("Weibo Telegram Monitor stopped")
