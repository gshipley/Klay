"""Only a complete launcher scan can establish that a game was uninstalled."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import klay
from klay.qt import import_worker


class ImportReconciliationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.shared = types.ModuleType("klay.shared")
        self.shared.data_dir = self.root
        self.settings = Mock()
        self.settings.source_enabled.return_value = True
        self.settings.get_bool.side_effect = lambda key, default=False: default
        self.settings.get_state_bool.side_effect = lambda key, default=False: default
        self.games = self.root / "klay" / "games"
        self.games.mkdir(parents=True)
        for source in ("nexus", "steam"):
            (self.games / f"{source}_one.json").write_text(json.dumps({
                "game_id": f"{source}_one", "source": source,
                "name": "Existing game", "executable": "launcher", "removed": False,
            }))

    def scan(self, sources):
        with (
            patch.dict(sys.modules, {"klay.shared": self.shared}),
            patch.object(klay, "shared", self.shared, create=True),
            patch.dict(os.environ, {"KLAY_IMPORT_MODE": "import", "KLAY_IMPORT_FAST": "1",
                                    "KLAY_DATA_DIR_NAME": "klay"}),
            patch.object(import_worker, "SettingsBackend", return_value=self.settings),
            patch.object(import_worker, "_load_source_classes", return_value=sources),
            patch.object(import_worker, "_emit_progress"),
            patch.object(import_worker, "_attempt_cover_update", return_value=(False, False)),
        ):
            return import_worker._run_import()

    def removed(self, source):
        return json.loads((self.games / f"{source}_one.json").read_text())["removed"]

    def test_successful_empty_scan_removes_only_its_source(self):
        class EmptySource:
            source_id = "nexus"

            def __iter__(self):
                return iter(())

        summary = self.scan([EmptySource])
        self.assertEqual(summary["removed"], 1)
        self.assertTrue(self.removed("nexus"))
        self.assertFalse(self.removed("steam"))

    def test_read_failure_preserves_games_and_reports_error(self):
        class UnreadableSource:
            source_id = "nexus"

            def __iter__(self):
                raise ValueError("Invalid launcher-state.json")

        summary = self.scan([UnreadableSource])
        self.assertEqual(summary["removed"], 0)
        self.assertIn("Invalid launcher-state.json", summary["errors"][0])
        self.assertFalse(self.removed("nexus"))

    def test_partial_scan_failure_preserves_unseen_games(self):
        class PartialSource:
            source_id = "nexus"

            def __iter__(self):
                yield import_worker.FakeGame({
                    "game_id": "nexus_two", "source": "nexus",
                    "name": "New game", "executable": "launcher two",
                })
                raise OSError("Library temporarily unavailable")

        summary = self.scan([PartialSource])
        self.assertEqual(summary["imported"], 1)
        self.assertEqual(summary["removed"], 0)
        self.assertTrue(summary["errors"])
        self.assertFalse(self.removed("nexus"))

    def test_initialization_failure_preserves_existing_games(self):
        class BrokenSource:
            source_id = "nexus"

            def __init__(self):
                raise OSError("Profile not available")

        self.assertEqual(self.scan([BrokenSource])["removed"], 0)
        self.assertFalse(self.removed("nexus"))

    def test_disabled_source_is_not_removed(self):
        self.settings.source_enabled.return_value = False

        class DisabledSource:
            source_id = "nexus"

            def __iter__(self):
                raise AssertionError("Disabled source was scanned")

        summary = self.scan([DisabledSource])
        self.assertEqual(summary["removed"], 0)
        self.assertEqual(summary["sources_scanned"], 0)
        self.assertEqual(summary["errors"], [])
        self.assertFalse(self.removed("nexus"))


if __name__ == "__main__":
    unittest.main()
