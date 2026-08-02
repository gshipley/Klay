from __future__ import annotations

import json
from pathlib import Path
from typing import Any


GENERAL_BOOL_KEYS = {
    "dark-mode": False,
    "show-splash": False,
    "start-big-picture": False,
    "auto-import": True,
    "exit-after-launch": False,
    "cover-launches-game": False,
    "high-quality-images": False,
    "remove-missing": True,
    "sgdb": False,
    "sgdb-prefer": False,
    "sgdb-animated": False,
    "igdb": False,
    "refresh-covers-on-metadata": False,
    "geforcenow-include-in-all-games": False,
    "geforcenow-close-on-stream-end": False,
}

SOURCE_BOOL_KEYS = {
    "steam": True,
    "geforcenow": True,
    "lutris": True,
    "heroic": True,
    "bottles": True,
    "itch": True,
    "legendary": True,
    "retroarch": True,
    "desktop": True,
    "flatpak": True,
}

IMPORT_BOOL_KEYS = {
    "lutris-import-steam": False,
    "lutris-import-flatpak": False,
    "heroic-import-epic": True,
    "heroic-import-gog": True,
    "heroic-import-amazon": True,
    "heroic-import-sideload": True,
    "flatpak-import-launchers": False,
}

STRING_KEYS = {
    "steam-location": "~/.steam/steam",
    "lutris-location": "~/.var/app/net.lutris.Lutris/data/lutris/",
    "lutris-cache-location": "~/.var/app/net.lutris.Lutris/cache/lutris",
    "heroic-location": "~/.config/heroic/",
    "bottles-location": "~/.var/app/com.usebottles.bottles/data/bottles/",
    "itch-location": "~/.var/app/io.itch.itch/config/itch/",
    "legendary-location": "~/.config/legendary/",
    "retroarch-location": "~/.var/app/org.libretro.RetroArch/config/retroarch/",
    "flatpak-system-location": "/var/lib/flatpak/",
    "flatpak-user-location": "~/.local/share/flatpak/",
    "sgdb-key": "",
    "igdb-client-id": "",
    "igdb-client-secret": "",
    "igdb-key": "",
}

STATE_BOOL_KEYS = {
    "show-sidebar": False,
    "geforcenow-key-art-cover-migration-done": False,
    "geforcenow-key-art-square-fill-migration-done": False,
}
STATE_STRING_KEYS = {"sort-mode": "last_played"}


class SettingsBackend:
    def __init__(self, data_dir_name: str = "klay") -> None:
        config_root = Path.home() / ".config" / data_dir_name
        config_root.mkdir(parents=True, exist_ok=True)
        self.fallback_path = config_root / "qt-settings.json"
        self.fallback_state_path = config_root / "qt-state.json"
        self.fallback = self._load_json(self.fallback_path)
        self.fallback_state = self._load_json(self.fallback_state_path)

        self.schema = None
        self.state_schema = None

        try:
            from klay import shared
        except Exception:
            shared = None

        if shared is not None:
            self.schema = getattr(shared, "schema", None)
            self.state_schema = getattr(shared, "state_schema", None)

    def _load_json(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_json(self, path: Path, data: dict[str, Any]) -> None:
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def _schema_has_key(self, key: str, state: bool = False) -> bool:
        schema = self.state_schema if state else self.schema
        if schema is None:
            return False
        try:
            return key in schema.list_keys()
        except Exception:
            return False

    def get_bool(self, key: str, default: bool = False) -> bool:
        if self._schema_has_key(key):
            try:
                return bool(self.schema.get_boolean(key))  # type: ignore[union-attr]
            except Exception:
                pass
        return bool(self.fallback.get(key, default))

    def set_bool(self, key: str, value: bool) -> None:
        updated = False
        if self._schema_has_key(key):
            try:
                self.schema.set_boolean(key, bool(value))  # type: ignore[union-attr]
                updated = True
            except Exception:
                pass
        self.fallback[key] = bool(value)
        self._save_json(self.fallback_path, self.fallback)
        if not updated:
            return

    def get_string(self, key: str, default: str = "") -> str:
        if self._schema_has_key(key):
            try:
                return str(self.schema.get_string(key))  # type: ignore[union-attr]
            except Exception:
                pass
        return str(self.fallback.get(key, default))

    def set_string(self, key: str, value: str) -> None:
        if self._schema_has_key(key):
            try:
                self.schema.set_string(key, str(value))  # type: ignore[union-attr]
            except Exception:
                pass
        self.fallback[key] = str(value)
        self._save_json(self.fallback_path, self.fallback)

    def get_state_bool(self, key: str, default: bool = False) -> bool:
        if self._schema_has_key(key, state=True):
            try:
                return bool(self.state_schema.get_boolean(key))  # type: ignore[union-attr]
            except Exception:
                pass
        return bool(self.fallback_state.get(key, default))

    def set_state_bool(self, key: str, value: bool) -> None:
        if self._schema_has_key(key, state=True):
            try:
                self.state_schema.set_boolean(key, bool(value))  # type: ignore[union-attr]
            except Exception:
                pass
        self.fallback_state[key] = bool(value)
        self._save_json(self.fallback_state_path, self.fallback_state)

    def get_state_string(self, key: str, default: str = "") -> str:
        if self._schema_has_key(key, state=True):
            try:
                return str(self.state_schema.get_string(key))  # type: ignore[union-attr]
            except Exception:
                pass
        return str(self.fallback_state.get(key, default))

    def set_state_string(self, key: str, value: str) -> None:
        if self._schema_has_key(key, state=True):
            try:
                self.state_schema.set_string(key, str(value))  # type: ignore[union-attr]
            except Exception:
                pass
        self.fallback_state[key] = str(value)
        self._save_json(self.fallback_state_path, self.fallback_state)

    def source_enabled(self, source_id: str) -> bool:
        base_source = source_id.split("_")[0]
        defaults = {**SOURCE_BOOL_KEYS}
        if base_source in defaults:
            return self.get_bool(base_source, defaults[base_source])
        return True
