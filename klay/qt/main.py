from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from klay import shared
from klay.qt.library import GameLibrary
from klay.qt.settings import GENERAL_BOOL_KEYS, SettingsBackend


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="klay")
    parser.add_argument(
        "-s",
        "--search",
        default="",
        help="Open the app with this term in the search entry",
    )
    parser.add_argument(
        "-l",
        "--launch",
        default="",
        help="Run a game with the given game_id",
    )
    parser.add_argument(
        "-b",
        "--big-picture",
        action="store_true",
        help="Open in fullscreen Big Picture Mode",
    )
    return parser.parse_args(argv)


def _run_launch_mode(library: GameLibrary, game_id: str) -> int:
    game = library.load_game_by_id(game_id)
    if game is None:
        print(f"Game not found: {game_id}", file=sys.stderr)
        return 1

    try:
        library.launch(game)
    except OSError as error:
        print(f"Failed to launch '{game.name}': {error}", file=sys.stderr)
        return 1
    return 0


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _asset_path(*segments: str) -> Path:
    pkgdatadir = getattr(shared, "PKGDATADIR", "")
    if pkgdatadir:
        candidate = Path(pkgdatadir).joinpath("assets", *segments)
        if candidate.exists():
            return candidate
    return _project_root().joinpath("assets", *segments)


def _splash_logo_path() -> Path | None:
    candidates = (
        _asset_path("images", "Klay.png"),
        _project_root() / "Klay.png",
    )
    for logo in candidates:
        if logo.is_file():
            return logo
    return None


def _splash_sound_path() -> Path | None:
    preferred_name = "alexis_gaming_cam-woosh-ding-ding-baton-370398.mp3"
    candidate_paths: list[Path] = []
    env_value = os.getenv("KLAY_SPLASH_SOUND", "").strip()
    if env_value:
        candidate_paths.append(Path(env_value).expanduser())

    cwd = Path.cwd()
    assets_audio_dir = _asset_path("audio")
    candidate_paths.extend(
        [
            assets_audio_dir / preferred_name,
            assets_audio_dir / "splash.mp3",
            assets_audio_dir / "splash.wav",
            assets_audio_dir / "splash.ogg",
            cwd / preferred_name,
            cwd / "splash.mp3",
            cwd / "splash.wav",
            cwd / "splash.ogg",
        ]
    )

    root = _project_root()
    candidate_paths.extend(
        [
            root / preferred_name,
            root / "splash.mp3",
            root / "splash.wav",
            root / "splash.ogg",
        ]
    )

    for candidate in candidate_paths:
        if candidate.is_file():
            return candidate

    for pattern in ("*.mp3", "*.wav", "*.ogg"):
        matches = sorted(assets_audio_dir.glob(pattern))
        if matches:
            return matches[0]
    for pattern in ("*.mp3", "*.wav", "*.ogg"):
        matches = sorted(cwd.glob(pattern))
        if matches:
            return matches[0]
    for pattern in ("*.mp3", "*.wav", "*.ogg"):
        matches = sorted(root.glob(pattern))
        if matches:
            return matches[0]
    return None


def _build_splash_pixmap(logo_path: Path | None, qt_modules: tuple) -> object:
    QSize, Qt = qt_modules[0], qt_modules[1]
    QColor, _QFont, QPainter, QPixmap = qt_modules[2], qt_modules[3], qt_modules[4], qt_modules[5]

    logo = QPixmap(str(logo_path)) if logo_path is not None else QPixmap()
    if not logo.isNull():
        return logo.scaled(
            QSize(420, 420),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    fallback = QPixmap(420, 420)
    fallback.fill(QColor("#141820"))
    painter = QPainter(fallback)
    painter.end()
    return fallback


def _sound_duration_ms(path: Path) -> int:
    try:
        result = subprocess.run(  # noqa: S603
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        seconds = float(result.stdout.strip() or "0")
    except Exception:
        return 0
    return max(0, int(seconds * 1000))


def _qt_app_icon(qicon_cls):
    icon = qicon_cls.fromTheme(shared.APP_ID)
    if not icon.isNull():
        return icon
    icon = qicon_cls.fromTheme("com.grantshipley.Klay")
    if not icon.isNull():
        return icon
    for candidate in (_asset_path("images", "Klay.png"), _project_root() / "Klay.png"):
        if candidate.is_file():
            return qicon_cls(str(candidate))
    return icon


def main(_version: str, argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    data_dir_name = os.getenv("KLAY_DATA_DIR_NAME", "klay")
    library = GameLibrary(data_dir_name=data_dir_name)

    if args.launch:
        return _run_launch_mode(library, args.launch)

    try:
        from PySide6.QtCore import QUrl, QSize, Qt
        from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
        from PySide6.QtWidgets import QApplication, QMessageBox, QSplashScreen
    except ModuleNotFoundError:
        print(
            "PySide6 is required to run Klay.\n"
            "Install it with your distro package manager "
            "(for example: `sudo dnf install python3-pyside6`) "
            "or `python3 -m pip install --user PySide6`.",
            file=sys.stderr,
        )
        return 1

    os.environ.setdefault("QT_WAYLAND_APP_ID", shared.APP_ID)
    if hasattr(QApplication, "setDesktopFileName"):
        QApplication.setDesktopFileName(shared.APP_ID)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Klay")
    app.setOrganizationDomain("grantshipley.com")
    app.setOrganizationName("Grant Shipley")
    app_icon = _qt_app_icon(QIcon)
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    if hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName(shared.APP_ID)

    settings = SettingsBackend(data_dir_name)
    show_splash = settings.get_bool("show-splash", GENERAL_BOOL_KEYS["show-splash"])
    start_big_picture = args.big_picture or settings.get_bool(
        "start-big-picture", GENERAL_BOOL_KEYS["start-big-picture"]
    )

    splash: QSplashScreen | None = None
    splash_player = None
    splash_audio = None
    media_status_enum = None
    splash_sound_duration_ms = 0
    splash_started = 0.0
    min_splash_ms = 0

    if show_splash:
        splash_started = time.perf_counter()
        min_splash_ms = max(0, int(os.getenv("KLAY_SPLASH_MIN_MS", "1200") or "1200"))
        logo_path = _splash_logo_path()
        splash = QSplashScreen(
            _build_splash_pixmap(
                logo_path,
                (QSize, Qt, QColor, QFont, QPainter, QPixmap),
            )
        )
        splash.show()
        app.processEvents()

        sound_path = _splash_sound_path()
        if sound_path is not None:
            try:
                from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

                splash_player = QMediaPlayer()
                splash_audio = QAudioOutput()
                splash_audio.setVolume(0.55)
                splash_player.setAudioOutput(splash_audio)
                splash_player.setSource(QUrl.fromLocalFile(str(sound_path)))
                splash_player.play()
                media_status_enum = QMediaPlayer.MediaStatus
                splash_sound_duration_ms = _sound_duration_ms(sound_path)
            except Exception:
                splash_player = None
                splash_audio = None

    from klay.qt.window import KlayMainWindow

    window = KlayMainWindow(library=library, initial_search=args.search)

    if show_splash and splash is not None:
        if splash_player is not None and media_status_enum is not None:
            max_wait_s = max(1, int(os.getenv("KLAY_SPLASH_MAX_AUDIO_S", "20") or "20"))
            deadline = time.perf_counter() + max_wait_s
            target_ms = splash_sound_duration_ms
            while time.perf_counter() < deadline:
                app.processEvents()
                status = splash_player.mediaStatus()
                if status in {media_status_enum.InvalidMedia, media_status_enum.NoMedia}:
                    break
                if status == media_status_enum.EndOfMedia:
                    break

                if target_ms <= 0:
                    duration = int(splash_player.duration())
                    if duration > 0:
                        target_ms = duration
                if target_ms > 0 and int(splash_player.position()) >= max(0, target_ms - 80):
                    break
                time.sleep(0.02)
        else:
            elapsed_ms = int((time.perf_counter() - splash_started) * 1000)
            remaining_ms = max(0, min_splash_ms - elapsed_ms)
            if remaining_ms > 0:
                deadline = time.perf_counter() + (remaining_ms / 1000.0)
                while time.perf_counter() < deadline:
                    app.processEvents()
                    time.sleep(0.02)

    if start_big_picture:
        window.set_big_picture_mode(True)
    else:
        window.show()

    if show_splash and splash is not None:
        if splash_player is not None:
            try:
                splash_player.stop()
            except Exception:
                pass
        splash.finish(window)
        app.processEvents()

    try:
        return app.exec()
    except Exception as error:  # pylint: disable=broad-exception-caught
        QMessageBox.critical(None, "Klay", str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main("dev"))
