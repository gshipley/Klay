"""Expand previously truncated Unreal copies without replacing private saves."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from klay.qt import jail_helper as helper


class JailLayoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="klay-layout-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.source = self.root / "installed-game"
        self.binary = self.source / "Project/Binaries/Win64"
        self.binary.mkdir(parents=True)
        (self.binary / "Game.exe").write_bytes(b"original fixture")
        self.runner = self.root / "trusted-proton"
        self.runner.mkdir()
        (self.runner / "proton").write_text("fixture must never run")
        (self.runner / "proton").chmod(0o700)
        self.raw = {"enabled": True, "game_dir": str(self.binary), "exe": "Game.exe", "runner_type": "proton", "runner": str(self.runner), "arguments": []}
        self.store = helper.Store("layout-test", "klay-test", home=self.home)
        self.addCleanup(self.store.close)
        helper.prepare_game(self.store, helper.validate_config(self.raw))
        self.store.lock()
        (self.store.path / "game/Game.exe").write_bytes(b"private modified executable")
        (self.store.path / "game/local-save.dat").write_bytes(b"private game save")
        (self.store.path / "prefix/save.dat").write_bytes(b"private prefix save")
        (self.store.path / "home/settings.dat").write_bytes(b"private home settings")
        (self.source / "Engine").mkdir()
        (self.source / "Engine/engine.pak").write_bytes(b"engine fixture")
        (self.source / "Project/Content").mkdir()
        (self.source / "Project/Content/content.pak").write_bytes(b"content fixture")
        self.config = helper.validate_config(self.raw)

    def assert_preserved(self):
        self.assertEqual((self.store.path / "game/Project/Binaries/Win64/Game.exe").read_bytes(), b"private modified executable")
        self.assertEqual((self.store.path / "game/Project/Binaries/Win64/local-save.dat").read_bytes(), b"private game save")
        self.assertEqual((self.store.path / "prefix/save.dat").read_bytes(), b"private prefix save")
        self.assertEqual((self.store.path / "home/settings.dat").read_bytes(), b"private home settings")
        self.assertEqual((self.store.path / "game/Engine/engine.pak").read_bytes(), b"engine fixture")
        self.assertEqual((self.store.path / "game/Project/Content/content.pak").read_bytes(), b"content fixture")
        self.assertEqual(self.store.read_json("snapshot.json")["source"], str(self.source))
        self.assertFalse((self.store.path / "layout-transaction.json").exists())

    def test_saved_binary_root_settings_rebase_executable_and_working_directory(self):
        self.assertEqual(self.config["game_dir"], str(self.source))
        self.assertEqual(self.config["exe"], "Project/Binaries/Win64/Game.exe")
        self.assertEqual(self.config["working_dir"], "Project/Binaries/Win64")
        nested = self.binary / "working"
        nested.mkdir()
        config = helper.validate_config(dict(self.raw, working_dir="working"))
        self.assertEqual(config["working_dir"], "Project/Binaries/Win64/working")

    def test_expansion_preserves_modified_copy_and_both_save_locations(self):
        before = [(self.store.path / name).stat().st_ino for name in ("game", "prefix", "home")]
        helper.prepare_game(self.store, self.config)
        self.assert_preserved()
        after = [(self.store.path / name).stat().st_ino for name in ("game/Project/Binaries/Win64", "prefix", "home")]
        self.assertEqual(before, after)
        helper.prepare_game(self.store, self.config)
        self.assert_preserved()
        self.assertEqual(list(self.store.path.glob(".layout-*")), [])

    def test_old_saved_settings_still_work_after_original_source_is_removed(self):
        helper.prepare_game(self.store, self.config)
        shutil.rmtree(self.source)
        stale = helper.validate_config(self.raw)
        self.assertEqual(stale["game_dir"], str(self.binary))
        helper.prepare_game(self.store, stale)
        self.assertEqual(stale["game_dir"], str(self.source))
        self.assertEqual(stale["exe"], "Project/Binaries/Win64/Game.exe")
        self.assert_preserved()

    def test_unrelated_snapshot_source_change_is_refused(self):
        metadata = self.store.read_json("snapshot.json")
        metadata["source"] = str(self.root / "unrelated/Project/Binaries/Win64")
        self.store.write_json("snapshot.json", metadata)
        with self.assertRaisesRegex(helper.JailError, "different game directory"):
            helper.prepare_game(self.store, self.config)
        self.assertEqual((self.store.path / "game/local-save.dat").read_bytes(), b"private game save")

    def test_symlink_in_new_source_does_not_change_existing_copy(self):
        (self.source / "Engine/linked").symlink_to(self.root / "private")
        with self.assertRaisesRegex(helper.JailError, "symlink or special file"):
            helper.prepare_game(self.store, self.config)
        self.assertEqual((self.store.path / "game/local-save.dat").read_bytes(), b"private game save")
        self.assertEqual(list(self.store.path.glob(".layout-*")), [])
        self.assertFalse((self.store.path / "layout-transaction.json").exists())

    def test_failed_copy_does_not_change_existing_copy(self):
        metadata = self.store.read_json("snapshot.json")
        with patch.object(helper, "_copy_tree", side_effect=OSError("simulated disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                helper.prepare_game(self.store, self.config)
        self.assertEqual(self.store.read_json("snapshot.json"), metadata)
        self.assertEqual((self.store.path / "game/local-save.dat").read_bytes(), b"private game save")
        self.assertEqual(list(self.store.path.glob(".layout-*")), [])

    def test_expansion_requires_the_same_launch_lock_that_excludes_running_games(self):
        with patch.object(self.store, "lock_fd", None):
            with self.assertRaisesRegex(helper.JailError, "holding its launch lock"):
                helper.prepare_game(self.store, self.config)
        self.assertEqual((self.store.path / "game/local-save.dat").read_bytes(), b"private game save")

    def test_every_rename_gap_is_recoverable_without_recopied_saves(self):
        original = helper.os.rename
        for failure in range(1, 4):
            with self.subTest(rename=failure):
                # Each successful recovery is converted to a fresh old-layout
                # fixture before testing the next interruption point.
                if failure > 1:
                    os.rename(self.store.path / "game/Project/Binaries/Win64", self.store.path / "preserved")
                    shutil.rmtree(self.store.path / "game")
                    os.rename(self.store.path / "preserved", self.store.path / "game")
                    self.store.write_json("snapshot.json", {"source": str(self.binary), "created_at": "fixture"})
                count = 0
                def interrupted(*args, **kwargs):
                    nonlocal count
                    count += 1
                    if count == failure:
                        raise OSError("simulated interruption")
                    return original(*args, **kwargs)
                with patch.object(helper.os, "rename", side_effect=interrupted):
                    with self.assertRaisesRegex(OSError, "interruption"):
                        helper.prepare_game(self.store, self.config)
                self.assertTrue((self.store.path / "layout-transaction.json").exists())
                with patch.object(helper, "_copy_tree", side_effect=AssertionError("recovery must not recopy")):
                    helper.prepare_game(self.store, self.config)
                self.assert_preserved()

    def test_recovery_after_snapshot_commit_and_before_marker_removal(self):
        original = helper.os.unlink
        def interrupted(name, *args, **kwargs):
            if name == "layout-transaction.json":
                raise OSError("simulated interruption")
            return original(name, *args, **kwargs)
        with patch.object(helper.os, "unlink", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interruption"):
                helper.prepare_game(self.store, self.config)
        helper.prepare_game(self.store, self.config)
        self.assert_preserved()

    def test_invalid_transaction_names_cannot_move_a_host_path(self):
        self.store.write_json("layout-transaction.json", {"stage": "../outside", "backup": "../other"})
        with self.assertRaisesRegex(helper.JailError, "recovery metadata"):
            helper.prepare_game(self.store, self.config)
        self.assertEqual((self.store.path / "game/local-save.dat").read_bytes(), b"private game save")

    def test_marker_symlink_cannot_trigger_normalization(self):
        shutil.rmtree(self.source / "Engine")
        (self.source / "Engine").symlink_to(self.home, target_is_directory=True)
        with self.assertRaisesRegex(helper.JailError, "layout contains a symlink"):
            helper.validate_config(self.raw)

    def test_check_accepts_pending_expansion_but_checks_complete_original_executable(self):
        outputs = type("Result", (), {"returncode": 0, "stdout": (helper.READY_PREFIX + '{"probe": true}').encode()})()
        with patch.object(helper, "check_dependencies", return_value=("/usr/bin/bwrap", "/usr/bin/python3")), patch.object(helper, "build_command", return_value=["fixture"]), patch.object(helper.subprocess, "run", return_value=outputs), patch.object(helper, "check_graphics", return_value={}):
            self.assertEqual(helper.check(self.config, self.store)["status"], "ready")
            (self.binary / "Game.exe").unlink()
            with self.assertRaises(FileNotFoundError):
                helper.check(self.config, self.store)

    def test_locked_check_recovers_interrupted_layout_after_original_is_removed(self):
        original = helper.os.rename
        count = 0
        def interrupted(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("simulated interruption")
            return original(*args, **kwargs)
        with patch.object(helper.os, "rename", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interruption"):
                helper.prepare_game(self.store, self.config)
        shutil.rmtree(self.source)
        stale = helper.validate_config(self.raw)
        outputs = type("Result", (), {"returncode": 0, "stdout": (helper.READY_PREFIX + '{"probe": true}').encode()})()
        with patch.object(helper, "check_dependencies", return_value=("/usr/bin/bwrap", "/usr/bin/python3")), patch.object(helper, "build_command", return_value=["fixture"]), patch.object(helper.subprocess, "run", return_value=outputs), patch.object(helper, "check_graphics", return_value={}):
            self.assertEqual(helper.check(stale, self.store)["status"], "ready")
        self.assertEqual(stale["game_dir"], str(self.source))
        self.assert_preserved()


if __name__ == "__main__":
    unittest.main()
