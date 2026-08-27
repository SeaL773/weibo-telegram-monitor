"""Utility functions: HTML escaping, time, base62 encoding."""

import html as html_mod
from datetime import datetime

from src.core.config import ET


def html_escape(s: str) -> str:
    return html_mod.escape(str(s))


def now_et():
    return datetime.now(ET)


# Base62 encoding for Weibo mid (numeric ID → base62 URL slug)
_BASE62_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _int_to_base62(n: int) -> str:
    if n == 0:
        return "0"
    result = []
    while n > 0:
        result.append(_BASE62_CHARS[n % 62])
        n //= 62
    return "".join(reversed(result))


def mid_to_base62(mid: str) -> str:
    """Convert numeric mid to base62 for URL (same algorithm as Weibo).
    Split mid into 7-char groups from right, convert each to base62, pad to 4 chars."""
    mid = str(mid)
    result = []
    # Process from right in chunks of 7
    i = len(mid)
    while i > 0:
        start = max(0, i - 7)
        chunk = mid[start:i]
        b62 = _int_to_base62(int(chunk))
        # Pad to 4 chars except the leftmost group
        if start > 0:
            b62 = b62.zfill(4)
        result.append(b62)
        i = start
    result.reverse()
    return "".join(result)
