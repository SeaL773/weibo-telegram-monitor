"""Persistent cookie storage shared by httpx and Playwright."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

import httpx

from src.utils.LoggerManager import logger as log


CookieRecord = Dict[str, Any]
ALLOWED_COOKIE_DOMAINS = ("weibo.com", "weibo.cn", "sina.com", "sina.com.cn")


def _is_allowed_domain(domain: str) -> bool:
    normalized = domain.lower().lstrip(".")
    return any(
        normalized == allowed or normalized.endswith(f".{allowed}")
        for allowed in ALLOWED_COOKIE_DOMAINS
    )


def _normalize_same_site(value: Any) -> Optional[str]:
    if not value:
        return None
    normalized = str(value).strip().lower().replace("_", "-")
    return {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no-restriction": "None",
        "unspecified": None,
    }.get(normalized)


def _normalize_cookie(raw: Mapping[str, Any]) -> Optional[CookieRecord]:
    name = str(raw.get("name", "")).strip()
    value = str(raw.get("value", ""))
    if not name:
        return None

    expires = raw.get("expires", raw.get("expirationDate"))
    try:
        expires = int(float(expires)) if expires not in (None, "", -1) else None
    except (TypeError, ValueError):
        expires = None

    record: CookieRecord = {
        "name": name,
        "value": value,
        "domain": str(raw.get("domain") or ".weibo.com"),
        "path": str(raw.get("path") or "/"),
        "secure": bool(raw.get("secure", True)),
        "httpOnly": bool(raw.get("httpOnly", raw.get("httponly", False))),
    }
    if not _is_allowed_domain(record["domain"]):
        log.warning(f"Ignored cookie for unsupported domain: {record['domain']}")
        return None
    if expires is not None:
        record["expires"] = expires
    same_site = _normalize_same_site(raw.get("sameSite", raw.get("samesite")))
    if same_site:
        record["sameSite"] = same_site
    return record


def parse_cookie_export(raw: str) -> List[CookieRecord]:
    """Parse common browser-extension cookie JSON formats."""
    try:
        payload = json.loads(raw or "[]")
    except Exception as exc:
        log.error(f"Failed to parse WEIBO_COOKIES: {exc}")
        return []

    if isinstance(payload, dict):
        payload = [
            {"name": name, "value": value, "domain": ".weibo.com", "path": "/"}
            for name, value in payload.items()
        ]
    if not isinstance(payload, list):
        log.error("WEIBO_COOKIES must be a JSON list or object")
        return []

    cookies = []
    for item in payload:
        if isinstance(item, Mapping):
            cookie = _normalize_cookie(item)
            if cookie:
                cookies.append(cookie)
    return cookies


class CookieStore:
    """Atomic JSON cookie store used as the single authentication source."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._cookies: Dict[tuple, CookieRecord] = {}
        self._load()

    @staticmethod
    def _key(cookie: Mapping[str, Any]) -> tuple:
        return (
            str(cookie.get("name", "")),
            str(cookie.get("domain", ".weibo.com")).lower(),
            str(cookie.get("path", "/")),
        )

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            records = payload.get("cookies", []) if isinstance(payload, dict) else payload
            for raw in records:
                if isinstance(raw, Mapping):
                    cookie = _normalize_cookie(raw)
                    if cookie:
                        self._cookies[self._key(cookie)] = cookie
            log.info(f"Loaded {len(self._cookies)} cookies from {self.path}")
        except Exception as exc:
            log.error(f"Failed to load persistent cookies from {self.path}: {exc}")

    def bootstrap(self, raw: str) -> None:
        """Seed from WEIBO_COOKIES only when no persistent state exists."""
        with self._lock:
            if self._cookies:
                return
            records = parse_cookie_export(raw)
            if not records:
                return
            self._cookies = {self._key(cookie): cookie for cookie in records}
            self._save_locked("environment bootstrap")

    def records(self) -> List[CookieRecord]:
        with self._lock:
            return [dict(cookie) for cookie in self._cookies.values()]

    def cookie_dict(self) -> Dict[str, str]:
        """Compatibility view for existing media download code."""
        with self._lock:
            return {cookie["name"]: cookie["value"] for cookie in self._cookies.values()}

    def playwright_cookies(self) -> List[CookieRecord]:
        return self.records()

    def to_httpx_cookies(self) -> httpx.Cookies:
        cookies = httpx.Cookies()
        with self._lock:
            for record in self._cookies.values():
                cookies.jar.set_cookie(self._to_cookiejar_cookie(record))
        return cookies

    @staticmethod
    def _to_cookiejar_cookie(record: Mapping[str, Any]) -> Cookie:
        domain = str(record.get("domain") or ".weibo.com")
        expires = record.get("expires")
        rest: Dict[str, Any] = {}
        if record.get("httpOnly"):
            rest["HttpOnly"] = True
        if record.get("sameSite"):
            rest["SameSite"] = record["sameSite"]
        return Cookie(
            version=0,
            name=str(record["name"]),
            value=str(record.get("value", "")),
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=domain.startswith("."),
            path=str(record.get("path") or "/"),
            path_specified=True,
            secure=bool(record.get("secure", True)),
            expires=int(expires) if expires is not None else None,
            discard=expires is None,
            comment=None,
            comment_url=None,
            rest=rest,
            rfc2109=False,
        )

    @staticmethod
    def _from_cookiejar(cookie: Cookie) -> CookieRecord:
        rest = getattr(cookie, "_rest", {}) or {}
        record: CookieRecord = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain or ".weibo.com",
            "path": cookie.path or "/",
            "secure": bool(cookie.secure),
            "httpOnly": "HttpOnly" in rest,
        }
        if cookie.expires is not None:
            record["expires"] = int(cookie.expires)
        same_site = _normalize_same_site(rest.get("SameSite"))
        if same_site:
            record["sameSite"] = same_site
        return record

    def replace_from_httpx(self, jar: CookieJar) -> bool:
        """Replace shared state with httpx's complete jar after each response."""
        records = [self._from_cookiejar(cookie) for cookie in jar]
        return self._replace(records, "httpx response")

    def merge_from_playwright(self, records: Iterable[Mapping[str, Any]]) -> bool:
        """Merge cookies updated by first-party page navigation."""
        changed = False
        with self._lock:
            for raw in records:
                cookie = _normalize_cookie(raw)
                if not cookie:
                    continue
                key = self._key(cookie)
                if self._cookies.get(key) != cookie:
                    self._cookies[key] = cookie
                    changed = True
            if changed:
                self._save_locked("Playwright navigation")
        return changed

    def _replace(self, records: Iterable[Mapping[str, Any]], reason: str) -> bool:
        normalized: Dict[tuple, CookieRecord] = {}
        for raw in records:
            cookie = _normalize_cookie(raw)
            if cookie:
                normalized[self._key(cookie)] = cookie
        with self._lock:
            if normalized == self._cookies:
                return False
            self._cookies = normalized
            self._save_locked(reason)
            return True

    def _save_locked(self, reason: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "cookies": list(self._cookies.values()),
        }
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(temp_path, 0o600)
        except OSError as exc:
            log.warning(f"Could not restrict cookie file permissions: {type(exc).__name__}")
        os.replace(temp_path, self.path)
        log.info(f"Persisted {len(self._cookies)} cookies after {reason}")
