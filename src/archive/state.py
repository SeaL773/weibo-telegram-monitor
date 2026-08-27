"""State persistence (load/save)."""

import json
import os
from src.utils.LoggerManager import logger as log
from pathlib import Path
from typing import Dict

from src.core.config import DATA_DIR, STATE_FILE



def load_state() -> Dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.debug(f"Failed to parse state file: {e}")
    return {"seen_ids": {}, "profiles": {}, "last_check": ""}


def save_state(state: Dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(STATE_FILE))
