from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import quote
import webbrowser
try:
    import requests
except ModuleNotFoundError:
    requests = None  # type: ignore[assignment]
try:
    from PIL import Image, ImageOps
except ModuleNotFoundError:
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]


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
    def publisher(self) -> str:
        return str(self.data.get("publisher") or "")

    @property
    def genres(self) -> list[str]:
        genres = self.data.get("genres")
        if not isinstance(genres, list):
            return []
        return [str(item).strip() for item in genres if str(item).strip()]

    @property
    def platforms(self) -> list[str]:
        platforms = self.data.get("platforms")
        if not isinstance(platforms, list):
            return []
        return [str(item).strip() for item in platforms if str(item).strip()]

    @property
    def categories(self) -> list[str]:
        categories = self.data.get("categories")
        if not isinstance(categories, list):
            return []
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in categories:
            text = " ".join(str(item).split()).strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text)
        return cleaned

    @property
    def release_date(self) -> str:
        return str(self.data.get("release_date") or "")

    @property
    def summary(self) -> str:
        return str(self.data.get("summary") or "")

    @property
    def website(self) -> str:
        return str(self.data.get("website") or "")

    @property
    def metacritic_score(self) -> int | None:
        value = self.data.get("metacritic_score")
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @property
    def igdb_rating(self) -> float | None:
        value = self.data.get("igdb_rating")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @property
    def igdb_url(self) -> str:
        return str(self.data.get("igdb_url") or "")

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
    def playtime_minutes(self) -> int | None:
        value = self.data.get("playtime_minutes")
        if value is None:
            return None
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            return None
        if minutes < 0:
            return None
        return minutes

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
        self.cover_cache_dir = data_root / "cache" / "remote-covers"
        self.cover_thumb_cache_dir = data_root / "cache" / "remote-cover-thumbs"
        self.games_dir.mkdir(parents=True, exist_ok=True)
        self.covers_dir.mkdir(parents=True, exist_ok=True)
        self.cover_cache_dir.mkdir(parents=True, exist_ok=True)
        self.cover_thumb_cache_dir.mkdir(parents=True, exist_ok=True)
        self.config_root.mkdir(parents=True, exist_ok=True)

    def _normalize_game_data(self, data: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(data)

        def _clean_text(value: Any) -> str | None:
            text = str(value).strip() if value is not None else ""
            return text or None

        def _clean_list(value: Any) -> list[str]:
            if not isinstance(value, list):
                return []
            cleaned: list[str] = []
            for item in value:
                text = str(item).strip()
                if text and text not in cleaned:
                    cleaned.append(text)
            return cleaned

        normalized["game_id"] = str(normalized.get("game_id", "")).strip()
        normalized["name"] = str(normalized.get("name", "")).strip()
        normalized["source"] = str(normalized.get("source", "")).strip()
        normalized["developer"] = _clean_text(normalized.get("developer"))
        normalized["publisher"] = _clean_text(normalized.get("publisher"))
        normalized["release_date"] = _clean_text(normalized.get("release_date"))
        normalized["summary"] = _clean_text(normalized.get("summary"))
        normalized["website"] = _clean_text(normalized.get("website"))
        metacritic = normalized.get("metacritic_score")
        try:
            normalized["metacritic_score"] = int(metacritic) if metacritic is not None else None
        except (TypeError, ValueError):
            normalized["metacritic_score"] = None
        igdb_rating = normalized.get("igdb_rating")
        try:
            normalized["igdb_rating"] = float(igdb_rating) if igdb_rating is not None else None
        except (TypeError, ValueError):
            normalized["igdb_rating"] = None
        normalized["igdb_url"] = _clean_text(normalized.get("igdb_url"))
        normalized["genres"] = _clean_list(normalized.get("genres"))
        normalized["platforms"] = _clean_list(normalized.get("platforms"))
        categories = normalized.get("categories")
        if isinstance(categories, list):
            normalized_categories: list[str] = []
            seen: set[str] = set()
            for item in categories:
                text = " ".join(str(item).split()).strip()
                if not text:
                    continue
                key = text.casefold()
                if key in seen:
                    continue
                seen.add(key)
                normalized_categories.append(text)
            normalized["categories"] = normalized_categories
        else:
            normalized["categories"] = []
        playtime_minutes = normalized.get("playtime_minutes")
        try:
            normalized["playtime_minutes"] = (
                max(0, int(playtime_minutes))
                if playtime_minutes is not None
                else None
            )
        except (TypeError, ValueError):
            normalized["playtime_minutes"] = None
        return normalized

    def _atomic_write_json(self, path: Path, data: dict[str, Any]) -> None:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f"{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as open_file:
            json.dump(data, open_file, sort_keys=True)
            open_file.flush()
            os.fsync(open_file.fileno())
            tmp_path = Path(open_file.name)
        os.replace(tmp_path, path)

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
            data = self._normalize_game_data(data)
            if not include_removed and data.get("removed"):
                continue
            if not include_blacklisted and data.get("blacklisted"):
                continue
            games.append(GameEntry(path=path, data=data))

        games.sort(key=lambda game: game.name.lower())
        return games

    def save_game(self, game: GameEntry) -> None:
        game.data = self._normalize_game_data(game.data)
        self._atomic_write_json(game.path, game.data)

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

    @staticmethod
    def _remote_image_suffix(
        url: str,
        *,
        mime: str = "",
        animated: bool = False,
        content_type: str = "",
    ) -> str:
        lowered = url.lower().split("?", 1)[0]
        mime = mime.lower()
        content_type = content_type.lower().split(";", 1)[0].strip()
        detected = f"{mime} {content_type}".strip()
        if lowered.endswith(".gif") or "gif" in detected:
            return ".gif"
        if lowered.endswith(".webp") or "webp" in detected:
            return ".webp"
        if lowered.endswith(".jpg") or lowered.endswith(".jpeg") or "jpeg" in detected or "jpg" in detected:
            return ".jpg"
        if lowered.endswith(".png") or "png" in detected:
            return ".png"
        if lowered.endswith(".tiff") or lowered.endswith(".tif") or "tiff" in detected:
            return ".tiff"
        return ".gif" if animated else ".png"

    def cached_remote_cover_path(
        self,
        url: str,
        *,
        mime: str = "",
        animated: bool = False,
        timeout: int = 10,
    ) -> Path | None:
        if requests is None:
            return None
        normalized = str(url).strip()
        if not normalized:
            return None

        cache_key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        for suffix in (".gif", ".png", ".jpg", ".jpeg", ".webp", ".tiff"):
            cached = self.cover_cache_dir / f"{cache_key}{suffix}"
            if cached.is_file():
                return cached

        try:
            response = requests.get(
                normalized,
                timeout=timeout,
                headers={"User-Agent": "Klay/1.0 (+https://github.com/CartridgesApp/Klay)"},
            )
            response.raise_for_status()
            payload = response.content
            content_type = str(response.headers.get("Content-Type") or "")
        except Exception:
            return None

        suffix = self._remote_image_suffix(
            normalized,
            mime=mime,
            animated=animated,
            content_type=content_type,
        )
        target = self.cover_cache_dir / f"{cache_key}{suffix}"
        try:
            with tempfile.NamedTemporaryFile(
                "wb",
                dir=str(self.cover_cache_dir),
                prefix=f"{cache_key}.",
                suffix=".tmp",
                delete=False,
            ) as open_file:
                open_file.write(payload)
                open_file.flush()
                os.fsync(open_file.fileno())
                tmp_path = Path(open_file.name)
            os.replace(tmp_path, target)
        except OSError:
            tmp_path = locals().get("tmp_path")
            if isinstance(tmp_path, Path):
                tmp_path.unlink(missing_ok=True)
            return None

        return target

    def cached_remote_thumbnail_path(
        self,
        *,
        url: str,
        mime: str = "",
        animated: bool = False,
        width: int = 170,
        height: int = 255,
        timeout: int = 8,
    ) -> Path | None:
        source = self.cached_remote_cover_path(
            url,
            mime=mime,
            animated=animated,
            timeout=timeout,
        )
        if source is None:
            return None

        width = max(32, int(width))
        height = max(32, int(height))
        key_source = f"{str(url).strip()}|{width}x{height}|v2"
        cache_key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()
        target = self.cover_thumb_cache_dir / f"{cache_key}.png"
        if target.is_file():
            return target

        if Image is None or ImageOps is None:
            return source

        lz = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        tmp_path: Path | None = None
        try:
            with Image.open(source) as image:
                if getattr(image, "is_animated", False):
                    image.seek(0)
                frame = image.convert("RGB")
                thumb = ImageOps.fit(frame, (width, height), method=lz)
                with tempfile.NamedTemporaryFile(
                    "wb",
                    dir=str(self.cover_thumb_cache_dir),
                    prefix=f"{cache_key}.",
                    suffix=".tmp",
                    delete=False,
                ) as open_file:
                    thumb.save(open_file, format="PNG", optimize=True)
                    open_file.flush()
                    os.fsync(open_file.fileno())
                    tmp_path = Path(open_file.name)
                os.replace(tmp_path, target)
        except Exception:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            return source
        return target

    @staticmethod
    def _search_name_variants(name: str) -> list[str]:
        base = " ".join(name.split()).strip()
        if not base:
            return []
        variants: list[str] = [base]

        normalized = re.sub(r"[®™©]", "", base).strip()
        if normalized and normalized not in variants:
            variants.append(normalized)

        apostrophe_stripped = normalized.replace("'", "").strip()
        if apostrophe_stripped and apostrophe_stripped not in variants:
            variants.append(apostrophe_stripped)

        romanized = re.sub(r"\b3\b", "III", normalized, flags=re.IGNORECASE).strip()
        if romanized and romanized not in variants:
            variants.append(romanized)

        arabic = re.sub(r"\bIII\b", "3", normalized, flags=re.IGNORECASE).strip()
        if arabic and arabic not in variants:
            variants.append(arabic)

        return variants

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
            "playtime_minutes": None,
            "name": name,
            "publisher": None,
            "genres": [],
            "platforms": [],
            "categories": [],
            "release_date": None,
            "summary": None,
            "website": None,
            "metacritic_score": None,
            "igdb_rating": None,
            "igdb_url": None,
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
        data = self._normalize_game_data(data)
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

    def search_sgdb_cover_options(
        self,
        *,
        game_name: str,
        api_key: str,
        animated: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if requests is None or not game_name.strip() or not api_key.strip():
            return []

        headers = {"Authorization": f"Bearer {api_key.strip()}"}
        base_url = "https://www.steamgriddb.com/api/v2"
        options: list[dict[str, Any]] = []

        target_limit = max(1, int(limit))
        variants = self._search_name_variants(game_name)
        normalized_target = game_name.strip().lower()
        candidate_rank: dict[str, tuple[int, int]] = {}
        noisy_terms = (
            "toolkit",
            "mod manager",
            "editor",
            "beta",
            "demo",
            "soundtrack",
            "artbook",
            "benchmark",
            "public test server",
            "test server",
            " pts",
        )

        def _score_name(query: str, row_name: str) -> int:
            query_norm = query.strip().lower()
            row_norm = row_name.strip().lower()
            if not query_norm or not row_norm:
                return 6
            if row_norm == query_norm:
                return 0
            if row_norm.startswith(query_norm):
                return 1
            if query_norm in row_norm:
                return 2
            row_alt = row_norm.replace("iii", "3").replace(" ii", " 2").replace(" iv", " 4")
            query_alt = query_norm.replace("iii", "3").replace(" ii", " 2").replace(" iv", " 4")
            if row_alt == query_alt:
                return 1
            if row_alt.startswith(query_alt):
                return 2
            if query_alt in row_alt:
                return 3
            base_score = 5
            penalty = 0
            for term in noisy_terms:
                if term in row_norm and term not in query_norm:
                    penalty += 2
            return base_score + penalty

        for variant_index, variant in enumerate(variants):
            try:
                search = requests.get(
                    f"{base_url}/search/autocomplete/{quote(variant)}",
                    headers=headers,
                    timeout=6,
                )
                search.raise_for_status()
                payload = search.json()
                data = payload.get("data", []) if isinstance(payload, dict) else []
            except Exception:
                continue

            for row in data[:20]:
                if not isinstance(row, dict):
                    continue
                game_id = str(row.get("id") or "").strip()
                if not game_id:
                    continue
                row_name = str(row.get("name") or "").strip().lower()
                score = min(
                    _score_name(variant, row_name),
                    _score_name(normalized_target, row_name),
                )
                previous = candidate_rank.get(game_id)
                if previous is None or (score, variant_index) < previous:
                    candidate_rank[game_id] = (score, variant_index)

        ranked_candidates = sorted(
            candidate_rank.items(),
            key=lambda pair: (pair[1][0], pair[1][1], pair[0]),
        )
        candidate_ids = [game_id for game_id, _rank in ranked_candidates][:8]
        candidate_scores = {game_id: rank[0] for game_id, rank in ranked_candidates}

        if not candidate_ids:
            return []

        seen_urls: set[str] = set()
        animated_count = 0

        def _fetch_grid_page(game_id: str, type_param: str, page: int) -> list[dict[str, Any]]:
            query = (
                f"{base_url}/grids/game/{game_id}"
                "?"
                + f"types={type_param}"
                + f"&page={page}"
            )
            try:
                grids = requests.get(query, headers=headers, timeout=6)
                grids.raise_for_status()
                payload = grids.json()
                return payload.get("data", []) if isinstance(payload, dict) else []
            except Exception:
                return []

        def _is_animated_candidate(image: dict[str, Any], *, strong_only: bool) -> bool:
            url = str(image.get("url", "")).strip().lower().split("?", 1)[0]
            mime = str(image.get("mime", "")).strip().lower()
            image_type = str(image.get("type", "")).strip().lower()
            notes = str(image.get("notes", "")).strip().lower()
            if image_type == "animated" or mime.startswith("video/") or url.endswith((".gif", ".mp4", ".webm")):
                return True
            if "animat" in notes:
                return True
            if strong_only:
                return False
            # SGDB often surfaces animated grids as webp in mixed queries.
            return mime == "image/webp" or url.endswith(".webp")

        def _append_images(
            images: list[dict[str, Any]],
            *,
            force_animated: bool = False,
            animated_fallback: bool = False,
            type_value: str = "",
            allow_landscape: bool = False,
        ) -> int:
            nonlocal animated_count
            appended = 0
            for image in images:
                url = str(image.get("url", "")).strip()
                if not url or url in seen_urls:
                    continue
                animated_flag = force_animated or _is_animated_candidate(
                    image,
                    strong_only=not animated_fallback,
                )
                if animated_fallback and not animated_flag:
                    continue

                width = str(image.get("width", "")).strip()
                height = str(image.get("height", "")).strip()
                try:
                    width_i = int(width) if width else 0
                    height_i = int(height) if height else 0
                except (TypeError, ValueError):
                    width_i, height_i = 0, 0

                # Keep cover-like art only (portrait orientation).
                is_landscape = width_i > 0 and height_i > 0 and width_i >= height_i
                if is_landscape and not allow_landscape:
                    continue

                seen_urls.add(url)
                style = str(image.get("style", "")).strip()
                mime = str(image.get("mime", "")).strip().lower()
                image_type = str(image.get("type", "")).strip().lower()
                label_parts = [
                    part
                    for part in [
                        "Animated" if animated_flag else "Static",
                        "Wide" if is_landscape else "",
                        f"{width}x{height}" if width and height else "",
                        style or image_type or type_value,
                        mime,
                    ]
                    if part
                ]
                label = " | ".join(label_parts) if label_parts else "Cover option"
                options.append(
                    {
                        "url": url,
                        "label": label,
                        "mime": mime,
                        "type": image_type or type_value,
                        "animated": animated_flag,
                        "provider": "sgdb",
                    }
                )
                if animated_flag:
                    animated_count += 1
                appended += 1
                if len(options) >= target_limit:
                    break
            return appended

        animated_candidate_ids = [
            game_id for game_id in candidate_ids if candidate_scores.get(game_id, 9) <= 3
        ][:4]
        if not animated_candidate_ids:
            animated_candidate_ids = candidate_ids[:3]

        if animated:
            for game_id in animated_candidate_ids:
                if len(options) >= target_limit:
                    break
                for page in range(1, 3):
                    if len(options) >= target_limit:
                        break
                    images = _fetch_grid_page(game_id, "animated", page)
                    if not images:
                        break
                    _append_images(images, force_animated=True, type_value="animated")

                # Fallback: mixed query catches SGDB webp animation entries.
                mixed_added = 0
                for page in range(1, 3):
                    if len(options) >= target_limit:
                        break
                    images = _fetch_grid_page(game_id, "animated,static", page)
                    if not images:
                        break
                    added = _append_images(
                        images,
                        animated_fallback=True,
                        type_value="animated",
                    )
                    mixed_added += added
                    if mixed_added > 0:
                        break

            # Last resort: include wide animated assets when no portrait animation exists.
            if animated_count == 0 and len(options) < target_limit:
                for game_id in animated_candidate_ids:
                    if len(options) >= target_limit:
                        break
                    images = _fetch_grid_page(game_id, "animated,static", 1)
                    if not images:
                        continue
                    _append_images(
                        images,
                        animated_fallback=True,
                        type_value="animated",
                        allow_landscape=True,
                    )
                    if animated_count >= 6:
                        break

        for game_id in candidate_ids:
            if len(options) >= target_limit:
                break
            static_added = 0
            per_candidate_cap = max(10, min(20, target_limit // 2))
            for page in range(1, 4):
                if len(options) >= target_limit:
                    break
                images = _fetch_grid_page(game_id, "static", page)
                if not images:
                    break
                static_added += _append_images(images, type_value="static")
                if static_added >= per_candidate_cap:
                    break

            # Fast path: if the best/near-best match already yields enough covers,
            # avoid searching weaker matches that usually add noisy results.
            if (
                len(options) >= min(30, target_limit)
                and candidate_scores.get(game_id, 9) <= 1
            ):
                break
        return options

    def _igdb_access_token(
        self,
        *,
        client_id: str,
        access_token: str,
        client_secret: str,
    ) -> str:
        token = access_token.strip()
        if token:
            return token
        if requests is None:
            return ""
        client_id = client_id.strip()
        client_secret = client_secret.strip()
        if not client_id or not client_secret:
            return ""
        try:
            response = requests.post(
                "https://id.twitch.tv/oauth2/token",
                params={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=7,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return ""
        return str(payload.get("access_token") or "").strip()

    def search_igdb_cover_options(
        self,
        *,
        game_name: str,
        client_id: str,
        access_token: str = "",
        client_secret: str = "",
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        if requests is None:
            return []
        client_id = client_id.strip()
        if not client_id or not game_name.strip():
            return []

        token = self._igdb_access_token(
            client_id=client_id,
            access_token=access_token,
            client_secret=client_secret,
        )
        if not token:
            return []

        headers = {
            "Client-ID": client_id,
            "Authorization": f"Bearer {token}",
        }
        options: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        target_limit = max(1, int(limit))
        for variant in self._search_name_variants(game_name):
            if len(options) >= target_limit:
                break
            safe_name = variant.replace('"', "").strip()
            query = (
                f'search "{safe_name}"; '
                "fields name,cover.image_id,cover.width,cover.height,cover.animated,url; "
                f"limit {max(6, min(18, target_limit))};"
            )

            try:
                response = requests.post(
                    "https://api.igdb.com/v4/games",
                    data=query.encode("utf-8"),
                    headers=headers,
                    timeout=7,
                )
                if response.status_code in {401, 403} and client_secret.strip():
                    token = self._igdb_access_token(
                        client_id=client_id,
                        access_token="",
                        client_secret=client_secret,
                    )
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                        response = requests.post(
                            "https://api.igdb.com/v4/games",
                            data=query.encode("utf-8"),
                            headers=headers,
                            timeout=7,
                        )
                response.raise_for_status()
                payload = response.json()
            except Exception:
                continue

            if not isinstance(payload, list):
                continue

            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                cover = entry.get("cover")
                if not isinstance(cover, dict):
                    continue
                image_id = str(cover.get("image_id") or "").strip()
                if not image_id:
                    continue
                animated = bool(cover.get("animated"))
                ext = "gif" if animated else "jpg"
                url = f"https://images.igdb.com/igdb/image/upload/t_1080p/{image_id}.{ext}"
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                width = str(cover.get("width", "")).strip()
                height = str(cover.get("height", "")).strip()
                mime = "image/gif" if animated else "image/jpeg"
                game_title = str(entry.get("name") or "").strip()
                label_parts = [
                    "IGDB",
                    "Animated" if animated else "Static",
                    f"{width}x{height}" if width and height else "",
                    game_title if game_title else "",
                    mime,
                ]
                label = " | ".join(part for part in label_parts if part)
                options.append(
                    {
                        "url": url,
                        "label": label,
                        "mime": mime,
                        "type": "animated" if animated else "static",
                        "animated": animated,
                        "provider": "igdb",
                    }
                )
                if len(options) >= target_limit:
                    break
        return options

    @staticmethod
    def _igdb_to_metadata(payload: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}

        summary = str(payload.get("summary") or "").strip()
        storyline = str(payload.get("storyline") or "").strip()
        if summary or storyline:
            result["summary"] = summary or storyline

        url = str(payload.get("url") or "").strip()
        if url:
            result["igdb_url"] = url

        website = ""
        websites = payload.get("websites")
        if isinstance(websites, list):
            for item in websites:
                if not isinstance(item, dict):
                    continue
                website = str(item.get("url") or "").strip()
                if website:
                    break
        if website:
            result["website"] = website

        rating = payload.get("rating")
        if rating is not None:
            try:
                result["igdb_rating"] = round(float(rating), 1)
            except (TypeError, ValueError):
                pass

        release_epoch = payload.get("first_release_date")
        if release_epoch is not None:
            try:
                release_dt = datetime.fromtimestamp(int(release_epoch), tz=timezone.utc)
                result["release_date"] = release_dt.strftime("%b %d, %Y")
            except (TypeError, ValueError, OSError):
                pass

        genres: list[str] = []
        for item in payload.get("genres") or []:
            if isinstance(item, dict):
                text = str(item.get("name") or "").strip()
                if text and text not in genres:
                    genres.append(text)
        if genres:
            result["genres"] = genres

        platforms: list[str] = []
        for item in payload.get("platforms") or []:
            if isinstance(item, dict):
                text = str(item.get("name") or "").strip()
                if text and text not in platforms:
                    platforms.append(text)
        if platforms:
            result["platforms"] = platforms

        developers: list[str] = []
        publishers: list[str] = []
        for item in payload.get("involved_companies") or []:
            if not isinstance(item, dict):
                continue
            company = item.get("company") or {}
            if not isinstance(company, dict):
                continue
            name = str(company.get("name") or "").strip()
            if not name:
                continue
            if bool(item.get("developer")) and name not in developers:
                developers.append(name)
            if bool(item.get("publisher")) and name not in publishers:
                publishers.append(name)
        if developers:
            result["developer"] = ", ".join(developers)
        if publishers:
            result["publisher"] = ", ".join(publishers)

        return result

    def search_igdb_metadata(
        self,
        *,
        game_name: str,
        client_id: str,
        access_token: str = "",
        client_secret: str = "",
    ) -> dict[str, Any] | None:
        if requests is None:
            return None
        client_id = client_id.strip()
        if not client_id or not game_name.strip():
            return None

        token = self._igdb_access_token(
            client_id=client_id,
            access_token=access_token,
            client_secret=client_secret,
        )
        if not token:
            return None

        headers = {
            "Client-ID": client_id,
            "Authorization": f"Bearer {token}",
        }
        fields = (
            "name,summary,storyline,url,rating,first_release_date,"
            "genres.name,platforms.name,involved_companies.developer,"
            "involved_companies.publisher,involved_companies.company.name,websites.url"
        )
        for variant in self._search_name_variants(game_name):
            safe_name = variant.replace('"', "").strip()
            if not safe_name:
                continue
            query = f'search "{safe_name}"; fields {fields}; limit 5;'
            try:
                response = requests.post(
                    "https://api.igdb.com/v4/games",
                    data=query.encode("utf-8"),
                    headers=headers,
                    timeout=7,
                )
                if response.status_code in {401, 403} and client_secret.strip():
                    token = self._igdb_access_token(
                        client_id=client_id,
                        access_token="",
                        client_secret=client_secret,
                    )
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                        response = requests.post(
                            "https://api.igdb.com/v4/games",
                            data=query.encode("utf-8"),
                            headers=headers,
                            timeout=7,
                        )
                response.raise_for_status()
                payload = response.json()
            except Exception:
                continue
            if not isinstance(payload, list) or not payload:
                continue
            best_match: dict[str, Any] | None = None
            best_score = 99
            normalized_variant = safe_name.lower()
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                entry_name = str(entry.get("name") or "").strip().lower()
                if not entry_name:
                    score = 9
                elif entry_name == normalized_variant:
                    score = 0
                elif entry_name.startswith(normalized_variant):
                    score = 1
                elif normalized_variant in entry_name:
                    score = 2
                else:
                    score = 4
                if score < best_score:
                    best_score = score
                    best_match = entry
            if best_match:
                metadata = self._igdb_to_metadata(best_match)
                if metadata:
                    return metadata
        return None

    def set_cover_from_url(
        self,
        game: GameEntry,
        url: str,
        *,
        mime: str = "",
        animated: bool = False,
    ) -> bool:
        cached = self.cached_remote_cover_path(
            url,
            mime=mime,
            animated=animated,
            timeout=10,
        )
        if cached is None:
            return False
        try:
            self.set_cover(game, cached)
        except OSError:
            return False
        return True
