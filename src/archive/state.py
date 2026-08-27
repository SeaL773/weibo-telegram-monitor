"""State persistence (load/save)."""

import json
import os
import time
from src.utils.LoggerManager import logger as log
from pathlib import Path
from typing import Any, Dict

from src.core.config import DATA_DIR, STATE_FILE

STATE_SCHEMA_VERSION = 1


def default_state() -> Dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "seen_ids": {},
        "initialized_targets": [],
        "profiles": {},
        "active_posts": {},
        "last_check": "",
    }


def atomic_write_json(path: Path, payload: Any) -> None:
    """Durably replace a JSON file without exposing a partially written target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp_path, path)


def _validate_state(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("state root must be an object")
    state = default_state()
    state.update(payload)
    if not isinstance(state.get("seen_ids"), dict):
        raise ValueError("seen_ids must be an object")
    if not isinstance(state.get("initialized_targets"), list):
        raise ValueError("initialized_targets must be an array")
    if not isinstance(state.get("profiles"), dict):
        raise ValueError("profiles must be an object")
    if not isinstance(state.get("active_posts"), dict):
        raise ValueError("active_posts must be an object")
    for uid, seen in state["seen_ids"].items():
        if not isinstance(uid, str) or not isinstance(seen, list):
            raise ValueError("seen_ids entries must map string UIDs to arrays")
        if any(not isinstance(mid, str) for mid in seen):
            raise ValueError("seen_ids arrays must contain strings")
    if any(not isinstance(uid, str) for uid in state["initialized_targets"]):
        raise ValueError("initialized_targets must contain strings")
    for uid, profile in state["profiles"].items():
        if not isinstance(uid, str) or not isinstance(profile, dict):
            raise ValueError("profiles entries must map string UIDs to objects")
    for key, value in state.items():
        if key.startswith("follows_"):
            if not isinstance(value, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not isinstance(item.get("screen_name", ""), str)
                for item in value
            ):
                raise ValueError(f"{key} must be an array of follow objects")
    for mid, active_post in state["active_posts"].items():
        if not isinstance(mid, str) or not isinstance(active_post, dict):
            raise ValueError("active_posts entries must map string IDs to objects")
        if not isinstance(active_post.get("uid"), str):
            raise ValueError("active post uid must be a string")
        if not isinstance(active_post.get("tg_message_id"), int):
            raise ValueError("active post tg_message_id must be an integer")
    version = int(state.get("schema_version", 0) or 0)
    if version > STATE_SCHEMA_VERSION:
        raise ValueError(f"unsupported state schema version {version}")
    state["schema_version"] = STATE_SCHEMA_VERSION
    return state

def load_state() -> Dict:
    if STATE_FILE.exists():
        try:
            return _validate_state(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            corrupt_path = STATE_FILE.with_name(
                f"{STATE_FILE.name}.corrupt-{int(time.time())}"
            )
            try:
                os.replace(STATE_FILE, corrupt_path)
                log.error(
                    f"Invalid state file quarantined as {corrupt_path.name}: "
                    f"{type(e).__name__}"
                )
            except OSError as quarantine_error:
                log.error(
                    "Invalid state file could not be quarantined: "
                    f"{type(quarantine_error).__name__}"
                )
    return default_state()


def save_state(state: Dict):
    state["schema_version"] = STATE_SCHEMA_VERSION
    atomic_write_json(STATE_FILE, state)
