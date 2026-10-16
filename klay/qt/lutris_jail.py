"""Resolve a Lutris game into a direct jail launch without running Lutris.

Only the installed game configuration is read. Installer scripts, the existing
Wine prefix, launch hooks, and Lutris/UMU runtimes are never imported or executed.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
import os
import ntpath
from pathlib import Path
import re
import shlex
import sqlite3
from typing import Any

from klay.qt.windows_shortcut import ShortcutError, read_shortcut, split_windows_arguments

from klay.qt.jail_helper import (
    JailError, _BINARY_DIRS, _binary_game_root, _layout_directory, _relative_within,
    _steam_library_roots, _suitable_game_root,
)

try:
    import yaml
except ModuleNotFoundError:
    yaml = None  # type: ignore[assignment]


class LutrisJailError(ValueError):
    """An installed Lutris game cannot be resolved without guessing."""


_MAX_YAML = 1024 * 1024
_LUTRIS_APP = "net.lutris.Lutris"


def _identity(path: Path) -> tuple[int, int] | str:
    try:
        metadata = path.stat()
        return metadata.st_dev, metadata.st_ino
    except OSError:
        return str(path.resolve())


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\x00\n\r"):
        raise LutrisJailError(f"Lutris has no valid {label}. Configure it in Lutris first.")
    return value.strip()


def _expand(value: str | Path, home: Path) -> Path:
    text = str(value)
    if text == "~":
        return home
    if text.startswith("~/"):
        return home / text[2:]
    if text.startswith("~") or "$" in text:
        raise LutrisJailError("Lutris paths with other users or environment variables need manual jail configuration.")
    return Path(text)


def _distinct(paths: list[Path]) -> list[Path]:
    result = []
    seen = set()
    for path in paths:
        canonical = path.resolve()
        key = _identity(canonical)
        # /home and /var/home can expose the same directory through a bind
        # mount rather than a symlink. Treat those aliases as one installation.
        if key not in seen:
            seen.add(key)
            result.append(canonical)
    return result


def _read_yaml(path: Path, *, required: bool = False) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise LutrisJailError(f"The installed Lutris game configuration is missing: {path}")
        return {}
    try:
        with path.open("r", encoding="utf-8") as stream:
            contents = stream.read(_MAX_YAML + 1)
        if len(contents) > _MAX_YAML:
            raise LutrisJailError(f"Lutris configuration is too large: {path}")
        value = yaml.safe_load(contents)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise LutrisJailError(f"Cannot read the Lutris configuration {path}: {error}") from error
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise LutrisJailError(f"Lutris configuration must contain a mapping: {path}")
    return value


def _section(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise LutrisJailError(f"Lutris's {name} configuration must be a mapping.")
    return value


def _lookup(data_root: Path, game_id: int) -> dict[str, Any] | None:
    database = data_root / "pga.db"
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
            return {str(key).lower(): row[key] for key in row.keys()} if row else None
    except sqlite3.Error as error:
        raise LutrisJailError(f"Cannot read the Lutris library {database}: {error}") from error


def _installation(game_id: int, location: str | Path | None, home: Path) -> tuple[Path, dict[str, Any]]:
    configured = _expand(location, home) if location else None
    if configured and (configured / "pga.db").is_file():
        row = _lookup(configured, game_id)
        if row is None:
            raise LutrisJailError("This game is missing from the configured Lutris library. Check Klay's Lutris source location.")
        return configured.resolve(), row
    roots = _distinct([
        home / ".local/share/lutris",
        home / f".var/app/{_LUTRIS_APP}/data/lutris",
        *([_expand(os.environ["HOST_XDG_DATA_HOME"], home) / "lutris"] if os.environ.get("HOST_XDG_DATA_HOME") else []),
        *([_expand(os.environ["XDG_DATA_HOME"], home) / "lutris"] if os.environ.get("XDG_DATA_HOME") and not os.environ.get("FLATPAK_ID") else []),
    ])
    matches = [(root, row) for root in roots if (root / "pga.db").is_file() and (row := _lookup(root, game_id))]
    if not matches:
        raise LutrisJailError("This game's installed Lutris entry was not found. Check Klay's Lutris source location.")
    if len(matches) != 1:
        raise LutrisJailError("More than one Lutris library contains this game ID. Select its Lutris source location in Klay.")
    return matches[0]


def _config_root(data_root: Path, config_token: str, home: Path) -> Path:
    flatpak_root = home / f".var/app/{_LUTRIS_APP}"
    if data_root.is_relative_to(flatpak_root.resolve()):
        candidates = [flatpak_root / "config/lutris"]
    else:
        config_home = os.environ.get("HOST_XDG_CONFIG_HOME")
        if not config_home and not os.environ.get("FLATPAK_ID"):
            config_home = os.environ.get("XDG_CONFIG_HOME")
        candidates = [(_expand(config_home, home) if config_home else home / ".config") / "lutris"]
        # A configured portable library can keep its configuration alongside it.
        candidates += [data_root / "config", data_root]
    matches = [root for root in _distinct(candidates) if (root / "games" / config_token).is_file()]
    if len(matches) != 1:
        raise LutrisJailError("The installed Lutris game YAML is missing or ambiguous. Check the selected Lutris source location.")
    return matches[0]


def _path(value: Any, label: str, home: Path, base: Path | None = None) -> Path:
    path = _expand(_text(value, label).replace("\\", "/"), home)
    if not path.is_absolute():
        if base is None:
            raise LutrisJailError(f"Lutris's {label} must have an absolute location.")
        path = base / path
    if ".." in path.parts:
        raise LutrisJailError(f"Lutris's {label} contains parent traversal; use manual jail configuration.")
    # Lutris tolerates Windows-style casing even on a case-sensitive host.
    # Match a unique directory entry; ambiguity must not pick another game.
    current = Path(path.anchor)
    for component in path.parts[1:]:
        candidate = current / component
        if not candidate.exists() and current.is_dir():
            try:
                matches = [entry for entry in current.iterdir() if entry.name.casefold() == component.casefold()]
            except OSError as error:
                raise LutrisJailError(f"Cannot inspect Lutris's {label}: {error}") from error
            if len(matches) > 1:
                raise LutrisJailError(f"Lutris's {label} has ambiguous letter casing.")
            if matches:
                candidate = matches[0]
        current = candidate
    return current


def _unreal_game_root(exe: Path, prefix: Path | None, home: Path) -> Path | None:
    """Recognize a packaged Unreal tree without searching arbitrary ancestors.

    Lutris often stores a Wine prefix as its game directory. In that case the
    executable's Win64 directory alone misses the sibling Content and Engine
    trees. This specific layout identifies the enclosing installed game while
    keeping the prefix, drive_c, and unrelated parent directories outside it.
    """
    platform = exe.parent
    binaries = platform.parent
    project = binaries.parent
    root = project.parent
    if platform.name.casefold() not in ("win32", "win64") or binaries.name.casefold() != "binaries":
        return None
    if not _suitable_game_root(root, prefix, home):
        return None
    # Links in either identifying tree cannot authorize a larger source. The
    # selected EXE path is checked separately before any jail copy is made.
    try:
        if not _layout_directory(root, "Engine") or not _layout_directory(project, "Content"):
            return None
    except JailError as error:
        raise LutrisJailError("The selected Lutris Unreal game layout contains a symbolic link.") from error
    return root


def _rebase_inside(path: Path, root: Path) -> Path | None:
    """Accept ancestor aliases, checking the original spelling for internal links."""
    relative = _relative_within(path, root)
    if relative is None:
        return None
    current = path
    for _ in range(len(relative.parts) + 1):
        if current.is_symlink():
            raise LutrisJailError("The selected Lutris game folder or executable contains a symbolic link.")
        current = current.parent
    return root / relative


def _shortcut_path(value: str, *, prefix: Path | None, home: Path, base: Path,
                   allowed_roots: list[Path] | None = None) -> Path:
    """Map only known local Wine paths; never consult dosdevices symlinks."""
    value = _text(value, "shortcut path")
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if value.startswith("\\\\?\\") and re.match(r"^[A-Za-z]:[\\/]", value[4:]):
        value = value[4:]
    variables = {"systemdrive": "C:", "programfiles": "C:\\Program Files",
                 "programfiles(x86)": "C:\\Program Files (x86)",
                 "systemroot": "C:\\windows", "windir": "C:\\windows"}
    def expand(match):
        name = match.group(1).casefold()
        if name not in variables:
            raise LutrisJailError("The Windows shortcut uses an unknown environment variable. Choose its game .exe under Advanced settings.")
        return variables[name]
    value = re.sub(r"%([^%]+)%", expand, value)
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value) or value.startswith(("\\\\", "//")):
        raise LutrisJailError("The Windows shortcut must identify a local file, not a network path or URL.")
    drive, remainder = ntpath.splitdrive(value)
    boundary = None
    if drive:
        if not remainder.startswith(("\\", "/")):
            raise LutrisJailError("Drive-relative Windows shortcut paths cannot be resolved safely.")
        relative = ntpath.normpath(remainder).replace("\\", "/").lstrip("/")
        if drive.casefold() == "c:":
            if prefix is None:
                raise LutrisJailError("A Wine prefix is required to resolve the shortcut's C: drive.")
            boundary = _path(str(prefix / "drive_c"), "Wine C: drive", home)
            mapping = prefix / "dosdevices/c:"
            if (mapping.exists() or mapping.is_symlink()) and _identity(mapping) != _identity(boundary):
                raise LutrisJailError("The Windows shortcut uses a custom Wine C: mapping. Choose its local game .exe under Advanced settings.")
            candidate = boundary / relative
        elif drive.casefold() == "z:":
            if prefix is not None:
                mapping = prefix / "dosdevices/z:"
                if (mapping.exists() or mapping.is_symlink()) and _identity(mapping) != _identity(Path("/")):
                    raise LutrisJailError("The Windows shortcut uses a custom Wine Z: mapping. Choose its local game .exe under Advanced settings.")
            candidate = Path("/") / relative
        else:
            raise LutrisJailError("The Windows shortcut uses an unsupported drive. Choose its local game .exe under Advanced settings.")
    elif value.startswith("\\"):
        raise LutrisJailError("The Windows shortcut has a rooted path without a drive letter.")
    elif Path(value).is_absolute():
        candidate = Path(value)
    else:
        candidate = Path(os.path.normpath(str(base / value.replace("\\", "/"))))
    candidate = _path(str(candidate), "shortcut path", home)
    roots = [boundary] if boundary is not None else allowed_roots
    if roots is not None:
        for root in roots:
            rebased = _rebase_inside(candidate, root)
            if rebased is not None:
                return rebased
        raise LutrisJailError("The Windows shortcut points outside its local game installation. Choose the whole game folder under Advanced settings.")
    return candidate


def _resolve_game_entry(game: dict[str, Any], row: dict[str, Any], home: Path) -> tuple[dict[str, Any], list[str]]:
    """Resolve installed shortcuts as data, retaining their launch semantics."""
    database = _path(row["directory"], "game directory", home) if row.get("directory") else None
    prefix = _path(game["prefix"], "Wine prefix", home, database) if game.get("prefix") else None
    raw = _text(game.get("exe"), "game executable")
    configured = _shortcut_path(raw, prefix=prefix, home=home, base=database or home) if ntpath.splitdrive(raw)[0] or raw.startswith('"') else _path(raw, "game executable", home, database)
    effective = dict(game, exe=str(configured))
    if configured.suffix.casefold() != ".lnk":
        working = game.get("working_dir")
        if isinstance(working, str) and (ntpath.splitdrive(working)[0] or "%" in working):
            effective["working_dir"] = str(_shortcut_path(working, prefix=prefix, home=home, base=database or home))
        return effective, []
    # Prefix-local shortcuts may not use an internal link to reach host data.
    scopes = [configured.parent]
    if prefix is not None:
        c_drive = _path(str(prefix / "drive_c"), "Wine C: drive", home)
        if _relative_within(configured, c_drive) is not None:
            _rebase_inside(configured, c_drive)
            scopes.append(c_drive)
    if database is not None and _suitable_game_root(database, prefix, home):
        _rebase_inside(configured, database)
        scopes.append(database)
    try:
        shortcut = read_shortcut(configured)
        targets = []
        for value in (shortcut.relative_path, shortcut.target):
            if not value:
                continue
            target = _shortcut_path(value, prefix=prefix, home=home, base=configured.parent, allowed_roots=scopes)
            if target.suffix.casefold() != ".exe":
                raise LutrisJailError("The Windows shortcut must point directly to a local game .exe; shortcut chains and helper commands require manual configuration.")
            targets.append(target)
        existing = {_identity(target): target for target in targets if target.is_file()}
        if not existing:
            attempted = ", ".join(str(target) for target in targets)
            raise LutrisJailError(f"The Windows shortcut's game executable was not found: {attempted}")
        if len(existing) != 1:
            raise LutrisJailError("The Windows shortcut identifies different existing executables. Choose its game .exe under Advanced settings.")
        target = next(iter(existing.values()))
        effective["exe"] = str(target)
        if shortcut.working_dir:
            effective["working_dir"] = str(_shortcut_path(shortcut.working_dir, prefix=prefix, home=home, base=configured.parent, allowed_roots=scopes))
        elif isinstance(game.get("working_dir"), str) and (ntpath.splitdrive(game["working_dir"])[0] or "%" in game["working_dir"]):
            effective["working_dir"] = str(_shortcut_path(game["working_dir"], prefix=prefix, home=home, base=configured.parent, allowed_roots=scopes))
        return effective, split_windows_arguments(shortcut.arguments)
    except (ShortcutError, OSError) as error:
        raise LutrisJailError(f"Cannot resolve the Windows shortcut {configured}: {error}") from error


def _game_paths(game: dict[str, Any], row: dict[str, Any], home: Path) -> tuple[Path, str, str | None]:
    game, _shortcut_arguments = _resolve_game_entry(game, row, home)
    database_dir = _path(row["directory"], "game directory", home) if row.get("directory") else None
    exe = _path(game.get("exe"), "game executable", home, database_dir)
    if not exe.is_file() or exe.suffix.lower() != ".exe":
        raise LutrisJailError(f"Lutris's game executable must be an existing local Windows .exe: {exe}")
    prefix = _path(game["prefix"], "Wine prefix", home, database_dir) if game.get("prefix") else None
    working = None
    if game.get("working_dir"):
        configured_working = _expand(_text(game["working_dir"], "working directory").replace("\\", "/"), home)
        if not configured_working.is_absolute():
            raise LutrisJailError(
                "Lutris's relative working directory cannot be inferred safely. "
                "Set an absolute directory in Lutris or use manual jail configuration."
            )
        working = _path(str(configured_working), "working directory", home)
    root = exe.parent
    if database_dir and _suitable_game_root(database_dir, prefix, home):
        rebased = _rebase_inside(exe, database_dir)
        if rebased is not None:
            root, exe = database_dir, rebased
    unreal_root = _unreal_game_root(exe, prefix, home)
    if unreal_root and root.is_relative_to(unreal_root):
        root = unreal_root
    if root == exe.parent:
        try:
            binary_root = _binary_game_root(exe.parent, prefix, home)
        except JailError as error:
            raise LutrisJailError(str(error)) from error
        if binary_root is not None:
            root = binary_root
    if working:
        if not working.is_dir() or not _suitable_game_root(working, prefix, home):
            raise LutrisJailError("Lutris's working directory is missing, a Wine prefix, or too broad to copy. Use manual jail configuration.")
        rebased_working = _rebase_inside(working, root)
        if rebased_working is not None:
            working = rebased_working
        else:
            rebased_exe = _rebase_inside(exe, working)
            if rebased_exe is not None:
                root, exe = working, rebased_exe
            else:
                raise LutrisJailError("Lutris's working directory is outside the game folder. Use manual jail configuration.")
    if root.name.casefold() in _BINARY_DIRS:
        raise LutrisJailError(
            "The executable is in a binary subfolder, but the complete game folder cannot be identified. "
            "Choose the whole game folder under Advanced settings."
        )
    if not _suitable_game_root(root, prefix, home):
        raise LutrisJailError("Cannot copy a Wine prefix or broad parent folder into the game jail.")
    # Ancestor aliases such as /home -> /var/home are legitimate. Links inside
    # the selected game tree are refused, including any link on the EXE path.
    current = root
    for component in (None, *exe.relative_to(root).parts):
        if component is not None:
            current /= component
        if current.is_symlink():
            raise LutrisJailError("The selected Lutris game folder or executable contains a symbolic link.")
    if working:
        current = root
        for component in working.relative_to(root).parts:
            current /= component
            if current.is_symlink():
                raise LutrisJailError("Lutris's working directory contains a symbolic link.")
    canonical = root.resolve()
    return canonical, str(exe.relative_to(root)), str(working.relative_to(root)) if working else None


def _runner_result(wine: Path, root: Path | None = None) -> dict[str, str]:
    if not wine.is_file() or not os.access(wine, os.X_OK):
        raise LutrisJailError(f"The selected Lutris Wine executable is missing or not executable: {wine}")
    resolved = wine.resolve()
    result = {"runner_type": "wine", "runner": str(resolved)}
    if root is not None:
        root = root.resolve()
        if not resolved.is_relative_to(root):
            raise LutrisJailError("The selected Wine executable escapes its trusted runner build.")
        proton = root / "proton"
        proton_wine = any(
            (root / relative).resolve() == resolved
            for relative in ("files/bin/wine", "files/bin/wine64", "dist/bin/wine", "dist/bin/wine64")
        )
        if proton_wine and (proton.exists() or proton.is_symlink()):
            # Proton's entry point prepares its isolated prefix and installs
            # the build's DXVK/VKD3D DLLs. Running files/bin/wine directly skips
            # that setup and leaves DirectX games with plain Wine graphics.
            if not proton.is_file() or not os.access(proton, os.X_OK):
                raise LutrisJailError(f"The selected Proton entry point is not executable: {proton}")
            if not proton.resolve().is_relative_to(root):
                raise LutrisJailError("The selected Proton entry point escapes its trusted runner build.")
            return {"runner_type": "proton", "runner": str(root)}
        result["runner_dir"] = str(root)
    return result


def _custom_runner(value: Any, home: Path) -> dict[str, str]:
    wine = _path(value, "custom Wine executable", home)
    if wine.is_relative_to(Path("/usr")):
        # Host-system paths are verified by the helper, including from Flatpak.
        return {"runner_type": "wine", "runner": str(wine)}
    if wine.name not in ("wine", "wine64") or wine.parent.name != "bin":
        raise LutrisJailError("Custom Wine must be bin/wine inside a specific trusted build. Use manual jail configuration.")
    root = wine.parent.parent
    if root.name in ("files", "dist"):
        root = root.parent
    broad = {home, *home.parents, Path("/opt"), Path("/usr/local"), home / ".local/share/lutris", home / ".local/share/lutris/runners/wine"}
    if _identity(root) in {_identity(path) for path in broad}:
        raise LutrisJailError("Custom Wine must select a specific build, not a shared parent directory.")
    return _runner_result(wine, root)


def _runner(wine: dict[str, Any], data_root: Path, home: Path) -> dict[str, str]:
    version = wine.get("version")
    if not version or version == "ge-proton":
        raise LutrisJailError("Lutris uses an automatic UMU Proton version. Select a specific installed Wine or Proton build in Lutris first.")
    version = _text(version, "Wine version")
    if version == "custom":
        return _custom_runner(wine.get("custom_wine_path"), home)
    if version == "system":
        return {"runner_type": "wine", "runner": "wine"}
    system_paths = {"wine-development": "/usr/lib/wine-development/wine", "winehq-devel": "/opt/wine-devel/bin/wine", "winehq-staging": "/opt/wine-staging/bin/wine"}
    if version in system_paths:
        path = Path(system_paths[version])
        return _custom_runner(str(path), home)
    if re.fullmatch(r"System wine-[A-Za-z0-9_.-]+", version):
        return {"runner_type": "wine", "runner": f"/usr/lib/{version[7:]}/bin/wine"}
    if "/" in version or "\\" in version or version in (".", ".."):
        raise LutrisJailError("Lutris's Wine version is not a valid installed build name.")
    try:
        steam_roots = _steam_library_roots(home)
    except JailError as error:
        raise LutrisJailError(str(error)) from error
    roots = _distinct([
        data_root / "runners/wine", home / ".local/share/lutris/runners/wine",
        home / f".var/app/{_LUTRIS_APP}/data/lutris/runners/wine",
        *(root / "compatibilitytools.d" for root in steam_roots),
        *(root / "steamapps/common" for root in steam_roots),
    ])
    # Exact names only: choosing a similarly named/newer build can silently
    # change the game's interpreter. Duplicate aliases to the same build agree.
    matches: dict[tuple[int, int], tuple[Path, Path]] = {}
    for parent in roots:
        build = parent / version
        if not build.is_dir():
            continue
        for relative in ("dist/bin/wine", "files/bin/wine", "bin/wine"):
            executable = build / relative
            if executable.is_file() and os.access(executable, os.X_OK):
                metadata = build.stat()
                matches.setdefault((metadata.st_dev, metadata.st_ino), (executable, build))
                break
    if not matches:
        raise LutrisJailError(f"Lutris's selected Wine/Proton build '{version}' is not installed or accessible. Select an installed build in Lutris.")
    if len(matches) != 1:
        raise LutrisJailError(f"More than one installed runner is named '{version}'. Select a custom Wine path in Lutris to disambiguate it.")
    executable, build = next(iter(matches.values()))
    return _runner_result(executable, build)


def resolve_lutris_jail(
    game: Any, *, lutris_location: str | Path | None = None, host_home: Path | None = None,
) -> dict[str, Any]:
    """Read the current stable Lutris entry and return a complete jail config.

    ``game`` accepts a GameEntry or its metadata mapping. A stale configured
    location falls back to native/Flatpak libraries only when one ID matches.
    No game execution, prefix reuse, downloads, or configuration writes occur.
    """
    if yaml is None:
        raise LutrisJailError(
            "Automatic Lutris jail configuration needs PyYAML. Install your distribution's "
            "python3-pyyaml package for native Klay, or install PyYAML in Klay's Python environment."
        )
    data = game if isinstance(game, Mapping) else getattr(game, "data", None)
    if not isinstance(data, Mapping):
        raise LutrisJailError("This game has no Lutris metadata.")
    match = re.fullmatch(r"lutris_(?:(.+)_)?([0-9]+)", str(data.get("game_id", "")))
    if match is None:
        raise LutrisJailError("This game has no stable Lutris game ID. Use manual jail configuration.")
    expected_runner, identifier = match.groups()
    if expected_runner and expected_runner != "wine":
        raise LutrisJailError("Automatic jails currently support Lutris Wine games with a local .exe.")
    home = (host_home or Path.home()).resolve()
    data_root, row = _installation(int(identifier), lutris_location, home)
    if row.get("runner") != "wine" or (expected_runner and row.get("runner") != expected_runner):
        raise LutrisJailError("This Lutris entry is not a Wine game. Use manual jail configuration.")
    if not row.get("installed", True):
        raise LutrisJailError("This game is not installed in Lutris.")
    token = _text(row.get("configpath"), "game configuration path")
    if Path(token).name != token or "\\" in token or token in (".", ".."):
        raise LutrisJailError("Lutris's game configuration path must stay inside its games directory.")
    if not token.endswith((".yml", ".yaml")):
        token += ".yml"
    config_root = _config_root(data_root, token, home)
    installed = _read_yaml(config_root / "games" / token, required=True)
    runner_defaults = _read_yaml(config_root / "runners/wine.yml")
    # Game options come only from top-level game, not the installer script.
    game_options = _section(installed, "game")
    default_wine = _section(runner_defaults, "wine")
    game_wine = _section(installed, "wine")
    wine_options = {**default_wine, **game_wine}
    # Lutris's version lookup skips an empty/'ge-proton' game value and then
    # checks the runner-level selection before using its automatic default.
    if not game_wine.get("version") or game_wine["version"] == "ge-proton":
        wine_options["version"] = default_wine.get("version")
    # Validate the effective system sections while refusing launch hooks/env
    # inheritance. The jail constructs its own restricted environment.
    _section(_read_yaml(config_root / "system.yml"), "system")
    _section(runner_defaults, "system")
    _section(installed, "system")
    game_options, shortcut_arguments = _resolve_game_entry(game_options, row, home)
    root, executable, working = _game_paths(game_options, row, home)
    args = game_options.get("args", "")
    if args is None:
        args = ""
    if not isinstance(args, str):
        raise LutrisJailError("Lutris game arguments must be a quoted command-line string.")
    try:
        arguments = shortcut_arguments + shlex.split(args)
    except ValueError as error:
        raise LutrisJailError(f"Lutris game arguments have invalid quoting: {error}") from error
    if len(arguments) > 128 or any("\x00" in argument for argument in arguments) or sum(map(len, arguments)) > 65536:
        raise LutrisJailError("Lutris game arguments exceed the jail's size limit or contain a null byte.")
    config = {"enabled": True, "game_dir": str(root), "exe": executable, "arguments": arguments, **_runner(wine_options, data_root, home)}
    if working is not None:
        config["working_dir"] = working
    return config
