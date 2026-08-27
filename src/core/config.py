"""All configuration: env vars, constants, and user tag mapping."""

import os
import sys
from pathlib import Path

import pytz


def _read_secret(name: str, default: str = "", required: bool = False) -> str:
    """Read a secret from NAME_FILE first, then from NAME."""
    file_path = os.environ.get(f"{name}_FILE", "").strip()
    if file_path:
        try:
            value = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Failed to read {name}_FILE") from exc
    else:
        value = os.environ.get(name, default).strip()
    if required and not value:
        raise RuntimeError(f"{name} is required")
    return value


def _read_int(
    name: str,
    default: int,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise RuntimeError(f"{name} must be {bounds}")
    return value


# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
WEIBO_COOKIES_RAW = _read_secret("WEIBO_COOKIES", "[]")
WEIBO_TARGETS = os.environ.get("WEIBO_TARGETS", "").strip()
TG_BOT_TOKEN = _read_secret("TG_BOT_TOKEN", required=True)
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
if not TG_CHAT_ID:
    raise RuntimeError("TG_CHAT_ID is required")
TG_API_ID = _read_int("TG_API_ID", 0)
TG_API_HASH = _read_secret("TG_API_HASH")
POLL_INTERVAL = _read_int("POLL_INTERVAL", 300, minimum=60)
POLL_JITTER = 60
FIRST_RUN_MODE = os.environ.get("FIRST_RUN_MODE", "archive").lower()
if FIRST_RUN_MODE not in {"archive", "push"}:
    raise RuntimeError("FIRST_RUN_MODE must be archive or push")

# ---------------------------------------------------------------------------
# Unified tag mapping: Weibo UID → hashtag name
# env: USER_TAGS="1234567890:ExampleUser"
# ---------------------------------------------------------------------------
_user_tag_raw = os.environ.get("USER_TAGS", "")
USER_TAG_MAP = {}
for _pair in _user_tag_raw.split(","):
    if ":" in _pair:
        _k, _v = _pair.split(":", 1)
        USER_TAG_MAP[_k.strip()] = _v.strip()


def get_user_tag(uid: str, fallback: str = "") -> str:
    return USER_TAG_MAP.get(uid, fallback)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ET = pytz.timezone("America/New_York")
MAX_SEEN_IDS = 500          # Max stored seen IDs per user
FIRST_RUN_PAGES = 50        # Pages to fetch on first run (archive all)
POLL_MAX_PAGES = _read_int("POLL_MAX_PAGES", 10, minimum=2, maximum=50)
PW_CONTEXT_MAX_PAGES = 5    # Playwright pages before context rotation
TG_CAPTION_MAX = 1024       # Telegram caption length limit
COMMENT_MONITOR_HOURS = 24  # Hours to monitor comments on new posts
FOLLOW_CHECK_INTERVAL = _read_int("FOLLOW_CHECK_INTERVAL", 1800, minimum=60)
COOKIE_REFRESH_INTERVAL = _read_int("COOKIE_REFRESH_INTERVAL", 21600, minimum=300)
MAX_IMAGE_BYTES = _read_int("MAX_IMAGE_BYTES", 20 * 1024 * 1024, minimum=1024)
MAX_VIDEO_BYTES = _read_int("MAX_VIDEO_BYTES", 500 * 1024 * 1024, minimum=1024)
HEALTH_TIMEOUT = _read_int("HEALTH_TIMEOUT", 1800, minimum=300)
OUTBOX_BATCH_SIZE = _read_int("OUTBOX_BATCH_SIZE", 20, minimum=1, maximum=200)
DATA_DIR = Path("data")
STATE_FILE = DATA_DIR / "state.json"
COOKIE_STATE_FILE = DATA_DIR / "auth_state.json"
OUTBOX_FILE = Path(os.environ.get("OUTBOX_FILE", str(DATA_DIR / "outbox.db")))
VIDEOS_DIR = DATA_DIR / "videos"
ARCHIVE_DIR = DATA_DIR / "archive"

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
