# Klay - Play games on KDE
Klay is a standalone Qt/KDE-focused game launcher that unifies games from multiple sources with rich cover art and metadata.

**Inspired by and based on the great [Cartridges](https://github.com/kra-mo/cartridges) app for GNOME.**

## Highlights

- Qt desktop app (`PySide6`) tuned for KDE workflows
- Imports from:
  - Steam
  - Lutris
  - Heroic
  - Bottles
  - itch
  - Legendary
  - RetroArch
  - Flatpak
  - Desktop Entries
- Main library cover grid with animated cover support (`.gif`, animated `.webp`)
- Game details page with backdrop, metadata, and quick actions
- SteamGridDB + IGDB integration for covers and metadata (Users have to provide their own API keys)
- Playtime display on game cards when available from importer sources
- Category management: create/rename/delete categories, assign games, customize category icons, and filter from the left nav
- Optional splash screen + startup sound

## Screenshots

<p align="center">
  <img src="screenshots/light_theme.png" alt="Klay light mode library view" width="49%">
  <img src="screenshots/dark_theme.png" alt="Klay dark mode" width="49%">
</p>

<p align="center">
  <img src="screenshots/game_detail.png" alt="Klay game details view" width="49%">
  <img src="screenshots/cover_manager.png" alt="Klay cover selection view" width="49%">
</p>

## Download (Flatpak)

Latest releases:

`https://github.com/gshipley/Klay/releases/latest`

Install from a downloaded release asset:

```bash
flatpak install --user --reinstall ./com.grantshipley.Klay.Devel.flatpak
flatpak run com.grantshipley.Klay.Devel
```

## Build

```bash
meson setup _build
meson compile -C _build
```

## Run

```bash
./_build/klay/klay
```

## Runtime Dependencies

- Python 3
- PySide6 (Qt frontend)
- PyGObject (`gi`, GLib/Gio bindings used by import backends/settings)

Fedora:

```bash
sudo dnf install python3-pyside6 python3-gobject
```

Pip (user install):

```bash
python3 -m pip install --user PySide6
```

Optional:

- `ffprobe` for accurate splash sound duration detection

## CLI Options

- `klay --search "term"`: open with a pre-filled search term
- `klay --launch GAME_ID`: launch a game directly by id

## Usage Guide

### 1. Import games

- Use `Import` from the main UI.
- Enable/disable sources in `Preferences -> Sources`.
- Configure source paths if your launchers are in non-default locations.

### 2. Browse and filter

- Left nav shows:
  - `All Games`
  - `Added`
  - imported source groups
  - custom categories (if any)
- Search filters by title, metadata fields, and categories.
- Sort by A-Z, Z-A, newest, oldest, and last played.

### 3. Game details

From details view you can:

- Play
- Edit
- Hide / Unhide
- Remove
- Search external databases
- Refresh metadata
- Change cover
- Manage categories

Clicking outside the details card returns to the main library.

### 4. Covers and animation

- Klay supports static and animated covers.
- Cover picker shows available candidates in card form and marks animated options.
- Animated covers are prioritized when enabled.
- Custom chosen covers are preserved and not overwritten by startup auto-import.

### 5. Metadata providers

`Preferences -> SteamGridDB`:

- Add SGDB API key
- Enable cover lookup
- Prefer SGDB covers
- Allow animated SGDB covers
- Optional: include image updates during metadata refresh

`Preferences -> IGDB`:

- Provide `Client ID` and either:
  - `Access Token`, or
  - `Client Secret` (Klay will fetch token automatically)
- Enable IGDB metadata enrichment
- Refresh metadata on demand

## Categories

- Assign categories per game from details view (`Categories`) or right-click menu (`Categories...`).
- Manage category definitions in `Preferences -> Categories`:
  - Add categories
  - Rename categories
  - Delete categories (with confirmation)
- Customize category icons in `Preferences -> Categories`:
  - Pick from built-in icon set
  - Upload your own icon
  - Clear a custom icon and fall back to default
- Category filters appear in the left nav only when games are assigned to that category.

## Preferences

General settings include:

- Dark mode
- Splash screen + startup sound
- Auto import on startup
- Exit after launch
- Cover click launches game vs opens details
- High-quality image preference
- Remove missing imported games

## Data Paths

Klay uses its own namespace:

- Config: `~/.config/klay/`
- Data: `~/.local/share/klay/`
- Games JSON: `~/.local/share/klay/games/`
- Covers: `~/.local/share/klay/covers/`

## Troubleshooting

### IGDB metadata not loading

- Verify `Client ID` is set.
- Provide either a valid `Access Token` or `Client Secret`.
- If using secret-only mode, ensure outbound access to Twitch token endpoint is allowed.

### Cover picker appears slow

- First open may fetch and cache provider results.
- Subsequent opens use local cache and should be faster.

### A launcher game is missing

- Confirm source is enabled in Preferences.
- Verify source path values.
- Re-run import and check status output in the import dialog.

## Project Notes

- App ID: `com.grantshipley.Klay`
- Binary: `klay`
- Python namespace: `klay`
- Extension docs: `docs/klay.md`
