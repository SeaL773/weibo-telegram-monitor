"""Offline tests for paginated fetch completeness semantics."""

import os
import unittest
from unittest.mock import patch

import httpx

os.environ.setdefault("TG_BOT_TOKEN", "test-token")
os.environ.setdefault("TG_CHAT_ID", "123456")

from src.api.posts import fetch_user_posts  # noqa: E402
from src.api.profile import check_follow_changes, get_follow_list  # noqa: E402


def response(payload: dict) -> httpx.Response:
    request = httpx.Request("GET", "https://weibo.com/ajax/test")
    return httpx.Response(200, json=payload, request=request)


def follow_page(start: int, count: int = 20) -> httpx.Response:
    users = [
        {"id": start + index, "screen_name": f"user-{start + index}"}
        for index in range(count)
    ]
    return response({"ok": 1, "users": users})


def post_page(*ids: int) -> httpx.Response:
    return response({"ok": 1, "data": {"list": [{"id": item} for item in ids]}})


class FakeClient:
    def __init__(self, results):
        self.results = iter(results)

    def get(self, _url, params=None):
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result


class FollowFetchTests(unittest.TestCase):
    @patch("src.api.profile.time.sleep", return_value=None)
    def test_mid_pagination_error_is_not_a_complete_snapshot(self, _sleep) -> None:
        client = FakeClient([follow_page(1), httpx.ReadTimeout("timeout")])

        result = get_follow_list(client, "42")

        self.assertFalse(result.complete)
        self.assertFalse(result.truncated)
        self.assertEqual(result.reason, "request_error")
        self.assertEqual(result.pages_fetched, 1)
        self.assertEqual(len(result.items), 20)

    @patch("src.api.profile.time.sleep", return_value=None)
    def test_incomplete_follow_fetch_does_not_replace_state(self, _sleep) -> None:
        original = [{"id": "999", "screen_name": "original"}]
        state = {"follows_42": list(original)}
        client = FakeClient([follow_page(1), httpx.ReadTimeout("timeout")])

        changes = check_follow_changes(client, "42", state)

        self.assertEqual(changes, ([], []))
        self.assertEqual(state["follows_42"], original)

    def test_valid_empty_follow_list_reports_unfollows(self) -> None:
        state = {"follows_42": [{"id": "1", "screen_name": "old-user"}]}
        client = FakeClient([response({"ok": 1, "users": []})])

        new_follows, unfollows = check_follow_changes(client, "42", state)

        self.assertEqual(new_follows, [])
        self.assertEqual(unfollows, ["old-user"])
        self.assertEqual(state["follows_42"], [])

    def test_empty_first_snapshot_is_stored_as_a_real_baseline(self) -> None:
        state = {}

        initial = check_follow_changes(
            FakeClient([response({"ok": 1, "users": []})]),
            "42",
            state,
        )
        later = check_follow_changes(FakeClient([follow_page(7, count=1)]), "42", state)

        self.assertEqual(initial, ([], []))
        self.assertEqual(later, (["user-7"], []))

    @patch("src.api.profile.time.sleep", return_value=None)
    def test_full_twentieth_page_is_marked_truncated(self, _sleep) -> None:
        client = FakeClient([follow_page(page * 20) for page in range(20)])

        result = get_follow_list(client, "42")

        self.assertFalse(result.complete)
        self.assertTrue(result.truncated)
        self.assertEqual(result.reason, "page_limit")
        self.assertEqual(result.pages_fetched, 20)
        self.assertEqual(len(result.items), 400)


class PostFetchTests(unittest.TestCase):
    def test_single_page_poll_is_a_complete_requested_window(self) -> None:
        result = fetch_user_posts(FakeClient([post_page(1, 2)]), "42")

        self.assertTrue(result.complete)
        self.assertEqual(result.reason, "requested_window")
        self.assertEqual([post["id"] for post in result.items], [1, 2])

    def test_valid_empty_post_page_is_complete(self) -> None:
        result = fetch_user_posts(FakeClient([post_page()]), "42", max_pages=50)

        self.assertTrue(result.complete)
        self.assertEqual(result.reason, "exhausted")
        self.assertEqual(result.items, [])

    @patch("src.api.posts.time.sleep", return_value=None)
    def test_regular_poll_pages_until_known_high_watermark(self, _sleep) -> None:
        result = fetch_user_posts(
            FakeClient([post_page(9, 8), post_page(7, 6)]),
            "42",
            max_pages=10,
            stop_ids={"7"},
        )

        self.assertTrue(result.complete)
        self.assertEqual(result.reason, "high_watermark")
        self.assertEqual(result.pages_fetched, 2)
        self.assertEqual([post["id"] for post in result.items], [9, 8, 7, 6])

    @patch("src.api.posts.time.sleep", return_value=None)
    def test_regular_poll_limit_does_not_claim_completeness(self, _sleep) -> None:
        result = fetch_user_posts(
            FakeClient([post_page(9), post_page(8)]),
            "42",
            max_pages=2,
            stop_ids={"1"},
        )

        self.assertFalse(result.complete)
        self.assertTrue(result.truncated)
        self.assertEqual(result.reason, "page_limit")

    @patch("src.api.posts.time.sleep", return_value=None)
    def test_first_run_request_error_preserves_partial_semantics(self, _sleep) -> None:
        client = FakeClient([post_page(1), httpx.ReadTimeout("timeout")])

        result = fetch_user_posts(client, "42", max_pages=50)

        self.assertFalse(result.complete)
        self.assertFalse(result.truncated)
        self.assertEqual(result.reason, "request_error")
        self.assertEqual([post["id"] for post in result.items], [1])

    @patch("src.api.posts.time.sleep", return_value=None)
    def test_first_run_page_limit_is_explicitly_truncated(self, _sleep) -> None:
        result = fetch_user_posts(
            FakeClient([post_page(1), post_page(2)]),
            "42",
            max_pages=2,
        )

        self.assertFalse(result.complete)
        self.assertTrue(result.truncated)
        self.assertEqual(result.reason, "page_limit")
        self.assertEqual([post["id"] for post in result.items], [1, 2])

    def test_malformed_post_payload_is_incomplete(self) -> None:
        result = fetch_user_posts(
            FakeClient([response({"ok": 1, "data": {}})]),
            "42",
        )

        self.assertFalse(result.complete)
        self.assertEqual(result.reason, "invalid_payload")


if __name__ == "__main__":
    unittest.main()
