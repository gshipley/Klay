# Qt Parity Report (Klay vs Cartridges)

This report tracks parity of the Qt frontend (`klay/qt`) against the original
GTK frontend (`klay/main.py`, `klay/window.py`) as of this fork revision.

## Visual Parity

- Status: `Close`
- Implemented:
  - Sidebar with `All Games`, `Added`, and per-source rows + counts.
  - Header actions layout (`Add`, `Search`, `Main Menu`) and keyboard actions.
  - Card-based library grid with 200x300 cover art, adwaita-like spacing, and title area.
  - Empty states: `No Games`, `No Games Found`, `No Hidden Games`.
  - In-window details view with back navigation, cover, metadata, and action toolbar.
- Remaining visual gaps:
  - libadwaita-specific motion/transitions are not replicated one-to-one.
  - Some typography and edge spacing still differ from GTK/libadwaita rendering.

## Feature Parity Matrix

- `search (--search)`:
  - GTK: Yes
  - Qt: Yes
  - Status: `Complete`
- `launch (--launch)`:
  - GTK: Yes
  - Qt: Yes
  - Status: `Complete`
- Browse library / source filtering:
  - GTK: Yes
  - Qt: Yes
  - Status: `Complete`
- In-app details page navigation:
  - GTK: Yes
  - Qt: Yes
  - Status: `Complete`
- Sort (`A-Z`, `Z-A`, `Newest`, `Oldest`, `Last Played`):
  - GTK: Yes
  - Qt: Yes
  - Status: `Complete`
- Show hidden games:
  - GTK: Yes
  - Qt: Yes
  - Status: `Complete`
- Add game:
  - GTK: Yes
  - Qt: Yes
  - Status: `Complete`
- Edit game:
  - GTK: Yes
  - Qt: Yes
  - Status: `Complete`
- Remove game:
  - GTK: Yes
  - Qt: Yes
  - Status: `Complete`
- Hide / unhide:
  - GTK: Yes
  - Qt: Yes
  - Status: `Complete`
- Undo (`Ctrl+Z`):
  - GTK: Yes
  - Qt: Yes (hide/remove and importer session undo)
  - Status: `Complete`
- External search actions (`IGDB`, `SteamGridDB`, `ProtonDB`, `PCGamingWiki`, `Lutris`, `HLTB`):
  - GTK: Yes
  - Qt: Yes
  - Status: `Complete`
- About dialog:
  - GTK: Yes
  - Qt: Yes
  - Status: `Complete`
- Preferences:
  - GTK: Yes
  - Qt: Yes (Qt-native dialog with general/source/import/SGDB settings)
  - Status: `Complete`
- Automatic source import (`Importer`, source scanners, manager pipelines):
  - GTK: Yes
  - Qt: Yes (Qt action runs importer worker and shows results/errors)
  - Status: `Complete`
- SteamGridDB cover manager pipeline:
  - GTK: Yes
  - Qt: Yes (SGDB lookup integrated into Qt import worker)
  - Status: `Complete`
- GSettings-backed runtime behavior (`exit-after-launch`, `cover-launches-game`, `remove-missing`, source toggles):
  - GTK: Yes
  - Qt: Yes (Settings backend wired for runtime + importer behaviors)
  - Status: `Complete`
