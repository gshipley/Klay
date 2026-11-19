"""Nexus discovery and command construction without accounts or game launches."""
from __future__ import annotations

import builtins
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import klay


class NexusSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.profile = self.root / "config" / "xodus-nexus"
        self.profile.mkdir(parents=True)
        self.data = self.root / "data"
        self.game_dir = self.root / "Game's $money $(never-run)"
        self.game_dir.mkdir()
        self.game_id = "19223a2a7f0d1706"
        self.settings = Mock()
        self.settings.get_string.return_value = str(self.profile)
        self.shared = types.ModuleType("klay.shared")
        self.shared.host_config_dir = self.profile.parent
        self.shared.host_data_dir = self.data
        self.shared.import_time = 123
        self.shared.schema = self.settings
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, {"klay.shared": self.shared}).start()
        patch.object(klay, "shared", self.shared, create=True).start()
        patch.object(builtins, "_", lambda value: value, create=True).start()
        self.module = importlib.import_module("klay.importer.nexus_source")
        self.location_module = importlib.import_module("klay.importer.location")
        patch.object(self.module, "shared", self.shared).start()
        patch.object(self.location_module, "shared", self.shared).start()
        self.desktop_lookup = patch.object(self.module, "_desktop_launcher", return_value=None)
        self.desktop_mock = self.desktop_lookup.start()
        self.wrapper = self.profile / "shortcuts" / "launch-game"
        self.wrapper.parent.mkdir()
        self.wrapper.write_text('#!/bin/sh\n# Not executed by these tests.\n', encoding="utf-8")
        self.record = {
            "id": self.game_id, "title": "A Game", "path": str(self.game_dir),
            "installState": "ready", "publisher": "Publisher",
        }
        self.write_library([self.record])

    def write_library(self, games):
        (self.profile / "launcher-state.json").write_text(json.dumps({"games": games}), encoding="utf-8")

    def scan(self):
        return list(self.module.NexusSource())

    def test_imports_ready_game_without_per_game_desktop_shortcut(self):
        game, extra = self.scan()[0]
        digest = sha256(os.fsencode(self.profile.resolve())).hexdigest()[:12]
        self.assertEqual(game.game_id, f"nexus_{digest}_{self.game_id}")
        self.assertEqual(game.source, "nexus")
        self.assertEqual(game.name, "A Game")
        self.assertEqual(game.publisher, "Publisher")
        self.assertEqual(game.added, 123)
        self.assertEqual(extra, {"store": "xbox"})
        self.assertEqual(shlex.split(game.executable), ["/bin/sh", str(self.wrapper), self.game_id])

    def test_command_keeps_spaces_quotes_and_shell_metacharacters_literal(self):
        renamed = self.profile.with_name("Nexus 'quoted' $HOME $(never-run); profile")
        self.profile.rename(renamed)
        self.profile = renamed
        self.settings.get_string.return_value = str(renamed)
        game, _ = self.scan()[0]
        self.assertEqual(shlex.split(game.executable), [
            "/bin/sh", str(renamed / "shortcuts" / "launch-game"), self.game_id,
        ])

    def test_profile_symlinks_share_identity(self):
        original, _ = self.scan()[0]
        alias = self.root / "home-config-alias"
        alias.symlink_to(self.profile, target_is_directory=True)
        self.settings.get_string.return_value = str(alias)
        aliased, _ = self.scan()[0]
        self.assertEqual(original.game_id, aliased.game_id)
        self.assertEqual(original.executable, aliased.executable)

    def test_same_local_id_in_distinct_profiles_stays_distinct(self):
        first, _ = self.scan()[0]
        other = self.root / "other-profile"
        other.mkdir()
        (other / "launcher-state.json").write_text(json.dumps({"games": [self.record]}))
        (other / "shortcuts").mkdir()
        (other / "shortcuts" / "launch-game").write_text("#!/bin/sh\n")
        self.settings.get_string.return_value = str(other)
        second, _ = self.scan()[0]
        self.assertNotEqual(first.game_id, second.game_id)

    def test_only_ready_existing_installations_are_imported(self):
        for patch_values in (
            {"installState": "incomplete"}, {"installState": "imported"},
            {"updateIncomplete": True}, {"path": str(self.root / "missing")},
            {"path": str(self.wrapper)},
        ):
            with self.subTest(patch_values=patch_values):
                self.write_library([{**self.record, **patch_values}])
                self.assertEqual(self.scan(), [])

    def test_empty_library_does_not_require_launcher(self):
        self.wrapper.unlink()
        self.write_library([])
        self.assertEqual(self.scan(), [])

    def test_malformed_library_raises_instead_of_reporting_empty(self):
        for value in (None, [], {}, {"games": {}}, {"games": [None]}):
            with self.subTest(value=value):
                (self.profile / "launcher-state.json").write_text(json.dumps(value))
                with self.assertRaises(self.module.NexusLibraryError):
                    self.scan()
        (self.profile / "launcher-state.json").write_text('{"games":')
        with self.assertRaises(self.module.NexusLibraryError):
            self.scan()

    def test_invalid_ready_records_fail_before_any_game_is_yielded(self):
        for changed in (
            {"id": "../bad"}, {"id": self.game_id.upper()}, {"id": 17},
            {"path": "relative/game"}, {"path": "bad\x00path"}, {"title": None},
            {"title": ""}, {"installState": []}, {"installState": "unknown"},
        ):
            with self.subTest(changed=changed):
                self.write_library([self.record, {**self.record, **changed}])
                source = iter(self.module.NexusSource())
                with self.assertRaises(self.module.NexusLibraryError):
                    next(source)

    def test_duplicate_identity_is_deduplicated_or_rejected_if_conflicting(self):
        self.write_library([self.record, dict(self.record)])
        self.assertEqual(len(self.scan()), 1)
        self.write_library([self.record, {**self.record, "title": "Conflicting"}])
        with self.assertRaises(self.module.NexusLibraryError):
            self.scan()

    def test_existing_no_sandbox_flag_follows_wrapper_game_id(self):
        self.desktop_mock.return_value = ["/some/Nexus.AppImage", "--no-sandbox"]
        game, _ = self.scan()[0]
        self.assertEqual(shlex.split(game.executable), [
            "/bin/sh", str(self.wrapper), self.game_id, "--no-sandbox",
        ])

    def test_missing_game_wrapper_uses_launcher_wrapper(self):
        self.wrapper.unlink()
        launcher = self.wrapper.with_name("launch-nexus")
        launcher.write_text("#!/bin/sh\n")
        game, _ = self.scan()[0]
        self.assertEqual(shlex.split(game.executable), [
            "/bin/sh", str(launcher), "--launch-game", self.game_id,
        ])

    def test_missing_wrappers_use_installed_launcher_and_explicit_profile(self):
        self.wrapper.unlink()
        self.desktop_mock.return_value = ["/path with spaces/Nexus-Launcher.AppImage", "--no-sandbox"]
        game, _ = self.scan()[0]
        self.assertEqual(shlex.split(game.executable), [
            "/path with spaces/Nexus-Launcher.AppImage", "--no-sandbox",
            "--nexus-profile", str(self.profile), "--launch-game", self.game_id,
        ])

    def test_missing_launcher_is_a_scan_failure(self):
        self.wrapper.unlink()
        with self.assertRaisesRegex(self.module.NexusLibraryError, "Open Nexus once"):
            self.scan()

    def test_https_cover_metadata_and_cached_icon_fallback(self):
        self.write_library([{**self.record, "coverUrl": "https://images.example/game.png"}])
        _, extra = self.scan()[0]
        self.assertEqual(extra["online_cover_url"], "https://images.example/game.png")
        icons = self.wrapper.parent / "icons"
        icons.mkdir()
        icon = icons / f"{self.game_id}.png"
        icon.write_bytes(b"image")
        for url in ("file:///etc/passwd", "http://images.example/cover", "https://[invalid", None):
            with self.subTest(url=url):
                self.write_library([{**self.record, "coverUrl": url}])
                _, extra = self.scan()[0]
                self.assertNotIn("online_cover_url", extra)
                self.assertEqual(extra["local_icon_path"], icon)

    def test_desktop_fallback_accepts_known_launcher_and_only_explicit_sandbox_flag(self):
        self.desktop_lookup.stop()
        self.wrapper.unlink()
        launcher = self.root / "App 'Images'" / "Nexus-Launcher-0.5.1-x86_64.appimage"
        launcher.parent.mkdir()
        launcher.write_text("#!/bin/sh\nexit 99\n")
        launcher.chmod(0o700)
        applications = self.data / "applications"
        applications.mkdir(parents=True)
        desktop = applications / "xodus-nexus.desktop"
        desktop.write_text('[Desktop Entry]\nExec=' + shlex.quote(str(launcher)) + ' --no-sandbox %U\n')
        game, _ = self.scan()[0]
        self.assertEqual(shlex.split(game.executable), [
            str(launcher), "--no-sandbox", "--nexus-profile", str(self.profile),
            "--launch-game", self.game_id,
        ])
        desktop.write_text('[Desktop Entry]\nExec=/bin/sh -c "touch something"\n')
        with self.assertRaises(self.module.NexusLibraryError):
            self.scan()


if __name__ == "__main__":
    unittest.main()
