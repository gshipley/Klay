from __future__ import annotations

import argparse
import os
import sys

from klay.qt.library import GameLibrary


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


def main(_version: str, argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    library = GameLibrary(data_dir_name=os.getenv("KLAY_DATA_DIR_NAME", "klay"))

    if args.launch:
        return _run_launch_mode(library, args.launch)

    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ModuleNotFoundError:
        print(
            "PySide6 is required to run Klay.\n"
            "Install it with your distro package manager "
            "(for example: `sudo dnf install python3-pyside6`) "
            "or `python3 -m pip install --user PySide6`.",
            file=sys.stderr,
        )
        return 1

    from klay.qt.window import KlayMainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Klay")
    app.setOrganizationDomain("kde.org")
    app.setOrganizationName("KDE")
    if hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName("org.kde.Klay")

    window = KlayMainWindow(library=library, initial_search=args.search)
    window.show()

    try:
        return app.exec()
    except Exception as error:  # pylint: disable=broad-exception-caught
        QMessageBox.critical(None, "Klay", str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main("dev"))
