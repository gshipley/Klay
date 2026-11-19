"""Discover installed Nexus games while leaving their runtime and accounts to Nexus."""
from __future__ import annotations

import configparser
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shlex
import shutil
from typing import NamedTuple
from urllib.parse import urlsplit

from klay import shared
from klay.game import Game
from klay.importer.location import Location, LocationSubPath
from klay.importer.source import Source, SourceIterable


class NexusLibraryError(ValueError):
    """An incomplete scan must not be treated as an empty installed library."""


_GAME_ID = re.compile(r"[a-f0-9]{16}\Z")
_LAUNCHER_NAME = re.compile(r"(?:nexus-launcher|xodus-nexus)(?:-[-\w.]+)?\.appimage\Z", re.I)
_DESKTOP_NAMES = ("org.xodusgaming.nexus.desktop", "xodus-nexus.desktop", "nexus-launcher.desktop")


def _valid_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not any(char in value for char in "\0\r\n")


def _desktop_launcher() -> list[str] | None:
    """Read a known installed launcher entry; never execute its Exec as shell text."""
    roots = [Path(shared.host_data_dir), Path("/usr/local/share"), Path("/usr/share")]
    for root in roots:
        for name in _DESKTOP_NAMES:
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                with (root / "applications" / name).open(encoding="utf-8") as handle:
                    parser.read_file(handle)
                entry = parser["Desktop Entry"]
                if entry.get("Hidden", "false").lower() == "true":
                    continue
                arguments = shlex.split(entry.get("Exec", ""))
                if not arguments:
                    continue
                executable = Path(arguments[0])
                if not executable.is_absolute():
                    found = shutil.which(arguments[0])
                    if not found:
                        continue
                    executable = Path(found)
                if not (executable.name in {"xodus-nexus", "nexus-launcher"} or _LAUNCHER_NAME.fullmatch(executable.name)):
                    continue
                if not executable.is_file() or not os.access(executable, os.X_OK):
                    continue
                # Nexus owns all launch arguments. Carry over only the explicit
                # Electron sandbox workaround from the user's installed entry.
                return [str(executable)] + (["--no-sandbox"] if "--no-sandbox" in arguments else [])
            except (OSError, ValueError, KeyError, configparser.Error):
                continue
    return None


class NexusSourceIterable(SourceIterable):
    source: "NexusSource"

    def __iter__(self):
        location = self.source.locations.config
        profile = Path(location.root).resolve()
        library_path = profile / "launcher-state.json"
        try:
            with library_path.open(encoding="utf-8") as handle:
                library = json.load(handle)
        except (OSError, ValueError) as error:
            raise NexusLibraryError(f"Cannot read Nexus library: {library_path}") from error
        if not isinstance(library, dict) or not isinstance(library.get("games"), list):
            raise NexusLibraryError("Nexus library must contain a games list.")

        # Validate before yielding, so malformed state cannot look like removal
        # of games encountered later in the file. Never read account data.
        ready: dict[str, dict] = {}
        for entry in library["games"]:
            if not isinstance(entry, dict):
                raise NexusLibraryError("Invalid game record in Nexus library.")
            state = entry.get("installState")
            if not isinstance(state, str) or state not in {"ready", "incomplete", "imported"}:
                raise NexusLibraryError("Invalid installation state in Nexus library.")
            if state != "ready" or entry.get("updateIncomplete") is True:
                continue
            game_id, title, game_path = entry.get("id"), entry.get("title"), entry.get("path")
            if not isinstance(game_id, str) or not _GAME_ID.fullmatch(game_id):
                raise NexusLibraryError("Invalid installed game ID in Nexus library.")
            if not _valid_text(title) or not _valid_text(game_path) or not Path(game_path).is_absolute():
                raise NexusLibraryError(f"Invalid installed game metadata for Nexus game {game_id}.")
            if not Path(game_path).is_dir():
                continue
            if game_id in ready and ready[game_id] != entry:
                raise NexusLibraryError(f"Conflicting installed records for Nexus game {game_id}.")
            ready[game_id] = entry

        if not ready:
            return
        launch_prefix, launch_suffix = self.source.launch_arguments(profile)
        profile_id = sha256(os.fsencode(profile)).hexdigest()[:12]
        for game_id, entry in ready.items():
            values = {
                "source": self.source.source_id,
                "added": shared.import_time,
                "name": entry["title"].strip(),
                "game_id": f"nexus_{profile_id}_{game_id}",
                "executable": shlex.join([*launch_prefix, game_id, *launch_suffix]),
            }
            if _valid_text(entry.get("publisher")):
                values["publisher"] = entry["publisher"].strip()
            additional_data = {"store": "xbox"}
            cover = entry.get("coverUrl")
            try:
                parsed_cover = urlsplit(cover) if isinstance(cover, str) else None
            except ValueError:
                parsed_cover = None
            if parsed_cover and parsed_cover.scheme == "https" and parsed_cover.netloc:
                additional_data["online_cover_url"] = cover
            else:
                icon = profile / "shortcuts" / "icons" / f"{game_id}.png"
                if icon.is_file():
                    additional_data["local_icon_path"] = icon
            yield Game(values), additional_data


class NexusLocations(NamedTuple):
    config: Location


class NexusSource(Source):
    source_id = "nexus"
    name = _("Nexus")
    available_on = {"linux"}
    iterable_class = NexusSourceIterable

    def __init__(self) -> None:
        self.locations = NexusLocations(Location(
            schema_key="nexus-location",
            candidates=(shared.host_config_dir / "xodus-nexus",),
            paths={"launcher-state.json": LocationSubPath("launcher-state.json")},
            invalid_subtitle=Location.CONFIG_INVALID_SUBTITLE,
        ))

    def launch_arguments(self, profile: Path) -> tuple[list[str], list[str]]:
        """Use Nexus's stable profile wrapper, including for unlaunched games."""
        desktop = _desktop_launcher()
        sandbox_arguments = ["--no-sandbox"] if desktop and "--no-sandbox" in desktop else []
        game_wrapper = profile / "shortcuts" / "launch-game"
        if game_wrapper.is_file() and os.access(game_wrapper, os.R_OK):
            # The generated wrapper places --launch-game before "$@", so the
            # ID must be its first argument and Electron flags follow the ID.
            return ["/bin/sh", str(game_wrapper)], sandbox_arguments
        launcher_wrapper = profile / "shortcuts" / "launch-nexus"
        if launcher_wrapper.is_file() and os.access(launcher_wrapper, os.R_OK):
            return ["/bin/sh", str(launcher_wrapper), *sandbox_arguments, "--launch-game"], []
        if desktop:
            return [*desktop, "--nexus-profile", str(profile), "--launch-game"], []
        raise NexusLibraryError("Nexus launcher was not found. Open Nexus once to refresh its launch shortcuts.")
