"""Regression tests for CookieStore and Playwright context coordination."""

import os
import sys
import tempfile
import types
import unittest
from datetime import timezone
from pathlib import Path
from unittest.mock import patch

import httpx

os.environ.setdefault("TG_BOT_TOKEN", "test")
os.environ.setdefault("TG_CHAT_ID", "test")

if "pytz" not in sys.modules:
    try:
        __import__("pytz")
    except ImportError:
        sys.modules["pytz"] = types.SimpleNamespace(
            timezone=lambda _name: timezone.utc,
        )

try:
    __import__("playwright.sync_api")
except ImportError:
    playwright_module = types.ModuleType("playwright")
    sync_api_module = types.ModuleType("playwright.sync_api")
    sync_api_module.sync_playwright = lambda: None
    playwright_module.sync_api = sync_api_module
    sys.modules["playwright"] = playwright_module
    sys.modules["playwright.sync_api"] = sync_api_module

from src.api.cookie_store import CookieStore  # noqa: E402
from src.screenshot import browser  # noqa: E402


class FakeContext:
    def __init__(self, cookies=None):
        self._cookies = list(cookies or [])
        self.added_cookies = []
        self.closed = False

    def cookies(self):
        return [dict(cookie) for cookie in self._cookies]

    def add_cookies(self, cookies):
        self.added_cookies = [dict(cookie) for cookie in cookies]
        self._cookies = [dict(cookie) for cookie in cookies]

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.contexts = []

    def is_connected(self):
        return True

    def new_context(self, **_kwargs):
        context = FakeContext()
        self.contexts.append(context)
        return context


class BrowserCookieCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = CookieStore(Path(self.temp_dir.name) / "auth_state.json")
        self.store.bootstrap('{"SUB":"old"}')
        self.fake_browser = FakeBrowser()

        browser._pw = object()
        browser._pw_browser = self.fake_browser
        browser._pw_context = None
        browser._pw_page_count = 0
        browser._pw_context_revision = None
        browser.configure_cookie_store(self.store)

    def tearDown(self):
        browser._pw = None
        browser._pw_browser = None
        browser._pw_context = None
        browser._pw_page_count = 0
        browser._pw_context_revision = None
        browser._cookie_store = None
        self.temp_dir.cleanup()

    @staticmethod
    def _cookie(value):
        return {
            "name": "SUB",
            "value": value,
            "domain": ".weibo.com",
            "path": "/",
        }

    def test_context_rotation_snapshots_after_saving_old_context(self):
        base_revision = self.store.revision
        old_context = FakeContext([self._cookie("browser-rotated")])
        browser._pw_context = old_context
        browser._pw_context_revision = base_revision
        browser._pw_page_count = browser._PW_MAX_PAGES

        with patch.object(browser.time, "sleep", return_value=None):
            new_context = browser._get_browser()

        self.assertTrue(old_context.closed)
        self.assertEqual(self.store.cookie_dict()["SUB"], "browser-rotated")
        self.assertEqual(
            new_context.added_cookies[0]["value"],
            "browser-rotated",
        )
        self.assertEqual(browser._pw_context_revision, self.store.revision)

    def test_stale_context_is_rejected_and_recreated_from_newer_store(self):
        context_revision = self.store.revision
        old_context = FakeContext([self._cookie("old")])
        browser._pw_context = old_context
        browser._pw_context_revision = context_revision

        jar = httpx.Cookies()
        jar.set("SUB", "httpx-rotated", domain=".weibo.com", path="/")
        self.store.replace_from_httpx(jar.jar)

        with patch.object(browser.time, "sleep", return_value=None):
            new_context = browser._get_browser()

        self.assertTrue(old_context.closed)
        self.assertEqual(self.store.cookie_dict()["SUB"], "httpx-rotated")
        self.assertEqual(
            new_context.added_cookies[0]["value"],
            "httpx-rotated",
        )
        self.assertEqual(browser._pw_context_revision, self.store.revision)


if __name__ == "__main__":
    unittest.main()
