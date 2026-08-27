#!/usr/bin/env python3
"""Test push: fetch latest post and push to TG."""
import asyncio
import os
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
asyncio.set_event_loop(asyncio.new_event_loop())

if os.environ.get("RUN_LIVE_TESTS") != "1":
    raise SystemExit("Set RUN_LIVE_TESTS=1 to run this live Telegram push test")

import sys
sys.path.insert(0, ".")
from src.api.client import parse_cookies, parse_targets, make_api_client
from src.api.posts import fetch_user_posts, extract_post_id
from src.core.push import push_post

cookie_dict, pw_cookies = parse_cookies()
api_client = make_api_client(cookie_dict)
uids = parse_targets()

for uid in uids:
    fetch_result = fetch_user_posts(api_client, uid, max_pages=1)
    if not fetch_result.complete:
        print(f"Fetch failed for {uid}: {fetch_result.reason}")
        continue
    posts = fetch_result.items
    if posts:
        latest = posts[0]
        mid = extract_post_id(latest)
        print(f"Pushing: mid={mid} for uid={uid}")
        push_post(latest, uid, cookie_dict)
        break
    else:
        print(f"No posts for {uid}")
