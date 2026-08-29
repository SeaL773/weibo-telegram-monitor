"""Tests for persistent cookie storage."""

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from src.api.cookie_store import CookieStore, parse_cookie_export

os.environ.setdefault("TG_BOT_TOKEN", "test")
os.environ.setdefault("TG_CHAT_ID", "test")
if "pytz" not in sys.modules:
    try:
        __import__("pytz")
    except ImportError:
        sys.modules["pytz"] = types.SimpleNamespace(timezone=lambda _name: timezone.utc)

from src.api.client import PersistentCookieClient, _check_response  # noqa: E402


class CookieStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "auth_state.json"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_browser_export_preserves_metadata(self) -> None:
        records = parse_cookie_export(json.dumps([{
            "name": "SUB",
            "value": "TOKEN",
            "domain": ".weibo.com",
            "path": "/",
            "expirationDate": 2_000_000_000.5,
            "secure": True,
            "httpOnly": True,
            "sameSite": "no_restriction",
        }]))

        self.assertEqual(records[0]["expires"], 2_000_000_000)
        self.assertEqual(records[0]["sameSite"], "None")
        self.assertTrue(records[0]["httpOnly"])

    def test_parse_browser_export_rejects_unrelated_domains(self) -> None:
        records = parse_cookie_export(json.dumps([
            {"name": "SUB", "value": "valid", "domain": ".weibo.com"},
            {"name": "SID", "value": "private", "domain": ".example.com"},
        ]))

        self.assertEqual([record["name"] for record in records], ["SUB"])

    def test_environment_is_only_used_for_first_boot(self) -> None:
        store = CookieStore(self.path)
        store.bootstrap('{"SUB":"first"}')

        reloaded = CookieStore(self.path)
        reloaded.bootstrap('{"SUB":"stale-env"}')

        self.assertEqual(reloaded.cookie_dict()["SUB"], "first")

    def test_playwright_merge_survives_reload(self) -> None:
        store = CookieStore(self.path)
        store.bootstrap('{"SUB":"old"}')
        store.merge_from_playwright([{
            "name": "SUB",
            "value": "rotated",
            "domain": ".weibo.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
            "expires": 2_000_000_000,
        }])

        reloaded = CookieStore(self.path)
        self.assertEqual(reloaded.cookie_dict()["SUB"], "rotated")
        self.assertEqual(reloaded.playwright_cookies()[0]["sameSite"], "Lax")

    def test_httpx_rotation_replaces_and_persists_jar(self) -> None:
        store = CookieStore(self.path)
        store.bootstrap('{"XSRF-TOKEN":"old"}')
        jar = httpx.Cookies()
        jar.set("XSRF-TOKEN", "new", domain=".weibo.com", path="/")
        jar.set("SUB", "session", domain=".weibo.com", path="/")

        changed = store.replace_from_httpx(jar.jar)

        self.assertTrue(changed)
        self.assertEqual(
            CookieStore(self.path).cookie_dict(),
            {"XSRF-TOKEN": "new", "SUB": "session"},
        )

    def test_stale_playwright_writer_cannot_overwrite_httpx_rotation(self) -> None:
        store = CookieStore(self.path)
        store.bootstrap('{"SUB":"old"}')
        context_revision, stale_context_cookies = store.snapshot()

        jar = httpx.Cookies()
        jar.set("SUB", "httpx-rotated", domain=".weibo.com", path="/")
        store.replace_from_httpx(jar.jar)

        accepted, revision = store.compare_and_merge_from_playwright(
            stale_context_cookies,
            expected_revision=context_revision,
        )

        self.assertFalse(accepted)
        self.assertEqual(revision, store.revision)
        self.assertEqual(store.cookie_dict()["SUB"], "httpx-rotated")

    def test_stale_httpx_writer_cannot_overwrite_browser_rotation(self) -> None:
        store = CookieStore(self.path)
        store.bootstrap('{"SUB":"old"}')
        request_revision, _ = store.snapshot()
        stale_jar = store.to_httpx_cookies()

        store.merge_from_playwright([{
            "name": "SUB",
            "value": "browser-rotated",
            "domain": ".weibo.com",
            "path": "/",
        }])

        accepted, revision = store.compare_and_replace_from_httpx(
            stale_jar.jar,
            expected_revision=request_revision,
        )

        self.assertFalse(accepted)
        self.assertEqual(revision, store.revision)
        self.assertEqual(store.cookie_dict()["SUB"], "browser-rotated")

    def test_revision_survives_reload(self) -> None:
        store = CookieStore(self.path)
        store.bootstrap('{"SUB":"first"}')
        original_revision = store.revision

        reloaded = CookieStore(self.path)

        self.assertEqual(reloaded.revision, original_revision)
        self.assertEqual(reloaded.snapshot()[0], original_revision)

    def test_client_persists_rotation_and_updates_xsrf_header(self) -> None:
        store = CookieStore(self.path)
        store.bootstrap('{"XSRF-TOKEN":"old"}')
        seen_xsrf = []
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            seen_xsrf.append(request.headers.get("X-XSRF-TOKEN"))
            headers = {}
            if call_count == 1:
                headers["set-cookie"] = (
                    "XSRF-TOKEN=new; Domain=.weibo.com; Path=/; Secure"
                )
            return httpx.Response(200, headers=headers, json={"ok": 1})

        mirror = store.cookie_dict()
        with PersistentCookieClient(
            store,
            mirror,
            transport=httpx.MockTransport(handler),
        ) as client:
            client.get("https://weibo.com/ajax/first")
            client.get("https://weibo.com/ajax/second")

        self.assertEqual(seen_xsrf, ["old", "new"])
        self.assertEqual(mirror["XSRF-TOKEN"], "new")
        self.assertEqual(CookieStore(self.path).cookie_dict()["XSRF-TOKEN"], "new")

    def test_client_adopts_browser_rotation_before_next_request(self) -> None:
        store = CookieStore(self.path)
        store.bootstrap('{"SUB":"old"}')
        seen_cookie_headers = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_cookie_headers.append(request.headers.get("cookie", ""))
            return httpx.Response(200, json={"ok": 1})

        mirror = store.cookie_dict()
        with PersistentCookieClient(
            store,
            mirror,
            transport=httpx.MockTransport(handler),
        ) as client:
            store.merge_from_playwright([{
                "name": "SUB",
                "value": "browser-rotated",
                "domain": ".weibo.com",
                "path": "/",
            }])
            client.get("https://weibo.com/ajax/test")

        self.assertEqual(seen_cookie_headers, ["SUB=browser-rotated"])
        self.assertEqual(store.cookie_dict()["SUB"], "browser-rotated")
        self.assertEqual(mirror["SUB"], "browser-rotated")

    def test_code_minus_101_is_classified_as_auth_failure(self) -> None:
        response = httpx.Response(
            200,
            json={"ok": 0, "code": "-101", "msg": "账号未登录"},
            request=httpx.Request("GET", "https://weibo.com/ajax/test"),
        )

        with patch("src.api.client._alert_tg") as alert:
            with self.assertRaisesRegex(RuntimeError, "auth"):
                _check_response(response, "42", context="cookie-check", raise_errors=True)

        alert.assert_called_once()
        self.assertIn("code=-101", alert.call_args.args[1])

    def test_nested_code_minus_101_is_classified_as_auth_failure(self) -> None:
        response = httpx.Response(
            200,
            json={"ok": 1, "data": {"code": -101, "msg": "账号未登录"}},
            request=httpx.Request("GET", "https://weibo.com/ajax/test"),
        )

        with patch("src.api.client._alert_tg") as alert:
            with self.assertRaisesRegex(RuntimeError, "auth"):
                _check_response(response, "42", context="cookie-check", raise_errors=True)

        alert.assert_called_once()

    def test_host_only_rotation_replaces_leading_dot_cookie(self) -> None:
        store = CookieStore(self.path)
        store.bootstrap(json.dumps([{
            "name": "SUB",
            "value": "old",
            "domain": ".weibo.com",
            "path": "/",
        }]))
        jar = httpx.Cookies()
        jar.set("SUB", "new", domain="weibo.com", path="/")

        store.replace_from_httpx(jar.jar)

        self.assertEqual(store.cookie_dict(), {"SUB": "new"})
        self.assertEqual(store.records()[0]["domain"], ".weibo.com")


if __name__ == "__main__":
    unittest.main()
