# steam_source.py
#
# Copyright 2022-2023 kramo
# Copyright 2023 Geoffrey Coulaud
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import re
from pathlib import Path
from typing import Iterable, NamedTuple

from klay import shared
from klay.game import Game
from klay.importer.location import Location, LocationSubPath
from klay.importer.source import SourceIterable, URLExecutableSource
from klay.utils.steam import SteamFileHelper, SteamInvalidManifestError


class SteamSourceIterable(SourceIterable):
    source: "SteamSource"

    _playtime_cache: dict[str, int] | None = None

    @staticmethod
    def _parse_localconfig_playtimes(contents: str) -> dict[str, int]:
        token_re = re.compile(r'"([^"\n]*)"|([{}])')
        stack: list[str] = []
        pending_key: str | None = None
        playtimes: dict[str, int] = {}

        def _current_appid() -> str | None:
            if len(stack) >= 2 and stack[-2].lower() == "apps" and stack[-1].isdigit():
                return stack[-1]
            return None

        for match in token_re.finditer(contents):
            quoted = match.group(1)
            brace = match.group(2)

            if brace == "{":
                if pending_key is not None:
                    stack.append(pending_key)
                    pending_key = None
                continue
            if brace == "}":
                pending_key = None
                if stack:
                    stack.pop()
                continue
            if quoted is None:
                continue

            if pending_key is None:
                pending_key = quoted
                continue

            key = pending_key
            value = quoted
            pending_key = None

            appid = _current_appid()
            if appid is None:
                continue
            if key.lower() != "playtime":
                continue
            try:
                minutes = int(value)
            except (TypeError, ValueError):
                continue
            if minutes < 0:
                continue
            previous = playtimes.get(appid)
            if previous is None or minutes > previous:
                playtimes[appid] = minutes

        return playtimes

    def _steam_playtime_minutes(self) -> dict[str, int]:
        if self._playtime_cache is not None:
            return self._playtime_cache

        playtime_by_appid: dict[str, int] = {}
        userdata_dir = self.source.locations.data.root / "userdata"
        for config_path in userdata_dir.glob("*/config/localconfig.vdf"):
            if not config_path.is_file():
                continue
            try:
                contents = config_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            local_map = self._parse_localconfig_playtimes(contents)
            for appid, minutes in local_map.items():
                previous = playtime_by_appid.get(appid)
                if previous is None or minutes > previous:
                    playtime_by_appid[appid] = minutes

        self._playtime_cache = playtime_by_appid
        return playtime_by_appid

    def get_manifest_dirs(self) -> Iterable[Path]:
        """Get dirs that contain Steam app manifests"""
        libraryfolders_path = self.source.locations.data["libraryfolders.vdf"]
        with open(libraryfolders_path, "r", encoding="utf-8") as file:
            contents = file.read()
        return [
            Path(path) / "steamapps"
            for path in re.findall('"path"\\s+"(.*)"\n', contents, re.IGNORECASE)
        ]

    def get_manifests(self) -> Iterable[Path]:
        """Get app manifests"""
        manifests = set()
        for steamapps_dir in self.get_manifest_dirs():
            if not steamapps_dir.is_dir():
                continue
            manifests.update(
                [
                    manifest
                    for manifest in steamapps_dir.glob("appmanifest_*.acf")
                    if manifest.is_file()
                ]
            )
        return manifests

    def __iter__(self):
        """Generator method producing games"""
        appid_cache = set()
        manifests = self.get_manifests()
        playtime_by_appid = self._steam_playtime_minutes()

        for manifest in manifests:
            # Get metadata from manifest
            steam = SteamFileHelper()
            try:
                local_data = steam.get_manifest_data(manifest)
            except (OSError, SteamInvalidManifestError) as error:
                logging.debug("Couldn't load appmanifest %s", manifest, exc_info=error)
                continue

            # Skip non installed games
            installed_mask = 4
            if not int(local_data["stateflags"]) & installed_mask:
                logging.debug("Skipped %s: not installed", manifest)
                continue

            # Skip duplicate appids
            appid = local_data["appid"]
            if appid in appid_cache:
                logging.debug("Skipped %s: appid already seen during import", manifest)
                continue
            appid_cache.add(appid)

            # Build game from local data
            values = {
                "added": shared.import_time,
                "name": local_data["name"],
                "source": self.source.source_id,
                "game_id": self.source.game_id_format.format(game_id=appid),
                "executable": self.source.make_executable(game_id=appid),
            }
            game = Game(values)

            # Add official cover image
            image_path = (
                self.source.locations.data["librarycache"]
                / appid
                / "library_600x900.jpg"
            )
            additional_data = {"local_image_path": image_path, "steam_appid": appid}
            if (playtime := playtime_by_appid.get(appid)) is not None:
                additional_data["playtime_minutes"] = playtime

            yield (game, additional_data)


class SteamLocations(NamedTuple):
    data: Location


class SteamSource(URLExecutableSource):
    source_id = "steam"
    name = _("Steam")
    available_on = {"linux", "win32", "darwin"}
    iterable_class = SteamSourceIterable
    url_format = "steam://rungameid/{game_id}"

    locations: SteamLocations

    def __init__(self) -> None:
        super().__init__()
        self.locations = SteamLocations(
            Location(
                schema_key="steam-location",
                candidates=(
                    shared.home / ".steam" / "steam",
                    shared.data_dir / "Steam",
                    shared.flatpak_dir / "com.valvesoftware.Steam" / "data" / "Steam",
                    shared.programfiles32_dir / "Steam",
                    shared.app_support_dir / "Steam",
                ),
                paths={
                    "libraryfolders.vdf": LocationSubPath(
                        "steamapps/libraryfolders.vdf"
                    ),
                    "librarycache": LocationSubPath("appcache/librarycache", True),
                },
                invalid_subtitle=Location.DATA_INVALID_SUBTITLE,
            )
        )
