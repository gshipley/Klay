"""Portable source boundaries, independent of optional YAML/Qt dependencies."""
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import unittest
from unittest.mock import patch

from klay.qt import jail_helper as helper, lutris_jail as detector


class DetectionPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.prefix = self.home / "prefix"

    def executable(self, folder):
        path = folder / "game.exe"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture only")
        return path

    def detect(self, exe, directory=None, **options):
        return detector._game_paths(
            {"exe": str(exe), "prefix": str(self.prefix), **options},
            {"directory": str(directory or self.prefix)}, self.home,
        )

    def test_common_binary_layouts_select_title_inside_installation_container(self):
        for container in ("Games", "GOG Games", "Program Files", "Program Files (x86)", "Steam/steamapps/common"):
            for relative in ("bin", "bin/x64", "x86", "bin64"):
                with self.subTest(container=container, relative=relative):
                    title = self.prefix / "drive_c" / container / "Title"
                    exe = self.executable(title / relative)
                    root, selected, working = self.detect(exe)
                    self.assertEqual(root, title)
                    self.assertEqual(selected, relative + "/game.exe")
                    self.assertIsNone(working)

    def test_external_binary_layout_needs_positive_content_evidence(self):
        title = self.root / "external/Title"
        exe = self.executable(title / "bin/x64")
        with self.assertRaisesRegex(detector.LutrisJailError, "whole game folder"):
            self.detect(exe)
        (title / "DATA").mkdir()
        self.assertEqual(self.detect(exe)[0], title)

    def test_unity_root_keeps_sibling_data_and_player_files(self):
        title = self.prefix / "drive_c/UnityTitle"
        exe = self.executable(title)
        (title / "game_Data").mkdir()
        (title / "UnityPlayer.dll").write_bytes(b"fixture")
        self.assertEqual(self.detect(exe)[0], title)

    def test_ancestor_aliases_preserve_whole_folder_and_working_directory(self):
        title = self.home / "Games/Title"
        exe = self.executable(title / "bin")
        alias = self.root / "alias"
        alias.symlink_to(self.home, target_is_directory=True)
        for selected, database, working in (
            (exe, alias / "Games/Title", title),
            (alias / "Games/Title/bin/game.exe", title, alias / "Games/Title"),
        ):
            with self.subTest(exe=selected):
                result = self.detect(selected, database, working_dir=str(working))
                self.assertEqual(result, (title, "bin/game.exe", "."))

    def test_alias_handling_does_not_allow_links_inside_selected_game(self):
        title = self.home / "Games/Title"
        exe = self.executable(title / "real")
        (title / "bin").symlink_to(exe.parent, target_is_directory=True)
        with self.assertRaisesRegex(detector.LutrisJailError, "symbolic link"):
            self.detect(title / "bin/game.exe", title)

    def test_known_shared_roots_are_not_inferred_even_with_content_markers(self):
        for title in (self.home / "Downloads", self.prefix, self.prefix / "drive_c", self.prefix / "drive_c/Games"):
            with self.subTest(title=title):
                exe = self.executable(title / "bin")
                (title / "data").mkdir(exist_ok=True)
                with self.assertRaisesRegex(detector.LutrisJailError, "whole game folder"):
                    self.detect(exe)
                with patch.object(helper, "_host_home", return_value=self.home):
                    if title != self.prefix:
                        self.assertIsNone(helper._game_layout_root(exe.parent))

    def test_unreal_markers_use_unique_windows_casing(self):
        title = self.root / "Title"
        exe = self.executable(title / "Project/Binaries/Win64")
        (title / "engine").mkdir()
        (title / "Project/content").mkdir()
        self.assertEqual(self.detect(exe)[0], title)
        self.assertEqual(helper._game_layout_root(exe.parent), title)


class GenericSavedLayoutTests(unittest.TestCase):
    def test_nested_binary_expansion_preserves_saves_and_survives_source_removal(self):
        self.expand_layout("bin/x64")

    def test_single_binary_directory_expansion_preserves_saves(self):
        self.expand_layout("bin")

    def test_three_binary_directory_expansion_preserves_saves(self):
        self.expand_layout("bin/x64/win32")

    def expand_layout(self, relative):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "home"
            home.mkdir()
            source = base / "installed/Title"
            binary = source / relative
            binary.mkdir(parents=True)
            (binary / "game.exe").write_bytes(b"original")
            alias = base / "alias"
            alias.symlink_to(base / "installed", target_is_directory=True)
            # The alias is above the selected binary root, like /home -> /var/home.
            original = {"game_dir": str(alias / "Title" / relative), "exe": "game.exe", "working_dir": ".", "runner": "/usr/bin/fixture"}
            store = helper.Store("generic-layout", "klay-test", home=home)
            try:
                store.lock()
                helper.prepare_game(store, dict(original))
                (store.path / "game/save.dat").write_bytes(b"private save")
                (store.path / "prefix/save.dat").write_bytes(b"prefix save")
                (source / "data").mkdir()
                (source / "data/content.pak").write_bytes(b"content")
                root, exe, working = helper._normalize_game_layout(Path(original["game_dir"]), PurePosixPath("game.exe"), PurePosixPath("."))
                config = dict(original, game_dir=str(root), exe=str(exe), working_dir=str(working))
                helper.prepare_game(store, config)
                self.assertEqual((store.path / "game" / relative / "save.dat").read_bytes(), b"private save")
                self.assertEqual((store.path / "game/data/content.pak").read_bytes(), b"content")
                self.assertEqual((store.path / "prefix/save.dat").read_bytes(), b"prefix save")
                shutil.rmtree(source)
                stale = dict(original)
                helper.prepare_game(store, stale)
                self.assertEqual(stale["exe"], relative + "/game.exe")
                unrelated = dict(config, game_dir=str(base / "Another"))
                with self.assertRaisesRegex(helper.JailError, "different game directory"):
                    helper.prepare_game(store, unrelated)
            finally:
                store.close()


class SteamLibraryDetectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "home"
        self.home.mkdir()
        self.data = self.home / ".local/share/lutris"
        self.metadata = self.home / ".local/share/Steam/steamapps/libraryfolders.vdf"
        self.metadata.parent.mkdir(parents=True)
        self.external = self.home.parent / "External Games"

    def proton(self, library):
        build = library / "steamapps/common/Selected Proton"
        (build / "files/bin").mkdir(parents=True)
        for path in (build / "proton", build / "files/bin/wine"):
            path.write_text("fixture must never execute")
            path.chmod(0o700)
        return build

    def test_selected_proton_in_declared_external_steam_library(self):
        build = self.proton(self.external)
        self.metadata.write_text('"libraryfolders" { "0" { "path" "' + str(self.external) + '" "apps" { "123" "1" } } }')
        result = detector._runner({"version": "Selected Proton"}, self.data, self.home)
        self.assertEqual(result, {"runner_type": "proton", "runner": str(build)})

    def test_both_flatpak_steam_locations_find_selected_build_and_deduplicate_alias(self):
        flatpak = self.home / ".var/app/com.valvesoftware.Steam"
        legacy = flatpak / ".local/share/Steam"
        build = self.proton(legacy)
        (flatpak / "data").mkdir()
        (flatpak / "data/Steam").symlink_to(legacy, target_is_directory=True)
        self.assertEqual(helper._steam_library_roots(self.home).count(legacy), 1)
        result = detector._runner({"version": "Selected Proton"}, self.data, self.home)
        self.assertEqual(result, {"runner_type": "proton", "runner": str(build)})

    def test_old_library_format_and_aliases_are_deduplicated(self):
        self.external.mkdir()
        alias = self.home.parent / "Library Alias"
        alias.symlink_to(self.external, target_is_directory=True)
        self.metadata.write_text('"LibraryFolders" { "TimeNextStatsReport" "1" "1" "' + str(self.external) + '" "2" "' + str(alias) + '" }')
        roots = helper._steam_library_roots(self.home)
        self.assertEqual(roots.count(self.external), 1)

    def test_same_build_name_in_two_libraries_is_ambiguous(self):
        self.proton(self.external)
        other = self.home.parent / "Another Library"
        self.proton(other)
        self.metadata.write_text('"libraryfolders" { "0" { "path" "' + str(self.external) + '" } "1" { "path" "' + str(other) + '" } }')
        with self.assertRaisesRegex(detector.LutrisJailError, "More than one installed runner"):
            detector._runner({"version": "Selected Proton"}, self.data, self.home)

    def test_malformed_ambiguous_or_unbounded_metadata_fails_closed(self):
        for contents in (
            '"libraryfolders" { "0" { "path" "unfinished',
            '"libraryfolders" { "0" "/one" "0" "/two" }',
            '"libraryfolders" { "0" { "path" "relative/location" } }',
            '"libraryfolders" { "0" { "path" "/mnt/../private" } }',
            '"nested" {' * 10 + '}' * 10,
            ' ' * (1024 * 1024 + 1),
        ):
            with self.subTest(contents=contents[:70]):
                self.metadata.write_text(contents)
                with self.assertRaises(detector.LutrisJailError):
                    detector._runner({"version": "Selected Proton"}, self.data, self.home)

    def test_metadata_does_not_trigger_search_of_undeclared_library(self):
        self.proton(self.external)
        self.metadata.write_text('"libraryfolders" {}')
        with self.assertRaisesRegex(detector.LutrisJailError, "not installed"):
            detector._runner({"version": "Selected Proton"}, self.data, self.home)
