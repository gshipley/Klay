"""Resolve installed Lutris fixtures without executing a game or runner."""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from klay.qt import lutris_jail
from klay.qt.lutris_jail import LutrisJailError, resolve_lutris_jail


class LutrisJailDependencyTests(unittest.TestCase):
    def test_missing_pyyaml_produces_actionable_resolver_error(self) -> None:
        with patch.object(lutris_jail, "yaml", None):
            with self.assertRaisesRegex(LutrisJailError, "PyYAML.*python3-pyyaml"):
                resolve_lutris_jail({"game_id": "lutris_wine_1"})


@unittest.skipIf(lutris_jail.yaml is None, "PyYAML is required for Lutris YAML fixtures")
class LutrisJailTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "home"
        self.home.mkdir()
        self.data = self.home / ".local/share/lutris"
        self.config = self.home / ".config/lutris"
        self.folder = self.home / "Games/An offline game"
        self.exe = self.folder / "bin/Game.exe"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b"fixture only")
        self.game = {"game_id": "lutris_wine_1", "name": "A renamed imported title", "source": "lutris_wine"}
        self.environment = patch.dict(os.environ, {"FLATPAK_ID": "", "XDG_DATA_HOME": "", "XDG_CONFIG_HOME": "", "HOST_XDG_DATA_HOME": "", "HOST_XDG_CONFIG_HOME": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.make_installation(self.data, self.config)
        self.make_runner(self.data / "runners/wine/selected-wine")
        self.write_yaml(self.config / "runners/wine.yml", "wine:\n  version: selected-wine\n")

    def write_yaml(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def make_runner(self, root: Path, layout: str = "bin/wine") -> Path:
        path = root / layout
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("This fixture must never be executed.", encoding="utf-8")
        path.chmod(0o755)
        return path

    def make_proton(self, root: Path, layout: str = "files/bin/wine") -> Path:
        self.make_runner(root, layout)
        wrapper = root / "proton"
        wrapper.write_text("This fixture must never be executed.", encoding="utf-8")
        wrapper.chmod(0o755)
        return wrapper

    def make_installation(self, data: Path, config: Path, *, folder: Path | None = None, token: str = "installed-game") -> None:
        data.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(data / "pga.db")) as connection, connection:
            connection.execute("CREATE TABLE games (id INTEGER PRIMARY KEY, name TEXT, runner TEXT, directory TEXT, configPath TEXT, installed INTEGER)")
            connection.execute("INSERT INTO games VALUES (?, ?, ?, ?, ?, ?)", (1, "Actual Lutris title", "wine", str(folder or self.folder), token, 1))
        self.write_yaml(config / "games/installed-game.yml", f"game:\n  exe: '{self.exe}'\n")

    def resolve(self, **kwargs):
        return resolve_lutris_jail(self.game, host_home=self.home, **kwargs)

    def test_stable_id_resolves_renamed_old_metadata_and_preserves_argument_boundaries(self) -> None:
        self.write_yaml(self.config / "games/installed-game.yml", f"game:\n  exe: '{self.exe}'\n  args: '--label \"words with spaces\" \"$(touch sentinel)\"'\n")
        result = resolve_lutris_jail(SimpleNamespace(data=self.game), host_home=self.home)
        self.assertEqual(result["game_dir"], str(self.folder))
        self.assertEqual(result["exe"], "bin/Game.exe")
        self.assertEqual(result["arguments"], ["--label", "words with spaces", "$(touch sentinel)"])
        self.assertEqual(result["runner_dir"], str(self.data / "runners/wine/selected-wine"))
        self.assertTrue(result["enabled"])
        self.assertFalse((self.home / "sentinel").exists())

    def test_ignores_installer_script_version_and_historical_wine_path(self) -> None:
        prefix = self.home / "Prefixes/example"
        actual = prefix / "drive_c/vendor/game/game.exe"
        actual.parent.mkdir(parents=True)
        actual.write_bytes(b"fixture")
        with closing(sqlite3.connect(self.data / "pga.db")) as connection, connection:
            connection.execute("UPDATE games SET directory = ?", (str(prefix),))
        self.write_yaml(self.config / "games/installed-game.yml", f"game:\n  exe: '{actual}'\n  prefix: '{prefix}'\nversion: Setup file\nscript:\n  game:\n    exe: _xXx_AUTO_WIN32_xXx_\n    prefix: $GAMEDIR\n  wine:\n    wine_path: /unrelated/obsolete/bin/wine\n")
        result = self.resolve()
        self.assertEqual(result["game_dir"], str(actual.parent))
        self.assertEqual(result["exe"], "game.exe")
        self.assertEqual(result["runner"], str(self.data / "runners/wine/selected-wine/bin/wine"))

    def unreal_installation(self, *, platform: str = "Win64") -> tuple[Path, Path, Path]:
        prefix = self.home / "Prefixes/unreal-game"
        root = prefix / "drive_c/Games/Complete Game"
        executable = root / "GameProject/Binaries" / platform / "GameProject-Shipping.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"fixture only")
        (root / "Engine").mkdir()
        (root / "GameProject/Content").mkdir()
        with closing(sqlite3.connect(self.data / "pga.db")) as connection, connection:
            connection.execute("UPDATE games SET directory = ?", (str(prefix),))
        self.write_yaml(self.config / "games/installed-game.yml", f"game:\n  exe: '{executable}'\n  prefix: '{prefix}'\n")
        return prefix, root, executable

    def test_packaged_unreal_in_prefix_includes_engine_and_project_content(self) -> None:
        prefix, root, executable = self.unreal_installation()
        (prefix / "unrelated-private-file").write_bytes(b"must stay outside the source")
        result = self.resolve()
        self.assertEqual(result["game_dir"], str(root))
        self.assertEqual(result["exe"], "GameProject/Binaries/Win64/GameProject-Shipping.exe")
        self.assertNotIn("working_dir", result)

    def test_packaged_unreal_expands_lutris_directory_that_only_contains_binaries(self) -> None:
        _, root, executable = self.unreal_installation(platform="Win32")
        with closing(sqlite3.connect(self.data / "pga.db")) as connection, connection:
            connection.execute("UPDATE games SET directory = ?", (str(executable.parent),))
        result = self.resolve()
        self.assertEqual(result["game_dir"], str(root))
        self.assertEqual(result["exe"], "GameProject/Binaries/Win32/GameProject-Shipping.exe")

    def test_unreal_preserves_explicit_working_directory_inside_expanded_game(self) -> None:
        prefix, root, executable = self.unreal_installation()
        self.write_yaml(self.config / "games/installed-game.yml", f"game:\n  exe: '{executable}'\n  prefix: '{prefix}'\n  working_dir: '{executable.parent}'\n")
        result = self.resolve()
        self.assertEqual(result["game_dir"], str(root))
        self.assertEqual(result["working_dir"], "GameProject/Binaries/Win64")

    def test_unreal_does_not_shrink_a_complete_lutris_game_directory(self) -> None:
        _, root, executable = self.unreal_installation()
        with closing(sqlite3.connect(self.data / "pga.db")) as connection, connection:
            connection.execute("UPDATE games SET directory = ?", (str(root),))
        result = self.resolve()
        self.assertEqual(result["game_dir"], str(root))
        self.assertEqual(result["exe"], str(executable.relative_to(root)))

    def test_unreal_without_both_content_and_engine_markers_does_not_expand(self) -> None:
        _, root, executable = self.unreal_installation()
        for marker in (root / "Engine", root / "GameProject/Content"):
            with self.subTest(marker=marker.name):
                marker.rmdir()
                with self.assertRaisesRegex(LutrisJailError, "complete game folder cannot be identified"):
                    self.resolve()
                marker.mkdir()

    def test_unreal_marker_symlink_cannot_authorize_larger_game_tree(self) -> None:
        _, root, _ = self.unreal_installation()
        other = self.home / "Unrelated engine"
        other.mkdir()
        marker = root / "Engine"
        marker.rmdir()
        marker.symlink_to(other, target_is_directory=True)
        with self.assertRaisesRegex(LutrisJailError, "Unreal game layout.*symbolic link"):
            self.resolve()

    def test_unreal_shape_cannot_select_drive_c_or_prefix_as_game_root(self) -> None:
        for component in ("", "drive_c"):
            with self.subTest(component=component):
                prefix = self.home / "Prefixes" / (component or "prefix-root")
                root = prefix / component
                executable = root / "Project/Binaries/Win64/Project.exe"
                executable.parent.mkdir(parents=True)
                executable.write_bytes(b"fixture only")
                (root / "Engine").mkdir()
                (root / "Project/Content").mkdir()
                with self.assertRaisesRegex(LutrisJailError, "complete game folder cannot be identified"):
                    lutris_jail._game_paths({"exe": str(executable), "prefix": str(prefix)}, {"directory": str(prefix)}, self.home)

    def test_unreal_detection_does_not_search_unrecognized_binary_hierarchy(self) -> None:
        _, root, executable = self.unreal_installation()
        other = root / "GameProject/Binaries/OtherPlatform"
        executable.parent.rename(other)
        self.write_yaml(self.config / "games/installed-game.yml", f"game:\n  exe: '{other / executable.name}'\n  prefix: '{root.parents[2]}'\n")
        self.assertEqual(self.resolve()["game_dir"], str(other))

    def test_game_wine_version_overrides_runner_defaults(self) -> None:
        selected = self.make_runner(self.data / "runners/wine/per-game")
        self.write_yaml(self.config / "games/installed-game.yml", f"game:\n  exe: '{self.exe}'\nwine:\n  version: per-game\n")
        self.assertEqual(self.resolve()["runner"], str(selected))

    def test_game_automatic_or_empty_version_inherits_concrete_runner_default(self) -> None:
        for value in ("ge-proton", "''", "null"):
            with self.subTest(value=value):
                self.write_yaml(self.config / "games/installed-game.yml", f"game:\n  exe: '{self.exe}'\nwine:\n  version: {value}\n")
                self.assertEqual(self.resolve()["runner"], str(self.data / "runners/wine/selected-wine/bin/wine"))

    def test_relative_exe_uses_database_game_directory_and_windows_casing(self) -> None:
        self.write_yaml(self.config / "games/installed-game.yml", "game:\n  exe: 'BIN\\game.EXE'\n")
        result = self.resolve()
        self.assertEqual(result["game_dir"], str(self.folder))
        self.assertEqual(result["exe"], "bin/Game.exe")

    def test_configured_flatpak_library_uses_paired_config_and_runner(self) -> None:
        flatpak = self.home / ".var/app/net.lutris.Lutris"
        data, config = flatpak / "data/lutris", flatpak / "config/lutris"
        self.make_installation(data, config)
        runner = self.make_runner(data / "runners/wine/flatpak-selected")
        self.write_yaml(config / "runners/wine.yml", "wine:\n  version: flatpak-selected\n")
        self.assertEqual(self.resolve(lutris_location=data)["runner"], str(runner))

    def test_stale_default_flatpak_location_falls_back_to_native_library(self) -> None:
        missing = self.home / ".var/app/net.lutris.Lutris/data/lutris"
        self.assertEqual(self.resolve(lutris_location=missing)["game_dir"], str(self.folder))

    def test_two_installations_without_location_are_ambiguous(self) -> None:
        flatpak = self.home / ".var/app/net.lutris.Lutris"
        self.make_installation(flatpak / "data/lutris", flatpak / "config/lutris")
        with self.assertRaisesRegex(LutrisJailError, "More than one Lutris library"):
            self.resolve()

    def test_existing_configured_library_does_not_guess_from_other_library(self) -> None:
        empty = self.home / "Other Lutris"
        self.make_installation(empty, empty / "config")
        with closing(sqlite3.connect(empty / "pga.db")) as connection, connection:
            connection.execute("DELETE FROM games")
        with self.assertRaisesRegex(LutrisJailError, "missing from the configured"):
            self.resolve(lutris_location=empty)

    def test_global_concrete_proton_name_uses_exact_build_wrapper(self) -> None:
        build = self.home / ".local/share/Steam/compatibilitytools.d/Proton-GE Latest"
        self.make_proton(build)
        self.write_yaml(self.config / "runners/wine.yml", "wine:\n  version: Proton-GE Latest\n")
        result = self.resolve()
        self.assertEqual(result["runner_type"], "proton")
        self.assertEqual(result["runner"], str(build))
        self.assertNotIn("runner_dir", result)

    def test_custom_proton_wine_path_uses_enclosing_build_wrapper(self) -> None:
        build = self.home / "Trusted tools/Specific Proton"
        self.make_proton(build)
        runner = build / "files/bin/wine"
        self.write_yaml(self.config / "runners/wine.yml", f"wine:\n  version: custom\n  custom_wine_path: '{runner}'\n")
        result = self.resolve()
        self.assertEqual(result["runner_type"], "proton")
        self.assertEqual(result["runner"], str(build))

    def test_legacy_proton_distribution_uses_wrapper(self) -> None:
        build = self.data / "runners/wine/Legacy Proton"
        self.make_proton(build, "dist/bin/wine")
        self.write_yaml(self.config / "runners/wine.yml", "wine:\n  version: Legacy Proton\n")
        result = self.resolve()
        self.assertEqual(result["runner_type"], "proton")
        self.assertEqual(result["runner"], str(build))

    def test_named_wine_build_without_proton_wrapper_stays_wine(self) -> None:
        build = self.data / "runners/wine/Portable Wine"
        runner = self.make_runner(build, "files/bin/wine")
        self.write_yaml(self.config / "runners/wine.yml", "wine:\n  version: Portable Wine\n")
        result = self.resolve()
        self.assertEqual(result["runner_type"], "wine")
        self.assertEqual(result["runner"], str(runner))
        self.assertEqual(result["runner_dir"], str(build))

    def test_nonstandard_wine_binary_is_not_replaced_by_unrelated_proton_file(self) -> None:
        build = self.data / "runners/wine/selected-wine"
        wrapper = build / "proton"
        wrapper.write_text("Unrelated fixture", encoding="utf-8")
        wrapper.chmod(0o755)
        result = self.resolve()
        self.assertEqual(result["runner_type"], "wine")
        self.assertEqual(result["runner"], str(build / "bin/wine"))

    def test_invalid_proton_wrapper_has_no_bare_wine_fallback(self) -> None:
        build = self.data / "runners/wine/Broken Proton"
        wrapper = self.make_proton(build)
        wrapper.chmod(0o644)
        self.write_yaml(self.config / "runners/wine.yml", "wine:\n  version: Broken Proton\n")
        with self.assertRaisesRegex(LutrisJailError, "Proton entry point is not executable"):
            self.resolve()

    def test_proton_wrapper_symlink_cannot_escape_selected_build(self) -> None:
        build = self.data / "runners/wine/Linked Proton"
        wrapper = self.make_proton(build)
        other = self.home / "Unrelated wrapper"
        other.write_text("Fixture must never execute", encoding="utf-8")
        other.chmod(0o755)
        wrapper.unlink()
        wrapper.symlink_to(other)
        self.write_yaml(self.config / "runners/wine.yml", "wine:\n  version: Linked Proton\n")
        with self.assertRaisesRegex(LutrisJailError, "Proton entry point escapes"):
            self.resolve()

    def test_custom_wine_cannot_select_the_users_home_as_build(self) -> None:
        runner = self.make_runner(self.home)
        self.write_yaml(self.config / "runners/wine.yml", f"wine:\n  version: custom\n  custom_wine_path: '{runner}'\n")
        with self.assertRaisesRegex(LutrisJailError, "specific build"):
            self.resolve()

    def test_system_runner_is_resolved_by_host_helper(self) -> None:
        self.write_yaml(self.config / "runners/wine.yml", "wine:\n  version: system\n")
        result = self.resolve()
        self.assertEqual(result["runner"], "wine")
        self.assertNotIn("runner_dir", result)

    def test_selected_runner_alias_resolves_to_its_canonical_build(self) -> None:
        build = self.home / "Trusted tools/Specific Wine"
        runner = self.make_runner(build)
        alias = self.data / "runners/wine/alias"
        alias.symlink_to(build, target_is_directory=True)
        self.write_yaml(self.config / "runners/wine.yml", "wine:\n  version: alias\n")
        self.assertEqual(self.resolve()["runner"], str(runner))
        self.assertEqual(self.resolve()["runner_dir"], str(build))

    def test_duplicate_named_runner_fails_instead_of_changing_interpreter(self) -> None:
        self.make_runner(self.home / ".var/app/net.lutris.Lutris/data/lutris/runners/wine/selected-wine")
        with self.assertRaisesRegex(LutrisJailError, "More than one installed runner"):
            self.resolve()

    def test_missing_selected_runner_has_no_unrelated_fallback(self) -> None:
        self.write_yaml(self.config / "runners/wine.yml", "wine:\n  version: missing-version\n")
        with self.assertRaisesRegex(LutrisJailError, "not installed"):
            self.resolve()

    def test_umu_automatic_version_requires_concrete_selection(self) -> None:
        for content in ("wine:\n  version: ge-proton\n", "wine: {}\n"):
            with self.subTest(content=content):
                self.write_yaml(self.config / "runners/wine.yml", content)
                with self.assertRaisesRegex(LutrisJailError, "automatic UMU"):
                    self.resolve()

    def test_explicit_working_directory_preserves_relative_cwd(self) -> None:
        self.write_yaml(self.config / "games/installed-game.yml", f"game:\n  exe: '{self.exe}'\n  working_dir: '{self.folder}'\n")
        result = self.resolve()
        self.assertEqual(result["game_dir"], str(self.folder))
        self.assertEqual(result["working_dir"], ".")
        self.assertEqual(result["exe"], "bin/Game.exe")

    def test_relative_explicit_working_directory_does_not_guess_database_base(self) -> None:
        self.write_yaml(self.config / "games/installed-game.yml", f"game:\n  exe: '{self.exe}'\n  working_dir: bin\n")
        with self.assertRaisesRegex(LutrisJailError, "relative working directory.*manual jail"):
            self.resolve()

    def test_external_working_directory_does_not_mount_a_common_ancestor(self) -> None:
        other = self.home / "Documents/private"
        other.mkdir(parents=True)
        self.write_yaml(self.config / "games/installed-game.yml", f"game:\n  exe: '{self.exe}'\n  working_dir: '{other}'\n")
        with self.assertRaisesRegex(LutrisJailError, "outside the game folder"):
            self.resolve()

    def test_explicit_prefix_working_directory_is_not_copied(self) -> None:
        self.write_yaml(self.config / "games/installed-game.yml", f"game:\n  exe: '{self.exe}'\n  prefix: '{self.folder}'\n  working_dir: '{self.folder}'\n")
        with self.assertRaisesRegex(LutrisJailError, "Wine prefix"):
            self.resolve()

    def test_symbolic_link_in_executable_path_is_rejected(self) -> None:
        self.exe.unlink()
        actual = self.home / "Elsewhere.exe"
        actual.write_bytes(b"fixture")
        self.exe.symlink_to(actual)
        with self.assertRaisesRegex(LutrisJailError, "symbolic link"):
            self.resolve()

    def test_installed_config_token_cannot_escape_games_directory(self) -> None:
        with closing(sqlite3.connect(self.data / "pga.db")) as connection, connection:
            connection.execute("UPDATE games SET configPath = '../other'")
        with self.assertRaisesRegex(LutrisJailError, "inside its games directory"):
            self.resolve()

    def test_yaml_python_objects_are_not_constructed(self) -> None:
        self.write_yaml(self.config / "games/installed-game.yml", "!!python/object/apply:builtins.eval ['1 + 1']\n")
        with self.assertRaisesRegex(LutrisJailError, "Cannot read"):
            self.resolve()

    def test_invalid_argument_quoting_is_explicit_error(self) -> None:
        self.write_yaml(self.config / "games/installed-game.yml", f"game:\n  exe: '{self.exe}'\n  args: '\"unfinished'\n")
        with self.assertRaisesRegex(LutrisJailError, "invalid quoting"):
            self.resolve()

    def test_non_string_arguments_cannot_silently_disappear(self) -> None:
        self.write_yaml(self.config / "games/installed-game.yml", f"game:\n  exe: '{self.exe}'\n  args: false\n")
        with self.assertRaisesRegex(LutrisJailError, "quoted command-line string"):
            self.resolve()

    def test_non_wine_or_uninstalled_entry_fails(self) -> None:
        for column, value, message in (("runner", "linux", "not a Wine game"), ("installed", 0, "not installed")):
            with self.subTest(column=column):
                with closing(sqlite3.connect(self.data / "pga.db")) as connection, connection:
                    connection.execute(f"UPDATE games SET {column} = ?", (value,))
                with self.assertRaisesRegex(LutrisJailError, message):
                    self.resolve()
                with closing(sqlite3.connect(self.data / "pga.db")) as connection, connection:
                    connection.execute("UPDATE games SET runner = 'wine', installed = 1")

    def test_read_only_resolution_does_not_change_library_or_configuration(self) -> None:
        files = [self.data / "pga.db", self.config / "games/installed-game.yml", self.config / "runners/wine.yml"]
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in files]
        self.resolve()
        after = [(path.read_bytes(), path.stat().st_mtime_ns) for path in files]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
