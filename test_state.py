"""Offline tests for durable monitor state."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import src.archive.state as state_module


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "state.json"
        self.path_patch = patch.object(state_module, "STATE_FILE", self.path)
        self.data_patch = patch.object(state_module, "DATA_DIR", self.path.parent)
        self.path_patch.start()
        self.data_patch.start()

    def tearDown(self) -> None:
        self.data_patch.stop()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_legacy_state_is_migrated_to_current_schema(self) -> None:
        self.path.write_text('{"seen_ids":{},"profiles":{}}', encoding="utf-8")

        state = state_module.load_state()

        self.assertEqual(state["schema_version"], state_module.STATE_SCHEMA_VERSION)
        self.assertEqual(state["active_posts"], {})

    def test_invalid_state_is_quarantined_instead_of_overwritten(self) -> None:
        self.path.write_text("{broken", encoding="utf-8")

        state = state_module.load_state()

        self.assertEqual(state["seen_ids"], {})
        self.assertFalse(self.path.exists())
        self.assertEqual(len(list(self.path.parent.glob("state.json.corrupt-*"))), 1)

    def test_save_state_replaces_complete_json(self) -> None:
        state = state_module.default_state()
        state["seen_ids"] = {"1": ["2"]}

        state_module.save_state(state)

        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["seen_ids"], {"1": ["2"]})
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_invalid_nested_state_is_quarantined(self) -> None:
        self.path.write_text(
            '{"seen_ids":{"1":"not-an-array"},"profiles":{},"active_posts":{}}',
            encoding="utf-8",
        )

        state = state_module.load_state()

        self.assertEqual(state, state_module.default_state())
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
