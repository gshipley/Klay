"""Library boundaries for per-game isolation; no runner or game is executed."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch, sentinel

from klay.qt.jail import JailError
from klay.qt.library import GameEntry, GameLibrary


class JailLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {
                "XDG_DATA_HOME": str(self.root / "data"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "FLATPAK_ID": "",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.library = GameLibrary()
        self.config = {
            "enabled": True,
            "game_dir": "/home/player/Games/Game with spaces",
            "exe": "bin/game.exe",
            "runner_type": "wine",
            "runner": "/usr/bin/wine",
            "arguments": ["--windowed", "literal $(command)", "value with spaces"],
        }

    def make_game(self, jail: object = sentinel.missing) -> GameEntry:
        data = {
            "game_id": "imported_1",
            "name": "An offline game",
            "executable": ["original-launcher", "--game", "one"],
            "last_played": 0,
        }
        if jail is not sentinel.missing:
            data["jail"] = jail
        return GameEntry(self.library.games_dir / "imported_1.json", data)

    def test_old_game_has_no_jail_and_uses_original_launch(self) -> None:
        game = self.make_game()
        self.assertFalse(game.jail_enabled)
        with (
            patch("klay.qt.library.jail.launch_game") as jailed,
            patch("klay.qt.library.subprocess.Popen", return_value=sentinel.process) as ordinary,
        ):
            self.assertIs(self.library.launch(game), sentinel.process)
        jailed.assert_not_called()
        ordinary.assert_called_once_with(game.executable, start_new_session=True)
        self.assertGreater(game.last_played, 0)

    def test_stale_ordinary_entry_uses_current_saved_jail(self) -> None:
        stale = self.make_game()
        self.library.save_game(stale)
        current = self.library.load_game_by_id(stale.game_id)
        current.set_value("jail", self.config)
        self.library.save_game(current)
        with (
            patch("klay.qt.library.jail.launch_game", return_value=sentinel.process) as jailed,
            patch("klay.qt.library.subprocess.Popen") as ordinary,
        ):
            self.assertIs(self.library.launch(stale), sentinel.process)
        jailed.assert_called_once_with(self.config, game_id=stale.game_id, data_dir_name="klay")
        ordinary.assert_not_called()
        self.assertTrue(stale.jail_enabled)
        self.assertGreater(stale.last_played, 0)

    def test_current_explicit_opt_out_and_updated_command_override_stale_jail(self) -> None:
        stale = self.make_game(self.config)
        self.library.save_game(stale)
        current = self.library.load_game_by_id(stale.game_id)
        current.set_value("jail", {"enabled": False})
        current.set_value("executable", ["updated-launcher", "--game", "one"])
        self.library.save_game(current)
        with (
            patch("klay.qt.library.jail.launch_game") as jailed,
            patch("klay.qt.library.subprocess.Popen", return_value=sentinel.process) as ordinary,
        ):
            self.assertIs(self.library.launch(stale), sentinel.process)
        jailed.assert_not_called()
        ordinary.assert_called_once_with(["updated-launcher", "--game", "one"], start_new_session=True)
        self.assertFalse(stale.jail_enabled)

    def test_corrupt_current_record_never_launches_cached_command(self) -> None:
        stale = self.make_game()
        self.library.save_game(stale)
        for raw in ("{broken", "null", "[]", "{}"):
            with self.subTest(raw=raw):
                stale.path.write_text(raw, encoding="utf-8")
                with (
                    patch("klay.qt.library.jail.launch_game") as jailed,
                    patch("klay.qt.library.subprocess.Popen") as ordinary,
                    self.assertRaises(OSError),
                ):
                    self.library.launch(stale)
                jailed.assert_not_called()
                ordinary.assert_not_called()

    def test_deleted_persisted_entry_never_launches_cached_command(self) -> None:
        stale = self.make_game()
        self.library.save_game(stale)
        stale.path.unlink()
        with patch("klay.qt.library.subprocess.Popen") as ordinary:
            with self.assertRaisesRegex(OSError, "deleted"):
                self.library.launch(stale)
        ordinary.assert_not_called()

    def test_add_reload_and_edit_preserve_jail_configuration_and_original_command(self) -> None:
        with patch.object(self.library, "set_cover"):
            game = self.library.add_manual_game(
                name="An offline game", executable="original-launcher --game one",
                jail=self.config,
            )
        self.library.update_manual_game(
            game, name="Renamed", executable="original-launcher --game one",
        )
        reloaded = self.library.load_game_by_id(game.game_id)
        self.assertIsNotNone(reloaded)
        assert reloaded is not None
        self.assertTrue(reloaded.jail_enabled)
        self.assertEqual(reloaded.jail_config, self.config)
        self.assertEqual(reloaded.executable, "original-launcher --game one")
        self.assertEqual(reloaded.name, "Renamed")
        stored = json.loads(game.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["jail"]["arguments"], self.config["arguments"])

    def test_enabled_jail_only_dispatches_to_jail_and_keeps_arguments_as_data(self) -> None:
        game = self.make_game(self.config)
        with (
            patch("klay.qt.library.jail.launch_game", return_value=sentinel.process) as jailed,
            patch("klay.qt.library.subprocess.Popen") as ordinary,
        ):
            self.assertIs(self.library.launch(game), sentinel.process)
        jailed.assert_called_once_with(
            self.config, game_id=game.game_id, data_dir_name="klay",
        )
        ordinary.assert_not_called()
        self.assertGreater(game.last_played, 0)

    def test_jail_failure_never_runs_original_command_or_marks_game_played(self) -> None:
        game = self.make_game(self.config)
        self.library.save_game(game)
        with (
            patch("klay.qt.library.jail.launch_game", side_effect=JailError("no isolation")),
            patch("klay.qt.library.subprocess.Popen") as ordinary,
        ):
            with self.assertRaisesRegex(JailError, "no isolation"):
                self.library.launch(game)
        ordinary.assert_not_called()
        self.assertEqual(game.last_played, 0)
        self.assertEqual(json.loads(game.path.read_text(encoding="utf-8"))["last_played"], 0)

    def test_edits_during_jail_preparation_survive_launch_timestamp_update(self) -> None:
        game = self.make_game(self.config)
        self.library.save_game(game)

        def prepare_then_return(*args, **kwargs):
            # Model an edit/import writing a newer record while the launch
            # worker still holds the old GameEntry snapshot.
            edited = self.library.load_game_by_id(game.game_id)
            assert edited is not None
            edited.data.update({
                "name": "New title from an import",
                "executable": "updated-launcher --new-route",
                "categories": ["Edited during preparation"],
                "jail": {**self.config, "enabled": False},
                "removed": True,
            })
            self.library.save_game(edited)
            return sentinel.process

        with patch("klay.qt.library.jail.launch_game", side_effect=prepare_then_return):
            self.assertIs(self.library.launch(game), sentinel.process)
        stored = json.loads(game.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["name"], "New title from an import")
        self.assertEqual(stored["executable"], "updated-launcher --new-route")
        self.assertEqual(stored["categories"], ["Edited during preparation"])
        self.assertEqual(stored["jail"], {**self.config, "enabled": False})
        self.assertTrue(stored["removed"])
        self.assertGreater(stored["last_played"], 0)

    def test_deleted_metadata_is_not_recreated_after_jail_preparation(self) -> None:
        game = self.make_game(self.config)
        self.library.save_game(game)

        def delete_then_return(*args, **kwargs):
            game.path.unlink()
            return sentinel.process

        with patch("klay.qt.library.jail.launch_game", side_effect=delete_then_return):
            self.assertIs(self.library.launch(game), sentinel.process)
        self.assertFalse(game.path.exists())
        self.assertIsNone(self.library.load_game_by_id(game.game_id, include_removed=True))

    def test_history_save_failure_retains_started_jail_supervisor(self) -> None:
        game = self.make_game(self.config)
        self.library.save_game(game)
        process = Mock()
        with (
            patch("klay.qt.library.jail.launch_game", return_value=process),
            patch.object(self.library, "save_game", side_effect=OSError("metadata disk full")),
            patch("klay.qt.library.subprocess.Popen") as ordinary,
        ):
            self.assertIs(self.library.launch(game), process)
        self.assertIn("metadata disk full", process.jail_warning)
        ordinary.assert_not_called()

    def test_stale_ui_action_preserves_new_jail_and_play_history(self) -> None:
        stale = self.make_game({"enabled": False})
        self.library.save_game(stale)
        current = self.library.load_game_by_id(stale.game_id)
        current.set_value("jail", self.config)
        current.set_value("last_played", 67890)
        self.library.save_game(current)
        self.library.set_hidden(stale, True)
        stored = self.library.load_game_by_id(stale.game_id)
        self.assertEqual(stored.jail_config, self.config)
        self.assertEqual(stored.last_played, 67890)
        self.assertTrue(stored.hidden)

    def test_hide_after_mark_played_preserves_newer_play_history(self) -> None:
        game = self.make_game(self.config)
        self.library.save_game(game)
        with patch("klay.qt.library.time", return_value=12345):
            self.library.mark_played(game)
        current = self.library.load_game_by_id(game.game_id)
        current.set_value("last_played", 67890)
        self.library.save_game(current)
        self.library.set_hidden(game, True)
        stored = self.library.load_game_by_id(game.game_id)
        self.assertEqual(stored.last_played, 67890)
        self.assertTrue(stored.hidden)

    def test_explicit_jail_opt_out_is_kept_even_with_stale_baseline(self) -> None:
        stale = self.make_game({"enabled": False})
        self.library.save_game(stale)
        current = self.library.load_game_by_id(stale.game_id)
        current.set_value("jail", self.config)
        self.library.save_game(current)
        stale.set_value("jail", {"enabled": False})
        self.library.save_game(stale)
        self.assertFalse(self.library.load_game_by_id(stale.game_id).jail_enabled)

    def test_play_history_does_not_recreate_entry_deleted_before_commit(self) -> None:
        game = self.make_game(self.config)
        self.library.save_game(game)
        save = self.library.save_game

        def delete_before_save(current, **kwargs):
            game.path.unlink()
            save(current, **kwargs)

        with patch.object(self.library, "save_game", side_effect=delete_before_save):
            self.library.mark_played(game)
        self.assertFalse(game.path.exists())

    def test_corrupt_current_record_is_not_replaced_by_stale_ui_save(self) -> None:
        for raw in ("null", "[]", "{broken"):
            with self.subTest(raw=raw):
                game = self.make_game(self.config)
                game.path.write_text(raw, encoding="utf-8")
                with self.assertRaisesRegex(OSError, "Invalid game metadata"):
                    self.library.set_hidden(game, True)
                self.assertEqual(game.path.read_text(encoding="utf-8"), raw)

    def test_edit_open_during_preparation_preserves_new_play_timestamp_and_import_fields(self) -> None:
        game = self.make_game(self.config)
        self.library.save_game(game)
        current = self.library.load_game_by_id(game.game_id)
        assert current is not None
        current.data.update(last_played=12345, genres=["Updated by import"], categories=["Favorites"])
        self.library.save_game(current)
        self.library.update_manual_game(
            game, name="Edited title", executable="updated-launcher",
            jail={**self.config, "arguments": ["--windowed"]},
        )
        stored = json.loads(game.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["last_played"], 12345)
        self.assertEqual(stored["genres"], ["Updated by import"])
        self.assertEqual(stored["categories"], ["Favorites"])
        self.assertEqual(stored["name"], "Edited title")
        self.assertEqual(stored["executable"], "updated-launcher")
        self.assertEqual(stored["jail"]["arguments"], ["--windowed"])

    def test_corrupt_jail_settings_cannot_silently_downgrade_to_ordinary_launch(self) -> None:
        for raw_config in (None, "enabled", [], {}, {"exe": "Game.exe"}, {"enabled": "false"}, {"enabled": 0}):
            with self.subTest(config=raw_config):
                game = self.make_game(raw_config)
                self.assertTrue(game.jail_enabled)
                with (
                    patch("klay.qt.library.jail.launch_game", side_effect=JailError("invalid jail")) as jailed,
                    patch("klay.qt.library.subprocess.Popen") as ordinary,
                ):
                    with self.assertRaises(JailError):
                        self.library.launch(game)
                jailed.assert_called_once()
                ordinary.assert_not_called()
                self.assertEqual(game.last_played, 0)

    def test_disabling_then_reenabling_keeps_configuration(self) -> None:
        game = self.make_game(self.config)
        disabled = {**self.config, "enabled": False}
        self.library.update_manual_game(
            game, name=game.name, executable="original-launcher --game one",
            jail=disabled,
        )
        reloaded = self.library.load_game_by_id(game.game_id)
        assert reloaded is not None
        self.assertFalse(reloaded.jail_enabled)
        self.assertEqual(reloaded.jail_config, disabled)
        with (
            patch("klay.qt.library.jail.launch_game") as jailed,
            patch("klay.qt.library.subprocess.Popen", return_value=sentinel.process) as ordinary,
        ):
            self.library.launch(reloaded)
        jailed.assert_not_called()
        ordinary.assert_called_once()
        self.library.update_manual_game(
            reloaded, name=reloaded.name, executable=reloaded.executable_text(),
            jail=self.config,
        )
        self.assertTrue(reloaded.jail_enabled)
        self.assertEqual(reloaded.jail_config, self.config)

    def test_report_and_folder_access_do_not_require_enabled_launch_settings(self) -> None:
        game = self.make_game({"enabled": False})
        report = {"jail_dir": "/home/player/.local/share/klay/jails/game-id", "status": "exited"}
        with (
            patch("klay.qt.library.jail.game_report", return_value=report) as read_report,
            patch("klay.qt.library.jail.open_folder") as open_folder,
        ):
            self.assertEqual(self.library.jail_report(game), report)
            self.library.open_jail_folder(game)
        self.assertEqual(read_report.call_count, 2)
        read_report.assert_called_with(
            {"enabled": False}, game_id=game.game_id, data_dir_name="klay",
        )
        open_folder.assert_called_once_with(report["jail_dir"])

    def test_lutris_detection_uses_existing_entry_and_source_location_without_launching(self) -> None:
        game = self.make_game()
        game.data.update(source="lutris_wine", game_id="lutris_wine_1", executable="xdg-open lutris:rungameid/1")
        settings = Mock()
        settings.get_string.return_value = str(self.root / "lutris")
        with (
            patch("klay.qt.settings.SettingsBackend", return_value=settings),
            patch("klay.qt.lutris_jail.resolve_lutris_jail", return_value=self.config) as detect,
            patch("klay.qt.library.subprocess.Popen") as launch,
        ):
            self.assertEqual(self.library.detect_jail_config(game), self.config)
        detect.assert_called_once_with(game.data, lutris_location=str(self.root / "lutris"))
        launch.assert_not_called()
        self.assertNotIn("jail", game.data)

    def test_unsupported_source_detection_has_manual_setup_instruction(self) -> None:
        with self.assertRaisesRegex(JailError, "Advanced settings"):
            self.library.detect_jail_config(self.make_game())


if __name__ == "__main__":
    unittest.main()
