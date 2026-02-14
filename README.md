# Klay

Klay is a standalone KDE-focused fork of Cartridges.

## What This Fork Is

- Separate project root: `Klay/`
- Separate app ID: `org.kde.Klay`
- Separate binary: `klay`
- Separate data/config/cache namespace: `klay`
- Separate Python module namespace: `klay`

## Build

```bash
meson setup _build
meson compile -C _build
```

## Run

```bash
./_build/klay/klay
```

## Runtime Dependency

The Qt frontend uses PySide6.

- Fedora: `sudo dnf install python3-pyside6`
- Pip (user install): `python3 -m pip install --user PySide6`

## CLI Options

- `klay --search "term"`: open with a pre-filled search term
- `klay --launch GAME_ID`: launch a game by id without opening the UI

## Extensions

See `docs/klay.md` for loading custom import-source extensions.

## Qt Parity Tracking

Qt parity status is tracked in `docs/qt-parity.md`.
# Klay
