#!/usr/bin/env python3
"""Isolated host-side jail launcher. This module deliberately uses only stdlib.

The Qt client may send this source to a host Python with ``-I -c``. Never import
Klay here: a Flatpak's Python and filesystem are different from the host's.
The jail is a filesystem/network boundary, not a VM or a malware scanner.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import selectors
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time
import uuid


MAX_REQUEST = 65536
MAX_LOG = 4 * 1024 * 1024
REPORT_LOG = 32768
MAX_FILES = 200000
MAX_COPY_BYTES = 150 * 1024**3
COPY_TIMEOUT = 600
START_TIMEOUT = 30
READY_PREFIX = "KLAY_JAIL_READY:"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class JailError(Exception):
    """An actionable configuration or confinement failure."""


def _process_start_ticks(pid: int) -> int:
    # comm may contain spaces or parentheses; starttime is field 22.
    value = (Path("/proc") / str(pid) / "stat").read_text()
    return int(value[value.rfind(")") + 2:].split()[19])


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _host_home() -> Path:
    # Fedora Atomic systems commonly alias /home to /var/home. Resolve trusted
    # account metadata, rather than inheriting HOME from an application's env.
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)


def _source_path(configured: str) -> Path:
    path = Path(configured)
    if stat.S_ISLNK(path.lstat().st_mode):
        raise JailError("The selected game directory cannot itself be a symlink.")
    # Legitimate host ancestor aliases (/home -> /var/home) are allowed. All
    # content below this directory is still opened with O_NOFOLLOW.
    return path.parent.resolve(strict=True) / path.name


def _same_path(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _relative_within(path: Path, directory: Path) -> Path | None:
    """Treat bind aliases as the same directory, even when resolve() differs."""
    if path.is_relative_to(directory):
        return path.relative_to(directory)
    for parent in path.parents:
        if _same_path(parent, directory):
            return path.relative_to(parent)
    return Path(".") if _same_path(path, directory) else None


def _within(path: Path, directory: Path) -> bool:
    return _relative_within(path, directory) is not None


def _suitable_game_root(path: Path, prefix: Path | None, home: Path) -> bool:
    """Bound automatic source selection; never infer a shared storage root."""
    canonical = path.resolve()
    broad = {home, *home.parents, *(Path(p) for p in (
        "/usr", "/opt", "/mnt", "/media", "/run/media", "/var", "/var/mnt", "/tmp", "/var/tmp",
    ))}
    broad.update((home / part).resolve() for part in (
        ".local", ".local/share", ".config", ".var", ".var/app",
        "Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "Games",
        ".wine", ".steam", ".steam/steam", ".steam/root", ".local/share/Steam",
        ".local/share/Steam/steamapps", ".local/share/Steam/compatibilitytools.d",
        ".local/share/lutris", ".local/share/lutris/runners", ".local/share/lutris/runners/wine",
        ".var/app/net.lutris.Lutris", ".var/app/net.lutris.Lutris/data/lutris",
        ".var/app/com.valvesoftware.Steam", ".var/app/com.valvesoftware.Steam/data/Steam",
        ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ))
    return (
        not any(_same_path(canonical, root.resolve()) for root in broad)
        and (prefix is None or not _same_path(canonical, prefix.resolve()))
        and path.name.casefold() not in (
            "drive_c", "dosdevices", "program files", "program files (x86)", "games", "gog games",
            "steamapps", "common", "windows", "users",
        )
        and not any((path / marker).exists() for marker in ("system.reg", "dosdevices", "drive_c"))
    )


def _layout_directory(parent: Path, name: str) -> Path | None:
    """Find a unique case-insensitive layout marker without following links."""
    if not parent.is_dir():
        return None
    matches = [entry for entry in parent.iterdir() if entry.name.casefold() == name.casefold()]
    if len(matches) != 1:
        return None
    marker = matches[0]
    if marker.is_symlink():
        raise JailError("The game layout contains a symlink; choose an ordinary game folder.")
    return marker if marker.is_dir() else None


_BINARY_DIRS = {"bin", "binaries", "x64", "x86", "win64", "win32", "amd64", "bin64", "bin32"}


def _binary_game_root(binary: Path, prefix: Path | None, home: Path) -> Path | None:
    """Infer bounded binary layouts with evidence of the installation root."""
    if not binary.is_dir():
        return None
    if binary.name.casefold() in ("win32", "win64") and binary.parent.name.casefold() == "binaries":
        # This may be only the project portion of a larger Unreal package.
        # Its enclosing Engine tree must be identified by the Unreal resolver.
        return None
    candidate = binary
    for _ in range(3):
        if candidate.name.casefold() not in _BINARY_DIRS:
            break
        candidate = candidate.parent
    if candidate == binary or candidate.name.casefold() in _BINARY_DIRS:
        return None
    if not _suitable_game_root(candidate, prefix, home):
        return None
    if candidate.parent.name.casefold() in {"games", "gog games", "program files", "program files (x86)"} or (
        candidate.parent.name.casefold() == "common" and candidate.parent.parent.name.casefold() == "steamapps"
    ):
        return candidate
    if any(_layout_directory(candidate, name) for name in ("data", "content", "assets", "resources")):
        return candidate
    return None


def _read_libraryfolders(path: Path) -> list[Path]:
    """Read Steam's bounded KeyValues metadata, never executing Steam code."""
    limit = 1024 * 1024
    with path.open("r", encoding="utf-8-sig") as stream:
        contents = stream.read(limit + 1)
    if len(contents) > limit:
        raise JailError(f"Steam library metadata exceeds 1 MiB: {path}")
    token_pattern = re.compile(r'\s+|//[^\n]*|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+')
    tokens = []
    position = 0
    while position < len(contents):
        match = token_pattern.match(contents, position)
        if match is None:
            raise JailError(f"Steam library metadata has invalid quoting: {path}")
        raw = match.group()
        position = match.end()
        if raw.isspace() or raw.startswith("//"):
            continue
        quoted = raw.startswith('"')
        value = re.sub(r'\\([\\"])', r'\1', raw[1:-1]) if quoted else raw
        tokens.append((value, not quoted and raw in ("{", "}")))
        if len(tokens) > 50000:
            raise JailError(f"Steam library metadata contains too many entries: {path}")
    cursor = 0

    def mapping(depth: int = 0) -> dict:
        nonlocal cursor
        if depth > 8:
            raise JailError(f"Steam library metadata is nested too deeply: {path}")
        result = {}
        while cursor < len(tokens):
            key, structural = tokens[cursor]
            cursor += 1
            if structural and key == "}" and depth:
                return result
            if structural or cursor >= len(tokens) or key.casefold() in result:
                raise JailError(f"Steam library metadata is malformed or ambiguous: {path}")
            value, structural = tokens[cursor]
            cursor += 1
            if structural and value == "{":
                value = mapping(depth + 1)
            elif structural:
                raise JailError(f"Steam library metadata is malformed: {path}")
            result[key.casefold()] = value
        if depth:
            raise JailError(f"Steam library metadata has an unclosed section: {path}")
        return result

    document = mapping()
    libraries = document.get("libraryfolders")
    if not isinstance(libraries, dict):
        raise JailError(f"Steam library metadata has no LibraryFolders section: {path}")
    result = []
    for key, value in libraries.items():
        if not key.isdecimal():
            continue
        location = value.get("path") if isinstance(value, dict) else value
        if location is None:
            continue
        if not isinstance(location, str) or any(c in location for c in "\x00\n\r"):
            raise JailError(f"Steam library metadata has an invalid library path: {path}")
        root = Path(location)
        if not root.is_absolute() or ".." in root.parts:
            raise JailError(f"Steam library metadata requires absolute library paths: {path}")
        result.append(root)
    return result


def _steam_library_roots(home: Path) -> list[Path]:
    """Find default Steam installs and only the external libraries they declare."""
    roots = []

    def add(path: Path) -> None:
        canonical = path.resolve()
        if not any(_same_path(canonical, existing) for existing in roots):
            roots.append(canonical)

    for path in (
        home / ".local/share/Steam", home / ".steam/steam", home / ".steam/root",
        home / ".var/app/com.valvesoftware.Steam/data/Steam",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ):
        add(path)
    for root in list(roots):
        for relative in ("steamapps/libraryfolders.vdf", "config/libraryfolders.vdf"):
            metadata = root / relative
            if metadata.is_file():
                try:
                    for library in _read_libraryfolders(metadata):
                        add(library)
                except (OSError, UnicodeError) as error:
                    raise JailError(f"Cannot read Steam library metadata {metadata}: {error}") from error
    return roots


def _response(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=True), flush=True)


def _open_directory(path: Path) -> int:
    """Walk every component without following a symlink, retaining directory fds."""
    if not path.is_absolute():
        raise JailError("Game directory must be an absolute path.")
    current = os.open("/", DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            if part in (".", ".."):
                raise JailError("Parent traversal is not allowed.")
            next_fd = os.open(part, DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def _regular_at(directory: int, relative: str) -> int:
    current = os.dup(directory)
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            next_fd = os.open(part, DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = next_fd
        result = os.open(parts[-1], FILE_FLAGS, dir_fd=current)
        if not stat.S_ISREG(os.fstat(result).st_mode):
            os.close(result)
            raise JailError("The selected executable must be a regular file.")
        return result
    finally:
        os.close(current)


def _directory_at(directory: int, relative: str) -> int:
    current = os.dup(directory)
    try:
        for part in PurePosixPath(relative).parts:
            next_fd = os.open(part, DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except BaseException:
        os.close(current)
        raise


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict) or config.get("enabled") is not True:
        raise JailError("Enable the jail and configure a Windows executable first.")
    game_dir = config.get("game_dir")
    exe = config.get("exe")
    runner = config.get("runner")
    runner_type = config.get("runner_type")
    runner_dir = config.get("runner_dir")
    arguments = config.get("arguments", [])
    working_dir = config.get("working_dir")
    for label, value in (("game directory", game_dir), ("executable", exe), ("runner", runner)):
        if not isinstance(value, str) or not value or "\x00" in value or "\n" in value:
            raise JailError(f"Choose a valid {label}.")
    if not os.path.isabs(game_dir) or ".." in Path(game_dir).parts:
        raise JailError("Game directory must be absolute, without parent traversal.")
    relative = PurePosixPath(exe)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or not exe.lower().endswith(".exe"):
        raise JailError("Choose a .exe path relative to the game directory.")
    if "\\" in exe:
        raise JailError("Use Linux path separators for the relative executable.")
    if working_dir is None:
        working_dir = str(relative.parent)
    elif not isinstance(working_dir, str) or not working_dir or any(character in working_dir for character in ("\x00", "\n", "\\")):
        raise JailError("Working directory must be a relative path within the game folder.")
    working_path = PurePosixPath(working_dir)
    if working_path.is_absolute() or ".." in working_path.parts:
        raise JailError("Working directory must stay within the game folder.")
    if not isinstance(arguments, list) or len(arguments) > 128 or any(
        not isinstance(arg, str) or "\x00" in arg for arg in arguments
    ) or sum(map(len, arguments)) > MAX_REQUEST:
        raise JailError("Game arguments must be a list of strings, at most 64 KiB.")
    if runner_type not in ("wine", "proton"):
        raise JailError("Jails support direct system Wine or a selected Proton build.")
    if runner_type == "wine" and not os.path.isabs(runner):
        if "/" in runner:
            raise JailError("Select a Wine command on PATH or an absolute executable path.")
        runner = shutil.which(runner)
        if not runner:
            raise JailError("Selected Wine command was not found on the host.")
    if not os.path.isabs(runner):
        raise JailError("Select an absolute Proton directory.")
    resolved_runner = Path(runner).resolve(strict=True)
    resolved_runner_dir = None
    if runner_type == "wine":
        if not resolved_runner.is_file() or not os.access(resolved_runner, os.X_OK):
            raise JailError("Selected Wine runner is not executable.")
        if runner_dir:
            if not isinstance(runner_dir, str) or not os.path.isabs(runner_dir) or any(character in runner_dir for character in ("\x00", "\n")):
                raise JailError("Select an absolute directory for the specific Wine build.")
            resolved_runner_dir = Path(runner_dir).resolve(strict=True)
            home = _host_home()
            broad_roots = {Path("/"), Path("/usr"), Path("/opt"), Path("/usr/local"), home, *home.parents}
            for data in (home / ".local/share/lutris", home / ".var/app/net.lutris.Lutris/data/lutris"):
                broad_roots.update({data.resolve(), (data / "runners").resolve(), (data / "runners/wine").resolve()})
            broad_roots.update({(home / ".local/share/Steam").resolve(), (home / ".steam").resolve(), (home / ".steam/steam").resolve(), (home / ".local/share/Steam/compatibilitytools.d").resolve()})
            if any(_same_path(resolved_runner_dir, root) for root in broad_roots) or not resolved_runner_dir.is_dir():
                raise JailError("Select only a specific Wine build, not a home, Steam, or Lutris parent directory.")
            runner_relative = _relative_within(resolved_runner, resolved_runner_dir)
            if runner_relative is None:
                raise JailError("The Wine executable must stay inside the selected Wine build.")
            layouts = ("bin/wine", "bin/wine64", "files/bin/wine", "files/bin/wine64", "dist/bin/wine", "dist/bin/wine64")
            if not any((resolved_runner_dir / relative).is_file() and _same_path((resolved_runner_dir / relative).resolve(), resolved_runner) for relative in layouts):
                raise JailError("The selected Wine build must contain bin/wine or files/bin/wine.")
            resolved_runner = resolved_runner_dir / runner_relative
            # Upgrade settings saved by the first Lutris detector. Proton's
            # Wine alone skips its DXVK/VKD3D and prefix initialization.
            if runner_relative.as_posix() in layouts[2:] and os.path.lexists(resolved_runner_dir / "proton"):
                entry = (resolved_runner_dir / "proton").resolve(strict=True)
                if not entry.is_relative_to(resolved_runner_dir) or not entry.is_file() or not os.access(entry, os.X_OK):
                    raise JailError("Selected Proton directory has no valid executable proton entry point.")
                runner_type, resolved_runner = "proton", resolved_runner_dir
                resolved_runner_dir = None
            elif runner_relative.as_posix().startswith("dist/"):
                raise JailError("A legacy dist Wine layout requires its Proton entry point.")
        elif not resolved_runner.is_relative_to(Path("/usr")):
            raise JailError("Select a system Wine executable installed under /usr.")
    else:
        if not resolved_runner.is_dir():
            raise JailError("Select the directory containing Proton's proton script.")
        entry = (resolved_runner / "proton").resolve(strict=True)
        if not entry.is_relative_to(resolved_runner) or not entry.is_file() or not os.access(entry, os.X_OK):
            raise JailError("Selected Proton directory has no executable proton entry point.")
        if resolved_runner == Path("/") or _same_path(resolved_runner, _host_home()):
            raise JailError("Select only a specific Proton build directory.")
    game_path, relative, working_path = _normalize_game_layout(Path(game_dir), relative, working_path)
    if _within(resolved_runner, game_path):
        raise JailError("The trusted runner must be outside the game's directory.")
    if runner_type == "proton" and _within(game_path, resolved_runner):
        raise JailError("The trusted Proton build and game directories must be separate.")
    if resolved_runner_dir:
        canonical_game = game_path.resolve()
        if _within(canonical_game, resolved_runner_dir) or _within(resolved_runner_dir, canonical_game):
            raise JailError("The trusted Wine build and game directories must be separate.")
    return dict(config, game_dir=str(game_path), exe=str(relative), runner_type=runner_type, runner=str(resolved_runner), runner_dir=str(resolved_runner_dir) if resolved_runner_dir else None, working_dir=str(working_path), arguments=arguments)


def check_dependencies() -> tuple[str, str]:
    if sys.platform != "linux":
        raise JailError("Game jails require Linux.")
    if sys.version_info < (3, 11):
        raise JailError("Install host Python 3.11 or newer to use game jails.")
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise JailError("Install bubblewrap (bwrap) on the host to use game jails.")
    python = Path("/usr/bin/python3")
    if not python.is_file():
        raise JailError("Install host Python 3 at /usr/bin/python3 to use game jails.")
    return str(Path(bwrap).resolve()), str(python)


class Store:
    """Trusted state lives beside, and is never mounted with, mutable game data."""

    def __init__(self, game_id: str, data_dir_name: str, home: Path | None = None):
        if not game_id or len(game_id) > 512:
            raise JailError("A stable game ID is required.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", data_dir_name) or data_dir_name in (".", ".."):
            raise JailError("Invalid application data directory name.")
        self.game_id, self.data_dir_name = game_id, data_dir_name
        home = (home or _host_home()).resolve(strict=True)
        self.path = home / ".local" / "share" / data_dir_name / "jails" / hashlib.sha256(game_id.encode()).hexdigest()
        current = _open_directory(home)
        try:
            for component in (".local", "share", data_dir_name, "jails", self.path.name):
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(component, DIRECTORY_FLAGS, dir_fd=current)
                info = os.fstat(child)
                if info.st_uid != os.getuid() or info.st_mode & 0o022:
                    os.close(child)
                    raise JailError("Jail storage must be owned by you and not writable by other users.")
                os.close(current)
                current = child
            self.fd = current
            current = -1
            self.logs_fd = self.directory("logs")
            self.lock_fd = None
            accounts = {
                "account-passwd": f"klay:x:{os.getuid()}:{os.getgid()}:Klay:/home/klay:/bin/sh\n",
                "account-group": f"klay:x:{os.getgid()}:\n",
                "account-nsswitch": "passwd: files\ngroup: files\nhosts: files\n",
                "account-machine-id": hashlib.sha256(("klay-jail:" + data_dir_name + ":" + game_id).encode()).hexdigest()[:32] + "\n",
            }
            for name, contents in accounts.items():
                self.write_bytes(name, contents.encode())
        finally:
            if current >= 0:
                os.close(current)

    def directory(self, name: str) -> int:
        try:
            os.mkdir(name, 0o700, dir_fd=self.fd)
        except FileExistsError:
            pass
        child = os.open(name, DIRECTORY_FLAGS, dir_fd=self.fd)
        info = os.fstat(child)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(child)
            raise JailError(f"Jail {name} directory must have private permissions.")
        return child

    def lock(self) -> None:
        self.lock_fd = os.open("launch.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.fd)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise JailError("This game's jail is already running or being prepared.") from exc
        self.write_json("owner.json", {"pid": os.getpid(), "start_ticks": _process_start_ticks(os.getpid())})

    def read_json(self, name: str) -> dict | None:
        try:
            fd = os.open(name, FILE_FLAGS, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise JailError("Invalid trusted jail metadata.")
            raw = stream.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise JailError("Jail metadata is too large.")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise JailError("Invalid jail metadata.")
        return result

    def write_json(self, name: str, value: dict) -> None:
        self.write_bytes(name, json.dumps(value, ensure_ascii=True).encode())

    def write_bytes(self, name: str, value: bytes) -> None:
        temporary = f".state-{uuid.uuid4().hex}"
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def close(self) -> None:
        for fd in (self.lock_fd, self.logs_fd, self.fd):
            if fd is not None:
                os.close(fd)


def _copy_tree(source: int, target: int, budget: dict) -> None:
    """Copy regular files/directories only, without pathname or symlink races."""
    for name in os.listdir(source):
        if time.monotonic() > budget["deadline"]:
            raise JailError("Copying the game exceeded ten minutes; no game was started.")
        budget["files"] += 1
        if budget["files"] > MAX_FILES:
            raise JailError("The game contains too many files for a jail snapshot.")
        before = os.stat(name, dir_fd=source, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            src = os.open(name, DIRECTORY_FLAGS, dir_fd=source)
            try:
                if (before.st_dev, before.st_ino) != (os.fstat(src).st_dev, os.fstat(src).st_ino):
                    raise JailError("Game files changed during copying; try again.")
                os.mkdir(name, 0o700, dir_fd=target)
                dest = os.open(name, DIRECTORY_FLAGS, dir_fd=target)
                try:
                    _copy_tree(src, dest, budget)
                finally:
                    os.close(dest)
            finally:
                os.close(src)
        elif stat.S_ISREG(before.st_mode):
            src = os.open(name, FILE_FLAGS, dir_fd=source)
            try:
                opened = os.fstat(src)
                if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise JailError("Game files changed during copying; try again.")
                if budget["bytes"] + opened.st_size > MAX_COPY_BYTES:
                    raise JailError("The game exceeds the 150 GiB jail snapshot limit.")
                dest = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o700 if opened.st_mode & 0o111 else 0o600, dir_fd=target)
                try:
                    while True:
                        chunk = os.read(src, 1024 * 1024)
                        if not chunk:
                            break
                        budget["bytes"] += len(chunk)
                        if budget["bytes"] > MAX_COPY_BYTES:
                            raise JailError("The game exceeds the 150 GiB jail snapshot limit.")
                        offset = 0
                        while offset < len(chunk):
                            offset += os.write(dest, chunk[offset:])
                        if time.monotonic() > budget["deadline"]:
                            raise JailError("Copying the game exceeded ten minutes; no game was started.")
                    after = os.fstat(src)
                    if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise JailError("Game files changed during copying; try again.")
                finally:
                    os.close(dest)
            finally:
                os.close(src)
        else:
            raise JailError(f"Game contains a symlink or special file: {name!r}. Jails accept regular files and directories only.")


def _unreal_layout_root(binary_dir: Path) -> Path | None:
    """Recognize only a complete packaged Unreal installation, not a prefix."""
    if binary_dir.name.casefold() not in ("win32", "win64") or binary_dir.parent.name.casefold() != "binaries":
        return None
    project = binary_dir.parent.parent
    root = project.parent
    home = _host_home()
    if not _suitable_game_root(root, None, home) or not root.is_dir():
        return None
    markers = (_layout_directory(root, "Engine"), _layout_directory(project, "Content"))
    if not all(markers):
        return None
    canonical = _source_path(str(root))
    source = _open_directory(canonical)
    try:
        for path in (*(str(marker.relative_to(root)) for marker in markers), str(binary_dir.relative_to(root))):
            fd = _directory_at(source, path)
            os.close(fd)
    finally:
        os.close(source)
    return canonical


def _game_layout_root(binary: Path) -> Path | None:
    root = _unreal_layout_root(binary)
    if root is not None:
        return root
    root = _binary_game_root(binary, None, _host_home())
    if root is None:
        return None
    canonical = _source_path(str(root))
    source = _open_directory(canonical)
    try:
        directory = _directory_at(source, str(binary.relative_to(root)))
        os.close(directory)
    finally:
        os.close(source)
    return canonical


def _valid_layout_relative(path: PurePosixPath) -> bool:
    if path.is_absolute() or ".." in path.parts or not 1 <= len(path.parts) <= 3:
        return False
    return all(part.casefold() in _BINARY_DIRS for part in path.parts) or (
        len(path.parts) == 3 and path.parts[1].casefold() == "binaries"
        and path.parts[2].casefold() in ("win32", "win64")
    )


def _normalize_game_layout(game: Path, exe: PurePosixPath, working: PurePosixPath) -> tuple[Path, PurePosixPath, PurePosixPath]:
    if len(exe.parts) != 1:
        return game, exe, working
    root = _game_layout_root(game)
    if root is None:
        return game, exe, working
    relative = _relative_within(game, root)
    if relative is None:
        raise JailError("The detected game layout does not contain the selected binary folder.")
    relative = PurePosixPath(relative)
    return root, relative / exe, relative / working


def _layout_relative(config: dict, snapshot: dict) -> str | None:
    """Authorize only the old executable-directory copy of this same game."""
    relative = PurePosixPath(config["exe"]).parent
    if not _valid_layout_relative(relative):
        return None
    root = Path(config["game_dir"])
    old_source = snapshot.get("source")
    if not isinstance(old_source, str) or not _same_path(Path(old_source), root / str(relative)):
        return None
    detected = _game_layout_root(root / str(relative))
    if detected is None or not _same_path(detected, root):
        return None
    return str(relative)


def _apply_saved_layout(config: dict, snapshot: dict | None) -> None:
    """Keep old saved settings usable after the original installer is removed."""
    previous = snapshot.get("previous_source") if snapshot else None
    if not isinstance(previous, str) or Path(config["game_dir"]).resolve() != Path(previous).resolve() or len(PurePosixPath(config["exe"]).parts) != 1:
        return
    source, relative = snapshot.get("source"), snapshot.get("layout_relative")
    if not isinstance(source, str) or not Path(source).is_absolute() or not isinstance(relative, str):
        raise JailError("Invalid saved jail layout metadata.")
    path = PurePosixPath(relative)
    if not _valid_layout_relative(path) or (Path(source) / relative).resolve() != Path(config["game_dir"]).resolve():
        raise JailError("Invalid saved jail layout paths.")
    config.update(game_dir=source, exe=str(path / config["exe"]), working_dir=str(path / config["working_dir"]))


def _resume_layout(store: Store, config: dict, transaction: dict) -> None:
    """Resume a trusted rename transaction without traversing game contents."""
    if store.lock_fd is None:
        raise JailError("Launch this game again to recover its jail layout while holding its launch lock.")
    stage, backup = transaction.get("stage"), transaction.get("backup")
    if not isinstance(stage, str) or not re.fullmatch(r"\.layout-new-[a-f0-9]{32}", stage) or backup != stage.replace(".layout-new-", ".layout-old-", 1):
        raise JailError("Invalid jail layout recovery metadata; the existing copy was preserved.")
    old, new, relative = transaction.get("old"), transaction.get("new"), transaction.get("relative")
    if not isinstance(old, dict) or not isinstance(new, dict) or new.get("source") != config["game_dir"] or not isinstance(relative, str):
        raise JailError("The pending jail layout belongs to a different source; its existing copy was preserved.")
    path = PurePosixPath(relative)
    if not _valid_layout_relative(path) or path != PurePosixPath(config["exe"]).parent or old.get("source") != str(Path(config["game_dir"]) / relative):
        raise JailError("Invalid jail layout recovery paths; the existing copy was preserved.")
    current = store.read_json("snapshot.json")
    if current != old and current != new:
        raise JailError("Jail snapshot changed during layout recovery; its existing copy was preserved.")
    exists = lambda name: os.path.lexists(store.path / name)
    if exists(stage):
        staged = os.open(stage, DIRECTORY_FLAGS, dir_fd=store.fd)
        try:
            parent = _directory_at(staged, str(path.parent))
            try:
                target_exists = os.path.lexists(store.path / stage / relative)
                if exists("game"):
                    if exists(backup) or target_exists:
                        raise JailError("Ambiguous jail layout recovery; all copies were preserved.")
                    fd = os.open("game", DIRECTORY_FLAGS, dir_fd=store.fd)
                    os.close(fd)
                    os.rename("game", backup, src_dir_fd=store.fd, dst_dir_fd=store.fd)
                    os.fsync(store.fd)
                if exists(backup):
                    if target_exists:
                        raise JailError("Ambiguous jail layout backup; all copies were preserved.")
                    fd = os.open(backup, DIRECTORY_FLAGS, dir_fd=store.fd)
                    os.close(fd)
                    os.rename(backup, path.name, src_dir_fd=store.fd, dst_dir_fd=parent)
                    os.fsync(parent)
                    os.fsync(store.fd)
                else:
                    fd = os.open(path.name, DIRECTORY_FLAGS, dir_fd=parent)
                    os.close(fd)
                os.rename(stage, "game", src_dir_fd=store.fd, dst_dir_fd=store.fd)
                os.fsync(store.fd)
            finally:
                os.close(parent)
        finally:
            os.close(staged)
    else:
        if exists(backup):
            raise JailError("Jail layout staging is missing; the previous copy is preserved in its backup.")
        fd = os.open("game", DIRECTORY_FLAGS, dir_fd=store.fd)
        try:
            child = _directory_at(fd, relative)
            os.close(child)
        finally:
            os.close(fd)
    store.write_json("snapshot.json", new)
    os.unlink("layout-transaction.json", dir_fd=store.fd)
    os.fsync(store.fd)


def _expand_layout(store: Store, config: dict, prepared: dict, relative: str) -> None:
    if store.lock_fd is None:
        raise JailError("Launch this game again to expand its jail layout while holding its launch lock.")
    source_path = _source_path(config["game_dir"])
    if _within(store.path, source_path) or _within(Path(config["runner"]), source_path):
        raise JailError("Game and runner directories must be outside jail storage and separate from each other.")
    stage = ".layout-new-" + uuid.uuid4().hex
    os.mkdir(stage, 0o700, dir_fd=store.fd)
    source = _open_directory(source_path)
    target = os.open(stage, DIRECTORY_FLAGS, dir_fd=store.fd)
    recorded = False
    try:
        budget = {"files": 0, "bytes": 0, "deadline": time.monotonic() + COPY_TIMEOUT}
        _copy_tree(source, target, budget)
        if not shutil.rmtree.avoids_symlink_attacks:
            raise JailError("Host Python cannot safely prepare the expanded jail layout.")
        parent = _directory_at(target, str(PurePosixPath(relative).parent))
        try:
            shutil.rmtree(PurePosixPath(relative).name, dir_fd=parent)
        finally:
            os.close(parent)
        updated = dict(prepared, source=config["game_dir"], expanded_at=_now(), previous_source=str(Path(config["game_dir"]) / relative), layout_relative=relative, files=budget["files"], bytes=budget["bytes"])
        # Normalize harmless ancestor aliases before recording exact recovery paths.
        previous = dict(prepared, source=str(Path(config["game_dir"]) / relative))
        transaction = {"stage": stage, "backup": stage.replace(".layout-new-", ".layout-old-", 1), "relative": relative, "old": previous, "new": updated}
        if previous != prepared:
            store.write_json("snapshot.json", previous)
        store.write_json("layout-transaction.json", transaction)
        recorded = True
        _resume_layout(store, config, transaction)
    finally:
        os.close(source)
        os.close(target)
        if not recorded and not os.path.lexists(store.path / "layout-transaction.json") and os.path.lexists(store.path / stage):
            if not shutil.rmtree.avoids_symlink_attacks:
                raise JailError("Host Python cannot safely remove a partial jail copy.")
            shutil.rmtree(stage, dir_fd=store.fd)


def prepare_game(store: Store, config: dict) -> None:
    runner_path = Path(config["runner"])
    if _within(runner_path, store.path):
        raise JailError("Game and runner directories must be outside jail storage.")
    if config.get("runner_dir"):
        wine_root = Path(config["runner_dir"])
        if _within(wine_root, store.path) or _within(store.path, wine_root):
            raise JailError("The trusted Wine build must be outside jail storage.")
    transaction = store.read_json("layout-transaction.json")
    _apply_saved_layout(config, transaction.get("new") if transaction else store.read_json("snapshot.json"))
    if transaction:
        _resume_layout(store, config, transaction)
    prepared = store.read_json("snapshot.json")
    if prepared:
        if prepared.get("source") != config["game_dir"]:
            relative = _layout_relative(config, prepared)
            if relative is None:
                raise JailError("This jail already has a copy of a different game directory.")
            _expand_layout(store, config, prepared, relative)
        # Mount roots cannot be replaced from inside the sandbox. Verify them,
        # but never create bootstrap files in the mutable game/prefix/home.
        for name in ("game", "prefix", "home"):
            fd = os.open(name, DIRECTORY_FLAGS, dir_fd=store.fd)
            os.close(fd)
        return
    source_path = _source_path(config["game_dir"])
    if _within(store.path, source_path) or _within(runner_path, source_path):
        raise JailError("Game and runner directories must be outside jail storage and separate from each other.")
    # A failed preparation may have published a copy before its metadata was
    # committed. No runner can have used it: launches require snapshot.json.
    # Refuse ambiguous state and clean only an unpublished, never-run setup.
    for name in ("game", "prefix", "home"):
        if os.path.lexists(store.path / name):
            if store.read_json("state.json") is not None:
                raise JailError("Jail snapshot metadata is missing; restore or reset this jail before launching.")
            if not shutil.rmtree.avoids_symlink_attacks:
                raise JailError("Host Python cannot safely remove an incomplete jail setup.")
            shutil.rmtree(name, dir_fd=store.fd)
    temporary = f".copy-{uuid.uuid4().hex}"
    os.mkdir(temporary, 0o700, dir_fd=store.fd)
    source = _open_directory(source_path)
    target = os.open(temporary, DIRECTORY_FLAGS, dir_fd=store.fd)
    try:
        budget = {"files": 0, "bytes": 0, "deadline": time.monotonic() + COPY_TIMEOUT}
        _copy_tree(source, target, budget)
        os.rename(temporary, "game", src_dir_fd=store.fd, dst_dir_fd=store.fd)
        for name in ("prefix", "home"):
            fd = store.directory(name)
            os.close(fd)
        store.write_json("snapshot.json", {"source": config["game_dir"], "created_at": _now(), "files": budget["files"], "bytes": budget["bytes"]})
    finally:
        os.close(source)
        os.close(target)
        # Only this trusted, unpublished temporary is ever recursively removed.
        # Python's fd-based rmtree is resistant to symlink replacement attacks.
        if os.path.lexists(store.path / temporary):
            if not shutil.rmtree.avoids_symlink_attacks:
                raise JailError("Host Python cannot safely remove a partial jail copy.")
            shutil.rmtree(temporary, dir_fd=store.fd)


# This trusted program is passed as argv, not loaded from a writable game tree.
# Payload stdout has a separate pipe; PR_SET_DUMPABLE=0 prevents payload access
# to the supervisor's startup pipe via /proc or ptrace.
INNER = r'''
import ctypes, errno, json, os, selectors, signal, stat, subprocess, sys, time, uuid
spec = json.loads(sys.argv[1])
libc = ctypes.CDLL(None, use_errno=True)
for option, value in ((38, 1), (4, 0)):
    if libc.prctl(option, value, 0, 0, 0) != 0:
        raise RuntimeError("Cannot apply no_new_privs/supervisor protection")
status = dict(line.split(":", 1) for line in open("/proc/self/status") if ":" in line)
if status.get("NoNewPrivs", "").strip() != "1":
    raise RuntimeError("no_new_privs is not enforced")
for cap in ("CapEff", "CapPrm", "CapBnd", "CapAmb"):
    if int(status.get(cap, "1").strip(), 16) != 0:
        raise RuntimeError("Capabilities remain enabled: " + cap)
for namespace, inode in spec["namespaces"].items():
    if os.stat("/proc/self/ns/" + namespace).st_ino == inode:
        raise RuntimeError("Namespace isolation failed: " + namespace)
interfaces = [line.split(":", 1)[0].strip() for line in open("/proc/net/dev") if ":" in line]
if any(interface != "lo" for interface in interfaces):
    raise RuntimeError("Host network interfaces are visible")
for path in spec["hidden"]:
    if os.path.lexists(path):
        raise RuntimeError("Host/private data is visible: " + path)
for directory in ("/game", "/prefix", "/home/klay"):
    probe = directory + "/.klay-probe-" + uuid.uuid4().hex
    fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    os.unlink(probe)
for directory in spec["readonly"]:
    if not os.statvfs(directory).f_flag & os.ST_RDONLY:
        raise RuntimeError("Runtime mount is not read-only: " + directory)
probe = "/usr/.klay-probe-" + uuid.uuid4().hex
try:
    fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
except OSError as exc:
    if exc.errno not in (errno.EROFS, errno.EACCES):
        raise
else:
    os.close(fd)
    os.unlink(probe)
    raise RuntimeError("System runtime is writable")
if spec["probe_only"]:
    print("KLAY_JAIL_READY:" + json.dumps({"probe": True}), flush=True)
    sys.exit(0)
if spec.get("proton_prefix"):
    # Keep compatibility metadata in the private home and link Proton to the
    # existing Wine prefix. No mutable prefix is moved or traversed by the host.
    # Older explicit Proton configurations already used /prefix/pfx.
    prefix = "/prefix/pfx" if os.path.isfile("/prefix/pfx/system.reg") and not os.path.isfile("/prefix/system.reg") else "/prefix"
    current = os.open("/home/klay", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in (".local", "share", "klay-proton"):
            try:
                os.mkdir(component, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            child_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child_fd
        try:
            os.symlink(prefix, "pfx", dir_fd=current)
        except FileExistsError:
            if os.readlink("pfx", dir_fd=current) != prefix:
                raise RuntimeError("Proton's private prefix link is invalid")
    finally:
        os.close(current)
child = subprocess.Popen(spec["argv"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, close_fds=True, cwd=spec["cwd"])
# Wine keeps infrastructure processes alive after the application exits. Proton
# can wait for those processes indefinitely, so its own PID is not a reliable
# indication that the game is still running. All /proc PIDs here belong to this
# private PID namespace; no host process is inspected or signalled.
WINE_SERVICES = frozenset((
    "wineserver", "wineserver64", "services.exe", "winedevice.exe",
    "plugplay.exe", "explorer.exe", "rpcss.exe", "svchost.exe",
    "wineboot.exe", "winemenubuilder.exe", "conhost.exe", "tabtip.exe",
))
WINE_WRAPPERS = frozenset(("umu.exe", "xalia.exe"))
GAME_IDLE_GRACE = 3.0
STOP_GRACE = 2.0

def read_process(pid, proc_root="/proc"):
    directory = proc_root + "/" + str(pid)
    try:
        with open(directory + "/stat") as stream:
            fields = stream.read(4096).rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        with open(directory + "/comm") as stream:
            name = stream.read(128).strip().casefold()
        with open(directory + "/cmdline", "rb") as stream:
            arguments = [argument.decode("utf-8", errors="replace")
                         for argument in stream.read(16384).split(b"\0") if argument]
        try:
            executable = os.readlink(directory + "/exe")
        except OSError:
            executable = None
        return {"pid": pid, "started": fields[19], "name": name,
                "arguments": arguments, "executable": executable}
    except (OSError, IndexError):
        # A process can exit between any two /proc reads. An inaccessible live
        # process is counted conservatively, rather than mistaken for a service.
        if os.path.exists(directory):
            return {"pid": pid, "started": None, "name": "", "arguments": []}
        return None

def live_processes(proc_root="/proc"):
    processes = []
    for entry in os.listdir(proc_root):
        if entry.isdecimal() and int(entry) != os.getpid():
            process = read_process(int(entry), proc_root)
            if process is not None:
                processes.append(process)
    return processes

def game_path(argument):
    path = argument.strip('"').replace("\\", "/").casefold()
    return path == "/game" or path.startswith("/game/") or path == "z:/game" or path.startswith("z:/game/")

def is_game_process(process):
    if spec.get("proton_prefix") and process["pid"] == child.pid:
        return False
    arguments = process["arguments"]
    names = {process["name"]}
    if arguments:
        names.add(arguments[0].replace("\\", "/").rsplit("/", 1)[-1].casefold())
        image = arguments[0].strip('"').replace("\\", "/").casefold()
        if image.startswith("//?/"):
            image = image[4:]
        # A game installed into its private C: drive can have the same basename
        # as Wine infrastructure. Its explicit non-system image path outranks
        # the service/wrapper name; only root Windows directories are special.
        if len(image) > 3 and image[0].isalpha() and image[1:3] == ":/" and image.endswith(".exe"):
            relative = image[3:]
            windows_builtin = relative.startswith(("windows/system32/", "windows/syswow64/")) or (
                relative.startswith("windows/") and relative.count("/") == 1)
            trusted_runtime = image.startswith(("z:/runner/", "z:/usr/"))
            if not windows_builtin and not trusted_runtime:
                return True
    # umu.exe includes the target game in its own arguments while waiting for
    # Wine services. Its target argument must not count as a live application.
    # An executable actually located in /game still counts, whatever its name.
    if names & WINE_WRAPPERS and not (arguments and game_path(arguments[0])):
        return False
    if any(game_path(argument) for argument in arguments):
        return True
    # Wine's accessibility helpers can start their own private session bus.
    # Identify these native helpers by their actual read-only system image,
    # rather than ignoring arbitrary application processes with the same name.
    if process.get("executable") in ("/usr/bin/dbus-daemon", "/usr/bin/dbus-launch"):
        return False
    service_names = WINE_SERVICES | {name[:15] for name in WINE_SERVICES}
    return not bool(names & service_names)

def signal_processes(processes, signum, *, preserve_wrappers=False, proc_root="/proc"):
    for process in processes:
        if preserve_wrappers:
            if process["pid"] == child.pid:
                continue
            if process["name"] in WINE_WRAPPERS:
                continue
        # Check start time again so an exited PID cannot target a replacement.
        current = read_process(process["pid"], proc_root)
        if current is None or current["started"] is None or current["started"] != process["started"]:
            continue
        try:
            os.kill(process["pid"], signum)
        except ProcessLookupError:
            pass

stop_requested = None
def terminate(signum, frame):
    global stop_requested
    stop_requested = signum
signal.signal(signal.SIGTERM, terminate)
signal.signal(signal.SIGINT, terminate)
print("KLAY_JAIL_READY:" + json.dumps({"runner_pid": child.pid}), flush=True)
# No payload output is copied onto the startup channel until the trusted ready
# message above has been written. Bound chunks and leave total limits to host.
selector = selectors.DefaultSelector()
selector.register(child.stdout, selectors.EVENT_READ)
exit_deadline = None
stream_eof = False
game_seen = False
idle_since = None
stop_reason = None
stop_deadline = None
kill_deadline = None
application_start_deadline = time.monotonic() + spec["application_start_timeout"]

def reap_children():
    # This supervisor is PID 1: detached Wine services are adopted here. Reap
    # them promptly, recording the direct runner status if waitpid finds it.
    # Popen.poll()/wait() then use that same status rather than mistaking an
    # already-reaped runner for a successful exit.
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return
        if pid == child.pid:
            child.returncode = os.waitstatus_to_exitcode(status)

while True:
    reap_children()
    now = time.monotonic()
    processes = live_processes()
    applications = any(is_game_process(process) for process in processes)
    if applications:
        game_seen = True
        idle_since = None
        exit_deadline = None
    elif game_seen and idle_since is None:
        idle_since = now
    if stop_reason is None:
        if stop_requested is not None:
            stop_reason = "signal"
        elif game_seen and idle_since is not None and now - idle_since >= GAME_IDLE_GRACE:
            stop_reason = "game_exited"
            print("Jail game processes exited; stopping remaining Wine services.", flush=True)
        elif not game_seen and child.poll() is None and now >= application_start_deadline:
            # Polling cannot observe a silent application that starts and exits
            # between snapshots. Bound a wrapper waiting on Wine services in
            # that case, while allowing a separate, generous prefix-setup budget.
            stop_reason = "application_start_timeout"
            print("Jail application startup timed out: no application process was observed within the startup budget; stopping the runner and Wine services.", flush=True)
        elif child.poll() is not None and not applications and not game_seen:
            # A setup failure or very short program can exit between snapshots.
            # Preserve its status and bound inherited stdout without inventing
            # an application-start timeout during a longer prefix setup.
            if stream_eof:
                break
            if exit_deadline is None:
                exit_deadline = now + 1
            elif now >= exit_deadline:
                break
        if stop_reason is not None:
            stop_deadline = now + STOP_GRACE
            signal_processes(processes, signal.SIGTERM,
                             preserve_wrappers=stop_reason == "game_exited" and spec.get("proton_prefix", False))
    if stop_reason is not None:
        if not processes:
            break
        if now >= stop_deadline and kill_deadline is None:
            signal_processes(processes, signal.SIGKILL)
            kill_deadline = now + .5
        elif kill_deadline is not None and now >= kill_deadline:
            # Exiting PID 1 also tears down anything forked during shutdown.
            break
    if not selector.select(.2):
        continue
    chunk = os.read(child.stdout.fileno(), 16384)
    if not chunk:
        stream_eof = True
        selector.unregister(child.stdout)
        continue
    try:
        os.write(sys.stdout.fileno(), chunk)
    except BrokenPipeError:
        stop_requested = signal.SIGTERM
selector.close()
reap_children()
code = child.poll()
if code is None:
    # Never block shutdown on a wrapper that ignores termination or cannot be
    # reaped immediately. The private namespace dies when this PID 1 exits.
    code = 0 if stop_reason == "game_exited" else 128 + (stop_requested or signal.SIGTERM)
elif stop_reason == "game_exited" and code in (-signal.SIGTERM, -signal.SIGKILL):
    code = 0
if stop_reason == "application_start_timeout" and code in (0, 128 + signal.SIGTERM, -signal.SIGTERM, -signal.SIGKILL):
    code = 124
sys.exit(code if code >= 0 else 128 - code)
'''


def controller_args(dev_root: Path = Path("/dev"), sysfs_root: Path = Path("/sys"),
                    udev_root: Path = Path("/run/udev/data")) -> tuple[list[str], list[str]]:
    """Share connected, accessible gamepads without sharing other input devices.

    libudev and Wine need both device nodes and their identification metadata.
    Rebuild only selected input aliases/ancestors in the private sysfs tree;
    never mount an entire USB device, /dev/input, or the host udev database.
    A controller's separate keyboard/vendor hidraw interfaces remain private.
    Devices connected after launch become available on the next launch.
    """
    selected = []
    denied = ("KEY", "KEYBOARD", "MOUSE", "TOUCHPAD", "TOUCHSCREEN", "TABLET")
    try:
        nodes = sorted((dev_root / "input").iterdir())
        devices_root = (sysfs_root / "devices").resolve(strict=True)
        database_root = udev_root.resolve(strict=True)
    except OSError:
        return [], []
    for node in nodes:
        if not re.fullmatch(r"(?:event|js)[0-9]+", node.name):
            continue
        try:
            info = node.lstat()
            if not stat.S_ISCHR(info.st_mode) or os.major(info.st_rdev) != 13:
                continue
            access = os.R_OK | (os.W_OK if node.name.startswith("event") else 0)
            if not os.access(node, access):
                continue
            number = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
            record = udev_root / ("c" + number)
            # The classification must be a regular udev-owned database entry,
            # not a symlink or an unrelated path with a plausible filename.
            if record.resolve(strict=True).parent != database_root:
                continue
            descriptor = os.open(record, FILE_FLAGS)
            try:
                record_info = os.fstat(descriptor)
                if not stat.S_ISREG(record_info.st_mode) or record_info.st_size > 65536:
                    continue
                content = os.read(descriptor, 65537).decode("utf-8", errors="replace")
            finally:
                os.close(descriptor)
            if len(content) > 65536:
                continue
            properties = dict(line[2:].split("=", 1) for line in content.splitlines()
                              if line.startswith("E:") and "=" in line[2:])
            if properties.get("ID_INPUT_JOYSTICK") != "1" or any(
                    properties.get("ID_INPUT_" + kind) == "1" for kind in denied):
                continue
            entry = (sysfs_root / "class/input" / node.name).resolve(strict=True)
            root = entry.parent
            if (not entry.is_relative_to(devices_root) or entry.name != node.name
                    or not re.fullmatch(r"input[0-9]+", root.name)
                    or (entry / "dev").read_text().strip() != number
                    or (sysfs_root / "dev/char" / number).resolve(strict=True) != entry):
                continue
            selected.append((node, number, record, entry, root))
        except (OSError, ValueError):
            # Inaccessible and disconnected pads must not prevent game launch.
            continue
    if not selected:
        return [], []

    command, readonly = [], []
    directories, aliases, bindings = set(), set(), set()

    def inside(path: Path) -> str:
        return "/sys/" + str(path.relative_to(sysfs_root))

    def directory(destination: str) -> None:
        if destination not in directories:
            command.extend(("--dir", destination))
            directories.add(destination)

    def bind(source: Path, destination: str) -> None:
        if destination not in bindings:
            command.extend(("--ro-bind", str(source), destination))
            readonly.append(destination)
            bindings.add(destination)

    def alias(target: str, destination: str) -> None:
        if destination not in aliases:
            command.extend(("--symlink", target, destination))
            aliases.add(destination)

    for destination in ("/dev/input", "/sys/class/input", "/sys/dev/char", "/run/udev/data"):
        directory(destination)
    attributes = ("uevent", "idVendor", "idProduct", "bcdDevice", "manufacturer", "product", "serial",
                  "bInterfaceNumber", "bInterfaceClass", "bInterfaceSubClass", "bInterfaceProtocol")
    for node, number, record, entry, root in selected:
        command.extend(("--dev-bind", str(node), "/dev/input/" + node.name))
        bind(record, "/run/udev/data/" + record.name)
        bind(root, inside(root))
        alias(inside(root), "/sys/class/input/" + root.name)
        alias(inside(entry), "/sys/class/input/" + node.name)
        alias(inside(entry), "/sys/dev/char/" + number)
        # Parent attributes let libudev identify USB/HID ancestry. Bind only
        # individual files: sibling keyboard/mouse trees remain invisible.
        for parent in root.parents:
            if parent == devices_root or not parent.is_relative_to(devices_root):
                break
            destination = inside(parent)
            directory(destination)
            for name in attributes:
                source = parent / name
                try:
                    if stat.S_ISREG(source.lstat().st_mode):
                        bind(source, destination + "/" + name)
                except OSError:
                    pass
            try:
                subsystem = (parent / "subsystem").resolve(strict=True)
                if (subsystem.is_relative_to(sysfs_root / "bus")
                        or subsystem.is_relative_to(sysfs_root / "class")):
                    directory(inside(subsystem))
                    alias(inside(subsystem), destination + "/subsystem")
                    directory(inside(subsystem / "devices"))
                    alias(destination, inside(subsystem / "devices" / parent.name))
            except OSError:
                pass
    return command, readonly


def _socket(path: Path) -> bool:
    try:
        info = path.lstat()
        return stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid()
    except OSError:
        return False


def gpu_sysfs_args(render_nodes: list[tuple[Path, int]], sysfs_root: Path = Path("/sys")) -> tuple[list[str], list[str]]:
    """Expose only the selected render devices' read-only identification tree.

    Mesa/libudev follow /sys/dev/char -> DRM device -> physical GPU to discover
    PCI identity and driver. Rebuild class/bus aliases in a private /sys; bind
    physical GPU subtrees only, leaving unrelated host sysfs inaccessible.
    """
    roots: set[Path] = set()
    char_links: dict[str, Path] = {}
    for node, device_number in render_nodes:
        if node.parent != Path("/dev/dri") or not re.fullmatch(r"renderD[0-9]+", node.name):
            continue
        try:
            entry = (sysfs_root / "class/drm" / node.name).resolve(strict=True)
            physical = (entry / "device").resolve(strict=True)
            if not physical.is_relative_to(sysfs_root / "devices") or physical == sysfs_root / "devices":
                continue
            number = f"{os.major(device_number)}:{os.minor(device_number)}"
            link = (sysfs_root / "dev/char" / number).resolve(strict=True)
            if not link.is_relative_to(physical):
                continue
            roots.add(physical)
            char_links[number] = link
        except OSError:
            continue
    if not roots:
        return [], []
    def inside(path: Path) -> str:
        return "/sys/" + str(path.relative_to(sysfs_root))
    command = ["--dir", "/sys", "--dir", "/sys/class/drm", "--dir", "/sys/dev/char", "--dir", "/sys/bus"]
    readonly = []
    for physical in sorted(roots):
        destination = inside(physical)
        command += ["--ro-bind", str(physical), destination]
        readonly.append(destination)
        # These empty directories make the GPU's subsystem/driver symlinks
        # valid without mounting a complete PCI bus or driver's other devices.
        try:
            subsystem = (physical / "subsystem").resolve(strict=True)
            if subsystem.is_relative_to(sysfs_root / "bus"):
                command += ["--dir", inside(subsystem), "--dir", inside(subsystem / "devices")]
                command += ["--symlink", destination, inside(subsystem / "devices" / physical.name)]
        except OSError:
            pass
        try:
            driver = (physical / "driver").resolve(strict=True)
            if driver.is_relative_to(sysfs_root / "bus"):
                command += ["--dir", inside(driver)]
        except OSError:
            pass
    try:
        entries = sorted((sysfs_root / "class/drm").iterdir())
    except OSError:
        entries = []
    for alias in entries:
        if not re.fullmatch(r"(?:renderD[0-9]+|card[0-9]+(?:-[A-Za-z0-9_.:-]+)?)", alias.name):
            continue
        try:
            target = alias.resolve(strict=True)
            if any(target.is_relative_to(physical) for physical in roots):
                command += ["--symlink", inside(target), f"/sys/class/drm/{alias.name}"]
        except OSError:
            pass
    for number, target in sorted(char_links.items()):
        command += ["--symlink", inside(target), f"/sys/dev/char/{number}"]
    return command, readonly


def cpu_sysfs_args(trusted_root: Path, sysfs_root: Path = Path("/sys")) -> tuple[list[str], list[str]]:
    """Share exact CPU/NUMA topology files used by Wine's processor APIs.

    Without cpu/online, Wine cannot build logical processor information, even
    when the scheduler and GetSystemInfo still report every processor. Never
    copy a complete CPU/node directory: those trees also contain hotplug,
    power, frequency, and other controls unrelated to processor topology.
    An immutable, content-hashed snapshot keeps the mount argv and readonly
    verification small even on hosts with thousands of logical processors.
    """
    import errno
    command, readonly, metadata = [], [], {}
    try:
        canonical_root = sysfs_root.resolve(strict=True)
    except (OSError, RuntimeError):
        return command, readonly

    def add(relative: str) -> None:
        source = sysfs_root / relative
        try:
            if (not stat.S_ISREG(source.lstat().st_mode)
                    or source.resolve(strict=True) != canonical_root / relative
                    or not os.access(source, os.R_OK)):
                return
            with os.fdopen(os.open(source, FILE_FLAGS), "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    return
                contents = stream.read(65537)
        except (OSError, RuntimeError):
            return
        if len(contents) > 65536:
            raise JailError("CPU topology metadata exceeds 64 KiB per file.")
        metadata[relative] = contents

    def children(relative: str, pattern: str) -> list[Path]:
        directory = sysfs_root / relative
        try:
            if directory.resolve(strict=True) != canonical_root / relative:
                return []
            return [entry for entry in sorted(directory.iterdir())
                    if re.fullmatch(pattern, entry.name) and not entry.is_symlink() and entry.is_dir()]
        except (OSError, RuntimeError):
            return []

    cpu = "devices/system/cpu"
    for name in ("online", "present"):
        add(f"{cpu}/{name}")
    for entry in children(cpu, r"cpu[0-9]+"):
        relative = f"{cpu}/{entry.name}"
        for name in ("physical_package_id", "thread_siblings", "thread_siblings_list"):
            add(f"{relative}/topology/{name}")
        for cache in children(f"{relative}/cache", r"index[0-9]+"):
            for name in ("shared_cpu_map", "level", "ways_of_associativity",
                         "coherency_line_size", "size", "type"):
                add(f"{relative}/cache/{cache.name}/{name}")
    node = "devices/system/node"
    add(f"{node}/online")
    for entry in children(node, r"node[0-9]+"):
        add(f"{node}/{entry.name}/cpumap")
    # Wine uses this optional Intel hybrid-CPU mask to classify performance
    # cores. Share the mask only, without the PMU device's other attributes.
    add("devices/cpu_core/cpus")
    if not metadata:
        return command, readonly
    if len(metadata) > MAX_FILES or sum(map(len, metadata.values())) > 16 * 1024**2:
        raise JailError("CPU topology metadata exceeds the supported snapshot size.")
    digest = hashlib.sha256()
    for relative, contents in sorted(metadata.items()):
        digest.update(relative.encode() + b"\0" + contents + b"\0")
    name = "cpu-metadata-" + digest.hexdigest()
    root_fd = _open_directory(trusted_root)
    stage = None
    try:
        try:
            existing = os.open(name, DIRECTORY_FLAGS, dir_fd=root_fd)
        except FileNotFoundError:
            stage = ".cpu-metadata-" + uuid.uuid4().hex
            os.mkdir(stage, 0o700, dir_fd=root_fd)
            stage_fd = os.open(stage, DIRECTORY_FLAGS, dir_fd=root_fd)
            try:
                for relative, contents in metadata.items():
                    current = os.dup(stage_fd)
                    try:
                        parts = PurePosixPath(relative).parts
                        for part in parts[:-1]:
                            try:
                                os.mkdir(part, 0o700, dir_fd=current)
                            except FileExistsError:
                                pass
                            child = os.open(part, DIRECTORY_FLAGS, dir_fd=current)
                            os.close(current)
                            current = child
                        target = os.open(parts[-1], os.O_CREAT | os.O_EXCL | os.O_WRONLY
                                         | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400, dir_fd=current)
                        with os.fdopen(target, "wb") as stream:
                            stream.write(contents)
                    finally:
                        os.close(current)
            finally:
                os.close(stage_fd)
            try:
                os.rename(stage, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                stage = None
            except OSError as error:
                if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
                # A concurrent check can publish the same immutable snapshot.
                existing = os.open(name, DIRECTORY_FLAGS, dir_fd=root_fd)
                os.close(existing)
        else:
            os.close(existing)
    finally:
        if stage is not None:
            shutil.rmtree(stage, dir_fd=root_fd)
        os.close(root_fd)
    snapshot = trusted_root / name
    for relative in (cpu, node, "devices/cpu_core"):
        if any(path.startswith(relative + "/") for path in metadata):
            destination = "/sys/" + relative
            command.extend(("--ro-bind", str(snapshot / relative), destination))
            readonly.append(destination)
    return command, readonly


def nvidia_module_args(shared_devices: list[Path], sysfs_root: Path = Path("/sys")) -> tuple[list[str], list[str]]:
    """Expose exact NVIDIA module metadata needed by shared GPU devices."""
    if not any(re.fullmatch(r"nvidia[0-9]+", device.name) for device in shared_devices):
        return [], []
    names = ["nvidia"]
    if Path("/dev/nvidia-uvm") in shared_devices:
        # Vulkan device creation with VKD3D's advanced memory features also
        # requires UVM metadata, even when GLX/DXVK already initialize.
        names.append("nvidia_uvm")
    command, readonly = [], []
    for name in names:
        module = sysfs_root / "module" / name
        try:
            if module.is_symlink() or not module.is_dir():
                continue
            if module.resolve(strict=True) != sysfs_root.resolve(strict=True) / "module" / name:
                continue
        except OSError:
            continue
        destination = "/sys/module/" + name
        command += ["--ro-bind", str(module), destination]
        readonly.append(destination)
    return command, readonly


def _elf_dynamic(path: Path) -> tuple[int, str | None, list[str]] | None:
    """Read x86 ELF dependencies without executing a library or its loader."""
    try:
        fd = os.open(path, FILE_FLAGS)
        with os.fdopen(fd, "rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            header = stream.read(64)
            if len(header) < 52 or header[:4] != b"\x7fELF" or header[5] != 1:
                return None
            bits = {1: 32, 2: 64}.get(header[4])
            if bits is None or struct.unpack_from("<H", header, 18)[0] != (3 if bits == 32 else 62):
                return None
            if bits == 64:
                if len(header) < 64:
                    return None
                offset = struct.unpack_from("<Q", header, 32)[0]
                entry_size, count = struct.unpack_from("<HH", header, 54)
                ph_format, minimum = "<IIQQQQQQ", 56
            else:
                offset = struct.unpack_from("<I", header, 28)[0]
                entry_size, count = struct.unpack_from("<HH", header, 42)
                ph_format, minimum = "<IIIIIIII", 32
            if entry_size < minimum or count > 1024 or offset + entry_size * count > size:
                return None
            loads, dynamic = [], None
            for index in range(count):
                stream.seek(offset + index * entry_size)
                fields = struct.unpack(ph_format, stream.read(minimum))
                kind = fields[0]
                file_offset, address, length = (fields[2], fields[3], fields[5]) if bits == 64 else (fields[1], fields[2], fields[4])
                if file_offset + length > size:
                    return None
                if kind == 1:
                    loads.append((address, file_offset, length))
                elif kind == 2:
                    dynamic = file_offset, length
            if dynamic is None or dynamic[1] > 1024 * 1024:
                return None
            stream.seek(dynamic[0])
            entries = stream.read(dynamic[1])
            width = 16 if bits == 64 else 8
            names, soname, table, table_size = [], None, None, None
            for index in range(0, len(entries) - width + 1, width):
                tag, value = struct.unpack_from("<QQ" if bits == 64 else "<II", entries, index)
                if tag == 0:
                    break
                if tag == 1:
                    names.append(value)
                elif tag == 5:
                    table = value
                elif tag == 10:
                    table_size = value
                elif tag == 14:
                    soname = value
            if table is None or table_size is None or table_size > 1024 * 1024:
                return None
            for address, file_offset, length in loads:
                if address <= table and table + table_size <= address + length:
                    stream.seek(file_offset + table - address)
                    strings = stream.read(table_size)
                    break
            else:
                return None
            def name_at(index: int) -> str:
                end = strings.find(b"\0", index)
                if index < 0 or end < index:
                    raise ValueError("Invalid ELF string")
                result = strings[index:end].decode("ascii")
                if result in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_+.-]+", result):
                    raise ValueError("Invalid ELF library name")
                return result
            return bits, name_at(soname) if soname is not None else None, [name_at(index) for index in names]
    except (OSError, ValueError, struct.error, UnicodeError):
        return None


def _host_library_roots() -> list[Path]:
    return [Path(path) for path in ("/usr/lib64", "/usr/lib32", "/usr/lib", "/usr/lib/x86_64-linux-gnu", "/usr/lib/i386-linux-gnu", "/lib64", "/lib32", "/lib", "/lib/x86_64-linux-gnu", "/lib/i386-linux-gnu")]


def _runtime_library_roots() -> list[Path]:
    """Find installed runtime library trees, never a whole Steam installation."""
    home = _host_home()
    steam_roots = [home / relative for relative in (".local/share/Steam", ".steam/steam", ".steam/root", ".var/app/com.valvesoftware.Steam/.local/share/Steam", ".var/app/com.valvesoftware.Steam/data/Steam")]
    roots = []
    # Explicit Steam library locations are data; no scripts are evaluated.
    for steam in list(steam_roots):
        try:
            with (steam / "steamapps/libraryfolders.vdf").open(encoding="utf-8", errors="replace") as stream:
                text = stream.read(1024 * 1024)
            for path in re.findall(r'"path"\s+"([^"\n\r]+)"', text):
                candidate = Path(path.replace("\\\\", "\\"))
                if candidate.is_absolute():
                    steam_roots.append(candidate)
        except OSError:
            pass
    for steam in steam_roots:
        common = steam / "steamapps/common"
        for name in ("SteamLinuxRuntime_sniper", "SteamLinuxRuntime_soldier", "SteamLinuxRuntime_4"):
            base = common / name
            if base.is_dir():
                roots.extend(sorted(base.glob("*/files/lib/*-linux-gnu")))
                roots.extend(sorted(base.glob("*/files/usr/lib/*-linux-gnu")))
    for base in (home / ".local/share/umu", home / ".var/app/net.lutris.Lutris/data/umu"):
        roots.extend(sorted(base.glob("steamrt*/*/files/lib/*-linux-gnu")))
        roots.extend(sorted(base.glob("steamrt*/*/files/usr/lib/*-linux-gnu")))
    result = []
    for root in roots:
        try:
            canonical = root.resolve(strict=True)
            if canonical.is_dir() and canonical not in result:
                result.append(canonical)
        except OSError:
            pass
    return result


def proton_runtime_args(config: dict) -> tuple[list[str], dict[str, str], list[str]]:
    """Supply missing media SONAMEs without replacing the host loader or GPU stack."""
    if config["runner_type"] != "proton":
        return [], {}, []
    runner = Path(config["runner"]).resolve()
    runner_roots = []
    for distribution in (runner / "files", runner / "dist"):
        runner_roots.extend(path for path in distribution.glob("lib*") if path.is_dir())
        runner_roots.extend(path for path in distribution.glob("lib/*-linux-gnu") if path.is_dir())
    plugin_roots = [root / "gstreamer-1.0" for root in runner_roots]
    pending = []
    for directory in plugin_roots:
        for plugin in sorted(directory.glob("*.so")):
            try:
                target = plugin.resolve(strict=True)
                if target.is_relative_to(runner):
                    dynamic = _elf_dynamic(target)
                    if dynamic:
                        pending.extend((dynamic[0], name) for name in dynamic[2])
            except OSError:
                pass
    if not pending:
        return [], {}, []
    runtime_roots = _runtime_library_roots()
    host_roots = _host_library_roots()
    cache = {}
    indexed = False
    def existing(bits: int, name: str) -> list[str] | None:
        for root in [*runner_roots, *host_roots]:
            path = root / name
            try:
                target = path.resolve(strict=True)
                if not target.is_relative_to(root.resolve()):
                    continue
                dynamic = _elf_dynamic(target)
                if dynamic and dynamic[0] == bits:
                    # Bundled codecs can themselves require older media
                    # SONAMEs. Keep following that chain without replacing
                    # libraries already provided by the host or runner.
                    return dynamic[2] if root in runner_roots else []
            except OSError:
                pass
        return None
    def candidate(bits: int, name: str) -> tuple[Path, list[str]] | None:
        nonlocal indexed
        if not indexed:
            indexed = True
            for root in runtime_roots:
                try:
                    canonical = root.resolve(strict=True)
                    entries = sorted(root.iterdir())
                except OSError:
                    continue
                for entry in entries:
                    if ".so" not in entry.name:
                        continue
                    try:
                        target = entry.resolve(strict=True)
                        if not target.is_relative_to(canonical) or not target.is_file():
                            continue
                        dynamic = _elf_dynamic(target)
                        if dynamic and dynamic[1]:
                            cache.setdefault((dynamic[0], dynamic[1]), (target, dynamic[2]))
                    except OSError:
                        pass
        return cache.get((bits, name))
    selected, seen = {}, set()
    while pending:
        bits, name = pending.pop()
        if (bits, name) in seen:
            continue
        seen.add((bits, name))
        # Mixing a runtime's libc/loader, C++ ABI or graphics providers with
        # the host breaks their coupled ABI. These always remain host-provided.
        if re.match(r"(?:ld[.-]|lib(?:c\.|dl\.|pthread\.|m\.|rt\.|resolv\.|util\.|stdc\+\+\.|gcc_s\.|GL|EGL|OpenGL|vulkan|drm|gbm|nvidia))", name):
            continue
        supplied = existing(bits, name)
        if supplied is not None:
            pending.extend((bits, needed) for needed in supplied)
            continue
        found = candidate(bits, name)
        if found:
            selected[(bits, name)] = found[0]
            pending.extend((bits, needed) for needed in found[1])
    command, readonly = [], []
    for (bits, name), source in sorted(selected.items()):
        destination = f"/runtime-libs/{'x86_64' if bits == 64 else 'i386'}/{name}"
        command += ["--ro-bind", str(source), destination]
        readonly.append(destination)
    environment = {"LD_LIBRARY_PATH": "/runtime-libs/x86_64:/runtime-libs/i386"} if selected else {}
    return command, environment, readonly


def build_command(config: dict, mutable_root: Path, trusted_root: Path, *, probe_only: bool = False) -> list[str]:
    bwrap, python = check_dependencies()
    uid = os.getuid()
    command = [bwrap, "--unshare-all", "--unshare-user", "--disable-userns", "--assert-userns-disabled", "--die-with-parent", "--new-session", "--as-pid-1", "--cap-drop", "ALL", "--clearenv", "--hostname", "klay-jail", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/run", "--dir", "/etc", "--dir", "/home", "--dir", "/home/klay"]
    command += ["--ro-bind", "/usr", "/usr"]
    for name in ("bin", "sbin", "lib", "lib32", "lib64"):
        host = Path("/") / name
        if host.is_symlink():
            command += ["--symlink", os.readlink(host), f"/{name}"]
        elif host.is_dir():
            command += ["--ro-bind", str(host), str(host)]
    for name in ("ld.so.cache", "ld.so.conf", "ld.so.conf.d", "fonts", "alsa", "vulkan", "OpenCL"):
        host = Path("/etc") / name
        if host.exists():
            command += ["--ro-bind", str(host), str(host)]
    for name, inside in (("account-passwd", "/etc/passwd"), ("account-group", "/etc/group"), ("account-nsswitch", "/etc/nsswitch.conf"), ("account-machine-id", "/etc/machine-id")):
        command += ["--ro-bind", str(trusted_root / name), inside]
    for name, inside in (("game", "/game"), ("prefix", "/prefix"), ("home", "/home/klay")):
        command += ["--bind", str(mutable_root / name), inside]
    environment = {"HOME": "/home/klay", "USER": "klay", "LOGNAME": "klay", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TMPDIR": "/tmp", "WINEPREFIX": "/prefix", "WINEDEBUG": "-all", "XDG_DATA_HOME": "/home/klay/.local/share", "XDG_CONFIG_HOME": "/home/klay/.config", "XDG_CACHE_HOME": "/home/klay/.cache"}
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}"))
    inside_runtime = f"/run/user/{uid}"
    command += ["--dir", "/run/user", "--dir", inside_runtime]
    environment["XDG_RUNTIME_DIR"] = inside_runtime
    wayland = os.environ.get("WAYLAND_DISPLAY", "")
    if re.fullmatch(r"[A-Za-z0-9_.-]+", wayland) and _socket(runtime / wayland):
        command += ["--ro-bind", str(runtime / wayland), f"{inside_runtime}/{wayland}"]
        environment["WAYLAND_DISPLAY"] = wayland
    display = os.environ.get("DISPLAY", "")
    match = re.fullmatch(r"(?:unix)?:([0-9]+)(?:\.[0-9]+)?", display)
    if match:
        xsocket = Path("/tmp/.X11-unix") / f"X{match.group(1)}"
        # Xwayland/Xorg sockets may be root-owned; never mount their directory.
        try:
            is_socket = stat.S_ISSOCK(xsocket.lstat().st_mode)
        except OSError:
            is_socket = False
        if is_socket:
            command += ["--dir", "/tmp/.X11-unix", "--ro-bind", str(xsocket), str(xsocket)]
            environment["DISPLAY"] = display
            authority = Path(os.environ.get("XAUTHORITY", str(_host_home() / ".Xauthority")))
            try:
                info = authority.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_uid == uid and info.st_size <= 1024 * 1024:
                    command += ["--ro-bind", str(authority), "/run/klay-Xauthority"]
                    environment["XAUTHORITY"] = "/run/klay-Xauthority"
                    # FamilyLocal cookies are keyed by hostname. libxcb does
                    # not consistently honor XAUTHLOCALHOSTNAME, so keep that
                    # name in the separate UTS namespace for local X11 auth.
                    command[command.index("--hostname") + 1] = os.uname().nodename
            except OSError:
                pass
    pulse = runtime / "pulse" / "native"
    if _socket(pulse):
        command += ["--dir", f"{inside_runtime}/pulse", "--ro-bind", str(pulse), f"{inside_runtime}/pulse/native"]
        environment["PULSE_SERVER"] = f"unix:{inside_runtime}/pulse/native"
    gpu_nodes = sorted(Path("/dev/dri").glob("renderD[0-9]*"))
    gpu_nodes += sorted(Path("/dev").glob("nvidia[0-9]*"))
    gpu_nodes += [Path("/dev/nvidiactl"), Path("/dev/nvidia-modeset"), Path("/dev/nvidia-uvm")]
    render_nodes = []
    shared_devices = []
    for device in gpu_nodes:
        try:
            info = device.lstat()
            if stat.S_ISCHR(info.st_mode) and os.access(device, os.R_OK | os.W_OK):
                if device.parent == Path("/dev/dri") and "--dir-dri" not in environment:
                    command += ["--dir", "/dev/dri"]
                    environment["--dir-dri"] = "1"
                command += ["--dev-bind", str(device), str(device)]
                shared_devices.append(device)
                if device.parent == Path("/dev/dri"):
                    render_nodes.append((device, info.st_rdev))
        except OSError:
            pass
    environment.pop("--dir-dri", None)
    metadata_args, gpu_readonly = gpu_sysfs_args(render_nodes)
    command += metadata_args
    module_args, module_readonly = nvidia_module_args(shared_devices)
    command += module_args
    gpu_readonly += module_readonly
    controller_command, controller_readonly = controller_args()
    command += controller_command
    cpu_command, cpu_readonly = cpu_sysfs_args(trusted_root)
    command += cpu_command
    runtime_command, runtime_environment, runtime_readonly = proton_runtime_args(config)
    command += runtime_command
    environment.update(runtime_environment)
    if config["runner_type"] == "proton":
        command += ["--ro-bind", config["runner"], "/runner"]
        argv = ["/runner/proton", "run", "/game/" + config["exe"], *config["arguments"]]
        # Recent GE-Proton uses this hint to launch its non-Steam umu.exe helper
        # rather than steam.exe. This does not invoke UMU or download a runtime.
        environment.update({"STEAM_COMPAT_DATA_PATH": "/home/klay/.local/share/klay-proton", "STEAM_COMPAT_INSTALL_PATH": "/game", "STEAM_COMPAT_CLIENT_INSTALL_PATH": "/home/klay", "STEAM_COMPAT_APP_ID": "0", "SteamAppId": "0", "SteamGameId": "0", "UMU_ID": "umu-default", "UMU_USE_STEAM": "0", "PROTONFIXES_DISABLE": "1", "WINEPREFIX": "/home/klay/.local/share/klay-proton/pfx"})
    else:
        wine = config["runner"]
        if config.get("runner_dir"):
            command += ["--ro-bind", config["runner_dir"], "/runner"]
            wine = "/runner/" + str(Path(wine).relative_to(Path(config["runner_dir"])))
            environment["PATH"] = str(PurePosixPath(wine).parent) + ":/usr/bin:/bin"
        environment["WINE"] = wine
        argv = [wine, "/game/" + config["exe"], *config["arguments"]]
    for key, value in environment.items():
        command += ["--setenv", key, value]
    hidden = [str(trusted_root), str(_host_home() / ".ssh"), str(_host_home() / ".steam"), str(_host_home() / ".local/share/Steam"), "/run/dbus/system_bus_socket", f"{inside_runtime}/bus"]
    readonly = (["/usr", "/runner"] if config["runner_type"] == "proton" or config.get("runner_dir") else ["/usr"]) + gpu_readonly + controller_readonly + cpu_readonly + runtime_readonly
    cwd = config.get("working_dir", str(PurePosixPath(config["exe"]).parent))
    spec = {"argv": argv, "cwd": str(PurePosixPath("/game") / cwd), "probe_only": probe_only, "proton_prefix": config["runner_type"] == "proton", "application_start_timeout": 600, "hidden": hidden, "readonly": readonly, "namespaces": {name: os.stat(f"/proc/self/ns/{name}").st_ino for name in ("user", "pid", "net", "mnt", "ipc", "uts")}}
    command += ["--chdir", "/game", "--", python, "-I", "-c", INNER, json.dumps(spec, separators=(",", ":"))]
    return command


def check_graphics(config: dict, mutable_root: Path, trusted_root: Path) -> dict:
    """Run available host graphics diagnostics behind the same jail boundary."""
    results = {}
    for name, executable, arguments in (("opengl", "glxinfo", ["-B"]), ("vulkan", "vulkaninfo", ["--summary"])):
        tool = Path("/usr/bin") / executable
        if not tool.is_file() or not os.access(tool, os.X_OK):
            results[name] = {"status": "unavailable", "detail": executable + " is not installed on the host."}
            continue
        if name == "opengl" and not os.environ.get("DISPLAY"):
            results[name] = {"status": "unavailable", "detail": "No X11 display is configured for the OpenGL probe."}
            continue
        command = build_command(config, mutable_root, trusted_root)
        spec = json.loads(command[-1])
        spec.update(argv=[str(tool), *arguments], cwd="/game", proton_prefix=False)
        command[-1] = json.dumps(spec, separators=(",", ":"))
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=10, check=False)
        except subprocess.TimeoutExpired:
            results[name] = {"status": "failed", "detail": executable + " timed out after ten seconds."}
            continue
        output = result.stdout.decode("utf-8", errors="replace")[-32768:]
        pattern = r"OpenGL renderer string:\s*(.+)" if name == "opengl" else r"deviceName\s*=\s*(.+)"
        renderers = list(dict.fromkeys(match.strip() for match in re.findall(pattern, output)))
        results[name] = {"status": "passed" if result.returncode == 0 and renderers else "failed", "renderers": renderers}
        if results[name]["status"] == "failed":
            results[name]["detail"] = output.strip()[-1200:]
    return results


def check(config: dict, store: Store) -> dict:
    check_dependencies()
    if config.get("runner_dir"):
        wine_root = Path(config["runner_dir"])
        if _within(wine_root, store.path) or _within(store.path, wine_root):
            raise JailError("The trusted Wine build must be outside jail storage.")
    transaction = store.read_json("layout-transaction.json")
    prepared = store.read_json("snapshot.json")
    _apply_saved_layout(config, transaction.get("new") if transaction else prepared)
    if transaction and store.lock_fd is not None:
        _resume_layout(store, config, transaction)
        prepared = store.read_json("snapshot.json")
    expanding = bool(prepared and prepared.get("source") != config["game_dir"])
    if expanding and _layout_relative(config, prepared) is None:
        raise JailError("This jail already has a copy of a different game directory.")
    if prepared and not expanding:
        source = os.open("game", DIRECTORY_FLAGS, dir_fd=store.fd)
        try:
            executable = _regular_at(source, config["exe"])
            os.close(executable)
            working = _directory_at(source, config["working_dir"])
            os.close(working)
        finally:
            os.close(source)
    else:
        source_path = _source_path(config["game_dir"])
        if _within(store.path, source_path) or _within(Path(config["runner"]), source_path):
            raise JailError("Game and runner directories must be outside jail storage and separate from each other.")
        source = _open_directory(source_path)
        try:
            executable = _regular_at(source, config["exe"])
            os.close(executable)
            working = _directory_at(source, config["working_dir"])
            os.close(working)
        finally:
            os.close(source)
    import tempfile
    with tempfile.TemporaryDirectory(prefix=".check-", dir=store.path) as temporary:
        root = Path(temporary)
        for name in ("game", "prefix", "home"):
            (root / name).mkdir(mode=0o700)
        command = build_command(config, root, store.path, probe_only=True)
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15, check=False)
        except subprocess.TimeoutExpired as exc:
            raise JailError("Sandbox checks timed out; the game was not started.") from exc
        output = result.stdout.decode("utf-8", errors="replace")[-4096:]
        if result.returncode or READY_PREFIX + '{"probe": true}' not in output:
            raise JailError("Sandbox checks failed. Your host must allow unprivileged user namespaces. " + output.strip()[-1200:])
        graphics = check_graphics(config, root, store.path)
        if graphics.get("opengl", {}).get("status") == "failed":
            raise JailError("OpenGL graphics check failed inside the jail; the game was not started. " + graphics["opengl"].get("detail", ""))
    return {"ok": True, "status": "ready", "jail_dir": str(store.path), "runner_type": config["runner_type"], "graphics": graphics, "limitations": "Shared Linux kernel, GPU, display and audio services; X11 permits access to the desktop session. Offline only; no malware safety verdict."}


def launch(config: dict, store: Store) -> int:
    check_dependencies()
    store.lock()
    preflight = check(config, store)
    prepare_game(store, config)
    copied_game = os.open("game", DIRECTORY_FLAGS, dir_fd=store.fd)
    try:
        executable = _regular_at(copied_game, config["exe"])
        os.close(executable)
        working = _directory_at(copied_game, config["working_dir"])
        os.close(working)
    finally:
        os.close(copied_game)
    filename = f"run-{int(time.time())}-{uuid.uuid4().hex[:12]}.log"
    log_fd = os.open(filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=store.logs_fd)
    log_path = str(store.path / "logs" / filename)
    state = {"ok": True, "status": "starting", "jail_dir": str(store.path), "log_path": log_path, "log_file": filename, "started_at": _now(), "supervisor_pid": os.getpid(), "supervisor_start_ticks": _process_start_ticks(os.getpid()), "runner_type": config["runner_type"], "graphics": preflight["graphics"]}
    store.write_json("state.json", state)
    process = None
    emitted = False
    header = ("KLAY_GRAPHICS_CHECK:" + json.dumps(preflight["graphics"]) + "\n").encode()
    os.write(log_fd, header)
    log_size = len(header)
    stop_requested = False
    try:
        process = subprocess.Popen(build_command(config, store.path, store.path), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True, start_new_session=True)
        def terminate(signum, frame):
            nonlocal stop_requested
            stop_requested = True
            if process.poll() is None:
                process.terminate()
        old_term = signal.signal(signal.SIGTERM, terminate)
        old_int = signal.signal(signal.SIGINT, terminate)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        pending = b""
        deadline = time.monotonic() + START_TIMEOUT
        startup_tail = b""
        while True:
            if not emitted and time.monotonic() > deadline:
                raise JailError("The jail did not acknowledge a successful runner start within 30 seconds.")
            events = selector.select(0.2)
            if not events:
                if process.poll() is not None:
                    break
                continue
            chunk = os.read(process.stdout.fileno(), 16384)
            if not chunk:
                break
            if log_size < MAX_LOG:
                piece = chunk[:MAX_LOG - log_size]
                os.write(log_fd, piece)
                log_size += len(piece)
            if not emitted:
                startup_tail = (startup_tail + chunk)[-4096:]
                pending += chunk
                if len(pending) > MAX_REQUEST:
                    raise JailError("Sandbox startup produced an invalid response.")
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if line.startswith(READY_PREFIX.encode()):
                        ready = json.loads(line[len(READY_PREFIX):])
                        if not isinstance(ready.get("runner_pid"), int):
                            raise JailError("Sandbox startup produced an invalid acknowledgement.")
                        emitted = True
                        state.update(status="running", sandbox_pid=process.pid)
                        store.write_json("state.json", state)
                        _response({"ok": True, "status": "running", "readiness": "sandbox_started", "jail_dir": str(store.path), "log_path": log_path})
                        break
        code = process.wait()
        if not emitted:
            raise JailError("Sandbox startup failed; no game launch was acknowledged. " + startup_tail.decode("utf-8", errors="replace").strip()[-1200:])
        state.update(status="stopped" if stop_requested else ("exited" if code == 0 else "failed"), exit_code=code, ended_at=_now())
        store.write_json("state.json", state)
        if stop_requested:
            return 0
        return code if 0 <= code < 126 else 1
    except BaseException as exc:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        state.update(ok=False, status="failed", error=str(exc), ended_at=_now())
        store.write_json("state.json", state)
        if emitted:
            return 1
        raise
    finally:
        os.close(log_fd)
        if process is not None:
            if process.stdout:
                process.stdout.close()
            if "selector" in locals():
                selector.close()
            if "old_term" in locals():
                signal.signal(signal.SIGTERM, old_term)
                signal.signal(signal.SIGINT, old_int)


def stop(store: Store) -> dict:
    """Stop only this lock's verified supervisor, using a PID reuse-safe handle."""
    lock = os.open("launch.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=store.fd)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            return {"ok": True, "status": "not_running", "jail_dir": str(store.path)}
        owner = store.read_json("owner.json")
        state = store.read_json("state.json") or {}
        # Older supervisors predate owner.json. Verify their command and PID
        # handle too, without deleting the live lock or touching the prefix.
        pid = owner.get("pid") if owner else state.get("supervisor_pid")
        ticks = owner.get("start_ticks") if owner else state.get("supervisor_start_ticks")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or pid == os.getpid():
            raise JailError("The jail is busy but its supervisor cannot be identified safely.")
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise JailError("This host does not support safe jail process termination.")
        try:
            process_fd = os.pidfd_open(pid)
        except ProcessLookupError:
            raise JailError("The previous supervisor ended; retry Stop Jail if preparation is still active.") from None
        try:
            proc = Path("/proc") / str(pid)
            if proc.stat().st_uid != os.getuid() or (ticks is not None and _process_start_ticks(pid) != ticks):
                raise JailError("The saved jail supervisor no longer matches the running process.")
            arguments = (proc / "cmdline").read_bytes().split(b"\0")
            def option(name: bytes) -> bytes | None:
                try:
                    return arguments[arguments.index(name) + 1]
                except (ValueError, IndexError):
                    return None
            valid_program = any(argument.endswith(b"/jail_helper.py") for argument in arguments)
            if b"-c" in arguments:
                try:
                    source = arguments[arguments.index(b"-c") + 1]
                    valid_program = b'READY_PREFIX = "KLAY_JAIL_READY:"' in source and b"class Store:" in source
                except IndexError:
                    pass
            if (b"-I" not in arguments or not valid_program or option(b"--operation") != b"launch"
                    or option(b"--game-id") != store.game_id.encode()
                    or option(b"--data-dir-name") != store.data_dir_name.encode()):
                raise JailError("Refusing to stop a process that is not this game's jail supervisor.")
            signal.pidfd_send_signal(process_fd, signal.SIGTERM)
            waiter = selectors.DefaultSelector()
            try:
                waiter.register(process_fd, selectors.EVENT_READ)
                if not waiter.select(5):
                    signal.pidfd_send_signal(process_fd, signal.SIGKILL)
                    if not waiter.select(5):
                        raise JailError("The jail has not finished stopping yet.")
            finally:
                waiter.close()
        finally:
            os.close(process_fd)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise JailError("The previous supervisor exited, but the jail is still busy.") from None
        state = store.read_json("state.json") or {}
        if state.get("supervisor_pid") == pid:
            state.update(status="stopped", ended_at=_now())
            store.write_json("state.json", state)
        return {"ok": True, "status": "stopped", "jail_dir": str(store.path)}
    finally:
        os.close(lock)


def report(store: Store) -> dict:
    state = store.read_json("state.json")
    if not state:
        return {"ok": True, "status": "never_run", "jail_dir": str(store.path), "log_text": ""}
    filename = state.get("log_file", "")
    if not isinstance(filename, str) or not re.fullmatch(r"run-[0-9]+-[a-f0-9]{12}\.log", filename):
        raise JailError("Invalid jail log metadata.")
    fd = os.open(filename, FILE_FLAGS, dir_fd=store.logs_fd)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise JailError("Jail log is not a regular file.")
        size = os.fstat(stream.fileno()).st_size
        stream.seek(max(0, size - REPORT_LOG))
        text = stream.read(REPORT_LOG).decode("utf-8", errors="replace")
    if state.get("status") in ("starting", "running"):
        lock = os.open("launch.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=store.fd)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                state.update(status="interrupted", error="The supervisor ended without recording a final runner status.")
        finally:
            os.close(lock)
    # Reconstruct paths from trusted storage, never follow a path from payload data.
    state.update(ok=True, jail_dir=str(store.path), log_path=str(store.path / "logs" / filename), log_text=text, log_truncated=size >= MAX_LOG or size > REPORT_LOG)
    state.pop("log_file", None)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("check", "launch", "report", "stop"), required=True)
    parser.add_argument("--game-id", required=True)
    parser.add_argument("--data-dir-name", default="klay")
    args = parser.parse_args(argv)
    store = None
    try:
        if sys.platform != "linux":
            raise JailError("Game jails require Linux.")
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise JailError("The jail request exceeds 64 KiB.")
        request = json.loads(raw or b"{}")
        config = validate_config(request) if args.operation not in ("report", "stop") else None
        store = Store(args.game_id, args.data_dir_name)
        if args.operation == "check":
            _response(check(config, store))
        elif args.operation == "launch":
            def cancel_preparation(signum, frame):
                raise JailError("Jail preparation was stopped.")
            signal.signal(signal.SIGTERM, cancel_preparation)
            return launch(config, store)
        elif args.operation == "stop":
            _response(stop(store))
        else:
            _response(report(store))
        return 0
    except (JailError, OSError, ValueError, subprocess.SubprocessError) as exc:
        _response({"ok": False, "error": str(exc)})
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
