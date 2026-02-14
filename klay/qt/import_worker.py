from __future__ import annotations

import importlib
import builtins
import json
import os
import re
import shlex
import sys
import tempfile
import types
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any, Callable
from urllib.parse import quote

try:
    import requests
except ModuleNotFoundError:
    requests = None  # type: ignore[assignment]

try:
    from PIL import Image, ImageFilter, ImageOps, ImageSequence
except ModuleNotFoundError:
    Image = None  # type: ignore[assignment]
    ImageFilter = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]
    ImageSequence = None  # type: ignore[assignment]

from klay.qt.settings import SettingsBackend


COVER_SIZE = (200, 300)
LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS") if Image else None
REQUEST_TIMEOUT_SECONDS = 4
DOWNLOAD_TIMEOUT_SECONDS = 8
SGDB_MAX_CONSECUTIVE_NETWORK_ERRORS = 4


def _emit_progress(**payload: Any) -> None:
    payload["kind"] = "progress"
    print(json.dumps(payload), flush=True)


def _ensure_translation_stub() -> None:
    # Importer/source modules use _() at module import time.
    if not hasattr(builtins, "_"):
        setattr(builtins, "_", lambda message, *_args, **_kwargs: message)


@dataclass
class ImportSummary:
    scanned: int = 0
    imported: int = 0
    removed: int = 0
    duplicates: int = 0
    metadata_updates: int = 0
    sources_scanned: int = 0
    cover_updates: int = 0
    new_cover_updates: int = 0
    errors: list[str] | None = None
    imported_ids: list[str] | None = None
    removed_ids: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "imported": self.imported,
            "removed": self.removed,
            "duplicates": self.duplicates,
            "metadata_updates": self.metadata_updates,
            "sources_scanned": self.sources_scanned,
            "cover_updates": self.cover_updates,
            "new_cover_updates": self.new_cover_updates,
            "errors": self.errors or [],
            "imported_ids": self.imported_ids or [],
            "removed_ids": self.removed_ids or [],
        }


class FakeGame:
    def __init__(self, data: dict[str, Any]) -> None:
        defaults = {
            "added": int(time()),
            "blacklisted": False,
            "developer": None,
            "executable": "",
            "game_id": "",
            "hidden": False,
            "last_played": 0,
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


def _clear_modules() -> None:
    for module_name in list(sys.modules):
        if module_name.startswith("klay.importer.") or module_name == "klay.extensions":
            sys.modules.pop(module_name, None)


def _load_source_classes(errors: list[str]) -> list[type]:
    saved_game_module = sys.modules.get("klay.game")
    fake_game_module = types.ModuleType("klay.game")
    fake_game_module.Game = FakeGame
    sys.modules["klay.game"] = fake_game_module

    _clear_modules()

    source_classes: list[type] = []
    try:
        modules_to_classes = (
            ("steam_source", "SteamSource"),
            ("lutris_source", "LutrisSource"),
            ("heroic_source", "HeroicSource"),
            ("bottles_source", "BottlesSource"),
            ("flatpak_source", "FlatpakSource"),
            ("desktop_source", "DesktopSource"),
            ("itch_source", "ItchSource"),
            ("legendary_source", "LegendarySource"),
            ("retroarch_source", "RetroarchSource"),
        )
        for module_name, class_name in modules_to_classes:
            module = importlib.import_module(f"klay.importer.{module_name}")
            source_classes.append(getattr(module, class_name))

        extensions = importlib.import_module("klay.extensions")
        source_classes.extend(
            extensions.collect_source_classes(extensions.load_extension_modules())
        )
    except Exception as error:  # pylint: disable=broad-exception-caught
        errors.append(f"Source load error: {type(error).__name__}: {error}")
    finally:
        if saved_game_module is not None:
            sys.modules["klay.game"] = saved_game_module
        else:
            sys.modules.pop("klay.game", None)

    return source_classes


def _compose_cover(image: Image.Image, icon_mode: bool = False) -> Image.Image:
    if ImageOps is None or ImageFilter is None or LANCZOS is None:
        raise RuntimeError("Pillow is required for cover processing.")

    image = image.convert("RGBA")
    target_w, target_h = COVER_SIZE
    aspect = image.width / max(image.height, 1)
    target_aspect = target_w / target_h

    if icon_mode or aspect > target_aspect * 1.12:
        background = ImageOps.fit(
            image.convert("RGB"),
            COVER_SIZE,
            method=LANCZOS,
        ).filter(ImageFilter.GaussianBlur(20))

        scale = 0.68 if icon_mode else 0.92
        foreground = ImageOps.contain(
            image,
            (int(target_w * scale), int(target_h * scale)),
            method=LANCZOS,
        )

        x = (target_w - foreground.width) // 2
        y = (target_h - foreground.height) // 2
        background.paste(foreground, (x, y), foreground)
        return background.convert("RGB")

    return ImageOps.fit(image.convert("RGB"), COVER_SIZE, method=LANCZOS).convert("RGB")


def _cover_base(covers_dir: Path, game_id: str) -> Path:
    covers_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("gif", "tiff", "png", "jpg", "jpeg", "webp"):
        (covers_dir / f"{game_id}.{suffix}").unlink(missing_ok=True)
    return covers_dir / game_id


def _save_cover_from_path(
    *,
    cover_path: Path,
    covers_dir: Path,
    game_id: str,
    icon_mode: bool = False,
) -> bool:
    def _save_with_pillow(path: Path) -> bool:
        if Image is None or ImageSequence is None:
            return False

        try:
            with Image.open(path) as image:
                target = _cover_base(covers_dir, game_id)
                if getattr(image, "is_animated", False):
                    frames: list[Image.Image] = []
                    durations: list[int] = []
                    for frame in ImageSequence.Iterator(image):
                        frames.append(_compose_cover(frame.convert("RGBA"), icon_mode=False))
                        durations.append(int(frame.info.get("duration", 100)))
                    if not frames:
                        return False
                    frames[0].save(
                        target.with_suffix(".gif"),
                        save_all=True,
                        append_images=frames[1:],
                        duration=durations,
                        loop=0,
                    )
                    return True

                still = _compose_cover(image, icon_mode=icon_mode)
                still.save(target.with_suffix(".png"), format="PNG", optimize=True)
                return True
        except Exception:  # pylint: disable=broad-exception-caught
            return False

    if Image is None or ImageSequence is None:
        # Keep behavior predictable: no processing pipeline available.
        return False

    if not cover_path.is_file():
        return False
    if _save_with_pillow(cover_path):
        return True

    # Fallback path: desktop/flatpak launchers often provide SVG icons that Pillow
    # cannot decode directly. Load with GdkPixbuf and then process via Pillow.
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except Exception:  # pylint: disable=broad-exception-caught
        return False

    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(cover_path))
    except Exception:  # pylint: disable=broad-exception-caught
        return False

    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as open_file:
        tmp_path = Path(open_file.name)
    try:
        pixbuf.savev(str(tmp_path), "png", [], [])
        return _save_with_pillow(tmp_path)
    except Exception:  # pylint: disable=broad-exception-caught
        return False
    finally:
        tmp_path.unlink(missing_ok=True)


def _download_to_temp(url: str) -> Path | None:
    if requests is None:
        return None

    try:
        response = requests.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        response.raise_for_status()
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    suffix = Path(url).suffix or ".img"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as open_file:
        open_file.write(response.content)
        return Path(open_file.name)


def _sgdb_cover_url(name: str, api_key: str, animated: bool) -> str | None:
    if requests is None:
        return None

    headers = {"Authorization": f"Bearer {api_key}"}
    base_url = "https://www.steamgriddb.com/api/v2"

    search = requests.get(
        f"{base_url}/search/autocomplete/{quote(name)}",
        headers=headers,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if search.status_code != 200:
        return None
    data = search.json().get("data", [])
    if not data:
        return None
    game_id = data[0]["id"]

    query_sets = [animated, False] if animated else [False]
    for allow_animated in query_sets:
        query = f"{base_url}/grids/game/{game_id}?dimensions=600x900"
        if allow_animated:
            query += "&types=animated"
        grids = requests.get(query, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        if grids.status_code != 200:
            continue
        images = grids.json().get("data", [])
        if not images:
            continue
        return images[0]["url"]

    return None


def _game_to_data(game: Any) -> dict[str, Any]:
    data = {
        "added": int(getattr(game, "added", int(time()))),
        "blacklisted": bool(getattr(game, "blacklisted", False)),
        "developer": getattr(game, "developer", None),
        "executable": getattr(game, "executable", ""),
        "game_id": str(getattr(game, "game_id", "")),
        "hidden": bool(getattr(game, "hidden", False)),
        "last_played": int(getattr(game, "last_played", 0)),
        "name": str(getattr(game, "name", "")),
        "removed": bool(getattr(game, "removed", False)),
        "source": str(getattr(game, "source", "")),
        "version": float(getattr(game, "version", 1.5)),
    }
    if data["developer"] == "":
        data["developer"] = None
    return data


def _load_existing_games(games_dir: Path) -> dict[str, dict[str, Any]]:
    games: dict[str, dict[str, Any]] = {}
    if not games_dir.is_dir():
        return games
    for game_file in games_dir.glob("*.json"):
        try:
            data = json.loads(game_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        game_id = str(data.get("game_id") or game_file.stem)
        data["game_id"] = game_id
        games[game_id] = data
    return games


def _source_enabled(settings: SettingsBackend, source_id: str) -> bool:
    return settings.source_enabled(source_id)


def _write_game(games_dir: Path, data: dict[str, Any]) -> None:
    games_dir.mkdir(parents=True, exist_ok=True)
    game_path = games_dir / f"{data['game_id']}.json"
    game_path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")


def _has_cover(covers_dir: Path, game_id: str) -> bool:
    # TIFF is intentionally ignored for detection because Qt deployments may
    # not ship TIFF image plugins; PNG/GIF/JPEG/WebP are used for UI reliability.
    for suffix in ("gif", "png", "jpg", "jpeg", "webp"):
        if (covers_dir / f"{game_id}.{suffix}").is_file():
            return True
    return False


def _steam_appid_from_game_data(game_data: dict[str, Any]) -> str | None:
    source = str(game_data.get("source", ""))
    if source.split("_")[0] != "steam":
        return None
    game_id = str(game_data.get("game_id", ""))
    if not game_id.startswith("steam_"):
        return None
    appid = game_id.split("_", 1)[1]
    return appid or None


def _attempt_cover_update(
    *,
    game_id: str,
    game_name: str,
    blacklisted: bool,
    covers_dir: Path,
    additional_data: dict[str, Any],
    settings: SettingsBackend,
    sgdb_cache: dict[tuple[str, bool], str | None],
    sgdb_state: dict[str, int | bool],
    errors: list[str],
) -> tuple[bool, bool]:
    had_cover_before = _has_cover(covers_dir, game_id)
    cover_updated = False

    if path := additional_data.get("local_image_path"):
        cover_updated = _save_cover_from_path(
            cover_path=Path(path),
            covers_dir=covers_dir,
            game_id=game_id,
            icon_mode=False,
        )
    elif path := additional_data.get("local_icon_path"):
        cover_updated = _save_cover_from_path(
            cover_path=Path(path),
            covers_dir=covers_dir,
            game_id=game_id,
            icon_mode=True,
        )
    elif url := additional_data.get("online_cover_url"):
        if tmp_file := _download_to_temp(str(url)):
            try:
                cover_updated = _save_cover_from_path(
                    cover_path=tmp_file,
                    covers_dir=covers_dir,
                    game_id=game_id,
                    icon_mode=False,
                )
            finally:
                tmp_file.unlink(missing_ok=True)

    use_sgdb = settings.get_bool("sgdb", False)
    prefer_sgdb = settings.get_bool("sgdb-prefer", False)
    if (
        use_sgdb
        and not blacklisted
        and (prefer_sgdb or not _has_cover(covers_dir, game_id))
        and (sgdb_key := settings.get_string("sgdb-key", "").strip())
    ):
        sgdb_url = _resolve_sgdb_cover_url(
            game_name=game_name,
            api_key=sgdb_key,
            animated=settings.get_bool("sgdb-animated", False),
            cache=sgdb_cache,
            state=sgdb_state,
            errors=errors,
        )
        if sgdb_url and (tmp_file := _download_to_temp(sgdb_url)):
            try:
                cover_updated = _save_cover_from_path(
                    cover_path=tmp_file,
                    covers_dir=covers_dir,
                    game_id=game_id,
                    icon_mode=False,
                )
            finally:
                tmp_file.unlink(missing_ok=True)

    return cover_updated, had_cover_before


def _sgdb_name_variants(name: str) -> list[str]:
    base = " ".join(name.split()).strip()
    if not base:
        return []

    variants: list[str] = [base]

    stripped = re.sub(r"[®™©]", "", base).strip()
    if stripped and stripped not in variants:
        variants.append(stripped)

    de_punct = re.sub(r"[\\/:|]+", " ", stripped or base)
    de_punct = " ".join(de_punct.split()).strip()
    if de_punct and de_punct not in variants:
        variants.append(de_punct)

    no_suffix = re.sub(
        r"\b(remastered|definitive edition|special edition|game of the year edition|complete edition)\b",
        "",
        de_punct or stripped or base,
        flags=re.IGNORECASE,
    )
    no_suffix = " ".join(no_suffix.split()).strip(" -:")
    if no_suffix and no_suffix not in variants:
        variants.append(no_suffix)

    return variants


def _resolve_sgdb_cover_url(
    *,
    game_name: str,
    api_key: str,
    animated: bool,
    cache: dict[tuple[str, bool], str | None],
    state: dict[str, int | bool],
    errors: list[str],
) -> str | None:
    if state.get("disabled"):
        return None

    for variant in _sgdb_name_variants(game_name):
        cache_key = (variant.lower(), animated)
        if cache_key in cache:
            cached = cache[cache_key]
            if cached:
                return cached
            continue

        try:
            sgdb_url = _sgdb_cover_url(variant, api_key, animated)
        except Exception as error:  # pylint: disable=broad-exception-caught
            error_name = type(error).__name__
            if requests is not None and isinstance(error, requests.RequestException):
                failures = int(state.get("network_failures", 0)) + 1
                state["network_failures"] = failures
                if failures >= SGDB_MAX_CONSECUTIVE_NETWORK_ERRORS:
                    state["disabled"] = True
                    errors.append(
                        "SteamGridDB refresh stopped after repeated network failures."
                    )
            else:
                errors.append(f"SGDB lookup failed for '{game_name}': {error_name}: {error}")
            cache[cache_key] = None
            continue

        state["network_failures"] = 0
        cache[cache_key] = sgdb_url
        if sgdb_url:
            return sgdb_url

    return None


def _refresh_metadata_and_covers(
    *,
    summary: ImportSummary,
    settings: SettingsBackend,
    games_dir: Path,
    covers_dir: Path,
    existing_games: dict[str, dict[str, Any]],
    steam_api_helper: Any,
    emit_progress: Callable[..., None],
) -> None:
    errors = summary.errors or []
    sgdb_cache: dict[tuple[str, bool], str | None] = {}
    sgdb_state: dict[str, int | bool] = {"network_failures": 0, "disabled": False}

    if settings.get_bool("sgdb", False) and not settings.get_string("sgdb-key", "").strip():
        errors.append("SteamGridDB is enabled but no API key is set.")

    candidates: list[tuple[str, dict[str, Any]]] = [
        (game_id, game_data)
        for game_id, game_data in existing_games.items()
        if not game_data.get("removed", False)
    ]
    total = len(candidates)
    processed = 0
    emit_progress(
        mode="refresh_metadata",
        phase="start",
        processed=0,
        total=total,
        remaining=total,
        metadata_updates=0,
        cover_updates=0,
        errors=0,
    )

    for game_id, game_data in candidates:
        processed += 1
        skipped = False
        skip_reason = ""
        if game_data.get("blacklisted", False):
            skipped = True
            skip_reason = "blacklisted"

        summary.scanned += 1
        updated = False

        if (
            not skipped
            and steam_api_helper
            and (steam_appid := _steam_appid_from_game_data(game_data))
        ):
            try:
                online_data = steam_api_helper.get_api_data(appid=str(steam_appid))
            except Exception as error:  # pylint: disable=broad-exception-caught
                error_name = type(error).__name__
                if error_name in {"SteamNotAGameError", "SteamGameNotFoundError"}:
                    if not game_data.get("blacklisted", False):
                        game_data["blacklisted"] = True
                        updated = True
                        summary.metadata_updates += 1
                else:
                    errors.append(
                        f"Steam API failed for {game_id}: {type(error).__name__}: {error}"
                    )
            else:
                developer = online_data.get("developer")
                if developer and developer != game_data.get("developer"):
                    game_data["developer"] = developer
                    updated = True
                    summary.metadata_updates += 1

        cover_updated = False
        had_cover_before = _has_cover(covers_dir, game_id)
        if not skipped:
            cover_updated, had_cover_before = _attempt_cover_update(
                game_id=game_id,
                game_name=str(game_data.get("name", game_id)),
                blacklisted=bool(game_data.get("blacklisted", False)),
                covers_dir=covers_dir,
                additional_data={},
                settings=settings,
                sgdb_cache=sgdb_cache,
                sgdb_state=sgdb_state,
                errors=errors,
            )

        if cover_updated:
            summary.cover_updates += 1
            if not had_cover_before:
                summary.new_cover_updates += 1

        if updated:
            _write_game(games_dir, game_data)

        emit_progress(
            mode="refresh_metadata",
            phase="item",
            game_id=game_id,
            game_name=str(game_data.get("name", game_id)),
            processed=processed,
            total=total,
            remaining=max(0, total - processed),
            metadata_updates=summary.metadata_updates,
            cover_updates=summary.cover_updates,
            new_cover_updates=summary.new_cover_updates,
            errors=len(errors),
            skipped=skipped,
            skip_reason=skip_reason,
            cover_updated=cover_updated,
            metadata_updated=updated,
        )

    emit_progress(
        mode="refresh_metadata",
        phase="done",
        processed=processed,
        total=total,
        remaining=max(0, total - processed),
        metadata_updates=summary.metadata_updates,
        cover_updates=summary.cover_updates,
        new_cover_updates=summary.new_cover_updates,
        errors=len(errors),
    )


def _run_import() -> dict[str, Any]:
    summary = ImportSummary(errors=[], imported_ids=[], removed_ids=[])
    errors = summary.errors if summary.errors is not None else []
    imported_ids = summary.imported_ids if summary.imported_ids is not None else []
    removed_ids = summary.removed_ids if summary.removed_ids is not None else []
    import_mode = os.getenv("KLAY_IMPORT_MODE", "import").strip().lower()

    data_dir_name = os.getenv("KLAY_DATA_DIR_NAME", "klay")
    settings = SettingsBackend(data_dir_name)
    _ensure_translation_stub()

    try:
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        from klay import shared
    except Exception as error:  # pylint: disable=broad-exception-caught
        errors.append(f"Import backend unavailable: {type(error).__name__}: {error}")
        return summary.as_dict()

    class WinStub:
        @staticmethod
        def get_direction():
            return Gtk.TextDirection.LTR

    shared.win = WinStub()
    shared.import_time = int(time())
    shared.games_dir = shared.data_dir / data_dir_name / "games"
    shared.covers_dir = shared.data_dir / data_dir_name / "covers"
    shared.games_dir.mkdir(parents=True, exist_ok=True)
    shared.covers_dir.mkdir(parents=True, exist_ok=True)

    steam_api_helper = None
    try:
        from klay.utils.steam import SteamAPIHelper, SteamRateLimiter

        steam_api_helper = SteamAPIHelper(SteamRateLimiter())
    except Exception as error:  # pylint: disable=broad-exception-caught
        errors.append(f"Steam API helper unavailable: {type(error).__name__}: {error}")

    existing_games = _load_existing_games(shared.games_dir)

    if import_mode == "refresh_metadata":
        _refresh_metadata_and_covers(
            summary=summary,
            settings=settings,
            games_dir=shared.games_dir,
            covers_dir=shared.covers_dir,
            existing_games=existing_games,
            steam_api_helper=steam_api_helper,
            emit_progress=_emit_progress,
        )
        summary.errors = errors
        summary.imported_ids = imported_ids
        summary.removed_ids = removed_ids
        return summary.as_dict()

    source_classes = _load_source_classes(errors)
    sgdb_cache: dict[tuple[str, bool], str | None] = {}
    sgdb_state: dict[str, int | bool] = {"network_failures": 0, "disabled": False}
    seen_ids: set[str] = set()
    scanned_ids: set[str] = set()
    emit_start_done = False

    for source_class in source_classes:
        if not emit_start_done:
            _emit_progress(
                mode="import",
                phase="start",
                sources_total=len(source_classes),
                sources_scanned=0,
                scanned=0,
                imported=0,
                removed=0,
                cover_updates=0,
                errors=0,
            )
            emit_start_done = True

        source_id = getattr(source_class, "source_id", source_class.__name__).split("_")[0]
        if not _source_enabled(settings, source_id):
            continue

        try:
            source = source_class()
        except Exception as error:  # pylint: disable=broad-exception-caught
            errors.append(f"{source_class.__name__} init failed: {type(error).__name__}: {error}")
            continue

        if not getattr(source, "is_available", True):
            continue

        summary.sources_scanned += 1
        _emit_progress(
            mode="import",
            phase="source",
            source=source_id,
            sources_scanned=summary.sources_scanned,
            sources_total=len(source_classes),
            scanned=summary.scanned,
            imported=summary.imported,
            removed=summary.removed,
            cover_updates=summary.cover_updates,
            errors=len(errors),
        )
        try:
            source_iter = iter(source)
        except Exception as error:  # pylint: disable=broad-exception-caught
            errors.append(
                f"{source_class.__name__} iterator failed: {type(error).__name__}: {error}"
            )
            continue

        while True:
            try:
                iteration_result = next(source_iter)
            except StopIteration:
                break
            except Exception as error:  # pylint: disable=broad-exception-caught
                errors.append(
                    f"{source_class.__name__} scan error: {type(error).__name__}: {error}"
                )
                continue

            if iteration_result is None:
                continue
            if isinstance(iteration_result, tuple):
                game, additional_data = iteration_result
            else:
                game, additional_data = iteration_result, {}

            game_data = _game_to_data(game)
            game_id = game_data["game_id"]
            if not game_id:
                continue

            summary.scanned += 1
            seen_ids.add(game_id)

            if game_id in scanned_ids:
                summary.duplicates += 1
                continue
            scanned_ids.add(game_id)

            existing = existing_games.get(game_id)
            if existing and not existing.get("removed", False):
                summary.duplicates += 1
                metadata_updated = False

                steam_appid = additional_data.get("steam_appid")
                if steam_api_helper and steam_appid is not None:
                    try:
                        online_data = steam_api_helper.get_api_data(appid=str(steam_appid))
                    except Exception as error:  # pylint: disable=broad-exception-caught
                        error_name = type(error).__name__
                        if error_name in {"SteamNotAGameError", "SteamGameNotFoundError"}:
                            if not existing.get("blacklisted", False):
                                existing["blacklisted"] = True
                                metadata_updated = True
                        else:
                            errors.append(
                                f"Steam API failed for {game_id}: {type(error).__name__}: {error}"
                            )
                    else:
                        developer = online_data.get("developer")
                        if developer and developer != existing.get("developer"):
                            existing["developer"] = developer
                            metadata_updated = True

                cover_updated, had_cover_before = _attempt_cover_update(
                    game_id=game_id,
                    game_name=str(existing.get("name", game_data["name"])),
                    blacklisted=bool(existing.get("blacklisted", False)),
                    covers_dir=shared.covers_dir,
                    additional_data=additional_data,
                    settings=settings,
                    sgdb_cache=sgdb_cache,
                    sgdb_state=sgdb_state,
                    errors=errors,
                )

                if cover_updated:
                    summary.cover_updates += 1
                    if not had_cover_before:
                        summary.new_cover_updates += 1

                if metadata_updated:
                    summary.metadata_updates += 1
                    _write_game(shared.games_dir, existing)

                _emit_progress(
                    mode="import",
                    phase="item",
                    source=source_id,
                    game_id=game_id,
                    game_name=str(existing.get("name", game_data["name"])),
                    scanned=summary.scanned,
                    imported=summary.imported,
                    removed=summary.removed,
                    duplicates=summary.duplicates,
                    cover_updates=summary.cover_updates,
                    new_cover_updates=summary.new_cover_updates,
                    metadata_updates=summary.metadata_updates,
                    errors=len(errors),
                    cover_updated=cover_updated,
                    metadata_updated=metadata_updated,
                )
                continue

            if existing:
                game_data["last_played"] = int(existing.get("last_played", 0))
                game_data["hidden"] = bool(existing.get("hidden", False))
                game_data["removed"] = False
                game_data["added"] = int(existing.get("added", game_data["added"]))

            steam_appid = additional_data.get("steam_appid")
            if steam_api_helper and steam_appid is not None:
                try:
                    online_data = steam_api_helper.get_api_data(appid=str(steam_appid))
                except Exception as error:  # pylint: disable=broad-exception-caught
                    error_name = type(error).__name__
                    if error_name in {"SteamNotAGameError", "SteamGameNotFoundError"}:
                        game_data["blacklisted"] = True
                    else:
                        errors.append(
                            f"Steam API failed for {game_id}: {type(error).__name__}: {error}"
                        )
                else:
                    developer = online_data.get("developer")
                    if developer:
                        game_data["developer"] = developer

            _write_game(shared.games_dir, game_data)
            imported_ids.append(game_id)
            summary.imported += 1

            cover_updated, had_cover_before = _attempt_cover_update(
                game_id=game_id,
                game_name=game_data["name"],
                blacklisted=bool(game_data.get("blacklisted", False)),
                covers_dir=shared.covers_dir,
                additional_data=additional_data,
                settings=settings,
                sgdb_cache=sgdb_cache,
                sgdb_state=sgdb_state,
                errors=errors,
            )

            if cover_updated:
                summary.cover_updates += 1
                if not had_cover_before:
                    summary.new_cover_updates += 1

            _emit_progress(
                mode="import",
                phase="item",
                source=source_id,
                game_id=game_id,
                game_name=game_data["name"],
                scanned=summary.scanned,
                imported=summary.imported,
                removed=summary.removed,
                duplicates=summary.duplicates,
                cover_updates=summary.cover_updates,
                new_cover_updates=summary.new_cover_updates,
                errors=len(errors),
                cover_updated=cover_updated,
            )

    if settings.get_bool("remove-missing", True):
        for game_id, data in existing_games.items():
            base_source = str(data.get("source", "")).split("_")[0]
            if base_source == "imported":
                continue
            if not _source_enabled(settings, base_source):
                continue
            if game_id in seen_ids:
                continue
            if data.get("removed", False):
                continue

            data["removed"] = True
            _write_game(shared.games_dir, data)
            removed_ids.append(game_id)
            summary.removed += 1

    summary.errors = errors
    summary.imported_ids = imported_ids
    summary.removed_ids = removed_ids
    _emit_progress(
        mode="import",
        phase="done",
        scanned=summary.scanned,
        imported=summary.imported,
        removed=summary.removed,
        duplicates=summary.duplicates,
        cover_updates=summary.cover_updates,
        new_cover_updates=summary.new_cover_updates,
        errors=len(errors),
    )
    return summary.as_dict()


def main() -> int:
    try:
        print(json.dumps(_run_import()), flush=True)
        return 0
    except Exception as error:  # pylint: disable=broad-exception-caught
        payload = {
            "fatal": True,
            "error": f"{type(error).__name__}: {error}",
            "imported_ids": [],
            "removed_ids": [],
            "errors": [f"fatal: {type(error).__name__}: {error}"],
        }
        print(json.dumps(payload), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
