# flatpak_source.py
#
# Copyright 2022-2023 kramo
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

from itertools import chain
from pathlib import Path
from typing import NamedTuple

from gi.repository import GLib

from klay import shared
from klay.game import Game
from klay.importer.location import Location, LocationSubPath
from klay.importer.source import ExecutableFormatSource, SourceIterable


class FlatpakSourceIterable(SourceIterable):
    source: "FlatpakSource"

    def __iter__(self):
        """Generator method producing games"""

        user_data = self.source.locations.user_data["icons"]
        system_data = self.source.locations.system_data["icons"]
        icon_theme = None
        try:
            import gi

            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk  # pylint: disable=import-outside-toplevel

            icon_theme = Gtk.IconTheme.new()
            if user_data:
                icon_theme.add_search_path(str(user_data))

            if system_data:
                icon_theme.add_search_path(str(system_data))
        except Exception:
            icon_theme = None

        if not (system_data or user_data):
            return

        app_blacklist = {
            "hu.kramo.Cartridges",
            "hu.kramo.Cartridges.Devel",
            "page.kramo.Cartridges",
            "page.kramo.Cartridges.Devel",
            "com.grantshipley.Klay",
            "com.grantshipley.Klay.Devel",
            shared.APP_ID,
        }
        blacklist = (
            app_blacklist
            if shared.schema.get_boolean("flatpak-import-launchers")
            else app_blacklist
            | {
                "com.valvesoftware.Steam",
                "net.lutris.Lutris",
                "com.heroicgameslauncher.hgl",
                "com.usebottles.Bottles",
                "io.itch.itch",
                "org.libretro.RetroArch",
            }
        )

        generators = set(
            location.iterdir()
            for location in (
                self.source.locations.user_data["applications"],
                self.source.locations.system_data["applications"],
            )
            if location
        )

        for entry in chain(*generators):
            if entry.suffix != ".desktop":
                continue

            keyfile = GLib.KeyFile.new()

            try:
                keyfile.load_from_file(str(entry), 0)

                if "Game" not in keyfile.get_string_list("Desktop Entry", "Categories"):
                    continue

                if (
                    flatpak_id := keyfile.get_string("Desktop Entry", "X-Flatpak")
                ) in blacklist or flatpak_id != entry.stem:
                    continue

                name = keyfile.get_string("Desktop Entry", "Name")

            except GLib.Error:
                continue

            values = {
                "source": self.source.source_id,
                "added": shared.import_time,
                "name": name,
                "game_id": self.source.game_id_format.format(game_id=flatpak_id),
                "executable": self.source.make_executable(flatpak_id=flatpak_id),
            }
            game = Game(values)

            additional_data = {}

            if icon_theme is not None:
                direction = 0
                if shared.win is not None and hasattr(shared.win, "get_direction"):
                    direction = shared.win.get_direction()
                try:
                    icon_info = icon_theme.lookup_icon(
                        keyfile.get_string("Desktop Entry", "Icon"),
                        None,
                        512,
                        1,
                        direction,
                        0,
                    )
                    icon_file = icon_info.get_file()
                    if icon_file and (icon_path := icon_file.get_path()):
                        additional_data = {"local_icon_path": Path(icon_path)}
                except Exception:
                    pass

            yield (game, additional_data)


class FlatpakLocations(NamedTuple):
    system_data: Location
    user_data: Location


class FlatpakSource(ExecutableFormatSource):
    """Generic Flatpak source"""

    source_id = "flatpak"
    name = _("Flatpak")
    iterable_class = FlatpakSourceIterable
    executable_format = "flatpak run {flatpak_id}"
    available_on = {"linux"}

    locations: FlatpakLocations

    def __init__(self) -> None:
        super().__init__()
        self.locations = FlatpakLocations(
            Location(
                schema_key="flatpak-system-location",
                candidates=("/var/lib/flatpak/",),
                paths={
                    "applications": LocationSubPath("exports/share/applications", True),
                    "icons": LocationSubPath("exports/share/icons", True),
                },
                invalid_subtitle=Location.DATA_INVALID_SUBTITLE,
                optional=True,
            ),
            Location(
                schema_key="flatpak-user-location",
                candidates=(shared.data_dir / "flatpak",),
                paths={
                    "applications": LocationSubPath("exports/share/applications", True),
                    "icons": LocationSubPath("exports/share/icons", True),
                },
                invalid_subtitle=Location.DATA_INVALID_SUBTITLE,
                optional=True,
            ),
        )
