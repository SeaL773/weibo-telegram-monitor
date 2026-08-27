"""Tests for persistent cookie storage."""

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import timezone
from pathlib import Path

import httpx

from src.api.cookie_store import CookieStore, parse_cookie_export

os.environ.setdefault("TG_BOT_TOKEN", "test")
os.environ.setdefault("TG_CHAT_ID", "test")
if "pytz" not in sys.modules:
    try:
        __import__("pytz")
    except ImportError:
        sys.modules["pytz"] = types.SimpleNamespace(timezone=lambda _name: timezone.utc)

from src.api.client import PersistentCookieClient  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
