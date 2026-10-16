"""Read local Windows Shell Link metadata without invoking Windows or Wine.

The binary layout follows Microsoft's MS-SHLLINK specification. Path mapping
and installation boundaries belong to the caller; no shortcut is executed.
"""
from __future__ import annotations

from dataclasses import dataclass
import ntpath
import os
from pathlib import Path
import stat
import struct


MAX_SHORTCUT_BYTES = 1024 * 1024
_CLSID = bytes.fromhex("0114020000000000c000000000000046")


class ShortcutError(ValueError):
    """A shortcut cannot be resolved faithfully as a local game launch."""


@dataclass(frozen=True)
class WindowsShortcut:
    target: str | None
    relative_path: str | None
    working_dir: str | None
    arguments: str


def _text(value: str) -> str:
    # Some GOG installers include a terminating NUL in StringData's count.
    value = value.rstrip("\0")
    if any(character in value for character in ("\0", "\n", "\r")):
        raise ShortcutError("The Windows shortcut contains an invalid string.")
    return value


def parse_shortcut(data: bytes) -> WindowsShortcut:
    if len(data) > MAX_SHORTCUT_BYTES:
        raise ShortcutError("The Windows shortcut exceeds 1 MiB.")
    if len(data) < 76 or data[:4] != b"L\0\0\0" or data[4:20] != _CLSID:
        raise ShortcutError("The selected .lnk is not a valid Windows shortcut.")

    def number(offset: int, width: int = 4) -> int:
        if offset < 0 or offset + width > len(data):
            raise ShortcutError("The Windows shortcut is truncated.")
        return int.from_bytes(data[offset:offset + width], "little")

    flags = number(20)
    if flags & (0x400 | 0x1000 | 0x2000 | 0x20000):
        raise ShortcutError("This Windows shortcut requires activation, elevation, or a compatibility shim. Choose its game .exe directly.")
    position = 76
    if flags & 1:
        position += 2 + number(position, 2)
        if position > len(data):
            raise ShortcutError("The Windows shortcut has a truncated target ID list.")

    target = None
    if flags & 2:
        size = number(position)
        if size < 28 or position + size > len(data):
            raise ShortcutError("The Windows shortcut has invalid LinkInfo bounds.")
        info = data[position:position + size]
        header, info_flags = struct.unpack_from("<II", info, 4)
        if (header != 28 and header < 36) or header > size:
            raise ShortcutError("The Windows shortcut has an invalid LinkInfo header.")

        def terminated(offset: int, *, unicode: bool = False) -> str:
            if offset < header or offset >= size:
                raise ShortcutError("The Windows shortcut has an invalid LinkInfo string offset.")
            width = 2 if unicode else 1
            end = offset
            while end + width <= size:
                if info[end:end + width] == b"\0" * width:
                    try:
                        return _text(info[offset:end].decode("utf-16le" if unicode else "cp1252"))
                    except UnicodeError as error:
                        raise ShortcutError("The Windows shortcut has invalid text encoding.") from error
                end += width
            raise ShortcutError("The Windows shortcut has an unterminated LinkInfo string.")

        if not flags & 0x100 and info_flags & 1:
            base_offset = struct.unpack_from("<I", info, 16)[0]
            suffix_offset = struct.unpack_from("<I", info, 24)[0]
            unicode_base, unicode_suffix = struct.unpack_from("<II", info, 28) if header >= 36 else (0, 0)
            base = terminated(unicode_base, unicode=True) if unicode_base else terminated(base_offset)
            suffix = terminated(unicode_suffix, unicode=True) if unicode_suffix else terminated(suffix_offset)
            if ntpath.splitdrive(suffix)[0]:
                raise ShortcutError("The Windows shortcut has an absolute path suffix.")
            target = base.rstrip("\\/") + "\\" + suffix.lstrip("\\/") if suffix else base
        elif not flags & 0x100 and info_flags & 2:
            raise ShortcutError("Network Windows shortcuts are unavailable in an offline jail.")
        position += size

    strings = {}
    for bit, name in ((4, "name"), (8, "relative"), (16, "working"), (32, "arguments"), (64, "icon")):
        if not flags & bit:
            continue
        count = number(position, 2)
        position += 2
        length = count * (2 if flags & 128 else 1)
        if position + length > len(data):
            raise ShortcutError("The Windows shortcut has truncated StringData.")
        try:
            strings[name] = _text(data[position:position + length].decode("utf-16le" if flags & 128 else "cp1252"))
        except UnicodeError as error:
            raise ShortcutError("The Windows shortcut has invalid text encoding.") from error
        position += length

    environment_target = None
    while position < len(data):
        size = number(position)
        if size == 0:
            position += 4
            if any(data[position:]):
                raise ShortcutError("The Windows shortcut has unexpected trailing data.")
            break
        if size < 8 or position + size > len(data):
            raise ShortcutError("The Windows shortcut has invalid ExtraData bounds.")
        signature = number(position + 4)
        if signature == 0xA0000001:
            if size != 0x314 or environment_target is not None:
                raise ShortcutError("The Windows shortcut has invalid environment data.")
            block = data[position + 8:position + size]
            try:
                ansi_end = block[:260].find(b"\0")
                if ansi_end < 0:
                    raise ShortcutError("The Windows shortcut has unterminated environment data.")
                wide = block[260:]
                wide_end = next((offset for offset in range(0, len(wide), 2) if wide[offset:offset + 2] == b"\0\0"), None)
                if wide_end is None:
                    raise ShortcutError("The Windows shortcut has unterminated environment data.")
                environment_target = _text(wide[:wide_end].decode("utf-16le")) or _text(block[:ansi_end].decode("cp1252"))
            except UnicodeError as error:
                raise ShortcutError("The Windows shortcut has invalid environment text encoding.") from error
        position += size
    if flags & 0x200 and environment_target is None:
        raise ShortcutError("The Windows shortcut is missing its environment target.")
    if environment_target and (target is None or flags & 0x02000000):
        target = environment_target
    relative = strings.get("relative") or None
    if not target and not relative:
        raise ShortcutError("The Windows shortcut has no supported local file target. Choose its game .exe directly.")
    return WindowsShortcut(target or None, relative, strings.get("working") or None, strings.get("arguments", ""))


def read_shortcut(path: Path) -> WindowsShortcut:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SHORTCUT_BYTES:
            raise ShortcutError("The Windows shortcut must be a regular file no larger than 1 MiB.")
        return parse_shortcut(stream.read(MAX_SHORTCUT_BYTES + 1))


def split_windows_arguments(text: str) -> list[str]:
    """Split Windows command-line arguments, preserving literal backslashes."""
    if len(text) > 65536 or any(character in text for character in ("\0", "\n", "\r")):
        raise ShortcutError("The Windows shortcut arguments are invalid or too large.")
    arguments, position = [], 0
    while position < len(text):
        while position < len(text) and text[position] in " \t":
            position += 1
        if position == len(text):
            break
        value, quoted = [], False
        while position < len(text) and (quoted or text[position] not in " \t"):
            slashes = 0
            while position < len(text) and text[position] == "\\":
                slashes += 1
                position += 1
            if position < len(text) and text[position] == '"':
                value.append("\\" * (slashes // 2))
                if slashes % 2:
                    value.append('"')
                elif quoted and position + 1 < len(text) and text[position + 1] == '"':
                    value.append('"')
                    position += 1
                else:
                    quoted = not quoted
                position += 1
            else:
                value.append("\\" * slashes)
                if position < len(text) and (quoted or text[position] not in " \t"):
                    value.append(text[position])
                    position += 1
        arguments.append("".join(value))
        if len(arguments) > 128:
            raise ShortcutError("The Windows shortcut has too many arguments.")
    return arguments
