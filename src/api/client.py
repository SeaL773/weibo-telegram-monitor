"""Weibo API client, response checking, cookie parsing."""

import sys
from typing import Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from src.core.config import (
    COOKIE_STATE_FILE,
    WEIBO_COOKIES_RAW,
    WEIBO_TARGETS,
    DESKTOP_UA,
)
from src.api.cookie_store import CookieStore, parse_cookie_export
from src.telegram.bot import _alert_tg
from src.utils.LoggerManager import logger as log

# ---------------------------------------------------------------------------
# Cookie parsing
# ---------------------------------------------------------------------------


def parse_cookies() -> tuple:
    """Parse WEIBO_COOKIES env var. Returns (cookie_dict, playwright_cookies)."""
    pw_cookies = parse_cookie_export(WEIBO_COOKIES_RAW)
    return {cookie["name"]: cookie["value"] for cookie in pw_cookies}, pw_cookies


def load_cookie_store() -> CookieStore:
    """Load persistent cookies, using the environment only on first boot."""
    store = CookieStore(COOKIE_STATE_FILE)
    store.bootstrap(WEIBO_COOKIES_RAW)
    return store


def parse_targets() -> List[str]:
    """Parse WEIBO_TARGETS env var (comma-separated UIDs)."""
    if not WEIBO_TARGETS:
        log.error("WEIBO_TARGETS not set!")
        sys.exit(1)
    targets = [uid.strip() for uid in WEIBO_TARGETS.split(",") if uid.strip()]
    if any(not uid.isdigit() for uid in targets):
        log.error("WEIBO_TARGETS must contain comma-separated numeric UIDs")
        sys.exit(1)
    return targets


def _safe_url(value: object) -> str:
    """Remove query strings and credentials before a URL reaches logs."""
    try:
        parts = urlsplit(str(value))
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except Exception:
        return "(invalid URL)"


# ---------------------------------------------------------------------------
# Weibo API client
# ---------------------------------------------------------------------------


class PersistentCookieClient(httpx.Client):
    """httpx client that persists Set-Cookie updates and refreshes XSRF."""

    def __init__(
        self,
        store: CookieStore,
        mirror: Dict[str, str],
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.cookie_store = store
        self.cookie_mirror = mirror
        super().__init__(
            headers={
                "User-Agent": DESKTOP_UA,
                "Referer": "https://weibo.com/",
                "X-Requested-With": "XMLHttpRequest",
            },
            cookies=store.to_httpx_cookies(),
            timeout=30,
            follow_redirects=True,
            transport=transport,
        )
        self._sync_views()

    def _sync_views(self) -> None:
        current = {cookie.name: cookie.value for cookie in self.cookies.jar}
        self.cookie_mirror.clear()
        self.cookie_mirror.update(current)
        self.headers["X-XSRF-TOKEN"] = current.get("XSRF-TOKEN", "")

    def sync_from_store(self) -> None:
        self.cookies.clear()
        for cookie in self.cookie_store.to_httpx_cookies().jar:
            self.cookies.jar.set_cookie(cookie)
        self._sync_views()

    def send(self, request, **kwargs):
        self._sync_views()
        response = super().send(request, **kwargs)
        self.cookie_store.replace_from_httpx(self.cookies.jar)
        self._sync_views()
        return response


def make_api_client(
    cookie_dict: Dict[str, str],
    cookie_store: Optional[CookieStore] = None,
) -> httpx.Client:
    if cookie_store is None:
        cookie_store = CookieStore(COOKIE_STATE_FILE)
        if not cookie_store.records():
            cookie_store.merge_from_playwright(
                {"name": name, "value": value, "domain": ".weibo.com", "path": "/"}
                for name, value in cookie_dict.items()
            )
    return PersistentCookieClient(cookie_store, cookie_dict)


# ---------------------------------------------------------------------------
# Alert / response checking
# ---------------------------------------------------------------------------

def _check_response(r, uid: str, context: str = "API") -> Optional[Dict]:
    """Check HTTP response for errors. Returns parsed JSON or None.
    Sends TG alerts for auth/rate/server errors."""

    # HTTP status code checks
    status = r.status_code
    if status == 302 or status == 301:
        location = r.headers.get("location", "")
        if "login" in location.lower() or "passport" in location.lower():
            log.error(f"[{context}] Redirected to login for {uid}: {_safe_url(location)}")
            _alert_tg("cookie",
                f"Weibo Monitor Cookie过期\n"
                f"UID: {uid}\n"
                f"{context}: 被重定向到登录页\n\n"
                f"请更新WEIBO_COOKIES")
            return None

    if status == 403:
        log.error(f"[{context}] 403 Forbidden for {uid}")
        _alert_tg("cookie",
            f"Weibo Monitor 403 Forbidden\n"
            f"UID: {uid}\n"
            f"{context}: 请求被拒绝, 可能Cookie失效或IP被封\n\n"
            f"请检查WEIBO_COOKIES")
        return None

    if status == 418:
        log.error(f"[{context}] 418 被风控 for {uid}")
        _alert_tg("rate",
            f"Weibo Monitor 被风控 (418)\n"
            f"UID: {uid}\n"
            f"{context}: 请求频率过高或被识别为爬虫\n\n"
            f"监控将自动退避")
        return None

    if status == 429:
        log.error(f"[{context}] 429 Rate Limited for {uid}")
        _alert_tg("rate",
            f"Weibo Monitor 请求限速 (429)\n"
            f"UID: {uid}\n"
            f"{context}: 被限制请求频率\n\n"
            f"监控将自动退避")
        return None

    if status >= 500:
        log.error(f"[{context}] Server error {status} for {uid}")
        return None

    if status != 200:
        log.warning(f"[{context}] Unexpected status {status} for {uid}")
        return None

    # Check if we were redirected to login (follow_redirects=True hides 302)
    final_url = str(r.url).lower()
    if "login.sina" in final_url or "passport.weibo" in final_url:
        log.error(f"[{context}] Redirected to login for {uid}: {_safe_url(r.url)}")
        _alert_tg("cookie",
            f"⚠️ Weibo Monitor Cookie过期\n"
            f"UID: {uid}\n"
            f"{context}: 被重定向到登录页\n\n"
            f"请更新WEIBO_COOKIES")
        return None

    # Parse JSON
    try:
        data = r.json()
    except Exception:
        log.error(f"[{context}] Non-JSON response for {uid} from {_safe_url(r.url)}")
        # Check if redirected to login page
        text_lower = r.text.lower() if r.text else ""
        if "passport" in text_lower or "login" in text_lower or "login.sina" in final_url:
            _alert_tg("cookie",
                f"⚠️ Weibo Monitor Cookie过期\n"
                f"UID: {uid}\n"
                f"{context}: 返回登录页面而非JSON\n\n"
                f"请更新WEIBO_COOKIES")
        return None

    # Check ok field
    ok = data.get("ok", 0)
    if ok != 1:
        msg = data.get("msg", "") or data.get("message", "")
        log.warning(f"[{context}] API error for {uid}: ok={ok}, msg={msg}")
        if "请登录" in msg or "login" in msg.lower() or ok == -100:
            _alert_tg("cookie",
                f"Weibo Monitor Cookie可能过期\n"
                f"UID: {uid}\n"
                f"{context}: {msg or f'ok={ok}'}\n\n"
                f"请更新WEIBO_COOKIES")
        elif "频繁" in msg or "操作过快" in msg:
            _alert_tg("rate",
                f"Weibo Monitor 操作频繁\n"
                f"UID: {uid}\n"
                f"{context}: {msg}\n\n"
                f"监控将自动退避")
        return None

    return data
