"""Parse inert MS-SHLLINK fixtures; no Windows program is ever executed."""
from pathlib import Path
import os
import struct
import tempfile
import unittest

from klay.qt.windows_shortcut import (
    ShortcutError, parse_shortcut, read_shortcut, split_windows_arguments,
)


def link_info(base, suffix="", *, unicode=False):
    """Build the local-volume form from MS-SHLLINK section 2.3."""
    header_size = 36 if unicode else 28
    volume = struct.pack("<IIII", 17, 3, 0, 16) + b"\0"
    base_ansi = base.encode("cp1252", errors="replace") + b"\0"
    suffix_ansi = suffix.encode("cp1252", errors="replace") + b"\0"
    base_offset = header_size + len(volume)
    suffix_offset = base_offset + len(base_ansi)
    body = volume + base_ansi + suffix_ansi
    unicode_offsets = ()
    if unicode:
        base_unicode = base.encode("utf-16-le") + b"\0\0"
        suffix_unicode = suffix.encode("utf-16-le") + b"\0\0"
        unicode_offsets = (header_size + len(body), header_size + len(body) + len(base_unicode))
        body += base_unicode + suffix_unicode
    fields = (header_size + len(body), header_size, 1, header_size, base_offset, 0, suffix_offset)
    return struct.pack("<" + "I" * (7 + len(unicode_offsets)), *fields, *unicode_offsets) + body


def environment_block(target, *, unicode_target=None):
    ansi = (target.encode("cp1252") + b"\0").ljust(260, b"\0")
    wide = ((unicode_target if unicode_target is not None else target).encode("utf-16-le") + b"\0\0").ljust(520, b"\0")
    return struct.pack("<II", 0x314, 0xA0000001) + ansi + wide


def shortcut(*, target=None, suffix="", unicode_info=False, unicode_strings=True,
             name=None, relative=None, working=None, arguments=None, icon=None,
             id_list=None, extra=b"", flags=0):
    """Construct fields independently of the production parser."""
    payload = b""
    if id_list is not None:
        flags |= 1
        payload += struct.pack("<H", len(id_list)) + id_list
    if target is not None:
        flags |= 2
        payload += link_info(target, suffix, unicode=unicode_info)
    if unicode_strings:
        flags |= 0x80
    for bit, value in ((4, name), (8, relative), (16, working), (32, arguments), (64, icon)):
        if value is not None:
            flags |= bit
            encoded = value.encode("utf-16-le" if unicode_strings else "cp1252")
            count = len(encoded) // (2 if unicode_strings else 1)
            payload += struct.pack("<H", count) + encoded
    header = bytearray(76)
    struct.pack_into("<I", header, 0, 76)
    header[4:20] = bytes.fromhex("0114020000000000c000000000000046")
    struct.pack_into("<I", header, 20, flags)
    struct.pack_into("<I", header, 60, 1)  # SW_SHOWNORMAL
    return bytes(header) + payload + extra + b"\0\0\0\0"


class WindowsShortcutTests(unittest.TestCase):
    def test_gog_shape_with_id_list_ansi_target_and_nul_terminated_string_data(self):
        target = r"C:\GOG Games\Example\EXAMPLE GAME.exe"
        result = parse_shortcut(shortcut(
            target=target, id_list=b"\x04\0ab\0\0",
            working="C:\\GOG Games\\Example\0", arguments="\0",
            icon="C:\\GOG Games\\Example\\gog.ico\0",
        ))
        self.assertEqual(result.target, target)
        self.assertEqual(result.working_dir, r"C:\GOG Games\Example")
        self.assertEqual(result.arguments, "")
        self.assertIsNone(result.relative_path)

    def test_local_base_and_common_suffix_are_combined(self):
        result = parse_shortcut(shortcut(target="C:\\Games\\Example\\", suffix="bin\\game.exe"))
        self.assertEqual(result.target, r"C:\Games\Example\bin\game.exe")

    def test_unicode_link_info_takes_precedence_over_lossy_ansi(self):
        target = "C:\\Games\\宇宙\\"
        result = parse_shortcut(shortcut(target=target, suffix="遊戯.exe", unicode_info=True))
        self.assertEqual(result.target, target + "遊戯.exe")

    def test_ansi_local_path_uses_windows_code_page(self):
        result = parse_shortcut(shortcut(target="C:\\Jeux\\été\\jeu.exe"))
        self.assertEqual(result.target, "C:\\Jeux\\été\\jeu.exe")

    def test_string_data_order_and_utf16_code_unit_lengths(self):
        result = parse_shortcut(shortcut(
            name="Ignored description 🌌", relative=".\\bin\\game.exe",
            working="C:\\Games\\宇宙", arguments='--label "hello 🌌"', icon="Ignored.ico",
        ))
        self.assertEqual(result.relative_path, ".\\bin\\game.exe")
        self.assertEqual(result.working_dir, "C:\\Games\\宇宙")
        self.assertEqual(result.arguments, '--label "hello 🌌"')

    def test_ansi_string_data(self):
        result = parse_shortcut(shortcut(relative="jeu.exe", working="C:\\Jeux\\été", arguments="--été", unicode_strings=False))
        self.assertEqual(result.working_dir, "C:\\Jeux\\été")
        self.assertEqual(result.arguments, "--été")

    def test_environment_target_keeps_variables_literal(self):
        result = parse_shortcut(shortcut(extra=environment_block(r"%GAME_HOME%\game.exe"), flags=0x200))
        self.assertEqual(result.target, r"%GAME_HOME%\game.exe")

    def test_environment_unicode_target_preferred(self):
        result = parse_shortcut(shortcut(extra=environment_block(r"C:\Games\unknown.exe", unicode_target="C:\\遊戯.exe"), flags=0x200))
        self.assertEqual(result.target, "C:\\遊戯.exe")

    def test_force_no_link_info_keeps_relative_target(self):
        result = parse_shortcut(shortcut(target=r"C:\Stale\game.exe", relative="game.exe", flags=0x100))
        self.assertIsNone(result.target)
        self.assertEqual(result.relative_path, "game.exe")

    def test_altered_launch_semantics_are_rejected(self):
        for flag in (0x1000, 0x2000, 0x20000):
            with self.subTest(flag=hex(flag)), self.assertRaises(ShortcutError):
                parse_shortcut(shortcut(target=r"C:\Games\game.exe", flags=flag))

    def test_rejects_bad_header_or_class_id(self):
        valid = shortcut(relative="game.exe")
        for data in (b"", valid[:75], b"\0" * 76, valid[:4] + b"x" * 16 + valid[20:]):
            with self.subTest(size=len(data)), self.assertRaises(ShortcutError):
                parse_shortcut(data)

    def test_truncated_id_list_and_string_data_rejected(self):
        for data in (shortcut(id_list=b"\0\0")[:79], shortcut(relative="game.exe")[:-8]):
            with self.subTest(size=len(data)), self.assertRaises(ShortcutError):
                parse_shortcut(data)

    def test_link_info_offsets_cannot_escape_block(self):
        valid = shortcut(target=r"C:\Games\game.exe")
        for offset in (0, 12, 0xFFFFFFFF):
            malformed = bytearray(valid)
            struct.pack_into("<I", malformed, 76 + 16, offset)
            with self.subTest(offset=offset), self.assertRaises(ShortcutError):
                parse_shortcut(bytes(malformed))

    def test_link_info_size_and_header_are_bounded(self):
        valid = shortcut(target=r"C:\Games\game.exe")
        for field, value in ((0, 0xFFFFFFFF), (0, 20), (4, 32), (4, 0xFFFFFFFF)):
            malformed = bytearray(valid)
            struct.pack_into("<I", malformed, 76 + field, value)
            with self.subTest(field=field, value=value), self.assertRaises(ShortcutError):
                parse_shortcut(bytes(malformed))

    def test_embedded_nul_and_invalid_utf16_rejected(self):
        with self.assertRaises(ShortcutError):
            parse_shortcut(shortcut(relative="game\0.exe"))
        malformed = bytearray(shortcut(relative="x"))
        malformed[78:80] = b"\0\xd8"  # lone high surrogate
        with self.assertRaises(ShortcutError):
            parse_shortcut(bytes(malformed))

    def test_extra_blocks_cannot_truncate_or_loop(self):
        for size in (1, 4, 0xFFFFFFFF):
            data = shortcut(relative="game.exe", extra=struct.pack("<II", size, 0xA0000001))
            with self.subTest(size=size), self.assertRaises(ShortcutError):
                parse_shortcut(data)

    def test_read_shortcut_and_size_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "Launch.lnk"
            path.write_bytes(shortcut(relative="game.exe"))
            self.assertEqual(read_shortcut(path).relative_path, "game.exe")
            path.write_bytes(b"x" * (1024 * 1024 + 1))
            with self.assertRaises(ShortcutError):
                read_shortcut(path)

    def test_read_shortcut_rejects_symbolic_links_and_special_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = root / "Actual.lnk"
            actual.write_bytes(shortcut(relative="game.exe"))
            alias = root / "Alias.lnk"
            alias.symlink_to(actual)
            with self.assertRaises((OSError, ShortcutError)):
                read_shortcut(alias)
            pipe = root / "Pipe.lnk"
            os.mkfifo(pipe)
            with self.assertRaises(ShortcutError):
                read_shortcut(pipe)


class WindowsArgumentsTests(unittest.TestCase):
    def test_spaces_empty_arguments_and_windows_paths(self):
        self.assertEqual(split_windows_arguments(r'--label "words with spaces" "" C:\Games\file.dat'),
                         ["--label", "words with spaces", "", r"C:\Games\file.dat"])

    def test_literal_shell_metacharacters_are_only_data(self):
        self.assertEqual(split_windows_arguments('"$(touch sentinel)" ; &'), ["$(touch sentinel)", ";", "&"])

    def test_escaped_quotes_and_backslashes_before_closing_quote(self):
        self.assertEqual(split_windows_arguments(r'"a\"b" "C:\Games\\"'), ['a"b', "C:\\Games\\"])


if __name__ == "__main__":
    unittest.main()
