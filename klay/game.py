from __future__ import annotations

import shlex
from time import time
from typing import Any


class Game:
    """Lightweight data container used by importer source backends."""

    def __init__(self, data: dict[str, Any]) -> None:
        defaults: dict[str, Any] = {
            "added": int(time()),
            "blacklisted": False,
            "developer": None,
            "executable": "",
            "game_id": "",
            "hidden": False,
            "last_played": 0,
            "playtime_minutes": None,
            "name": "",
            "removed": False,
            "source": "",
            "version": 1.5,
        }
        defaults.update(data)
        self.update_values(defaults)

    def update_values(self, data: dict[str, Any]) -> None:
        for key, value in data.items():
            if key == "executable" and isinstance(value, list):
                value = shlex.join(value)
            setattr(self, key, value)

    @property
    def base_source(self) -> str:
        return str(self.source).split("_")[0]
