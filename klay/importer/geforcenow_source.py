# geforcenow_source.py
#
# Copyright 2026
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

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

try:
    import brotli
except ImportError:
    brotli = None  # type: ignore[assignment]

import requests
from requests.exceptions import RequestException

from klay import shared
from klay.game import Game
from klay.importer.source import Source, SourceIterable
from klay.utils.steam import SteamFileHelper, SteamInvalidManifestError

GFN_APPS_ENDPOINT = "https://games.geforce.com/graphql"
GFN_APPS_FALLBACK_ENDPOINT = "https://api-prod.nvidia.com/services/gfngames/v1/gameList"
GFN_MAX_PAGES = 20
GFN_CACHE_TTL_SECONDS = 6 * 60 * 60
REQUEST_TIMEOUT_SECONDS = 12
GFN_LIBRARY_OWNED_STATUSES = {"MANUAL", "PLATFORM_SYNC", "OWNED", "STEAM_SYNC"}
GFN_DEFAULT_SHORT_NAME = "game_gfn_pc"


def _sanitize_game_token(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    sanitized = sanitized.strip("._-")
    return sanitized or "gfn"


class GeForceNowSourceIterable(SourceIterable):
    source: "GeForceNowSource"

    def _installed_steam_games(self) -> tuple[dict[str, str], Path | None]:
        from klay.importer.steam_source import SteamSource, SteamSourceIterable

        steam_games: dict[str, str] = {}
        steam_root: Path | None = None
        try:
            steam_source = SteamSource()
            steam_iterable = SteamSourceIterable(steam_source)
            manifests = steam_iterable.get_manifests()
            steam_root = steam_source.locations.data.root
        except Exception as error:  # pylint: disable=broad-exception-caught
            logging.debug(
                "GeForce NOW: unable to read installed Steam games", exc_info=error
            )
            return steam_games, steam_root

        steam = SteamFileHelper()
        for manifest in manifests:
            try:
                local_data = steam.get_manifest_data(manifest)
            except (OSError, SteamInvalidManifestError):
                continue

            try:
                state_flags = int(str(local_data.get("stateflags", "0")).strip())
            except ValueError:
                continue

            if not (state_flags & 4):
                continue

            appid = str(local_data.get("appid", "")).strip()
            if not appid.isdigit():
                continue

            name = str(local_data.get("name", "")).strip()
            if appid not in steam_games:
                steam_games[appid] = name or f"Steam {appid}"

        return steam_games, steam_root

    @staticmethod
    def _steam_id_from_loginusers(steam_root: Path | None) -> str | None:
        if steam_root is None:
            return None

        loginusers_path = steam_root / "config" / "loginusers.vdf"
        if not loginusers_path.is_file():
            return None

        try:
            contents = loginusers_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None

        users = re.findall(
            r'"(?P<steamid>\d{17})"\s*\{(?P<body>.*?)\n\s*\}',
            contents,
            flags=re.DOTALL,
        )
        if not users:
            return None

        def _has_flag(body: str, key: str) -> bool:
            return re.search(rf'"{re.escape(key)}"\s+"1"', body) is not None

        for steam_id, body in users:
            if _has_flag(body, "MostRecent"):
                return steam_id

        for steam_id, body in users:
            if _has_flag(body, "AllowAutoLogin"):
                return steam_id

        return users[0][0]

    @staticmethod
    def _parse_local_owned_steam_ids(contents: str) -> set[str]:
        token_re = re.compile(r'"([^"\n]*)"|([{}])')
        stack: list[str] = []
        pending_key: str | None = None
        appids: set[str] = set()

        for match in token_re.finditer(contents):
            quoted = match.group(1)
            brace = match.group(2)

            if brace == "{":
                if pending_key is not None:
                    lower_stack = [part.lower() for part in stack]
                    is_appid = pending_key.isdigit() and int(pending_key) > 9
                    if is_appid:
                        if (
                            len(lower_stack) >= 4
                            and lower_stack[-4:] == ["software", "valve", "steam", "apps"]
                        ):
                            appids.add(pending_key)
                        elif lower_stack and lower_stack[-1] == "apptickets":
                            appids.add(pending_key)

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

            pending_key = None

        return appids

    @staticmethod
    def _local_steam_owned_games(
        steam_root: Path | None,
        steam_catalog: dict[str, dict[str, str]],
    ) -> dict[str, str]:
        if steam_root is None:
            return {}

        userdata_dir = steam_root / "userdata"
        if not userdata_dir.is_dir():
            return {}

        steam_games: dict[str, str] = {}
        for config_path in userdata_dir.glob("*/config/localconfig.vdf"):
            if not config_path.is_file():
                continue
            try:
                contents = config_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for appid in GeForceNowSourceIterable._parse_local_owned_steam_ids(contents):
                if appid in steam_catalog and appid not in steam_games:
                    steam_games[appid] = steam_catalog[appid]["title"]
        return steam_games

    @staticmethod
    def _public_steam_games(steam_root: Path | None) -> dict[str, str]:
        steam_id = GeForceNowSourceIterable._steam_id_from_loginusers(steam_root)
        if not steam_id:
            return {}

        url = f"https://steamcommunity.com/profiles/{steam_id}/games/?xml=1"
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
        except RequestException:
            return {}

        xml_data = response.text.strip()
        if not xml_data.startswith("<?xml"):
            return {}

        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError:
            return {}

        if root.tag != "gamesList":
            return {}

        steam_games: dict[str, str] = {}
        for game_node in root.findall("./games/game"):
            appid = (game_node.findtext("appID") or "").strip()
            if not appid.isdigit():
                continue
            name = (game_node.findtext("name") or "").strip()
            steam_games[appid] = name or f"Steam {appid}"
        return steam_games

    @staticmethod
    def _extract_json_payload(blob: bytes) -> dict[str, Any] | None:
        marker = b'{"data":{"panels"'
        start = blob.find(marker)
        if start < 0:
            return None

        depth = 0
        in_string = False
        escaped = False
        end: int | None = None

        for index in range(start, len(blob)):
            byte = blob[index]
            if in_string:
                if escaped:
                    escaped = False
                elif byte == 0x5C:
                    escaped = True
                elif byte == 0x22:
                    in_string = False
                continue

            if byte == 0x22:
                in_string = True
            elif byte == 0x7B:
                depth += 1
            elif byte == 0x7D:
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break

        if end is None:
            return None

        try:
            payload = json.loads(blob[start:end].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any] | None:
        start = text.find("{")
        if start < 0:
            return None

        depth = 0
        in_string = False
        escaped = False
        end: int | None = None

        for index, char in enumerate(text[start:], start):
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break

        if end is None:
            return None

        try:
            payload = json.loads(text[start:end])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _extract_cache_request_url(blob: bytes) -> str | None:
        marker = GFN_APPS_ENDPOINT.encode("utf-8") + b"?"
        start = blob.find(marker)
        if start < 0:
            return None

        end = start
        while end < len(blob):
            byte = blob[end]
            if byte < 0x21 or byte > 0x7E:
                break
            end += 1

        try:
            url = blob[start:end].decode("utf-8")
        except UnicodeDecodeError:
            url = blob[start:end].decode("utf-8", errors="ignore")

        url = url.strip()
        return url or None

    @staticmethod
    def _extract_http_cache_json_payload(blob: bytes) -> dict[str, Any] | None:
        header_start = blob.find(b"HTTP/1.1 200")
        if header_start < 0:
            return None

        header_block = blob[header_start : min(len(blob), header_start + 2048)]
        content_length_match = re.search(rb"content-length:(\d+)", header_block)
        if content_length_match is None:
            return None

        try:
            content_length = int(content_length_match.group(1))
        except ValueError:
            return None
        if content_length <= 0:
            return None

        content_encoding = ""
        content_encoding_match = re.search(
            rb"content-encoding:([^\x00\r\n]+)",
            header_block,
        )
        if content_encoding_match is not None:
            try:
                content_encoding = (
                    content_encoding_match.group(1).decode("utf-8").strip().lower()
                )
            except UnicodeDecodeError:
                content_encoding = ""

        body_anchor = header_start - content_length
        for delta in range(-128, 129):
            body_start = body_anchor + delta
            body_end = body_start + content_length
            if body_start < 0 or body_end > header_start:
                continue

            payload_bytes = blob[body_start:body_end]
            if content_encoding == "br":
                if brotli is None:
                    return None
                try:
                    payload_bytes = brotli.decompress(payload_bytes)
                except Exception:  # pylint: disable=broad-exception-caught
                    continue
            elif content_encoding not in {"", "identity"}:
                return None

            payload_bytes = payload_bytes.lstrip()
            if not payload_bytes.startswith(b"{"):
                continue

            try:
                payload = json.loads(payload_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue

            if isinstance(payload, dict):
                return payload

        return None

    @staticmethod
    def _owned_library_filter(filters: Any) -> bool:
        if not isinstance(filters, dict):
            return False

        variants = filters.get("variants")
        if not isinstance(variants, dict):
            return False

        gfn = variants.get("gfn")
        if not isinstance(gfn, dict):
            return False

        library = gfn.get("library")
        if not isinstance(library, dict):
            return False

        status = library.get("status")
        if not isinstance(status, dict):
            return False

        return str(status.get("notEquals") or "").strip().upper() == "NOT_OWNED"

    @staticmethod
    def _owned_library_query_key(url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.netloc != urlparse(GFN_APPS_ENDPOINT).netloc:
            return None

        params = parse_qs(parsed.query)
        request_type = str((params.get("requestType") or [""])[0]).strip()
        if request_type != "apps":
            return None

        raw_variables = (params.get("variables") or [""])[0]
        if not raw_variables:
            return None

        variables = GeForceNowSourceIterable._extract_json_object(raw_variables)
        if not isinstance(variables, dict):
            return None

        if str(variables.get("searchString") or "").strip():
            return None
        if not GeForceNowSourceIterable._owned_library_filter(variables.get("filters")):
            return None

        normalized_variables = dict(variables)
        normalized_variables.pop("cursor", None)
        normalized_variables.pop("fetchCount", None)
        return json.dumps(normalized_variables, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _best_online_cover_url(images: Any) -> str | None:
        if not isinstance(images, dict):
            return None

        for key in ("BOX_ART", "KEY_ART", "HERO_IMAGE", "TV_BANNER"):
            candidate = images.get(key)
            if isinstance(candidate, str):
                url = candidate.strip()
                if url.startswith(("https://", "http://")):
                    return url

        for candidate in images.values():
            if isinstance(candidate, str):
                url = candidate.strip()
                if url.startswith(("https://", "http://")):
                    return url
        return None

    @staticmethod
    def _catalog_variant_for_library_variant(
        catalog_entry: dict[str, Any],
        *,
        app_store: str,
        variant_id: str,
    ) -> dict[str, str] | None:
        variants = catalog_entry.get("variants")
        if not isinstance(variants, list):
            return None

        fallback_match: dict[str, str] | None = None
        for variant in variants:
            if not isinstance(variant, dict):
                continue

            candidate_store = str(variant.get("app_store") or "").upper()
            if candidate_store != app_store:
                continue

            if fallback_match is None:
                fallback_match = {
                    "variant_id": str(variant.get("variant_id") or "").strip(),
                    "store_id": str(variant.get("store_id") or "").strip(),
                    "short_name": str(variant.get("short_name") or "").strip(),
                }

            candidate_variant_id = str(variant.get("variant_id") or "").strip()
            if variant_id and candidate_variant_id == variant_id:
                return {
                    "variant_id": candidate_variant_id,
                    "store_id": str(variant.get("store_id") or "").strip(),
                    "short_name": str(variant.get("short_name") or "").strip(),
                }

        return fallback_match

    @staticmethod
    def _library_variant_priority(*, selected: bool, status: str) -> int:
        if selected:
            return 2
        if status in GFN_LIBRARY_OWNED_STATUSES:
            return 1
        return 0

    @staticmethod
    def _library_game_token(
        *,
        app_store: str,
        store_id: str,
        variant_id: str,
        parent_game_id: str,
    ) -> str:
        if app_store == "STEAM" and store_id.isdigit():
            return store_id

        store_slug = re.sub(r"[^a-z0-9]+", "_", app_store.lower()).strip("_") or "store"
        stable_key = store_id.strip() or variant_id.strip() or parent_game_id.strip()
        return _sanitize_game_token(f"{store_slug}_{stable_key}")

    @staticmethod
    def _steam_cover_url(appid: str) -> str:
        return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/library_600x900.jpg"

    def _merge_owned_library_app(
        self,
        owned_games: dict[str, dict[str, Any]],
        catalog: dict[str, dict[str, Any]],
        app: Any,
        *,
        fallback_title: str = "",
    ) -> None:
        if not isinstance(app, dict):
            return

        gfn_id = str(app.get("id") or "").strip()
        if not gfn_id:
            return

        catalog_entry = catalog.get(gfn_id)
        if not isinstance(catalog_entry, dict):
            return

        cms_id = str(catalog_entry.get("cms_id") or "").strip()
        if not cms_id.isdigit():
            return

        catalog_title = str(catalog_entry.get("title") or "").strip()
        title = str(app.get("title") or "").strip() or fallback_title or catalog_title
        online_cover_url = (
            str(catalog_entry.get("cover_url") or "").strip()
            or self._best_online_cover_url(app.get("images"))
            or ""
        )

        variants = app.get("variants")
        if not isinstance(variants, list):
            return

        for variant in variants:
            if not isinstance(variant, dict):
                continue

            app_store = str(variant.get("appStore") or "").strip().upper()
            if not app_store:
                continue

            gfn_state = variant.get("gfn")
            if not isinstance(gfn_state, dict):
                continue

            library_state = gfn_state.get("library")
            if not isinstance(library_state, dict):
                continue

            status = str(library_state.get("status") or "").strip().upper()
            selected = bool(library_state.get("selected"))
            if not selected and status not in GFN_LIBRARY_OWNED_STATUSES:
                continue

            variant_id = str(variant.get("id") or "").strip()
            catalog_variant = self._catalog_variant_for_library_variant(
                catalog_entry,
                app_store=app_store,
                variant_id=variant_id,
            )
            store_id = (
                str(catalog_variant.get("store_id") or "").strip()
                if catalog_variant
                else ""
            )
            short_name = str(variant.get("shortName") or "").strip()
            if not short_name and catalog_variant is not None:
                short_name = str(catalog_variant.get("short_name") or "").strip()
            if not short_name:
                short_name = variant_id if variant_id.isdigit() else GFN_DEFAULT_SHORT_NAME

            game_token = self._library_game_token(
                app_store=app_store,
                store_id=store_id,
                variant_id=variant_id,
                parent_game_id=gfn_id,
            )
            if not game_token:
                continue

            steam_appid = store_id if app_store == "STEAM" and store_id.isdigit() else ""
            cover_url = (
                self._steam_cover_url(steam_appid) if steam_appid else online_cover_url
            )
            priority = self._library_variant_priority(selected=selected, status=status)

            existing = owned_games.get(game_token)
            if existing is not None:
                existing_priority = int(existing.get("_priority", 0))
                if existing_priority > priority:
                    continue
                if (
                    existing_priority == priority
                    and existing.get("online_cover_url")
                    and not cover_url
                ):
                    continue

            owned_games[game_token] = {
                "name": title or game_token,
                "cms_id": cms_id,
                "short_name": short_name,
                "parent_game_id": gfn_id,
                "gfn_library": True,
                "store": app_store,
                "steam_appid": steam_appid,
                "online_cover_url": cover_url,
                "_priority": priority,
            }

    def _cached_gfn_http_library_apps(self) -> list[dict[str, Any]]:
        cache_root = self.source.gfn_http_cache_path()
        if cache_root is None or not cache_root.is_dir():
            return []

        grouped_queries: dict[str, dict[str, Any]] = {}
        for cache_file in cache_root.rglob("*"):
            if not cache_file.is_file():
                continue

            try:
                blob = cache_file.read_bytes()
            except OSError:
                continue

            if b"requestType=apps" not in blob or b"NOT_OWNED" not in blob:
                continue

            request_url = self._extract_cache_request_url(blob)
            if not request_url:
                continue

            query_key = self._owned_library_query_key(request_url)
            if query_key is None:
                continue

            payload = self._extract_http_cache_json_payload(blob)
            if payload is None:
                continue

            apps = payload.get("data", {}).get("apps")
            if not isinstance(apps, dict):
                continue

            items = apps.get("items")
            if not isinstance(items, list):
                continue

            page_info = apps.get("pageInfo")
            total_count = 0
            if isinstance(page_info, dict):
                try:
                    total_count = int(page_info.get("totalCount") or 0)
                except (TypeError, ValueError):
                    total_count = 0

            query_group = grouped_queries.setdefault(
                query_key,
                {"items": {}, "page_count": 0, "total_count": 0},
            )
            if total_count > int(query_group["total_count"]):
                query_group["total_count"] = total_count
            query_group["page_count"] = int(query_group["page_count"]) + 1

            cached_items = query_group["items"]
            if not isinstance(cached_items, dict):
                cached_items = {}
                query_group["items"] = cached_items

            for item in items:
                if not isinstance(item, dict):
                    continue

                item_id = str(item.get("id") or "").strip()
                if not item_id:
                    continue

                existing = cached_items.get(item_id)
                if existing is None:
                    cached_items[item_id] = item
                    continue

                existing_cover = self._best_online_cover_url(existing.get("images"))
                item_cover = self._best_online_cover_url(item.get("images"))
                if not existing_cover and item_cover:
                    cached_items[item_id] = item

        if not grouped_queries:
            return []

        best_group = max(
            grouped_queries.values(),
            key=lambda group: (
                int(group.get("total_count", 0)),
                len(group.get("items", {})),
                int(group.get("page_count", 0)),
            ),
        )
        items = best_group.get("items")
        return list(items.values()) if isinstance(items, dict) else []

    def _cached_gfn_library_games(
        self,
        catalog: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        owned_games: dict[str, dict[str, Any]] = {}
        for app in self._cached_gfn_http_library_apps():
            self._merge_owned_library_app(owned_games, catalog, app)

        cache_storage_root = self.source.gfn_cache_storage_path()
        if cache_storage_root is None or not cache_storage_root.is_dir():
            return owned_games

        for cache_file in cache_storage_root.rglob("*"):
            if not cache_file.is_file():
                continue
            try:
                blob = cache_file.read_bytes()
            except OSError:
                continue

            if b"requestType=panels/Library" not in blob:
                continue

            payload = self._extract_json_payload(blob)
            if payload is None:
                continue

            panels = payload.get("data", {}).get("panels")
            if not isinstance(panels, list):
                continue

            for panel in panels:
                if not isinstance(panel, dict) or panel.get("name") != "LIBRARY":
                    continue

                sections = panel.get("sections")
                if not isinstance(sections, list):
                    continue

                for section in sections:
                    if not isinstance(section, dict):
                        continue
                    items = section.get("items")
                    if not isinstance(items, list):
                        continue

                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        self._merge_owned_library_app(owned_games, catalog, item.get("app"))

        return owned_games

    def __iter__(self):
        if not self.source.gfn_client_detected:
            return

        catalog = self.source.catalog()
        if not catalog:
            return

        owned_games = self._cached_gfn_library_games(catalog)
        steam_catalog = self.source.steam_variant_catalog(catalog)

        installed_steam_games, steam_root = self._installed_steam_games()
        local_steam_games = self._local_steam_owned_games(steam_root, steam_catalog)
        public_steam_games = {}
        if not owned_games and not local_steam_games:
            public_steam_games = self._public_steam_games(steam_root)

        steam_owned_games: dict[str, str] = dict(local_steam_games)
        steam_owned_games.update(public_steam_games)
        steam_owned_games.update(installed_steam_games)

        for appid, name in steam_owned_games.items():
            catalog_entry = steam_catalog.get(appid)
            if catalog_entry is None:
                continue

            game_token = _sanitize_game_token(appid)
            if game_token in owned_games:
                continue

            owned_games[game_token] = {
                "name": name or str(catalog_entry.get("title") or f"Steam {appid}"),
                "cms_id": str(catalog_entry.get("cms_id") or "").strip(),
                "short_name": str(catalog_entry.get("short_name") or "").strip()
                or GFN_DEFAULT_SHORT_NAME,
                "parent_game_id": str(catalog_entry.get("gfn_id") or "").strip(),
                "store": "STEAM",
                "steam_appid": appid,
                "online_cover_url": self._steam_cover_url(appid),
                "_priority": 0,
            }

        if not owned_games:
            return

        for game_token, entry in sorted(
            owned_games.items(),
            key=lambda row: (
                str(row[1].get("name") or "").casefold(),
                str(row[0]),
            ),
        ):
            cms_id = str(entry.get("cms_id") or "").strip()
            if not cms_id.isdigit():
                continue

            short_name = str(entry.get("short_name") or "").strip() or GFN_DEFAULT_SHORT_NAME
            parent_game_id = str(entry.get("parent_game_id") or "").strip()

            values = {
                "source": self.source.source_id,
                "added": shared.import_time,
                "name": str(entry.get("name") or game_token),
                "game_id": self.source.game_id_format.format(game_id=game_token),
                "executable": self.source.make_executable(
                    cms_id=cms_id,
                    short_name=short_name,
                    parent_game_id=parent_game_id,
                ),
            }
            game = Game(values)

            additional_data: dict[str, Any] = {}
            store = str(entry.get("store") or "").strip()
            if store:
                additional_data["store"] = store

            steam_appid = str(entry.get("steam_appid") or "").strip()
            if steam_appid.isdigit():
                additional_data["steam_appid"] = steam_appid

            online_cover_url = str(entry.get("online_cover_url") or "").strip()
            if online_cover_url:
                additional_data["online_cover_url"] = online_cover_url
            if bool(entry.get("gfn_library")):
                additional_data["gfn_library"] = True
                if "KEY_ART_" in online_cover_url:
                    additional_data["square_fill_cover"] = True

            yield game, additional_data


class GeForceNowSource(Source):
    source_id = "geforcenow"
    name = _("GeForce NOW")
    available_on = {"linux"}
    iterable_class = GeForceNowSourceIterable
    flatpak_app_id = "com.nvidia.geforcenow"

    locations: tuple[()]
    _catalog_cache: dict[str, dict[str, Any]] | None

    def __init__(self) -> None:
        super().__init__()
        self.locations = ()
        self._catalog_cache = None

    @property
    def gfn_client_detected(self) -> bool:
        install_candidates = (
            shared.host_data_dir / "flatpak" / "app" / self.flatpak_app_id,
            shared.data_dir / "flatpak" / "app" / self.flatpak_app_id,
            Path("/var/lib/flatpak/app") / self.flatpak_app_id,
        )
        if any(path.is_dir() for path in install_candidates):
            return True

        if (flatpak := shutil.which("flatpak")) is None:
            return False
        try:
            process = subprocess.run(
                [flatpak, "info", self.flatpak_app_id],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return process.returncode == 0

    @property
    def _cache_path(self) -> Path:
        cache_dir = shared.cache_dir / shared.DATA_DIR_NAME
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / "geforcenow-catalog.json"

    def gfn_cache_storage_path(self) -> Path | None:
        candidates = (
            shared.home
            / ".var"
            / "app"
            / self.flatpak_app_id
            / ".local"
            / "state"
            / "NVIDIA"
            / "GeForceNOW"
            / "CefCache"
            / "Default"
            / "Service Worker"
            / "CacheStorage",
            shared.home
            / ".local"
            / "state"
            / "NVIDIA"
            / "GeForceNOW"
            / "CefCache"
            / "Default"
            / "Service Worker"
            / "CacheStorage",
        )
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        return None

    def gfn_http_cache_path(self) -> Path | None:
        candidates = (
            shared.home
            / ".var"
            / "app"
            / self.flatpak_app_id
            / ".local"
            / "state"
            / "NVIDIA"
            / "GeForceNOW"
            / "CefCache"
            / "Default"
            / "Cache"
            / "Cache_Data",
            shared.home
            / ".local"
            / "state"
            / "NVIDIA"
            / "GeForceNOW"
            / "CefCache"
            / "Default"
            / "Cache"
            / "Cache_Data",
        )
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        return None

    @staticmethod
    def _normalize_catalog(
        raw_catalog: Any,
    ) -> dict[str, dict[str, Any]] | None:
        if not isinstance(raw_catalog, dict):
            return None

        normalized: dict[str, dict[str, Any]] = {}
        for gfn_id, entry in raw_catalog.items():
            if not isinstance(gfn_id, str):
                continue
            gfn_id = gfn_id.strip()
            if not gfn_id:
                continue
            if not isinstance(entry, dict):
                continue

            cms_id = str(entry.get("cms_id", "")).strip()
            title = str(entry.get("title", "")).strip()
            if not (cms_id.isdigit() and title):
                continue
            cover_url = str(entry.get("cover_url", "")).strip()

            raw_variants = entry.get("variants")
            if not isinstance(raw_variants, list):
                raw_variants = []

            seen_variants: set[tuple[str, str, str]] = set()
            variants: list[dict[str, str]] = []
            for variant in raw_variants:
                if not isinstance(variant, dict):
                    continue

                app_store = str(variant.get("app_store", "")).strip().upper()
                if not app_store:
                    continue

                os_type = str(variant.get("os_type", "")).strip().upper()
                if os_type and os_type != "WINDOWS":
                    continue

                variant_id = str(variant.get("variant_id", "")).strip()
                store_id = str(variant.get("store_id", "")).strip()
                short_name = str(variant.get("short_name", "")).strip()

                signature = (app_store, variant_id, store_id)
                if signature in seen_variants:
                    continue
                seen_variants.add(signature)

                variants.append(
                    {
                        "app_store": app_store,
                        "variant_id": variant_id,
                        "store_id": store_id,
                        "short_name": short_name,
                        "os_type": os_type or "WINDOWS",
                    }
                )

            normalized[gfn_id] = {
                "cms_id": cms_id,
                "title": title,
                "cover_url": cover_url,
                "variants": variants,
            }

        return normalized or None

    @staticmethod
    def _legacy_steam_catalog_to_catalog(
        legacy_steam_catalog: dict[str, Any],
    ) -> dict[str, dict[str, Any]] | None:
        converted: dict[str, dict[str, Any]] = {}
        for appid, entry in legacy_steam_catalog.items():
            if not isinstance(appid, str) or not appid.isdigit():
                continue
            if not isinstance(entry, dict):
                continue

            cms_id = str(entry.get("cms_id", "")).strip()
            title = str(entry.get("title", "")).strip()
            gfn_id = str(entry.get("gfn_id", "")).strip()
            if not (cms_id.isdigit() and title and gfn_id):
                continue

            catalog_entry = converted.setdefault(
                gfn_id,
                {"cms_id": cms_id, "title": title, "variants": []},
            )
            if not catalog_entry.get("cms_id"):
                catalog_entry["cms_id"] = cms_id
            if not catalog_entry.get("title"):
                catalog_entry["title"] = title

            variants = catalog_entry.setdefault("variants", [])
            if not isinstance(variants, list):
                variants = []
                catalog_entry["variants"] = variants
            variants.append(
                {
                    "app_store": "STEAM",
                    "variant_id": "",
                    "store_id": appid,
                    "short_name": GFN_DEFAULT_SHORT_NAME,
                    "os_type": "WINDOWS",
                }
            )

        return GeForceNowSource._normalize_catalog(converted)

    @staticmethod
    def _catalog_has_cover_urls(catalog: dict[str, dict[str, Any]]) -> bool:
        for entry in catalog.values():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("cover_url") or "").strip():
                return True
        return False

    def _load_catalog_cache(self) -> dict[str, dict[str, Any]] | None:
        path = self._cache_path
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        if not isinstance(payload, dict):
            return None

        updated_at = payload.get("updated_at")
        if not isinstance(updated_at, (int, float)):
            return None
        if int(time()) - int(updated_at) > GFN_CACHE_TTL_SECONDS:
            return None

        catalog = self._normalize_catalog(payload.get("catalog"))
        if catalog is not None and self._catalog_has_cover_urls(catalog):
            return catalog

        legacy = payload.get("steam_catalog")
        if isinstance(legacy, dict):
            return self._legacy_steam_catalog_to_catalog(legacy)
        return None

    def _save_catalog_cache(self, catalog: dict[str, dict[str, Any]]) -> None:
        path = self._cache_path
        payload = {
            "updated_at": int(time()),
            "catalog": catalog,
        }
        try:
            path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        except OSError:
            return

    @staticmethod
    def _query_catalog_page(after: str) -> tuple[list[dict], bool, str]:
        query = (
            f'{{ apps(country:"US", language:"en_US", after: "{after}") '
            "{ pageInfo { hasNextPage endCursor } "
            "items { id cmsId title type images { KEY_ART HERO_IMAGE TV_BANNER } "
            "variants { id title appStore osType storeId shortName } } "
            "} }"
        )
        try:
            response = requests.get(
                GFN_APPS_ENDPOINT,
                params={"requestType": "apps", "query": query},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except RequestException:
            response = requests.post(
                GFN_APPS_FALLBACK_ENDPOINT,
                data=query,
                headers={"Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        response.raise_for_status()
        payload = response.json()
        apps = payload.get("data", {}).get("apps", {})
        items = apps.get("items") or []
        if not isinstance(items, list):
            items = []
        page_info = apps.get("pageInfo") or {}
        has_next_page = bool(page_info.get("hasNextPage"))
        end_cursor = str(page_info.get("endCursor") or "")
        return items, has_next_page, end_cursor

    def _fetch_catalog(self) -> dict[str, dict[str, Any]]:
        after = ""
        catalog: dict[str, dict[str, Any]] = {}
        seen_variants: dict[str, set[tuple[str, str, str]]] = {}
        for _page in range(GFN_MAX_PAGES):
            try:
                items, has_next_page, end_cursor = self._query_catalog_page(after)
            except (RequestException, ValueError, TypeError) as error:
                logging.warning(
                    "GeForce NOW: failed to fetch catalog page", exc_info=error
                )
                break

            for item in items:
                if not isinstance(item, dict):
                    continue

                item_type = str(item.get("type") or "").upper()
                if item_type != "GAME":
                    continue

                title = str(item.get("title") or "").strip()
                gfn_id = str(item.get("id") or "").strip()
                cms_id = str(item.get("cmsId") or "").strip()
                if not (title and gfn_id and cms_id.isdigit()):
                    continue

                catalog_entry = catalog.setdefault(
                    gfn_id,
                    {"cms_id": cms_id, "title": title, "cover_url": "", "variants": []},
                )
                if not str(catalog_entry.get("cms_id") or "").isdigit():
                    catalog_entry["cms_id"] = cms_id
                if not str(catalog_entry.get("title") or "").strip():
                    catalog_entry["title"] = title
                if not str(catalog_entry.get("cover_url") or "").strip():
                    catalog_entry["cover_url"] = (
                        GeForceNowSourceIterable._best_online_cover_url(item.get("images"))
                        or ""
                    )

                variants = item.get("variants") or []
                if not isinstance(variants, list):
                    continue
                variant_signatures = seen_variants.setdefault(gfn_id, set())
                for variant in variants:
                    if not isinstance(variant, dict):
                        continue

                    app_store = str(variant.get("appStore") or "").upper()
                    os_type = str(variant.get("osType") or "").upper()
                    if not app_store or (os_type and os_type != "WINDOWS"):
                        continue

                    variant_id = str(variant.get("id") or "").strip()
                    store_id = str(variant.get("storeId") or "").strip()
                    short_name = str(variant.get("shortName") or "").strip()
                    signature = (app_store, variant_id, store_id)
                    if signature in variant_signatures:
                        continue
                    variant_signatures.add(signature)

                    raw_variants = catalog_entry.get("variants")
                    if not isinstance(raw_variants, list):
                        raw_variants = []
                        catalog_entry["variants"] = raw_variants
                    raw_variants.append(
                        {
                            "app_store": app_store,
                            "variant_id": variant_id,
                            "store_id": store_id,
                            "short_name": short_name,
                            "os_type": os_type or "WINDOWS",
                        }
                    )

            if not has_next_page or not end_cursor:
                break
            after = end_cursor

        return catalog

    def catalog(self) -> dict[str, dict[str, Any]]:
        if self._catalog_cache is not None:
            return self._catalog_cache

        cached = self._load_catalog_cache()
        if cached is not None:
            self._catalog_cache = cached
            return self._catalog_cache

        fetched = self._fetch_catalog()
        self._catalog_cache = fetched
        if fetched:
            self._save_catalog_cache(fetched)
        return self._catalog_cache

    @staticmethod
    def steam_variant_catalog(
        catalog: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, str]]:
        steam_catalog: dict[str, dict[str, str]] = {}
        for gfn_id, entry in catalog.items():
            if not isinstance(entry, dict):
                continue

            cms_id = str(entry.get("cms_id") or "").strip()
            title = str(entry.get("title") or "").strip()
            if not (cms_id.isdigit() and title and gfn_id):
                continue

            variants = entry.get("variants")
            if not isinstance(variants, list):
                continue

            for variant in variants:
                if not isinstance(variant, dict):
                    continue

                app_store = str(variant.get("app_store") or "").strip().upper()
                if app_store != "STEAM":
                    continue

                appid = str(variant.get("store_id") or "").strip()
                if not appid.isdigit():
                    continue
                if appid in steam_catalog:
                    continue

                steam_catalog[appid] = {
                    "cms_id": cms_id,
                    "title": title,
                    "gfn_id": gfn_id,
                    "short_name": str(variant.get("short_name") or "").strip(),
                }
        return steam_catalog

    def steam_catalog(self) -> dict[str, dict[str, str]]:
        return self.steam_variant_catalog(self.catalog())

    def make_executable(
        self,
        *,
        cms_id: str,
        short_name: str = GFN_DEFAULT_SHORT_NAME,
        parent_game_id: str = "",
    ) -> str:
        safe_cms_id = quote(str(cms_id).strip(), safe="")
        safe_short_name = quote(str(short_name).strip(), safe="")
        safe_parent_game_id = quote(str(parent_game_id).strip(), safe="")
        route = (
            f"#?cmsId={safe_cms_id}"
            "&launchSource=External"
            f"&shortName={safe_short_name or GFN_DEFAULT_SHORT_NAME}"
            f"&parentGameId={safe_parent_game_id}"
        )
        return (
            "flatpak run --command=/app/cef/GeForceNOW "
            f"{self.flatpak_app_id} '--url-route={route}'"
        )
