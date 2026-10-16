"""Resolve shortcut targets without Wine, Qt, or an optional YAML dependency."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from klay.qt import lutris_jail as detector
from test_windows_shortcut import environment_block, shortcut


class LutrisShortcutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "home"
        self.prefix = self.home / "Games/example-prefix"
        self.title = self.prefix / "drive_c/GOG Games/Example Title"
        self.title.mkdir(parents=True)
        self.exe = self.title / "EXAMPLE GAME.exe"
        self.exe.write_bytes(b"inert fixture: must never execute")
        (self.title / "EXAMPLE GAME_Data").mkdir()
        (self.title / "UnityPlayer.dll").write_bytes(b"fixture")
        self.link = self.title / "Launch Example.lnk"
        self.target = r"C:\GOG Games\Example Title\EXAMPLE GAME.exe"
        self.row = {"directory": str(self.prefix), "runner": "wine", "installed": 1, "configpath": "example"}

    def resolve(self, **options):
        game = {"exe": str(self.link), "prefix": str(self.prefix), **options}
        root = self.home / "config/lutris"
        def config(path, **kwargs):
            return {"game": game} if path == root / "games/example.yml" else {}
        # The filesystem-facing shortcut parser and all path checks remain real.
        # Only Lutris metadata and unrelated runner selection are fixtures.
        with patch.object(detector, "yaml", object()), \
                patch.object(detector, "_installation", return_value=(self.home / "data/lutris", self.row)), \
                patch.object(detector, "_config_root", return_value=root), \
                patch.object(detector, "_read_yaml", side_effect=config), \
                patch.object(detector, "_runner", return_value={"runner_type": "wine", "runner": "/inert/wine"}):
            return detector.resolve_lutris_jail({"game_id": "lutris_wine_1"}, host_home=self.home)

    def test_gog_shortcut_preserves_whole_unity_game(self):
        self.link.write_bytes(shortcut(target=self.target, working="C:\\GOG Games\\Example Title\0", arguments="\0", id_list=b"\0\0"))
        result = self.resolve()
        self.assertEqual(result["game_dir"], str(self.title))
        self.assertEqual(result["exe"], self.exe.name)
        self.assertEqual(result["working_dir"], ".")
        self.assertEqual(result["arguments"], [])

    def test_shortcut_arguments_precede_lutris_arguments_and_keep_windows_escaping(self):
        self.link.write_bytes(shortcut(target=self.target, arguments=r'--asset "C:\Game Data\file.dat" ""'))
        result = self.resolve(args='--label "configured words"')
        self.assertEqual(result["arguments"], ["--asset", r"C:\Game Data\file.dat", "", "--label", "configured words"])

    def test_shortcut_working_directory_takes_precedence_over_lutris(self):
        other = self.home / "unrelated"
        other.mkdir()
        self.link.write_bytes(shortcut(target=self.target, working=r"C:\GOG Games\Example Title"))
        self.assertEqual(self.resolve(working_dir=str(other))["working_dir"], ".")

    def test_relative_shortcut_target_uses_shortcut_location(self):
        self.link.write_bytes(shortcut(relative=".\\EXAMPLE GAME.exe"))
        result = self.resolve()
        self.assertEqual(result["game_dir"], str(self.title))
        self.assertEqual(result["exe"], self.exe.name)

    def test_c_drive_target_uses_unique_windows_casing(self):
        self.link.write_bytes(shortcut(target=self.target.lower()))
        self.assertEqual(self.resolve()["exe"], self.exe.name)

    def test_literal_percent_in_executable_name_is_not_an_environment_variable(self):
        target = self.title / "100% Game.exe"
        target.write_bytes(b"inert fixture")
        self.link.write_bytes(shortcut(target=r"C:\GOG Games\Example Title\100% Game.exe"))
        self.assertEqual(self.resolve()["exe"], target.name)

    def test_extended_local_windows_target_path(self):
        self.link.write_bytes(shortcut(target="\\\\?\\" + self.target))
        self.assertEqual(self.resolve()["exe"], self.exe.name)

    def test_direct_windows_executable_and_working_directory(self):
        result = self.resolve(exe=self.target, working_dir=r"C:\GOG Games\Example Title")
        self.assertEqual(result["game_dir"], str(self.title))
        self.assertEqual(result["exe"], self.exe.name)
        self.assertEqual(result["working_dir"], ".")

    def test_system_drive_environment_target(self):
        self.link.write_bytes(shortcut(extra=environment_block(r"%SystemDrive%\GOG Games\Example Title\EXAMPLE GAME.exe"), flags=0x200))
        self.assertEqual(self.resolve()["exe"], self.exe.name)

    def test_program_files_environment_target(self):
        for variable, container in (("ProgramFiles", "Program Files"), ("ProgramFiles(x86)", "Program Files (x86)")):
            with self.subTest(variable=variable):
                root = self.prefix / "drive_c" / container / "Another Game"
                root.mkdir(parents=True)
                target = root / "Actual.exe"
                target.write_bytes(b"inert fixture")
                self.link.write_bytes(shortcut(extra=environment_block(f"%{variable}%\\Another Game\\Actual.exe"), flags=0x200))
                result = self.resolve()
                self.assertEqual(result["game_dir"], str(root))
                self.assertEqual(result["exe"], "Actual.exe")

    def test_z_drive_target_uses_same_explicit_local_game_tree(self):
        dosdevices = self.prefix / "dosdevices"
        dosdevices.mkdir()
        (dosdevices / "z:").symlink_to("/", target_is_directory=True)
        self.link.write_bytes(shortcut(target="Z:" + str(self.exe).replace("/", "\\")))
        self.assertEqual(self.resolve()["game_dir"], str(self.title))

    def test_missing_target_does_not_substitute_another_exe(self):
        self.link.write_bytes(shortcut(target=r"C:\GOG Games\Example Title\Missing.exe"))
        with self.assertRaises(detector.LutrisJailError):
            self.resolve()

    def test_unsupported_target_types_require_manual_configuration(self):
        for target in (r"\\server\share\game.exe", r"\rooted\game.exe", r"D:\game.exe", r"C:game.exe", r"%UNRECOGNIZED_HOME%\game.exe", "https://example.invalid/game.exe"):
            with self.subTest(target=target):
                self.link.write_bytes(shortcut(target=target))
                with self.assertRaises(detector.LutrisJailError):
                    self.resolve()

    def test_conflicting_existing_targets_cannot_select_relative_substitute(self):
        other = self.title / "Different.exe"
        other.write_bytes(b"another inert executable")
        self.link.write_bytes(shortcut(target=self.target, relative="Different.exe"))
        with self.assertRaisesRegex(detector.LutrisJailError, "different existing executables"):
            self.resolve()

    def test_nonstandard_c_drive_mapping_is_not_assumed_to_be_drive_c(self):
        other = self.home / "different-drive"
        other.mkdir()
        dosdevices = self.prefix / "dosdevices"
        dosdevices.mkdir()
        (dosdevices / "c:").symlink_to(other, target_is_directory=True)
        self.link.write_bytes(shortcut(target=self.target))
        with self.assertRaises(detector.LutrisJailError):
            self.resolve()

    def test_nonstandard_z_drive_mapping_is_not_assumed_to_be_host_root(self):
        other = self.home / "different-drive"
        other.mkdir()
        dosdevices = self.prefix / "dosdevices"
        dosdevices.mkdir()
        (dosdevices / "z:").symlink_to(other, target_is_directory=True)
        self.link.write_bytes(shortcut(target="Z:" + str(self.exe).replace("/", "\\")))
        with self.assertRaises(detector.LutrisJailError):
            self.resolve()

    def test_shortcut_chains_are_not_followed(self):
        other = self.title / "Other.lnk"
        other.write_bytes(shortcut(target=self.target))
        self.link.write_bytes(shortcut(target=r"C:\GOG Games\Example Title\Other.lnk"))
        with self.assertRaises(detector.LutrisJailError):
            self.resolve()

    def test_symlink_inside_game_cannot_redirect_shortcut_target(self):
        self.exe.unlink()
        other = self.home / "private.exe"
        other.write_bytes(b"must remain outside game")
        self.exe.symlink_to(other)
        self.link.write_bytes(shortcut(target=self.target))
        with self.assertRaises(detector.LutrisJailError):
            self.resolve()

    def test_missing_exe_reports_the_configured_path(self):
        absent = self.title / "Missing.exe"
        with self.assertRaises(detector.LutrisJailError) as raised:
            self.resolve(exe=str(absent))
        self.assertIn(str(absent), str(raised.exception))


if __name__ == "__main__":
    unittest.main()
