"""Offline tests for open-source security boundaries."""

import os
import sys
import types
import unittest
from datetime import timezone

os.environ.setdefault("TG_BOT_TOKEN", "test-token")
os.environ.setdefault("TG_CHAT_ID", "123456")

if "pytz" not in sys.modules:
    try:
        __import__("pytz")
    except ImportError:
        sys.modules["pytz"] = types.SimpleNamespace(timezone=lambda _name: timezone.utc)

# The pure storage boundary tests do not need a live Pyrogram transport.
sys.modules.setdefault(
    "src.telegram.pyrogram",
    types.SimpleNamespace(pyro_send_video=lambda *_args, **_kwargs: False),
)

from src.api.client import _safe_url  # noqa: E402
from src.archive.storage import is_allowed_media_url, _safe_component  # noqa: E402


class SecurityBoundaryTests(unittest.TestCase):
    def test_safe_url_removes_credentials_and_query(self) -> None:
        result = _safe_url("https://user:secret@example.com/path?token=private#fragment")

        self.assertEqual(result, "https://example.com/path")

    def test_media_allowlist_requires_https_and_weibo_cdn(self) -> None:
        self.assertTrue(is_allowed_media_url("https://wx1.sinaimg.cn/large/file.jpg"))
        self.assertTrue(is_allowed_media_url("https://f.video.weibocdn.com/file.mp4"))
        self.assertFalse(is_allowed_media_url("http://wx1.sinaimg.cn/file.jpg"))
        self.assertFalse(is_allowed_media_url("https://example.com/file.jpg"))

    def test_path_component_cannot_escape_archive_directory(self) -> None:
        self.assertEqual(_safe_component("../../123/../../secret"), "______123_______secret")
        self.assertEqual(_safe_component("abc-DEF_123"), "abc-DEF_123")


if __name__ == "__main__":
    unittest.main()
