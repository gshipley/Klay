from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTabWidget,
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


class PreferencesDialog(QDialog):
    def __init__(self, settings: SettingsBackend, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.bool_widgets: dict[str, QCheckBox] = {}
        self.string_widgets: dict[str, QLineEdit] = {}
        self.refresh_requested = False

        self.setWindowTitle("Preferences")
        self.setModal(True)
        self.resize(760, 560)

        root = QVBoxLayout(self)

        tabs = QTabWidget()
        root.addWidget(tabs, 1)

        tabs.addTab(self._build_general_page(), "General")
        tabs.addTab(self._build_sources_page(), "Sources")
        tabs.addTab(self._build_import_page(), "Import")
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
        layout.addStretch(1)
        return page

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
            " automatically during metadata refresh."
        )
        info.setWordWrap(True)
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
        layout.addWidget(
            self._checkbox(
                "refresh-covers-on-metadata",
                "Include cover updates during metadata refresh",
                default=GENERAL_BOOL_KEYS["refresh-covers-on-metadata"],
            )
        )

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
