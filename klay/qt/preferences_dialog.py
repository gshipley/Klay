from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from klay.qt.settings import (
    GENERAL_BOOL_KEYS,
    IMPORT_BOOL_KEYS,
    SOURCE_BOOL_KEYS,
    STRING_KEYS,
    SettingsBackend,
)


SOURCE_LAYOUT = (
    ("steam", "Steam", (("steam-location", "Data Directory"),)),
    ("geforcenow", "GeForce NOW", ()),
    (
        "lutris",
        "Lutris",
        (
            ("lutris-location", "Data Directory"),
            ("lutris-cache-location", "Cache Directory"),
        ),
    ),
    ("heroic", "Heroic", (("heroic-location", "Config Directory"),)),
    ("bottles", "Bottles", (("bottles-location", "Data Directory"),)),
    ("itch", "itch", (("itch-location", "Config Directory"),)),
    ("legendary", "Legendary", (("legendary-location", "Config Directory"),)),
    ("retroarch", "RetroArch", (("retroarch-location", "Config Directory"),)),
    (
        "flatpak",
        "Flatpak",
        (
            ("flatpak-system-location", "System Data Directory"),
            ("flatpak-user-location", "User Data Directory"),
        ),
    ),
    ("desktop", "Desktop Entries", ()),
)

CATEGORY_ICON_FILTER = "Images (*.png *.svg *.webp *.jpg *.jpeg *.bmp *.gif)"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _asset_path(*segments: str) -> Path:
    try:
        from klay import shared
    except Exception:
        shared = None

    if shared is not None:
        pkgdatadir = getattr(shared, "PKGDATADIR", "")
        if pkgdatadir:
            candidate = Path(pkgdatadir).joinpath("assets", *segments)
            if candidate.exists():
                return candidate
    return _project_root().joinpath("assets", *segments)


class PreferencesDialog(QDialog):
    def __init__(
        self,
        settings: SettingsBackend,
        *,
        category_labels: dict[str, str] | None = None,
        category_icons: dict[str, str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.bool_widgets: dict[str, QCheckBox] = {}
        self.string_widgets: dict[str, QLineEdit] = {}
        self.category_labels_by_key = {
            str(key).strip().casefold(): str(label).strip()
            for key, label in (category_labels or {}).items()
            if str(key).strip() and str(label).strip()
        }
        self._category_redirects: dict[str, str] = {}
        self._deleted_category_keys: set[str] = set()
        self.category_icon_paths = {
            str(key).strip().casefold(): str(path).strip()
            for key, path in (category_icons or {}).items()
            if str(key).strip() and str(path).strip()
        }
        self.category_list: QListWidget | None = None
        self.category_built_in_list: QListWidget | None = None
        self.category_preview_label: QLabel | None = None
        self.category_preview_title: QLabel | None = None
        self.category_hint_label: QLabel | None = None
        self.category_add_button: QToolButton | None = None
        self.category_edit_button: QToolButton | None = None
        self.category_delete_button: QToolButton | None = None
        self.category_set_icon_button: QPushButton | None = None
        self.category_clear_icon_button: QPushButton | None = None
        self.category_bundled_icons = self._bundled_category_icon_paths()
        self.refresh_requested = False
        self.import_requested = False

        self.setWindowTitle("Preferences")
        self.setModal(True)
        self.resize(760, 560)

        root = QVBoxLayout(self)

        tabs = QTabWidget()
        root.addWidget(tabs, 1)

        tabs.addTab(self._build_general_page(), "General")
        tabs.addTab(self._build_sources_page(), "Sources")
        tabs.addTab(self._build_geforcenow_page(), "GeForce NOW")
        tabs.addTab(self._build_import_page(), "Import")
        tabs.addTab(self._build_categories_page(), "Categories")
        tabs.addTab(self._build_sgdb_page(), "SteamGridDB")
        tabs.addTab(self._build_igdb_page(), "IGDB")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _checkbox(
        self, key: str, label: str, *, default: bool, tooltip: str | None = None
    ) -> QCheckBox:
        widget = QCheckBox(label)
        widget.setChecked(self.settings.get_bool(key, default))
        if tooltip:
            widget.setToolTip(tooltip)
        self.bool_widgets[key] = widget
        return widget

    def _line_edit(self, key: str, *, default: str) -> QLineEdit:
        edit = QLineEdit(self.settings.get_string(key, default))
        self.string_widgets[key] = edit
        return edit

    def _icon_tool_button(
        self,
        *,
        icon_names: tuple[str, ...],
        fallback_text: str,
        tooltip: str,
        slot,
    ) -> QToolButton:
        button = QToolButton()
        button.setToolTip(tooltip)
        button.setAutoRaise(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        icon = QIcon()
        for name in icon_names:
            candidate = QIcon.fromTheme(name)
            if not candidate.isNull():
                icon = candidate
                break
        if icon.isNull():
            button.setText(fallback_text)
        else:
            button.setIcon(icon)
            button.setIconSize(QSize(18, 18))
        button.clicked.connect(slot)
        return button

    def _build_general_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        layout.addWidget(
            self._checkbox(
                "dark-mode",
                "Use dark mode",
                default=GENERAL_BOOL_KEYS["dark-mode"],
            )
        )
        layout.addWidget(
            self._checkbox(
                "show-splash",
                "Show splash screen and startup sound",
                default=GENERAL_BOOL_KEYS["show-splash"],
                tooltip="Disable this to start directly into the library without splash or sound.",
            )
        )
        layout.addWidget(
            self._checkbox(
                "auto-import",
                "Automatically import games on startup",
                default=GENERAL_BOOL_KEYS["auto-import"],
            )
        )
        layout.addWidget(
            self._checkbox(
                "exit-after-launch",
                "Exit application after launching a game",
                default=GENERAL_BOOL_KEYS["exit-after-launch"],
            )
        )
        layout.addWidget(
            self._checkbox(
                "cover-launches-game",
                "Activating a game card launches it instead of opening details",
                default=GENERAL_BOOL_KEYS["cover-launches-game"],
            )
        )
        layout.addWidget(
            self._checkbox(
                "high-quality-images",
                "Prefer high quality images where available",
                default=GENERAL_BOOL_KEYS["high-quality-images"],
            )
        )
        layout.addWidget(
            self._checkbox(
                "refresh-covers-on-metadata",
                "Include image updates during metadata refresh (may overwrite existing covers)",
                default=GENERAL_BOOL_KEYS["refresh-covers-on-metadata"],
                tooltip="When enabled, metadata refresh can replace existing cover images with new provider matches.",
            )
        )
        layout.addWidget(
            self._checkbox(
                "remove-missing",
                "Mark missing imported games as removed",
                default=GENERAL_BOOL_KEYS["remove-missing"],
            )
        )

        layout.addStretch(1)
        return page

    def _build_sources_page(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        page_layout.addWidget(scroll)

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(14, 14, 14, 14)
        container_layout.setSpacing(12)

        for source_key, source_name, path_rows in SOURCE_LAYOUT:
            group = QGroupBox(source_name)
            group_layout = QVBoxLayout(group)
            group_layout.setSpacing(8)

            enabled_box = self._checkbox(
                source_key,
                f"Enable {source_name}",
                default=SOURCE_BOOL_KEYS.get(source_key, True),
            )
            group_layout.addWidget(enabled_box)

            form = QFormLayout()
            form.setSpacing(6)
            for key, label in path_rows:
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(6)

                path_edit = self._line_edit(key, default=STRING_KEYS[key])
                row_layout.addWidget(path_edit, 1)

                browse_btn = QPushButton("Browse")
                browse_btn.clicked.connect(
                    lambda _checked=False, edit=path_edit: self._choose_directory(edit)
                )
                row_layout.addWidget(browse_btn)

                form.addRow(label, row)
            if path_rows:
                group_layout.addLayout(form)

            container_layout.addWidget(group)

        container_layout.addStretch(1)
        scroll.setWidget(container)
        return page

    def _build_import_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        layout.addWidget(
            QLabel("Source-specific import behavior:")
        )
        layout.addWidget(
            self._checkbox(
                "lutris-import-steam",
                "Lutris: include Steam entries",
                default=IMPORT_BOOL_KEYS["lutris-import-steam"],
            )
        )
        layout.addWidget(
            self._checkbox(
                "lutris-import-flatpak",
                "Lutris: include Flatpak entries",
                default=IMPORT_BOOL_KEYS["lutris-import-flatpak"],
            )
        )
        layout.addWidget(
            self._checkbox(
                "heroic-import-epic",
                "Heroic: include Epic games",
                default=IMPORT_BOOL_KEYS["heroic-import-epic"],
            )
        )
        layout.addWidget(
            self._checkbox(
                "heroic-import-gog",
                "Heroic: include GOG games",
                default=IMPORT_BOOL_KEYS["heroic-import-gog"],
            )
        )
        layout.addWidget(
            self._checkbox(
                "heroic-import-amazon",
                "Heroic: include Amazon games",
                default=IMPORT_BOOL_KEYS["heroic-import-amazon"],
            )
        )
        layout.addWidget(
            self._checkbox(
                "heroic-import-sideload",
                "Heroic: include sideload games",
                default=IMPORT_BOOL_KEYS["heroic-import-sideload"],
            )
        )
        layout.addWidget(
            self._checkbox(
                "flatpak-import-launchers",
                "Flatpak: include launcher apps",
                default=IMPORT_BOOL_KEYS["flatpak-import-launchers"],
            )
        )
        import_now_button = QPushButton("Import Games Now")
        import_now_button.clicked.connect(self._request_import)
        layout.addWidget(import_now_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return page

    def _build_geforcenow_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        info = QLabel(
            "Configure GeForce NOW-specific library behavior.\n"
            "Source enablement remains in the Sources tab."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        layout.addWidget(
            self._checkbox(
                "geforcenow-include-in-all-games",
                "Include GeForce NOW games in All Games",
                default=GENERAL_BOOL_KEYS["geforcenow-include-in-all-games"],
                tooltip=(
                    "When disabled, GeForce NOW games appear only in the GeForce NOW"
                    " source filter."
                ),
            )
        )
        layout.addWidget(
            self._checkbox(
                "geforcenow-close-on-stream-end",
                "Close GeForce NOW after stream ends (best effort)",
                default=GENERAL_BOOL_KEYS["geforcenow-close-on-stream-end"],
                tooltip=(
                    "Monitors GeForce NOW logs for stream-end events and closes the"
                    " launched GeForce NOW process automatically."
                ),
            )
        )

        layout.addStretch(1)
        return page

    def _build_categories_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("categoriesPage")
        page.setStyleSheet(
            """
            #categoriesPage QGroupBox {
                border: 1px solid palette(mid);
                border-radius: 10px;
                margin-top: 12px;
                padding: 12px 8px 8px 8px;
                font-weight: 600;
            }
            #categoriesPage QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: #4f88c6;
            }
            #categoriesPage QListWidget {
                border: 1px solid palette(mid);
                border-radius: 8px;
                padding: 4px;
            }
            #categoriesPage QLabel[role="hint"] {
                color: palette(mid);
            }
            #categoriesPage QToolButton {
                border: 1px solid palette(mid);
                border-radius: 6px;
                min-width: 30px;
                min-height: 30px;
                padding: 2px;
            }
            #categoriesPage QToolButton:hover {
                border-color: palette(highlight);
            }
            """
        )
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        content = QHBoxLayout()
        content.setSpacing(12)
        layout.addLayout(content, 1)

        list_panel = QGroupBox("Category Roster")
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(6, 8, 6, 4)
        list_layout.setSpacing(8)

        self.category_list = QListWidget()
        self.category_list.setAlternatingRowColors(True)
        self.category_list.setIconSize(QSize(20, 20))
        self.category_list.currentItemChanged.connect(
            lambda _current, _previous: self._update_category_icon_controls()
        )
        list_layout.addWidget(self.category_list, 1)

        roster_hint = QLabel("Categories appear in the sidebar when games use them.")
        roster_hint.setProperty("role", "hint")
        roster_hint.setWordWrap(True)
        list_layout.addWidget(roster_hint)

        list_controls = QHBoxLayout()
        list_controls.setSpacing(6)
        self.category_add_button = self._icon_tool_button(
            icon_names=("list-add-symbolic", "list-add"),
            fallback_text="+",
            tooltip="Add category",
            slot=self._add_category,
        )
        list_controls.addWidget(self.category_add_button)

        self.category_edit_button = self._icon_tool_button(
            icon_names=("document-edit-symbolic", "document-edit"),
            fallback_text="E",
            tooltip="Rename selected category",
            slot=self._rename_category,
        )
        list_controls.addWidget(self.category_edit_button)

        self.category_delete_button = self._icon_tool_button(
            icon_names=("list-remove-symbolic", "list-remove", "edit-delete-symbolic"),
            fallback_text="-",
            tooltip="Delete selected category",
            slot=self._delete_category,
        )
        list_controls.addWidget(self.category_delete_button)
        list_controls.addStretch(1)
        list_layout.addLayout(list_controls)

        content.addWidget(list_panel, 1)

        icon_panel = QGroupBox("Icon Loadout")
        icon_layout = QVBoxLayout(icon_panel)
        icon_layout.setContentsMargins(6, 8, 6, 4)
        icon_layout.setSpacing(10)

        self.category_preview_title = QLabel("No category selected")
        icon_layout.addWidget(self.category_preview_title)

        self.category_preview_label = QLabel("")
        self.category_preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.category_preview_label.setFixedSize(120, 120)
        self.category_preview_label.setStyleSheet(
            "border: 1px solid palette(mid);"
            "border-radius: 10px;"
            "background: rgba(0, 0, 0, 0.18);"
        )
        icon_layout.addWidget(self.category_preview_label, alignment=Qt.AlignmentFlag.AlignLeft)

        self.category_hint_label = QLabel("Select an icon tile to apply it instantly.")
        self.category_hint_label.setProperty("role", "hint")
        self.category_hint_label.setWordWrap(True)
        icon_layout.addWidget(self.category_hint_label)

        self.category_built_in_list = QListWidget()
        self.category_built_in_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.category_built_in_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.category_built_in_list.setMovement(QListWidget.Movement.Static)
        self.category_built_in_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.category_built_in_list.setIconSize(QSize(46, 46))
        self.category_built_in_list.setGridSize(QSize(64, 64))
        self.category_built_in_list.setSpacing(6)
        self.category_built_in_list.setUniformItemSizes(True)
        self.category_built_in_list.itemClicked.connect(
            lambda _item: self._apply_built_in_category_icon()
        )
        self.category_built_in_list.itemActivated.connect(
            lambda _item: self._apply_built_in_category_icon()
        )
        icon_layout.addWidget(self.category_built_in_list, 1)

        icon_controls = QHBoxLayout()
        icon_controls.setSpacing(8)
        self.category_set_icon_button = QPushButton("Upload Icon...")
        self.category_set_icon_button.clicked.connect(self._choose_category_icon)
        icon_controls.addWidget(self.category_set_icon_button)

        self.category_clear_icon_button = QPushButton("Clear Icon")
        self.category_clear_icon_button.clicked.connect(self._clear_category_icon)
        icon_controls.addWidget(self.category_clear_icon_button)
        icon_controls.addStretch(1)
        icon_layout.addLayout(icon_controls)

        content.addWidget(icon_panel, 1)

        self._populate_bundled_icon_list()
        self._refresh_category_list()
        return page

    def _bundled_category_icon_dir(self) -> Path:
        return _asset_path("category-icons", "game-icons-popular-color")

    def _bundled_category_icon_paths(self) -> list[str]:
        icon_dir = self._bundled_category_icon_dir()
        if not icon_dir.is_dir():
            return []
        candidates: list[str] = []
        for extension in ("*.svg", "*.png", "*.webp", "*.jpg", "*.jpeg", "*.bmp", "*.gif"):
            candidates.extend(str(path) for path in sorted(icon_dir.glob(extension)))
        return candidates

    def _selected_category_key(self) -> str | None:
        if self.category_list is None:
            return None
        item = self.category_list.currentItem()
        if item is None:
            return None
        key = str(item.data(Qt.ItemDataRole.UserRole) or "").strip().casefold()
        return key or None

    @staticmethod
    def _clean_category_name(value: str) -> str:
        return " ".join(value.split()).strip()

    def _normalize_redirects(self) -> None:
        normalized: dict[str, str] = {}
        for source, target in list(self._category_redirects.items()):
            src = self._clean_category_name(str(source)).casefold()
            dst = self._clean_category_name(str(target)).casefold()
            if not src or not dst or src == dst:
                continue
            visited = {src}
            while dst in self._category_redirects and dst not in visited:
                visited.add(dst)
                next_target = self._clean_category_name(self._category_redirects[dst]).casefold()
                if not next_target:
                    break
                dst = next_target
            normalized[src] = dst
        self._category_redirects = normalized

    def _add_category(self) -> None:
        name, accepted = QInputDialog.getText(
            self,
            "Add Category",
            "Category name:",
        )
        if not accepted:
            return
        name = self._clean_category_name(name)
        if not name:
            return
        key = name.casefold()
        self.category_labels_by_key[key] = name
        self._deleted_category_keys.discard(key)
        self._refresh_category_list()
        if self.category_list is not None:
            for row in range(self.category_list.count()):
                item = self.category_list.item(row)
                if str(item.data(Qt.ItemDataRole.UserRole) or "").strip().casefold() == key:
                    self.category_list.setCurrentRow(row)
                    break

    def _rename_category(self) -> None:
        key = self._selected_category_key()
        if key is None:
            return
        current_name = self.category_labels_by_key.get(key, key)
        new_name, accepted = QInputDialog.getText(
            self,
            "Rename Category",
            "Category name:",
            text=current_name,
        )
        if not accepted:
            return
        new_name = self._clean_category_name(new_name)
        if not new_name:
            return
        new_key = new_name.casefold()
        if new_key == key:
            self.category_labels_by_key[key] = new_name
            self._refresh_category_list()
            return

        old_icon_path = self.category_icon_paths.pop(key, "").strip()
        existing_name = self.category_labels_by_key.get(new_key)
        self.category_labels_by_key.pop(key, None)
        self.category_labels_by_key[new_key] = new_name if existing_name is None else existing_name
        if old_icon_path and not self.category_icon_paths.get(new_key):
            self.category_icon_paths[new_key] = old_icon_path

        self._deleted_category_keys.discard(new_key)
        self._deleted_category_keys.discard(key)
        self._category_redirects[key] = new_key
        for source, target in list(self._category_redirects.items()):
            if target == key:
                self._category_redirects[source] = new_key
        self._normalize_redirects()

        self._refresh_category_list()
        if self.category_list is not None:
            for row in range(self.category_list.count()):
                item = self.category_list.item(row)
                if str(item.data(Qt.ItemDataRole.UserRole) or "").strip().casefold() == new_key:
                    self.category_list.setCurrentRow(row)
                    break

    def _delete_category(self) -> None:
        key = self._selected_category_key()
        if key is None:
            return
        label = self.category_labels_by_key.get(key, key)
        response = QMessageBox.question(
            self,
            "Delete Category",
            f"Delete category '{label}'?\n\n"
            "This will remove the category from games when you save preferences.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response != QMessageBox.StandardButton.Yes:
            return

        self.category_labels_by_key.pop(key, None)
        self.category_icon_paths.pop(key, None)
        self._deleted_category_keys.add(key)

        self._category_redirects = {
            source: target
            for source, target in self._category_redirects.items()
            if source != key and target != key
        }
        self._normalize_redirects()
        self._refresh_category_list()

    def category_labels(self) -> dict[str, str]:
        return dict(self.category_labels_by_key)

    def category_redirects(self) -> dict[str, str]:
        self._normalize_redirects()
        return dict(self._category_redirects)

    def deleted_category_keys(self) -> set[str]:
        return {
            key
            for key in self._deleted_category_keys
            if key and key not in self.category_labels_by_key
        }

    def _refresh_category_list(self) -> None:
        if self.category_list is None:
            return

        selected_key = self._selected_category_key()
        self.category_list.clear()

        for key in sorted(
            self.category_labels_by_key,
            key=lambda item: self.category_labels_by_key[item].casefold(),
        ):
            item = QListWidgetItem(self.category_labels_by_key[key])
            item.setData(Qt.ItemDataRole.UserRole, key)
            if icon_path := self.category_icon_paths.get(key, "").strip():
                icon = QIcon(icon_path)
                if not icon.isNull():
                    item.setIcon(icon)
            self.category_list.addItem(item)
            if key == selected_key:
                self.category_list.setCurrentItem(item)

        if self.category_list.currentItem() is None and self.category_list.count() > 0:
            self.category_list.setCurrentRow(0)

        self._update_category_icon_controls()

    def _populate_bundled_icon_list(self) -> None:
        if self.category_built_in_list is None:
            return
        self.category_built_in_list.clear()
        for path in self.category_bundled_icons:
            icon_path = str(path).strip()
            if not icon_path:
                continue
            file_path = Path(icon_path)
            item = QListWidgetItem("")
            item.setData(Qt.ItemDataRole.UserRole, icon_path)
            icon = QIcon(icon_path)
            if not icon.isNull():
                item.setIcon(icon)
            item.setToolTip(file_path.stem.replace("-", " "))
            item.setSizeHint(QSize(56, 56))
            self.category_built_in_list.addItem(item)

    def _selected_built_in_icon_path(self) -> str:
        if self.category_built_in_list is None:
            return ""
        item = self.category_built_in_list.currentItem()
        if item is None:
            return ""
        return str(item.data(Qt.ItemDataRole.UserRole) or "").strip()

    def _set_preview_icon(self, icon_path: str) -> None:
        if self.category_preview_label is None:
            return
        pixmap = QIcon(icon_path).pixmap(QSize(96, 96)) if icon_path else QIcon().pixmap(QSize(96, 96))
        if pixmap.isNull():
            fallback = QIcon.fromTheme("image-x-generic")
            pixmap = fallback.pixmap(QSize(96, 96))
        if pixmap.isNull():
            self.category_preview_label.setPixmap(pixmap)
            self.category_preview_label.setText("No\nPreview")
            return
        self.category_preview_label.setText("")
        self.category_preview_label.setPixmap(pixmap)

    def _sync_built_in_selection(self, icon_path: str) -> None:
        if self.category_built_in_list is None:
            return
        target = str(Path(icon_path).resolve(strict=False)) if icon_path else ""
        previous = self.category_built_in_list.blockSignals(True)
        self.category_built_in_list.clearSelection()
        if target:
            for index in range(self.category_built_in_list.count()):
                item = self.category_built_in_list.item(index)
                candidate = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
                if not candidate:
                    continue
                candidate_resolved = str(Path(candidate).resolve(strict=False))
                if candidate_resolved == target:
                    self.category_built_in_list.setCurrentItem(item)
                    break
        self.category_built_in_list.blockSignals(previous)

    def _update_category_icon_controls(self) -> None:
        selected_key = self._selected_category_key()
        icon_path = self.category_icon_paths.get(selected_key or "", "").strip()
        has_selection = selected_key is not None

        if self.category_add_button is not None:
            self.category_add_button.setEnabled(True)
        if self.category_edit_button is not None:
            self.category_edit_button.setEnabled(has_selection)
        if self.category_delete_button is not None:
            self.category_delete_button.setEnabled(has_selection)
        if self.category_set_icon_button is not None:
            self.category_set_icon_button.setEnabled(has_selection)
        if self.category_built_in_list is not None:
            self.category_built_in_list.setEnabled(
                has_selection and bool(self.category_bundled_icons)
            )
        if self.category_preview_title is None:
            return
        if not has_selection:
            self.category_preview_title.setText("No category selected")
            self._sync_built_in_selection("")
            self._set_preview_icon("")
            if self.category_hint_label is not None:
                self.category_hint_label.setText("Select a category, then pick an icon tile.")
            if self.category_clear_icon_button is not None:
                self.category_clear_icon_button.setEnabled(False)
            return
        label = self.category_labels_by_key.get(selected_key or "", selected_key or "")
        self.category_preview_title.setText(label)
        self._sync_built_in_selection(icon_path)
        if self.category_hint_label is not None:
            self.category_hint_label.setText("Select an icon tile to apply it instantly.")
        if self.category_clear_icon_button is not None:
            self.category_clear_icon_button.setEnabled(bool(icon_path))
        if icon_path:
            self._set_preview_icon(icon_path)
            return
        self._set_preview_icon("")

    def _apply_built_in_category_icon(self) -> None:
        key = self._selected_category_key()
        if key is None:
            return
        path = self._selected_built_in_icon_path()
        if not path:
            return
        self.category_icon_paths[key] = path
        self._refresh_category_list()

    def _choose_category_icon(self) -> None:
        key = self._selected_category_key()
        if key is None:
            return

        start_path = self.category_icon_paths.get(key, "").strip()
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Select Category Icon",
            start_path,
            CATEGORY_ICON_FILTER,
        )
        if not path:
            return

        self.category_icon_paths[key] = path
        self._refresh_category_list()

    def _clear_category_icon(self) -> None:
        key = self._selected_category_key()
        if key is None:
            return

        self.category_icon_paths.pop(key, None)
        self._refresh_category_list()

    def _build_sgdb_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        info = QLabel(
            'SteamGridDB requires an API key. Generate one at '
            '<a href="https://www.steamgriddb.com/profile/preferences/api">'
            "steamgriddb.com</a>."
        )
        info.setWordWrap(True)
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setOpenExternalLinks(True)
        layout.addWidget(info)

        key_form = QFormLayout()
        self.sgdb_key_edit = self._line_edit("sgdb-key", default=STRING_KEYS["sgdb-key"])
        key_form.addRow("API Key", self.sgdb_key_edit)
        layout.addLayout(key_form)

        self.sgdb_enabled_box = self._checkbox(
            "sgdb",
            "Enable SteamGridDB cover lookup",
            default=GENERAL_BOOL_KEYS["sgdb"],
        )
        self.sgdb_prefer_box = self._checkbox(
            "sgdb-prefer",
            "Prefer SteamGridDB covers over source-provided covers",
            default=GENERAL_BOOL_KEYS["sgdb-prefer"],
        )
        self.sgdb_animated_box = self._checkbox(
            "sgdb-animated",
            "Allow animated covers when available",
            default=GENERAL_BOOL_KEYS["sgdb-animated"],
        )

        layout.addWidget(self.sgdb_enabled_box)
        layout.addWidget(self.sgdb_prefer_box)
        layout.addWidget(self.sgdb_animated_box)

        self.sgdb_refresh_button = QPushButton("Refresh Metadata Now")
        self.sgdb_refresh_button.clicked.connect(self._request_refresh)
        layout.addWidget(self.sgdb_refresh_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self.sgdb_key_edit.textChanged.connect(self._update_sgdb_state)
        self._update_sgdb_state()

        layout.addStretch(1)
        return page

    def _build_igdb_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        info = QLabel(
            "IGDB requires a Twitch app Client ID and either an app access token"
            " or a Client Secret. If a secret is provided, Klay will fetch the token"
            " automatically during metadata refresh. Create/manage your Twitch app at "
            '<a href="https://dev.twitch.tv/console/apps">dev.twitch.tv/console/apps</a>.'
        )
        info.setWordWrap(True)
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setOpenExternalLinks(True)
        layout.addWidget(info)

        self.igdb_enabled_box = self._checkbox(
            "igdb",
            "Enable IGDB metadata enrichment",
            default=GENERAL_BOOL_KEYS["igdb"],
        )
        layout.addWidget(self.igdb_enabled_box)

        igdb_form = QFormLayout()
        self.igdb_client_id_edit = self._line_edit("igdb-client-id", default=STRING_KEYS["igdb-client-id"])
        self.igdb_client_secret_edit = self._line_edit(
            "igdb-client-secret",
            default=STRING_KEYS["igdb-client-secret"],
        )
        self.igdb_client_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.igdb_key_edit = self._line_edit("igdb-key", default=STRING_KEYS["igdb-key"])
        self.igdb_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        igdb_form.addRow("Client ID", self.igdb_client_id_edit)
        igdb_form.addRow("Client Secret", self.igdb_client_secret_edit)
        igdb_form.addRow("Access Token (optional)", self.igdb_key_edit)
        layout.addLayout(igdb_form)

        self.igdb_refresh_button = QPushButton("Refresh Metadata Now")
        self.igdb_refresh_button.clicked.connect(self._request_refresh)
        layout.addWidget(self.igdb_refresh_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self.igdb_client_id_edit.textChanged.connect(self._update_igdb_state)
        self.igdb_client_secret_edit.textChanged.connect(self._update_igdb_state)
        self.igdb_key_edit.textChanged.connect(self._update_igdb_state)
        self._update_igdb_state()

        layout.addStretch(1)
        return page

    def _choose_directory(self, edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Select Directory",
            edit.text().strip() or "",
        )
        if path:
            edit.setText(path)

    def _update_sgdb_state(self) -> None:
        has_key = bool(self.sgdb_key_edit.text().strip())
        self.sgdb_enabled_box.setEnabled(has_key)
        self.sgdb_prefer_box.setEnabled(has_key)
        self.sgdb_animated_box.setEnabled(has_key)
        self.sgdb_refresh_button.setEnabled(has_key)
        if not has_key:
            self.sgdb_enabled_box.setChecked(False)
            self.sgdb_prefer_box.setChecked(False)
            self.sgdb_animated_box.setChecked(False)

    def _request_refresh(self) -> None:
        # Ensure SGDB lookups are enabled for refreshes initiated from this page.
        if self.sgdb_key_edit.text().strip():
            self.sgdb_enabled_box.setChecked(True)
        if self.igdb_client_id_edit.text().strip() and (
            self.igdb_client_secret_edit.text().strip() or self.igdb_key_edit.text().strip()
        ):
            self.igdb_enabled_box.setChecked(True)
        self.refresh_requested = True
        self.accept()

    def _request_import(self) -> None:
        self.import_requested = True
        self.accept()

    def _update_igdb_state(self) -> None:
        has_client = bool(self.igdb_client_id_edit.text().strip())
        has_secret = bool(self.igdb_client_secret_edit.text().strip())
        has_token = bool(self.igdb_key_edit.text().strip())
        has_creds = has_client and (has_secret or has_token)
        self.igdb_enabled_box.setEnabled(has_creds)
        self.igdb_refresh_button.setEnabled(has_creds)
        if not has_creds:
            self.igdb_enabled_box.setChecked(False)

    def apply(self) -> None:
        for key, widget in self.bool_widgets.items():
            self.settings.set_bool(key, widget.isChecked())

        for key, widget in self.string_widgets.items():
            value = widget.text().strip()
            if key in STRING_KEYS and not value:
                value = STRING_KEYS[key]
            self.settings.set_string(key, value)

        cleaned_category_icons = {
            key.strip().casefold(): path.strip()
            for key, path in self.category_icon_paths.items()
            if key.strip() and path.strip()
        }
        cleaned_category_labels = {
            key.strip().casefold(): self._clean_category_name(label)
            for key, label in self.category_labels_by_key.items()
            if key.strip() and self._clean_category_name(label)
        }
        self.settings.set_string(
            "category-definitions",
            json.dumps(cleaned_category_labels, sort_keys=True),
        )
        self.settings.set_string(
            "category-icons",
            json.dumps(cleaned_category_icons, sort_keys=True),
        )

        sgdb_key = self.sgdb_key_edit.text().strip()
        self.settings.set_string("sgdb-key", sgdb_key)
        if not sgdb_key:
            self.settings.set_bool("sgdb", False)
            self.settings.set_bool("sgdb-prefer", False)
            self.settings.set_bool("sgdb-animated", False)

        igdb_client_id = self.igdb_client_id_edit.text().strip()
        igdb_client_secret = self.igdb_client_secret_edit.text().strip()
        igdb_token = self.igdb_key_edit.text().strip()
        if not igdb_client_id or (not igdb_client_secret and not igdb_token):
            self.settings.set_bool("igdb", False)
