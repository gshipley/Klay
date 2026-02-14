# main.py
#
# Copyright 2022-2024 kramo
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

import json
import logging
import lzma
import os
import shlex
import sys
from time import time
from typing import Any, Optional
from urllib.parse import quote

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

# pylint: disable=wrong-import-position
from gi.repository import Adw, Gio, GLib, Gtk

from klay import shared
from klay.details_dialog import DetailsDialog
from klay.extensions import collect_source_classes, load_extension_modules
from klay.game import Game
from klay.importer.bottles_source import BottlesSource
from klay.importer.desktop_source import DesktopSource
from klay.importer.flatpak_source import FlatpakSource
from klay.importer.heroic_source import HeroicSource
from klay.importer.importer import Importer  # yo dawg
from klay.importer.itch_source import ItchSource
from klay.importer.legendary_source import LegendarySource
from klay.importer.lutris_source import LutrisSource
from klay.importer.retroarch_source import RetroarchSource
from klay.importer.source import Source
from klay.importer.steam_source import SteamSource
from klay.logging.setup import log_system_info, setup_logging
from klay.preferences import KlayPreferences
from klay.store.managers.cover_manager import CoverManager
from klay.store.managers.display_manager import DisplayManager
from klay.store.managers.file_manager import FileManager
from klay.store.managers.sgdb_manager import SgdbManager
from klay.store.managers.steam_api_manager import SteamAPIManager
from klay.store.store import Store
from klay.utils.run_executable import run_executable
from klay.window import KlayWindow


BUILTIN_SOURCE_CLASSES: tuple[type[Source], ...] = (
    LutrisSource,
    SteamSource,
    HeroicSource,
    BottlesSource,
    FlatpakSource,
    DesktopSource,
    ItchSource,
    LegendarySource,
    RetroarchSource,
)


class KlayApplication(Adw.Application):
    state = shared.AppState.DEFAULT
    win: KlayWindow
    init_search_term: Optional[str] = None
    source_classes_by_id: dict[str, type[Source]]

    def __init__(self) -> None:
        shared.store = Store()
        super().__init__(application_id=shared.APP_ID)
        self.source_classes_by_id = {}
        self.load_source_classes()

        search = GLib.OptionEntry()
        search.long_name = "search"
        search.short_name = ord("s")
        search.flags = 0
        search.arg = int(GLib.OptionArg.STRING)
        search.arg_data = None
        search.description = "Open the app with this term in the search entry"
        search.arg_description = "TERM"

        launch = GLib.OptionEntry()
        launch.long_name = "launch"
        launch.short_name = ord("l")
        launch.flags = int(GLib.OptionFlags.NONE)
        launch.arg = int(GLib.OptionArg.STRING)
        launch.arg_data = None
        launch.description = "Run a game with the given game_id"
        launch.arg_description = "GAME_ID"

        self.add_main_option_entries((search, launch))

        if sys.platform.startswith("darwin"):
            if settings := Gtk.Settings.get_default():
                settings.props.gtk_decoration_layout = "close,minimize,maximize:"
        elif shared.FLAVOR == "klay":
            if settings := Gtk.Settings.get_default():
                if "kde" in os.getenv("XDG_CURRENT_DESKTOP", "").lower():
                    settings.props.gtk_icon_theme_name = "breeze"

    def do_activate(self) -> None:  # pylint: disable=arguments-differ
        """Called on app creation"""
        try:
            setup_logging()
        except ValueError:
            pass

        log_system_info()

        # Create the main window
        win = self.props.active_window  # pylint: disable=no-member
        if not win:
            shared.win = win = KlayWindow(application=self)

        # Save window geometry
        shared.state_schema.bind(
            "width", shared.win, "default-width", Gio.SettingsBindFlags.DEFAULT
        )
        shared.state_schema.bind(
            "height", shared.win, "default-height", Gio.SettingsBindFlags.DEFAULT
        )
        shared.state_schema.bind(
            "is-maximized", shared.win, "maximized", Gio.SettingsBindFlags.DEFAULT
        )

        # Load games from disk
        shared.store.add_manager(FileManager(), False)
        shared.store.add_manager(DisplayManager())
        self.state = shared.AppState.LOAD_FROM_DISK
        self.load_games_from_disk()
        self.state = shared.AppState.DEFAULT
        shared.win.create_source_rows()

        # Add rest of the managers for game imports
        shared.store.add_manager(CoverManager())
        shared.store.add_manager(SteamAPIManager())
        shared.store.add_manager(SgdbManager())
        shared.store.toggle_manager_in_pipelines(FileManager, True)

        # Create actions
        self.create_actions(
            {
                ("quit", ("<primary>q",)),
                ("about",),
                ("preferences", ("<primary>comma",)),
                ("launch_game",),
                ("hide_game",),
                ("edit_game",),
                ("add_game", ("<primary>n",)),
                ("import", ("<primary>i",)),
                ("remove_game_details_view", ("Delete",)),
                ("remove_game",),
                ("igdb_search",),
                ("sgdb_search",),
                ("protondb_search",),
                ("pcgw_search",),
                ("lutris_search",),
                ("hltb_search",),
                ("show_sidebar", ("F9",), shared.win),
                ("show_hidden", ("<primary>h",), shared.win),
                ("go_to_parent", ("<alt>Up",), shared.win),
                ("go_home", ("<alt>Home",), shared.win),
                ("toggle_search", ("<primary>f",), shared.win),
                ("undo", ("<primary>z",), shared.win),
                ("open_menu", ("F10",), shared.win),
                ("close", ("<primary>w",), shared.win),
            }
        )

        sort_action = Gio.SimpleAction.new_stateful(
            "sort_by",
            GLib.VariantType.new("s"),
            sort_mode := GLib.Variant("s", shared.state_schema.get_string("sort-mode")),
        )
        sort_action.connect("activate", shared.win.on_sort_action)
        shared.win.add_action(sort_action)
        shared.win.on_sort_action(sort_action, sort_mode)

        if self.init_search_term:  # For command line activation
            shared.win.search_bar.set_search_mode(True)
            shared.win.search_entry.set_text(self.init_search_term)
            shared.win.search_entry.set_position(-1)

        shared.win.present()

        if shared.schema.get_boolean("auto-import"):
            self.on_import_action()

    def do_handle_local_options(self, options: GLib.VariantDict) -> int:
        if search := options.lookup_value("search"):
            self.init_search_term = search.get_string()
        elif game_id := options.lookup_value("launch"):
            try:
                data = json.load(
                    (path := shared.games_dir / (game_id.get_string() + ".json")).open(
                        "r", encoding="utf-8"
                    )
                )
                executable = (
                    shlex.join(data["executable"])
                    if isinstance(data["executable"], list)
                    else data["executable"]
                )
                name = data["name"]

                run_executable(executable)

                data["last_played"] = int(time())
                json.dump(data, path.open("w", encoding="utf-8"))

            except (IndexError, KeyError, OSError, json.decoder.JSONDecodeError):
                return 1

            self.register()
            self.send_notification(
                "launch", Gio.Notification.new(_("{} launched").format(name))
            )

            # Sleep for 6 seconds before withdrawing the notification
            # The amount a notification stays up is ~5, so leave an extra second for the animation
            GLib.usleep(6000000)
            self.withdraw_notification("launch")

            return 0
        return -1

    def load_games_from_disk(self) -> None:
        if shared.games_dir.is_dir():
            for game_file in shared.games_dir.iterdir():
                try:
                    data = json.load(game_file.open())
                except (OSError, json.decoder.JSONDecodeError):
                    continue
                game = Game(data)
                shared.store.add_game(game, {"skip_save": True})

    def get_source_name(self, source_id: str) -> Any:
        if source_id == "all":
            name = _("All Games")
        elif source_id == "imported":
            name = _("Added")
        else:
            if source_class := self.source_classes_by_id.get(source_id.split("_")[0]):
                name = source_class.name
            else:
                return source_id
        return name

    def load_source_classes(self) -> None:
        for source_class in BUILTIN_SOURCE_CLASSES:
            self.source_classes_by_id[source_class.source_id] = source_class

        for source_class in collect_source_classes(load_extension_modules()):
            if source_class.source_id in self.source_classes_by_id:
                logging.warning(
                    "Extension source id '%s' conflicts with a built-in source. Skipped.",
                    source_class.source_id,
                )
                continue
            self.source_classes_by_id[source_class.source_id] = source_class

    def source_enabled(self, source_id: str) -> bool:
        try:
            return shared.schema.get_boolean(source_id)
        except GLib.Error:
            # Extension sources are enabled when no explicit schema key exists.
            return True

    def on_about_action(self, *_args: Any) -> None:
        # Get the debug info from the log files
        debug_str = ""
        for index, path in enumerate(shared.log_files):
            # Add a horizontal line between runs
            if index > 0:
                debug_str += "─" * 37 + "\n"
            # Add the run's logs
            log_file = (
                lzma.open(path, "rt", encoding="utf-8")
                if path.name.endswith(".xz")
                else open(path, "r", encoding="utf-8")
            )
            debug_str += log_file.read()
            log_file.close()

        about = Adw.AboutDialog.new_from_appdata(
            shared.PREFIX + "/" + shared.APP_ID + ".metainfo.xml", shared.VERSION
        )
        about.set_developers(
            (
                "kramo https://kramo.page",
                "Geoffrey Coulaud https://geoffrey-coulaud.fr",
                "Rilic https://rilic.red",
                "Arcitec https://github.com/Arcitec",
                "Paweł Lidwin https://github.com/imLinguin",
                "Domenico https://github.com/Domefemia",
                "Rafael Mardojai CM https://mardojai.com",
                "Clara Hobbs https://github.com/Ratfink",
                "Sabri Ünal https://github.com/sabriunal",
            )
        )
        about.set_designers(("kramo https://kramo.page",))
        about.set_copyright("© 2022-2024 kramo")
        # Translators: Replace this with Your Name, Your Name <your.email@example.com>, or Your Name https://your-site.com for it to show up in the About dialog.
        about.set_translator_credits(_("translator-credits"))
        about.set_debug_info(debug_str)
        about.set_debug_info_filename(shared.BINARY_NAME + ".log")
        about.add_legal_section(
            "Steam Branding",
            "© 2023 Valve Corporation",
            Gtk.License.CUSTOM,
            "Steam and the Steam logo are trademarks and/or registered trademarks of Valve Corporation in the U.S. and/or other countries.",  # pylint: disable=line-too-long
        )
        about.present(shared.win)

    def on_preferences_action(
        self,
        _action: Any = None,
        _parameter: Any = None,
        page_name: Optional[str] = None,
        expander_row: Optional[str] = None,
    ) -> Optional[KlayPreferences]:
        if KlayPreferences.is_open:
            return

        win = KlayPreferences()
        if page_name:
            win.set_visible_page_name(page_name)
        if expander_row:
            getattr(win, expander_row).set_expanded(True)
        win.present(shared.win)

        return win

    def on_launch_game_action(self, *_args: Any) -> None:
        shared.win.active_game.launch()

    def on_hide_game_action(self, *_args: Any) -> None:
        shared.win.active_game.toggle_hidden()

    def on_edit_game_action(self, *_args: Any) -> None:
        DetailsDialog(shared.win.active_game).present(shared.win)

    def on_add_game_action(self, *_args: Any) -> None:
        if DetailsDialog.is_open:
            return

        DetailsDialog().present(shared.win)

    def on_import_action(self, *_args: Any) -> None:
        shared.importer = Importer()

        for source_class in self.source_classes_by_id.values():
            if not self.source_enabled(source_class.source_id):
                continue
            try:
                shared.importer.add_source(source_class())
            except Exception:  # pylint: disable=broad-exception-caught
                logging.exception("Failed to initialize source %s", source_class.source_id)

        shared.importer.run()

    def on_remove_game_action(self, *_args: Any) -> None:
        shared.win.active_game.remove_game()

    def on_remove_game_details_view_action(self, *_args: Any) -> None:
        if shared.win.navigation_view.get_visible_page() == shared.win.details_page:
            self.on_remove_game_action()

    def search(self, uri: str) -> None:
        Gio.AppInfo.launch_default_for_uri(f"{uri}{quote(shared.win.active_game.name)}")

    def on_igdb_search_action(self, *_args: Any) -> None:
        self.search("https://www.igdb.com/search?type=1&q=")

    def on_sgdb_search_action(self, *_args: Any) -> None:
        self.search("https://www.steamgriddb.com/search/grids?term=")

    def on_protondb_search_action(self, *_args: Any) -> None:
        self.search("https://www.protondb.com/search?q=")

    def on_pcgw_search_action(self, *_args: Any) -> None:
        self.search("https://www.pcgamingwiki.com/w/index.php?search=")

    def on_lutris_search_action(self, *_args: Any) -> None:
        self.search("https://lutris.net/games?q=")

    def on_hltb_search_action(self, *_args: Any) -> None:
        self.search("https://howlongtobeat.com/?q=")

    def on_quit_action(self, *_args: Any) -> None:
        self.quit()

    def create_actions(self, actions: set) -> None:
        for action in actions:
            simple_action = Gio.SimpleAction.new(action[0], None)

            scope = action[2] if action[2:3] else self
            simple_action.connect("activate", getattr(scope, f"on_{action[0]}_action"))

            if action[1:2]:
                self.set_accels_for_action(
                    f"app.{action[0]}" if scope == self else f"win.{action[0]}",
                    (
                        tuple(s.replace("<primary>", "<meta>") for s in action[1])
                        if sys.platform.startswith("darwin")
                        else action[1]
                    ),
                )

            scope.add_action(simple_action)


def main(_version: int) -> Any:
    """App entry point"""
    app = KlayApplication()
    return app.run(sys.argv)
