from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import quote
import webbrowser


@dataclass
class GameEntry:
    path: Path
    data: dict[str, Any]

    @property
    def game_id(self) -> str:
        return str(self.data.get("game_id", self.path.stem))

    @property
    def name(self) -> str:
        return str(self.data.get("name", self.path.stem))

    @property
    def developer(self) -> str:
        return str(self.data.get("developer") or "")

    @property
    def source(self) -> str:
        return str(self.data.get("source") or "")

    @property
    def base_source(self) -> str:
        return self.source.split("_")[0] if self.source else ""

    @property
    def added(self) -> int:
        return int(self.data.get("added") or 0)

    @property
    def last_played(self) -> int:
        return int(self.data.get("last_played") or 0)

    @property
    def executable(self) -> str | list[str]:
        executable = self.data.get("executable", "")
        if isinstance(executable, (str, list)):
            return executable
        return str(executable)

    @property
    def hidden(self) -> bool:
        return bool(self.data.get("hidden"))

    @property
    def removed(self) -> bool:
        return bool(self.data.get("removed"))

    @property
    def blacklisted(self) -> bool:
        return bool(self.data.get("blacklisted"))

    def executable_text(self) -> str:
        executable = self.executable
        return shlex.join(executable) if isinstance(executable, list) else executable

    def set_value(self, key: str, value: Any) -> None:
        self.data[key] = value


class GameLibrary:
    SOURCE_LABELS = {
        "steam": "Steam",
        "lutris": "Lutris",
        "heroic": "Heroic",
        "desktop": "Desktop Entries",
        "flatpak": "Flatpak",
        "bottles": "Bottles",
        "itch": "itch",
        "legendary": "Legendary",
        "retroarch": "RetroArch",
        "imported": "Added",
    }

    SEARCH_ENGINES = {
        "igdb": "https://www.igdb.com/search?type=1&q=",
        "sgdb": "https://www.steamgriddb.com/search/grids?term=",
        "protondb": "https://www.protondb.com/search?q=",
        "pcgw": "https://www.pcgamingwiki.com/w/index.php?search=",
        "lutris": "https://lutris.net/games?q=",
        "hltb": "https://howlongtobeat.com/?q=",
    }

    def __init__(self, data_dir_name: str = "klay") -> None:
        self.data_dir_name = data_dir_name
        xdg_data = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        xdg_config = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
        data_root = xdg_data / data_dir_name
        self.config_root = xdg_config / data_dir_name
        self.games_dir = data_root / "games"
        self.covers_dir = data_root / "covers"
        self.games_dir.mkdir(parents=True, exist_ok=True)
        self.covers_dir.mkdir(parents=True, exist_ok=True)
        self.config_root.mkdir(parents=True, exist_ok=True)

    def load_games(
        self, *, include_removed: bool = False, include_blacklisted: bool = False
    ) -> list[GameEntry]:
        if not self.games_dir.is_dir():
            return []

        games: list[GameEntry] = []
        for path in sorted(self.games_dir.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as open_file:
                    data = json.load(open_file)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            if not include_removed and data.get("removed"):
                continue
            if not include_blacklisted and data.get("blacklisted"):
                continue
            games.append(GameEntry(path=path, data=data))

        games.sort(key=lambda game: game.name.lower())
        return games

    def save_game(self, game: GameEntry) -> None:
        with game.path.open("w", encoding="utf-8") as open_file:
            json.dump(game.data, open_file)

    def cover_path(self, game: GameEntry) -> Path | None:
        if not self.covers_dir.is_dir():
            return None
        for suffix in ("gif", "png", "jpg", "jpeg", "webp", "tiff"):
            path = self.covers_dir / f"{game.game_id}.{suffix}"
            if path.is_file():
                return path
        return None

    def set_cover(self, game: GameEntry, cover_source: Path | None) -> None:
        for suffix in ("tiff", "gif", "png", "jpg", "jpeg", "webp"):
            (self.covers_dir / f"{game.game_id}.{suffix}").unlink(missing_ok=True)

        if cover_source is None:
            return

        suffix = cover_source.suffix.lower().lstrip(".")
        if suffix not in {"tiff", "gif", "png", "jpg", "jpeg", "webp"}:
            suffix = "png"
        destination = self.covers_dir / f"{game.game_id}.{suffix}"
        shutil.copy2(cover_source, destination)

    def next_imported_game_id(self) -> str:
        max_num = 0
        for path in self.games_dir.glob("imported_*.json"):
            match = re.match(r"imported_(\d+)\.json$", path.name)
            if not match:
                continue
            max_num = max(max_num, int(match.group(1)))
        return f"imported_{max_num + 1}"

    def add_manual_game(
        self,
        *,
        name: str,
        executable: str,
        developer: str = "",
        cover_source: Path | None = None,
    ) -> GameEntry:
        timestamp = int(time())
        game_id = self.next_imported_game_id()
        data: dict[str, Any] = {
            "added": timestamp,
            "blacklisted": False,
            "developer": developer or None,
            "executable": executable,
            "game_id": game_id,
            "hidden": False,
            "last_played": 0,
            "name": name,
            "removed": False,
            "source": "imported",
            "version": 1.5,
        }
        path = self.games_dir / f"{game_id}.json"
        game = GameEntry(path=path, data=data)
        self.save_game(game)
        self.set_cover(game, cover_source)
        return game

    def update_manual_game(
        self,
        game: GameEntry,
        *,
        name: str,
        executable: str,
        developer: str = "",
        cover_source: Path | None = None,
    ) -> None:
        game.set_value("name", name)
        game.set_value("executable", executable)
        game.set_value("developer", developer or None)
        self.save_game(game)
        if cover_source is not None:
            self.set_cover(game, cover_source)

    def set_hidden(self, game: GameEntry, hidden: bool) -> None:
        game.set_value("hidden", hidden)
        self.save_game(game)

    def set_removed(self, game: GameEntry, removed: bool) -> None:
        game.set_value("removed", removed)
        self.save_game(game)

    def source_label(self, source: str) -> str:
        return self.SOURCE_LABELS.get(source, source.replace("_", " ").title())

    def load_game_by_id(self, game_id: str, *, include_removed: bool = False) -> GameEntry | None:
        path = self.games_dir / f"{game_id}.json"
        try:
            with path.open("r", encoding="utf-8") as open_file:
                data = json.load(open_file)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        if not include_removed and data.get("removed"):
            return None
        return GameEntry(path=path, data=data)

    def mark_played(self, game: GameEntry) -> None:
        game.data["last_played"] = int(time())
        self.save_game(game)

    def launch(self, game: GameEntry) -> None:
        executable = game.executable
        if isinstance(executable, list):
            subprocess.Popen(executable, start_new_session=True)  # noqa: S603
        elif sys.platform.startswith("win"):
            subprocess.Popen(
                executable,
                shell=True,
                start_new_session=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )  # noqa: S602,S603
        else:
            subprocess.Popen(executable, shell=True, start_new_session=True)  # noqa: S602,S603

        self.mark_played(game)

    def open_web_search(self, game: GameEntry, engine: str) -> bool:
        url = self.SEARCH_ENGINES.get(engine)
        if not url:
            return False
        return webbrowser.open(url + quote(game.name))
