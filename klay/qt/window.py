from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSettings, QSize, QThread, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsBlurEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QProgressBar,
    QSplitter,
    QStackedLayout,
    QStackedWidget,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from klay import shared
from klay.qt.library import GameEntry, GameLibrary
from klay.qt.preferences_dialog import PreferencesDialog
from klay.qt.settings import GENERAL_BOOL_KEYS, STATE_BOOL_KEYS, STATE_STRING_KEYS, SettingsBackend


ROLE_FILTER = Qt.ItemDataRole.UserRole + 1
ROLE_GAME_ID = Qt.ItemDataRole.UserRole + 2
ROLE_TITLE = Qt.ItemDataRole.UserRole + 3
ROLE_COVER_PIXMAP = Qt.ItemDataRole.UserRole + 4
ROLE_SIDEBAR_COUNT = Qt.ItemDataRole.UserRole + 5
ROLE_SIDEBAR_HEADING = Qt.ItemDataRole.UserRole + 6

COVER_SIZE = QSize(200, 300)


def _fmt_timestamp(timestamp: int) -> str:
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _normalize_name(name: str) -> str:
    normalized = name.strip().lower()
    if normalized.startswith("the "):
        return normalized[4:]
    return normalized


def _source_icon(source: str) -> QIcon:
    icon_name = {
        "all": "view-grid-symbolic",
        "imported": "list-add-symbolic",
        "steam": "steam-source-symbolic",
        "lutris": "lutris-source-symbolic",
        "heroic": "heroic-source-symbolic",
        "desktop": "user-desktop-symbolic",
        "flatpak": "flatpak-source-symbolic",
        "bottles": "bottles-source-symbolic",
        "itch": "itch-source-symbolic",
        "legendary": "legendary-source-symbolic",
        "retroarch": "retroarch-source-symbolic",
    }.get(source, "application-x-executable-symbolic")
    icon = QIcon.fromTheme(icon_name)
    if icon.isNull():
        icon = QIcon.fromTheme("application-x-executable")
    return icon


def _fit_cover(pixmap: QPixmap, target_size: QSize) -> QPixmap:
    scaled = pixmap.scaled(
        target_size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    x = max(0, (scaled.width() - target_size.width()) // 2)
    y = max(0, (scaled.height() - target_size.height()) // 2)
    return scaled.copy(x, y, target_size.width(), target_size.height())


class GameCardDelegate(QStyledItemDelegate):
    def __init__(self, window: "KlayMainWindow", parent: QWidget) -> None:
        super().__init__(parent)
        self.window = window

    def sizeHint(self, _option: QStyleOptionViewItem, _index) -> QSize:
        return QSize(216, 368)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = option.rect.adjusted(6, 6, -6, -6)
        hovered = self.window.hovered_card_row == index.row()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)

        background = QColor("#f9f9fb")
        title_background = QColor("#ffffff")
        border = QColor("#d8d8df")
        title_color = QColor("#2e2e34")
        if option.palette.window().color().lightness() < 128:
            background = QColor("#2f2f35")
            title_background = QColor("#34343b")
            border = QColor("#4b4b56")
            title_color = QColor("#f3f3f6")

        if hovered:
            border = option.palette.highlight().color()
        elif selected:
            border = option.palette.mid().color()

        shadow_rect = rect.adjusted(0, 1, 0, 1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 18))
        painter.drawRoundedRect(shadow_rect, 10, 10)

        painter.setPen(QPen(border, 1.2))
        painter.setBrush(background)
        painter.drawRoundedRect(rect, 10, 10)

        cover_rect = QRect(rect.left() + 1, rect.top() + 1, rect.width() - 2, 300)
        title_rect = QRect(
            rect.left() + 1,
            cover_rect.bottom() + 1,
            rect.width() - 2,
            rect.height() - cover_rect.height() - 2,
        )

        clip_path = QPainterPath()
        clip_path.addRoundedRect(rect, 10, 10)
        painter.setClipPath(clip_path)

        cover = index.data(ROLE_COVER_PIXMAP)
        if isinstance(cover, QPixmap):
            painter.drawPixmap(cover_rect, cover)
        painter.setClipping(False)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(title_background)
        painter.drawRect(title_rect)

        text_rect = title_rect.adjusted(11, 8, -11, -8)
        painter.setPen(title_color)
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
            str(index.data(ROLE_TITLE) or ""),
        )
        painter.restore()


class SidebarDelegate(QStyledItemDelegate):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)

    def sizeHint(self, _option: QStyleOptionViewItem, index) -> QSize:
        if index.data(ROLE_SIDEBAR_HEADING):
            return QSize(180, 26)
        return QSize(180, 36)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = option.rect.adjusted(6, 1, -6, -1)
        if index.data(ROLE_SIDEBAR_HEADING):
            heading_color = option.palette.mid().color()
            painter.setPen(heading_color)
            heading_font = QFont(option.font)
            heading_font.setBold(True)
            heading_font.setPointSize(max(8, heading_font.pointSize() - 1))
            painter.setFont(heading_font)
            painter.drawText(
                rect.adjusted(4, 8, -4, 0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
            )
            painter.restore()
            return

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            background = (
                option.palette.midlight().color()
                if selected
                else option.palette.alternateBase().color()
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(background)
            painter.drawRoundedRect(rect, 6, 6)

        icon: QIcon = index.data(Qt.ItemDataRole.DecorationRole) or QIcon()
        icon_rect = QRect(rect.left() + 8, rect.top() + 10, 16, 16)
        if not icon.isNull():
            icon.paint(painter, icon_rect)

        painter.setPen(option.palette.text().color())
        painter.drawText(
            QRect(rect.left() + 32, rect.top(), rect.width() - 76, rect.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
        )

        count = index.data(ROLE_SIDEBAR_COUNT)
        if isinstance(count, int):
            painter.setPen(option.palette.mid().color())
            painter.drawText(
                QRect(rect.right() - 34, rect.top(), 28, rect.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                str(count),
            )

        painter.restore()


@dataclass
class UndoEntry:
    game_id: str
    field: str
    previous: bool


@dataclass
class ImportSession:
    imported_ids: list[str]
    removed_ids: list[str]


class WorkerProgressDialog(QDialog):
    def __init__(self, *, title: str, message: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumSize(640, 320)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        self.message_label = QLabel(message)
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        layout.addWidget(self.progress_bar)

        self.stats_label = QLabel("Waiting for progress updates…")
        self.stats_label.setWordWrap(True)
        layout.addWidget(self.stats_label)

        self.current_item_label = QLabel("")
        self.current_item_label.setWordWrap(True)
        layout.addWidget(self.current_item_label)

        self.details_view = QPlainTextEdit()
        self.details_view.setReadOnly(True)
        self.details_view.setMaximumBlockCount(300)
        layout.addWidget(self.details_view, 1)

    def set_summary_message(self, text: str) -> None:
        self.message_label.setText(text)

    def update_stats(self, payload: dict[str, Any]) -> None:
        processed = payload.get("processed")
        total = payload.get("total")
        if isinstance(total, int) and total > 0 and isinstance(processed, int):
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(min(processed, total))
        else:
            self.progress_bar.setRange(0, 0)

        pieces: list[str] = []
        if isinstance(processed, int):
            pieces.append(f"Processed: {processed}")
        if isinstance(total, int):
            pieces.append(f"Total: {total}")
        if (remaining := payload.get("remaining")) is not None:
            pieces.append(f"Remaining: {remaining}")
        if (scanned := payload.get("scanned")) is not None:
            pieces.append(f"Scanned: {scanned}")
        if (imported := payload.get("imported")) is not None:
            pieces.append(f"Imported: {imported}")
        if (metadata_updates := payload.get("metadata_updates")) is not None:
            pieces.append(f"Metadata updates: {metadata_updates}")
        if (cover_updates := payload.get("cover_updates")) is not None:
            pieces.append(f"Cover updates: {cover_updates}")
        if (new_cover_updates := payload.get("new_cover_updates")) is not None:
            pieces.append(f"New covers: {new_cover_updates}")
        if (removed := payload.get("removed")) is not None:
            pieces.append(f"Removed: {removed}")
        if (duplicates := payload.get("duplicates")) is not None:
            pieces.append(f"Duplicates: {duplicates}")
        if (errors := payload.get("errors")) is not None:
            pieces.append(f"Errors: {errors}")

        self.stats_label.setText(" | ".join(pieces) if pieces else "Working…")

        game_name = payload.get("game_name")
        source = payload.get("source")
        skipped = payload.get("skipped")
        skip_reason = payload.get("skip_reason")
        if game_name:
            text = str(game_name)
            if source:
                text += f" ({source})"
            if skipped:
                reason = f" [{skip_reason}]" if skip_reason else ""
                text += f" - skipped{reason}"
            self.current_item_label.setText(text)

        phase = payload.get("phase")
        detail_line = ""
        if phase == "source":
            detail_line = f"Scanning source: {payload.get('source', '-')}"
        elif phase == "item" and game_name:
            detail_line = f"{game_name}"
            if source:
                detail_line += f" [{source}]"
            if skipped:
                detail_line += f" (skipped: {skip_reason})"
        elif phase == "done":
            detail_line = "Finished processing."
        elif phase == "start":
            detail_line = "Started."
        if detail_line:
            self.details_view.appendPlainText(detail_line)


class ImportWorkerThread(QThread):
    completed = Signal(dict)
    failed = Signal(str)
    progress = Signal(dict)

    def __init__(
        self,
        data_dir_name: str,
        mode: str = "import",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.data_dir_name = data_dir_name
        self.mode = mode

    def run(self) -> None:  # type: ignore[override]
        env = os.environ.copy()
        env["KLAY_DATA_DIR_NAME"] = self.data_dir_name
        env["KLAY_IMPORT_MODE"] = self.mode

        python_path_entries = [entry for entry in sys.path if entry]
        if existing := env.get("PYTHONPATH"):
            python_path_entries.extend(existing.split(os.pathsep))
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(python_path_entries))

        try:
            process = subprocess.Popen(  # noqa: S603
                [sys.executable, "-m", "klay.qt.import_worker"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                bufsize=1,
            )
        except OSError as error:
            self.failed.emit(str(error))
            return

        payload: dict[str, Any] = {}
        output_lines: list[str] = []
        stream = process.stdout
        if stream is None:
            self.failed.emit("Import worker failed to start output stream.")
            return

        for line in stream:
            text = line.strip()
            if not text:
                continue
            output_lines.append(text)
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            if parsed.get("kind") == "progress":
                self.progress.emit(parsed)
                continue
            payload = parsed

        process.wait()

        if not payload:
            for candidate in reversed(output_lines):
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict) and parsed.get("kind") != "progress":
                    payload = parsed
                    break

        if not payload:
            message = output_lines[-1] if output_lines else f"Import worker exited with code {process.returncode}"
            self.failed.emit(message)
            return

        if process.returncode != 0 and not payload.get("fatal"):
            errors = payload.setdefault("errors", [])
            if isinstance(errors, list):
                errors.append(
                    f"Import worker exited with code {process.returncode}"
                )
            else:
                payload["errors"] = [f"Import worker exited with code {process.returncode}"]

        self.completed.emit(payload)


class KlayMainWindow(QMainWindow):
    def __init__(self, library: GameLibrary, initial_search: str = "") -> None:
        super().__init__()
        self.library = library
        self.settings = QSettings("KDE", "Klay")
        self.runtime_settings = SettingsBackend(self.library.data_dir_name)
        self.initial_search = initial_search

        self.games: list[GameEntry] = []
        self.filtered: list[GameEntry] = []
        self.current_filter = "all"
        self.sort_mode = "last_played"
        self.show_hidden = False
        self.cover_cache: dict[str, QPixmap] = {}
        self.undo_stack: list[UndoEntry] = []
        self.hovered_card_row = -1
        self.active_game_id: str | None = None
        self.active_details_cover: QPixmap | None = None
        self.import_thread: ImportWorkerThread | None = None
        self.import_progress_dialog: WorkerProgressDialog | None = None
        self.last_import_session: ImportSession | None = None

        self.setWindowTitle("Klay")
        self.resize(1170, 795)

        self._build_ui()
        self._build_actions()
        self._load_state()
        self.reload_games()

        if self.initial_search:
            self.search_entry.setText(self.initial_search)
            self.search_toggle.setChecked(True)
            self.search_row.setVisible(True)

        if self.runtime_settings.get_bool("auto-import", GENERAL_BOOL_KEYS["auto-import"]):
            self.import_games(auto=True)

    def _build_ui(self) -> None:
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(self.splitter)

        self.sidebar_frame = QFrame()
        self.sidebar_frame.setObjectName("SidebarFrame")
        sidebar_layout = QVBoxLayout(self.sidebar_frame)
        sidebar_layout.setContentsMargins(8, 8, 8, 8)
        sidebar_layout.setSpacing(8)

        sidebar_header = QHBoxLayout()
        app_icon = QLabel()
        app_icon.setPixmap(QIcon.fromTheme("applications-games").pixmap(QSize(16, 16)))
        sidebar_header.addWidget(app_icon)
        app_name = QLabel("Klay")
        app_name.setObjectName("SidebarTitle")
        sidebar_header.addWidget(app_name)
        sidebar_header.addStretch(1)
        sidebar_layout.addLayout(sidebar_header)

        self.sidebar_list = QListWidget()
        self.sidebar_list.setObjectName("SidebarList")
        self.sidebar_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sidebar_list.setMouseTracking(True)
        self.sidebar_list.setItemDelegate(SidebarDelegate(self.sidebar_list))
        self.sidebar_list.currentItemChanged.connect(self._on_sidebar_changed)
        sidebar_layout.addWidget(self.sidebar_list, 1)

        self.splitter.addWidget(self.sidebar_frame)

        self.navigation_stack = QStackedWidget()
        self.library_page = self._build_library_page()
        self.details_page = self._build_details_page()
        self.navigation_stack.addWidget(self.library_page)
        self.navigation_stack.addWidget(self.details_page)
        self.splitter.addWidget(self.navigation_stack)

        self.splitter.setSizes([235, 950])
        self.setCentralWidget(central)
        self.statusBar().showMessage("Ready")
        self._apply_styles()

    def _build_library_page(self) -> QWidget:
        page = QFrame()
        main_layout = QVBoxLayout(page)
        main_layout.setContentsMargins(12, 8, 12, 8)
        main_layout.setSpacing(8)

        header = QHBoxLayout()
        self.toggle_sidebar_btn = QToolButton()
        self.toggle_sidebar_btn.setIcon(QIcon.fromTheme("sidebar-show-symbolic"))
        self.toggle_sidebar_btn.setToolTip("Toggle Sidebar")
        self.toggle_sidebar_btn.clicked.connect(self.toggle_sidebar)
        header.addWidget(self.toggle_sidebar_btn)

        self.add_menu_button = QToolButton()
        self.add_menu_button.setIcon(QIcon.fromTheme("list-add-symbolic"))
        self.add_menu_button.setToolTip("Add Game")
        self.add_menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        header.addWidget(self.add_menu_button)

        header.addStretch(1)
        self.page_title = QLabel("All Games")
        title_font = QFont(self.font())
        title_font.setBold(True)
        self.page_title.setFont(title_font)
        header.addWidget(self.page_title)
        header.addStretch(1)

        self.search_toggle = QToolButton()
        self.search_toggle.setCheckable(True)
        self.search_toggle.setIcon(QIcon.fromTheme("system-search-symbolic"))
        self.search_toggle.setToolTip("Search")
        self.search_toggle.toggled.connect(self.toggle_search_row)
        header.addWidget(self.search_toggle)

        self.main_menu_button = QToolButton()
        self.main_menu_button.setIcon(QIcon.fromTheme("open-menu-symbolic"))
        self.main_menu_button.setToolTip("Main Menu")
        self.main_menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        header.addWidget(self.main_menu_button)

        main_layout.addLayout(header)

        self.search_row = QWidget()
        search_row_layout = QHBoxLayout(self.search_row)
        search_row_layout.setContentsMargins(0, 0, 0, 0)
        search_row_layout.addStretch(1)
        self.search_entry = QLineEdit()
        self.search_entry.setPlaceholderText("Search")
        self.search_entry.setMaximumWidth(500)
        self.search_entry.textChanged.connect(self.apply_filters)
        search_row_layout.addWidget(self.search_entry)
        search_row_layout.addStretch(1)
        self.search_row.setVisible(False)
        main_layout.addWidget(self.search_row)

        self.content_stack = QStackedWidget()
        self.games_list = QListWidget()
        self.games_list.setObjectName("GamesList")
        self.games_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.games_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.games_list.setMovement(QListWidget.Movement.Static)
        self.games_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.games_list.setSpacing(12)
        self.games_list.setIconSize(COVER_SIZE)
        self.games_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.games_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.games_list.setMouseTracking(True)
        self.games_list.setItemDelegate(GameCardDelegate(self, self.games_list))
        self.games_list.viewport().installEventFilter(self)
        self.games_list.itemActivated.connect(self.activate_selected_game)
        self.games_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.games_list.customContextMenuRequested.connect(self.show_context_menu)
        self.content_stack.addWidget(self.games_list)

        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.setContentsMargins(24, 48, 24, 24)
        empty_layout.setSpacing(10)
        self.empty_title = QLabel("No Games")
        empty_title_font = QFont(self.font())
        empty_title_font.setPointSize(empty_title_font.pointSize() + 6)
        empty_title_font.setBold(True)
        self.empty_title.setFont(empty_title_font)
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_title)

        self.empty_subtitle = QLabel("Use the + button to add games")
        self.empty_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_subtitle)

        self.empty_import_button = QPushButton("Import")
        self.empty_import_button.clicked.connect(self.import_games)
        empty_layout.addWidget(self.empty_import_button, alignment=Qt.AlignmentFlag.AlignCenter)
        empty_layout.addStretch(1)
        self.content_stack.addWidget(empty_page)

        main_layout.addWidget(self.content_stack, 1)
        return page

    def _build_details_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("DetailsPage")
        stack_layout = QStackedLayout(page)
        stack_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)

        self.details_backdrop = QLabel()
        self.details_backdrop.setObjectName("DetailsBackdrop")
        self.details_backdrop.setScaledContents(True)
        blur_effect = QGraphicsBlurEffect(self.details_backdrop)
        blur_effect.setBlurRadius(40)
        self.details_backdrop.setGraphicsEffect(blur_effect)
        stack_layout.addWidget(self.details_backdrop)

        foreground = QWidget()
        stack_layout.addWidget(foreground)

        details_layout = QVBoxLayout(foreground)
        details_layout.setContentsMargins(12, 8, 12, 12)
        details_layout.setSpacing(10)

        header = QHBoxLayout()
        self.details_back_btn = QToolButton()
        self.details_back_btn.setIcon(QIcon.fromTheme("go-previous-symbolic"))
        self.details_back_btn.setToolTip("Back")
        self.details_back_btn.clicked.connect(self.show_library_page)
        header.addWidget(self.details_back_btn)
        header.addStretch(1)
        self.details_header_title = QLabel("Game Details")
        header_font = QFont(self.font())
        header_font.setBold(True)
        self.details_header_title.setFont(header_font)
        header.addWidget(self.details_header_title)
        header.addStretch(1)
        details_layout.addLayout(header)

        body_frame = QFrame()
        body_frame.setObjectName("DetailsBody")
        body_layout = QHBoxLayout(body_frame)
        body_layout.setContentsMargins(24, 24, 24, 24)
        body_layout.setSpacing(40)

        self.details_cover = QLabel()
        self.details_cover.setObjectName("DetailsCover")
        self.details_cover.setFixedSize(200, 300)
        self.details_cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body_layout.addWidget(self.details_cover, alignment=Qt.AlignmentFlag.AlignTop)

        right = QVBoxLayout()
        right.setSpacing(8)
        body_layout.addLayout(right, 1)

        self.details_title = QLabel("Game Title")
        title_font = QFont(self.font())
        title_font.setPointSize(title_font.pointSize() + 9)
        title_font.setBold(True)
        self.details_title.setFont(title_font)
        self.details_title.setWordWrap(True)
        right.addWidget(self.details_title)

        self.details_developer = QLabel("")
        dev_font = QFont(self.font())
        dev_font.setBold(True)
        self.details_developer.setFont(dev_font)
        self.details_developer.setWordWrap(True)
        right.addWidget(self.details_developer)

        dates_row = QHBoxLayout()
        self.details_added = QLabel("Added: -")
        self.details_last_played = QLabel("Last played: -")
        dates_row.addWidget(self.details_added)
        dates_row.addWidget(self.details_last_played)
        dates_row.addStretch(1)
        right.addLayout(dates_row)

        executable_heading = QLabel("Executable")
        heading_font = QFont(self.font())
        heading_font.setBold(True)
        executable_heading.setFont(heading_font)
        right.addWidget(executable_heading)

        self.details_executable = QLabel("-")
        self.details_executable.setWordWrap(True)
        self.details_executable.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        right.addWidget(self.details_executable)
        right.addStretch(1)

        button_row = QHBoxLayout()
        self.details_play_btn = QPushButton("Play")
        self.details_play_btn.clicked.connect(self.launch_active_game)
        button_row.addWidget(self.details_play_btn)

        self.details_edit_btn = QToolButton()
        self.details_edit_btn.setText("Edit")
        self.details_edit_btn.clicked.connect(self.edit_active_game)
        button_row.addWidget(self.details_edit_btn)

        self.details_hide_btn = QToolButton()
        self.details_hide_btn.clicked.connect(self.toggle_hide_active_game)
        button_row.addWidget(self.details_hide_btn)

        self.details_remove_btn = QToolButton()
        self.details_remove_btn.setText("Remove")
        self.details_remove_btn.clicked.connect(self.remove_active_game)
        button_row.addWidget(self.details_remove_btn)

        self.details_search_btn = QToolButton()
        self.details_search_btn.setText("Search")
        self.details_search_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        search_menu = QMenu(self.details_search_btn)
        for label, engine in (
            ("IGDB", "igdb"),
            ("SteamGridDB", "sgdb"),
            ("ProtonDB", "protondb"),
            ("PCGamingWiki", "pcgw"),
            ("Lutris", "lutris"),
            ("HowLongToBeat", "hltb"),
        ):
            action = search_menu.addAction(label)
            action.triggered.connect(
                lambda checked=False, selected_engine=engine: self.search_active_game(selected_engine)
            )
        self.details_search_btn.setMenu(search_menu)
        button_row.addWidget(self.details_search_btn)
        button_row.addStretch(1)
        right.addLayout(button_row)

        details_layout.addWidget(body_frame, 1)
        return page

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QFrame#SidebarFrame {
                background: palette(alternate-base);
                border-right: 1px solid palette(midlight);
            }
            QLabel#SidebarTitle {
                font-weight: 600;
            }
            QListWidget#SidebarList {
                border: none;
                background: transparent;
            }
            QListWidget#SidebarList::item {
                border-radius: 6px;
            }
            QListWidget#GamesList {
                border: none;
                background: palette(base);
                padding: 3px;
            }
            QWidget#DetailsPage {
                background: palette(base);
            }
            QLabel#DetailsBackdrop {
                background: palette(base);
            }
            QFrame#DetailsBody {
                border-radius: 10px;
                border: 1px solid palette(midlight);
                background: palette(base);
            }
            QLabel#DetailsCover {
                border-radius: 8px;
                border: 1px solid palette(midlight);
                background: palette(base);
            }
            """
        )

    def _build_actions(self) -> None:
        self.add_game_action = QAction("Add Game", self)
        self.add_game_action.setShortcut(QKeySequence.StandardKey.New)
        self.add_game_action.triggered.connect(self.add_game)
        self.addAction(self.add_game_action)

        self.import_action = QAction("Import", self)
        self.import_action.setShortcut(QKeySequence("Ctrl+I"))
        self.import_action.triggered.connect(self.import_games)
        self.addAction(self.import_action)

        add_menu = QMenu(self)
        add_menu.addAction(self.add_game_action)
        add_menu.addSeparator()
        add_menu.addAction(self.import_action)
        self.add_menu_button.setMenu(add_menu)

        self.sort_action_group = QActionGroup(self)
        self.sort_action_group.setExclusive(True)
        self.sort_actions: dict[str, QAction] = {}

        sort_menu = QMenu("Sort", self)
        for label, mode in (
            ("A-Z", "a-z"),
            ("Z-A", "z-a"),
            ("Newest", "newest"),
            ("Oldest", "oldest"),
            ("Last Played", "last_played"),
        ):
            action = sort_menu.addAction(label)
            action.setCheckable(True)
            action.setData(mode)
            action.triggered.connect(
                lambda checked=False, selected_mode=mode: self.set_sort_mode(selected_mode)
            )
            self.sort_action_group.addAction(action)
            self.sort_actions[mode] = action

        self.show_hidden_action = QAction("Show Hidden", self)
        self.show_hidden_action.setShortcut(QKeySequence("Ctrl+H"))
        self.show_hidden_action.setCheckable(True)
        self.show_hidden_action.toggled.connect(self.toggle_show_hidden)
        self.addAction(self.show_hidden_action)

        self.toggle_sidebar_action = QAction("Toggle Sidebar", self)
        self.toggle_sidebar_action.setShortcut(QKeySequence("F9"))
        self.toggle_sidebar_action.triggered.connect(self.toggle_sidebar)
        self.addAction(self.toggle_sidebar_action)

        self.toggle_search_action = QAction("Toggle Search", self)
        self.toggle_search_action.setShortcut(QKeySequence.Find)
        self.toggle_search_action.triggered.connect(
            lambda: self.search_toggle.setChecked(not self.search_toggle.isChecked())
        )
        self.addAction(self.toggle_search_action)

        self.go_to_parent_action = QAction("Back", self)
        self.go_to_parent_action.setShortcut(QKeySequence("Alt+Up"))
        self.go_to_parent_action.triggered.connect(self.show_library_page)
        self.addAction(self.go_to_parent_action)

        self.go_home_action = QAction("Home", self)
        self.go_home_action.setShortcut(QKeySequence("Alt+Home"))
        self.go_home_action.triggered.connect(self.go_home)
        self.addAction(self.go_home_action)

        self.undo_action = QAction("Undo", self)
        self.undo_action.setShortcut(QKeySequence.Undo)
        self.undo_action.triggered.connect(self.undo_last_action)
        self.addAction(self.undo_action)

        self.close_action = QAction("Close", self)
        self.close_action.setShortcut(QKeySequence.Close)
        self.close_action.triggered.connect(self.close)
        self.addAction(self.close_action)

        self.preferences_action = QAction("Preferences", self)
        self.preferences_action.setShortcut(QKeySequence("Ctrl+,"))
        self.preferences_action.triggered.connect(self.open_preferences)
        self.addAction(self.preferences_action)

        self.about_action = QAction("About", self)
        self.about_action.triggered.connect(self.show_about)

        self.reload_action = QAction("Reload Library", self)
        self.reload_action.setShortcut(QKeySequence.Refresh)
        self.reload_action.triggered.connect(self.reload_games)
        self.addAction(self.reload_action)

        menu = QMenu(self)
        menu.addMenu(sort_menu)
        menu.addAction(self.show_hidden_action)
        menu.addSeparator()
        menu.addAction(self.preferences_action)
        menu.addAction(self.about_action)
        menu.addAction(self.reload_action)
        self.main_menu_button.setMenu(menu)

        QShortcut(QKeySequence(Qt.Key.Key_Return), self).activated.connect(
            self.activate_selected_game
        )

    def _load_state(self) -> None:
        geometry = self.settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)

        sizes = self.settings.value("window/splitter_sizes")
        if isinstance(sizes, list) and len(sizes) == 2:
            self.splitter.setSizes([int(sizes[0]), int(sizes[1])])

        self.sort_mode = self.runtime_settings.get_state_string(
            "sort-mode", STATE_STRING_KEYS["sort-mode"]
        )
        if self.sort_mode not in self.sort_actions:
            self.sort_mode = STATE_STRING_KEYS["sort-mode"]
        self.sort_actions[self.sort_mode].setChecked(True)

        sidebar_visible = self.runtime_settings.get_state_bool(
            "show-sidebar", STATE_BOOL_KEYS["show-sidebar"]
        )
        self.sidebar_frame.setVisible(sidebar_visible)

        self.show_hidden = self.settings.value("state/show_hidden", False, bool)
        self.show_hidden_action.setChecked(self.show_hidden)

    def _save_state(self) -> None:
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("window/splitter_sizes", self.splitter.sizes())
        self.settings.setValue("state/show_hidden", self.show_hidden)
        self.runtime_settings.set_state_string("sort-mode", self.sort_mode)
        self.runtime_settings.set_state_bool("show-sidebar", self.sidebar_frame.isVisible())

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.import_thread is not None and self.import_thread.isRunning():
            QMessageBox.information(
                self,
                "Import In Progress",
                "Wait for the current import to finish before closing Klay.",
            )
            event.ignore()
            return

        self._save_state()
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._refresh_details_backdrop()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if watched is self.games_list.viewport():
            if event.type() == QEvent.Type.MouseMove:
                if hasattr(event, "position"):
                    point = event.position().toPoint()  # type: ignore[attr-defined]
                elif hasattr(event, "pos"):
                    point = event.pos()  # type: ignore[attr-defined]
                else:
                    point = QPoint(-1, -1)
                index = self.games_list.indexAt(point)
                row = index.row() if index.isValid() else -1
                if row != self.hovered_card_row:
                    self.hovered_card_row = row
                    self.games_list.viewport().update()
            elif event.type() == QEvent.Type.Leave:
                if self.hovered_card_row != -1:
                    self.hovered_card_row = -1
                    self.games_list.viewport().update()
        return super().eventFilter(watched, event)

    def _add_sidebar_item(
        self,
        *,
        label: str,
        count: int | None = None,
        key: str | None = None,
        heading: bool = False,
    ) -> None:
        item = QListWidgetItem(label)
        if heading:
            item.setData(ROLE_SIDEBAR_HEADING, True)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.sidebar_list.addItem(item)
            return

        item.setData(ROLE_FILTER, key)
        item.setData(ROLE_SIDEBAR_COUNT, count)
        if key:
            item.setIcon(_source_icon(key))
        self.sidebar_list.addItem(item)

    def _on_sidebar_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if not current:
            return
        filter_key = current.data(ROLE_FILTER)
        if not filter_key:
            return
        self.current_filter = str(filter_key)
        self._update_page_title()
        self.apply_filters()

    def _update_page_title(self) -> None:
        if self.show_hidden:
            self.page_title.setText("Hidden Games")
            return
        if self.current_filter == "all":
            self.page_title.setText("All Games")
        elif self.current_filter == "imported":
            self.page_title.setText("Added")
        else:
            self.page_title.setText(self.library.source_label(self.current_filter))

    def reload_games(self) -> None:
        selected_game_id = self.selected_game_id()
        self.games = self.library.load_games(
            include_removed=False,
            include_blacklisted=False,
        )
        self.cover_cache.clear()
        self.populate_sidebar()
        self.apply_filters(select_game_id=selected_game_id)

        if self.navigation_stack.currentWidget() == self.details_page:
            game = self.active_game()
            if game is None:
                self.show_library_page()
            else:
                self.open_game_details(game)

    def populate_sidebar(self) -> None:
        self.sidebar_list.blockSignals(True)
        current_filter = self.current_filter
        self.sidebar_list.clear()

        visible_games = [game for game in self.games if not game.hidden]
        added_games = [game for game in visible_games if game.base_source == "imported"]
        source_counts: dict[str, int] = {}
        for game in visible_games:
            if game.base_source == "imported":
                continue
            source_counts[game.base_source] = source_counts.get(game.base_source, 0) + 1

        self._add_sidebar_item(label="All Games", count=len(visible_games), key="all")
        if added_games:
            self._add_sidebar_item(label="Added", count=len(added_games), key="imported")
        if source_counts:
            self._add_sidebar_item(label="Imported", heading=True)
            for source, count in sorted(
                source_counts.items(),
                key=lambda row: (-row[1], self.library.source_label(row[0]).lower()),
            ):
                self._add_sidebar_item(
                    label=self.library.source_label(source),
                    count=count,
                    key=source,
                )

        row_to_select = -1
        for index in range(self.sidebar_list.count()):
            item = self.sidebar_list.item(index)
            if item.data(ROLE_FILTER) == current_filter:
                row_to_select = index
                break
        if row_to_select == -1:
            for index in range(self.sidebar_list.count()):
                if self.sidebar_list.item(index).data(ROLE_FILTER):
                    row_to_select = index
                    break
        if row_to_select >= 0:
            self.sidebar_list.setCurrentRow(row_to_select)
        self.sidebar_list.blockSignals(False)
        self._update_page_title()

    def selected_game_id(self) -> str | None:
        item = self.games_list.currentItem()
        if not item:
            return None
        game_id = item.data(ROLE_GAME_ID)
        return str(game_id) if game_id else None

    def game_by_id(self, game_id: str) -> GameEntry | None:
        for game in self.games:
            if game.game_id == game_id:
                return game
        return None

    def selected_game(self) -> GameEntry | None:
        game_id = self.selected_game_id()
        if not game_id:
            return None
        return self.game_by_id(game_id)

    def active_game(self) -> GameEntry | None:
        if not self.active_game_id:
            return None
        return self.game_by_id(self.active_game_id)

    def set_sort_mode(self, mode: str) -> None:
        if mode not in self.sort_actions:
            return
        self.sort_mode = mode
        self.sort_actions[mode].setChecked(True)
        self.runtime_settings.set_state_string("sort-mode", mode)
        self.apply_filters(select_game_id=self.selected_game_id())

    def toggle_search_row(self, show: bool) -> None:
        if self.navigation_stack.currentWidget() != self.library_page:
            self.search_toggle.setChecked(False)
            return

        self.search_row.setVisible(show)
        if show:
            self.search_entry.setFocus()
            self.search_entry.selectAll()
        else:
            self.search_entry.clear()

    def toggle_sidebar(self) -> None:
        visible = not self.sidebar_frame.isVisible()
        self.sidebar_frame.setVisible(visible)
        self.runtime_settings.set_state_bool("show-sidebar", visible)

    def toggle_show_hidden(self, show_hidden: bool) -> None:
        self.show_hidden = show_hidden
        if show_hidden:
            self.sidebar_frame.setVisible(False)
        else:
            self.sidebar_frame.setVisible(
                self.runtime_settings.get_state_bool(
                    "show-sidebar", STATE_BOOL_KEYS["show-sidebar"]
                )
            )
        self._update_page_title()
        self.show_library_page()
        self.apply_filters()

    def go_home(self) -> None:
        if self.show_hidden:
            self.show_hidden_action.setChecked(False)
        self.show_library_page()

    def show_library_page(self) -> None:
        self.navigation_stack.setCurrentWidget(self.library_page)
        self.details_backdrop.clear()
        self.active_details_cover = None
        if self.search_toggle.isChecked():
            self.search_row.setVisible(True)

    def _sorted_games(self, games: list[GameEntry]) -> list[GameEntry]:
        if self.sort_mode == "a-z":
            return sorted(games, key=lambda game: _normalize_name(game.name))
        if self.sort_mode == "z-a":
            return sorted(games, key=lambda game: _normalize_name(game.name), reverse=True)
        if self.sort_mode == "newest":
            return sorted(
                games,
                key=lambda game: (game.added, _normalize_name(game.name)),
                reverse=True,
            )
        if self.sort_mode == "oldest":
            return sorted(games, key=lambda game: (game.added, _normalize_name(game.name)))
        return sorted(
            games,
            key=lambda game: (game.last_played, _normalize_name(game.name)),
            reverse=True,
        )

    def _placeholder_cover(self) -> QPixmap:
        pixmap = QPixmap(COVER_SIZE)
        pixmap.fill(QColor("#1f1f24"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor("#4a4a55"), 2))
        painter.drawRoundedRect(2, 2, COVER_SIZE.width() - 4, COVER_SIZE.height() - 4, 10, 10)
        painter.setPen(QColor("#d6d6db"))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "NO COVER")
        painter.end()
        return pixmap

    def _game_cover(self, game: GameEntry) -> QPixmap:
        cached = self.cover_cache.get(game.game_id)
        if cached is not None:
            return cached

        pixmap = QPixmap()
        cover_candidates = []
        if cover_path := self.library.cover_path(game):
            cover_candidates.append(cover_path)
        # Try alternate suffixes if the first discovered file is unreadable.
        for suffix in ("gif", "png", "jpg", "jpeg", "webp", "tiff"):
            candidate = self.library.covers_dir / f"{game.game_id}.{suffix}"
            if not candidate.is_file() or candidate in cover_candidates:
                continue
            cover_candidates.append(candidate)

        for candidate in cover_candidates:
            if pixmap.load(str(candidate)):
                break
        if pixmap.isNull():
            pixmap = self._placeholder_cover()
        else:
            pixmap = _fit_cover(pixmap, COVER_SIZE)
        self.cover_cache[game.game_id] = pixmap
        return pixmap

    def _set_empty_state(self) -> None:
        term = self.search_entry.text().strip()
        if term:
            self.empty_title.setText("No Games Found")
            self.empty_subtitle.setText("Try a different search")
            self.empty_import_button.setVisible(False)
        elif self.show_hidden:
            self.empty_title.setText("No Hidden Games")
            self.empty_subtitle.setText("Games you hide will appear here")
            self.empty_import_button.setVisible(False)
        else:
            self.empty_title.setText("No Games")
            self.empty_subtitle.setText("Use the + button to add games")
            self.empty_import_button.setVisible(True)

        self.content_stack.setCurrentIndex(1)
        self.statusBar().showMessage("0 game(s)")

    def apply_filters(self, *, select_game_id: str | None = None) -> None:
        term = self.search_entry.text().strip().lower()

        if self.show_hidden:
            games = [game for game in self.games if game.hidden]
        else:
            games = [game for game in self.games if not game.hidden]
            if self.current_filter == "imported":
                games = [game for game in games if game.base_source == "imported"]
            elif self.current_filter != "all":
                games = [game for game in games if game.base_source == self.current_filter]

        if term:
            games = [
                game
                for game in games
                if term in game.name.lower()
                or term in game.developer.lower()
                or term in game.source.lower()
            ]

        self.filtered = self._sorted_games(games)
        self.games_list.clear()

        for game in self.filtered:
            item = QListWidgetItem()
            item.setData(ROLE_GAME_ID, game.game_id)
            item.setData(ROLE_TITLE, game.name)
            item.setData(ROLE_COVER_PIXMAP, self._game_cover(game))
            item.setToolTip(game.name)
            self.games_list.addItem(item)

        if not self.filtered:
            self._set_empty_state()
            return

        self.content_stack.setCurrentIndex(0)
        self.statusBar().showMessage(f"{len(self.filtered)} game(s)")

        if select_game_id:
            for index in range(self.games_list.count()):
                item = self.games_list.item(index)
                if item.data(ROLE_GAME_ID) == select_game_id:
                    self.games_list.setCurrentItem(item)
                    break
        elif self.games_list.count() and self.games_list.currentRow() < 0:
            self.games_list.setCurrentRow(0)

    def _show_error(self, title: str, text: str) -> None:
        QMessageBox.critical(self, title, text)

    def launch_game(self, game: GameEntry) -> None:
        try:
            self.library.launch(game)
        except OSError as error:
            self._show_error("Launch Failed", str(error))
            return
        self.statusBar().showMessage(f"Launched {game.name}")
        if self.runtime_settings.get_bool(
            "exit-after-launch", GENERAL_BOOL_KEYS["exit-after-launch"]
        ):
            self.close()
            return
        self.reload_games()

    def _push_undo(self, game: GameEntry, field: str, previous: bool) -> None:
        self.undo_stack.append(UndoEntry(game_id=game.game_id, field=field, previous=previous))
        if len(self.undo_stack) > 30:
            self.undo_stack = self.undo_stack[-30:]

    def toggle_hide_game(self, game: GameEntry) -> None:
        previous = game.hidden
        self._push_undo(game, "hidden", previous)
        self.library.set_hidden(game, not game.hidden)
        if self.navigation_stack.currentWidget() == self.details_page and game.hidden != self.show_hidden:
            self.show_library_page()
        self.reload_games()

    def remove_game(self, game: GameEntry) -> None:
        answer = QMessageBox.question(
            self,
            "Remove Game",
            f"Remove '{game.name}' from Klay?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        previous = game.removed
        self._push_undo(game, "removed", previous)
        self.library.set_removed(game, True)
        if self.navigation_stack.currentWidget() == self.details_page:
            self.show_library_page()
        self.reload_games()

    def undo_last_action(self) -> None:
        if self._undo_import_session():
            return

        if not self.undo_stack:
            self.statusBar().showMessage("Nothing to undo")
            return

        entry = self.undo_stack.pop()
        game = self.library.load_game_by_id(entry.game_id, include_removed=True)
        if game is None:
            self.statusBar().showMessage("Nothing to undo")
            return

        if entry.field == "hidden":
            self.library.set_hidden(game, entry.previous)
        elif entry.field == "removed":
            self.library.set_removed(game, entry.previous)

        self.reload_games()
        self.statusBar().showMessage("Undo complete")

    def _undo_import_session(self) -> bool:
        if self.last_import_session is None:
            return False

        changed = False
        for game_id in self.last_import_session.imported_ids:
            game = self.library.load_game_by_id(game_id, include_removed=True)
            if game is None or game.removed:
                continue
            self.library.set_removed(game, True)
            changed = True

        for game_id in self.last_import_session.removed_ids:
            game = self.library.load_game_by_id(game_id, include_removed=True)
            if game is None or not game.removed:
                continue
            self.library.set_removed(game, False)
            changed = True

        self.last_import_session = None
        if changed:
            self.reload_games()
            self.statusBar().showMessage("Last import undone")
        else:
            self.statusBar().showMessage("Nothing to undo")
        return True

    def search_web(self, game: GameEntry, engine: str) -> None:
        if not self.library.open_web_search(game, engine):
            self._show_error("Search Error", f"Unknown search engine: {engine}")

    def search_active_game(self, engine: str) -> None:
        game = self.active_game()
        if game is None:
            return
        self.search_web(game, engine)

    def _game_form_dialog(
        self,
        *,
        title: str,
        game: GameEntry | None = None,
    ) -> tuple[str, str, str, Path | None] | None:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(460)

        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        layout.addLayout(form)

        name_edit = QLineEdit(game.name if game else "")
        executable_edit = QLineEdit(game.executable_text() if game else "")
        developer_edit = QLineEdit(game.developer if game else "")
        cover_edit = QLineEdit()
        cover_edit.setReadOnly(True)

        def browse_cover() -> None:
            filename, _filter = QFileDialog.getOpenFileName(
                dialog,
                "Select Cover",
                "",
                "Images (*.png *.jpg *.jpeg *.webp *.tiff *.gif)",
            )
            if filename:
                cover_edit.setText(filename)

        cover_row = QWidget()
        cover_layout = QHBoxLayout(cover_row)
        cover_layout.setContentsMargins(0, 0, 0, 0)
        cover_layout.addWidget(cover_edit, 1)
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(browse_cover)
        cover_layout.addWidget(browse_btn)

        form.addRow("Name", name_edit)
        form.addRow("Executable", executable_edit)
        form.addRow("Developer", developer_edit)
        form.addRow("Cover", cover_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(buttons)

        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)

        def validate() -> None:
            ok_button.setEnabled(bool(name_edit.text().strip() and executable_edit.text().strip()))

        name_edit.textChanged.connect(validate)
        executable_edit.textChanged.connect(validate)
        validate()

        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None

        cover_path = Path(cover_edit.text()) if cover_edit.text().strip() else None
        return (
            name_edit.text().strip(),
            executable_edit.text().strip(),
            developer_edit.text().strip(),
            cover_path,
        )

    def add_game(self) -> None:
        result = self._game_form_dialog(title="Add Game")
        if not result:
            return
        name, executable, developer, cover_path = result
        self.library.add_manual_game(
            name=name,
            executable=executable,
            developer=developer,
            cover_source=cover_path,
        )
        self.reload_games()

    def edit_game(self, game: GameEntry) -> None:
        result = self._game_form_dialog(title="Edit Game", game=game)
        if not result:
            return
        name, executable, developer, cover_path = result
        self.library.update_manual_game(
            game,
            name=name,
            executable=executable,
            developer=developer,
            cover_source=cover_path,
        )
        self.reload_games()

    def edit_active_game(self) -> None:
        game = self.active_game()
        if game is None:
            return
        self.edit_game(game)

    def launch_active_game(self) -> None:
        game = self.active_game()
        if game is None:
            return
        self.launch_game(game)

    def toggle_hide_active_game(self) -> None:
        game = self.active_game()
        if game is None:
            return
        self.toggle_hide_game(game)

    def remove_active_game(self) -> None:
        game = self.active_game()
        if game is None:
            return
        self.remove_game(game)

    def _set_import_in_progress(self, importing: bool) -> None:
        self.import_action.setEnabled(not importing)
        self.add_game_action.setEnabled(not importing)
        self.preferences_action.setEnabled(not importing)
        self.empty_import_button.setEnabled(not importing)

    def _close_import_progress(self) -> None:
        if self.import_progress_dialog is None:
            return
        self.import_progress_dialog.close()
        self.import_progress_dialog.deleteLater()
        self.import_progress_dialog = None

    def _on_import_thread_progress(self, payload: dict[str, Any], mode: str) -> None:
        if self.import_progress_dialog is not None:
            self.import_progress_dialog.update_stats(payload)

        game_id = payload.get("game_id")
        if payload.get("cover_updated") and isinstance(game_id, str) and game_id:
            self._refresh_cover_for_game_id(game_id)

        processed = payload.get("processed")
        total = payload.get("total")
        game_name = payload.get("game_name")
        if isinstance(processed, int) and isinstance(total, int) and total > 0:
            status = f"{processed}/{total}"
            if game_name:
                status += f" - {game_name}"
            self.statusBar().showMessage(status)
        elif payload.get("phase") == "source":
            source = str(payload.get("source") or "")
            action = "Refreshing" if mode == "refresh_metadata" else "Importing"
            self.statusBar().showMessage(f"{action}: {source}")

    def _refresh_cover_for_game_id(self, game_id: str) -> None:
        self.cover_cache.pop(game_id, None)

        game = self.game_by_id(game_id)
        if game is None:
            return

        new_cover = self._game_cover(game)
        for index in range(self.games_list.count()):
            item = self.games_list.item(index)
            if item.data(ROLE_GAME_ID) != game_id:
                continue
            item.setData(ROLE_COVER_PIXMAP, new_cover)
            break
        self.games_list.viewport().update()

        if self.active_game_id == game_id and self.navigation_stack.currentWidget() == self.details_page:
            self.details_cover.setPixmap(
                new_cover.scaled(
                    self.details_cover.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.active_details_cover = new_cover
            self._refresh_details_backdrop()

    def _on_import_thread_completed(
        self,
        payload: dict[str, Any],
        auto: bool,
        mode: str,
    ) -> None:
        self._set_import_in_progress(False)
        self._close_import_progress()

        imported = int(payload.get("imported", 0) or 0)
        removed = int(payload.get("removed", 0) or 0)
        duplicates = int(payload.get("duplicates", 0) or 0)
        metadata_updates = int(payload.get("metadata_updates", 0) or 0)
        cover_updates = int(payload.get("cover_updates", 0) or 0)
        new_cover_updates = int(payload.get("new_cover_updates", 0) or 0)
        raw_errors = payload.get("errors", [])
        if not isinstance(raw_errors, list):
            raw_errors = [raw_errors]
        errors = [str(error) for error in raw_errors if str(error).strip()]

        raw_imported_ids = payload.get("imported_ids", [])
        if not isinstance(raw_imported_ids, list):
            raw_imported_ids = [raw_imported_ids]
        imported_ids = [
            str(game_id)
            for game_id in raw_imported_ids
            if str(game_id).strip()
        ]
        raw_removed_ids = payload.get("removed_ids", [])
        if not isinstance(raw_removed_ids, list):
            raw_removed_ids = [raw_removed_ids]
        removed_ids = [str(game_id) for game_id in raw_removed_ids if str(game_id).strip()]
        if mode == "import" and (imported_ids or removed_ids):
            self.last_import_session = ImportSession(
                imported_ids=imported_ids,
                removed_ids=removed_ids,
            )
        else:
            self.last_import_session = None

        self.reload_games()

        if payload.get("fatal"):
            error_text = errors[0] if errors else "Import worker failed."
            title = "Refresh Failed" if mode == "refresh_metadata" else "Import Failed"
            self._show_error(title, error_text)
            self.statusBar().showMessage("Refresh failed" if mode == "refresh_metadata" else "Import failed")
            return

        if mode == "refresh_metadata":
            if metadata_updates == 0 and cover_updates == 0:
                summary = "No metadata or cover changes"
            else:
                parts: list[str] = []
                if metadata_updates:
                    parts.append(f"{metadata_updates} metadata updates")
                if cover_updates:
                    parts.append(f"{cover_updates} cover updates")
                if new_cover_updates:
                    parts.append(f"{new_cover_updates} new covers")
                summary = ", ".join(parts)
        elif imported == 0 and removed == 0:
            summary = "No new games found"
        else:
            parts: list[str] = []
            if imported:
                parts.append(f"{imported} imported")
            if removed:
                parts.append(f"{removed} removed")
            if duplicates:
                parts.append(f"{duplicates} duplicates")
            if cover_updates:
                parts.append(f"{cover_updates} cover updates")
            if new_cover_updates:
                parts.append(f"{new_cover_updates} new covers")
            summary = ", ".join(parts) if parts else "Import finished"

        self.statusBar().showMessage(summary)

        if errors:
            message = summary + "\n\n" + "\n".join(errors[:8])
            if len(errors) > 8:
                message += f"\n... and {len(errors) - 8} more"
            if auto:
                self.statusBar().showMessage(f"Auto import warnings: {summary}")
                return
            title = (
                "Refresh Finished With Warnings"
                if mode == "refresh_metadata"
                else "Import Finished With Warnings"
            )
            QMessageBox.warning(self, title, message)
            return

        if not auto:
            title = "Refresh Complete" if mode == "refresh_metadata" else "Import Complete"
            QMessageBox.information(self, title, summary)

    def _on_import_thread_failed(self, message: str, auto: bool, mode: str) -> None:
        self._set_import_in_progress(False)
        self._close_import_progress()
        self.statusBar().showMessage("Refresh failed" if mode == "refresh_metadata" else "Import failed")
        title = "Refresh Failed" if mode == "refresh_metadata" else "Import Failed"
        if auto:
            self._show_error("Auto Import Failed", message)
            return
        self._show_error(title, message)

    def _on_import_thread_finished(self) -> None:
        self._set_import_in_progress(False)
        self._close_import_progress()
        if self.import_thread is not None:
            self.import_thread.deleteLater()
        self.import_thread = None

    def _run_worker(self, *, mode: str, auto: bool) -> None:
        if self.import_thread is not None and self.import_thread.isRunning():
            self.statusBar().showMessage("A background task is already running")
            return

        self._set_import_in_progress(True)
        progress_text = (
            "Refreshing metadata and covers…"
            if mode == "refresh_metadata"
            else "Importing games…"
        )
        progress_title = "Refresh Metadata" if mode == "refresh_metadata" else "Import"
        self.import_progress_dialog = WorkerProgressDialog(
            title=progress_title,
            message=progress_text,
            parent=self,
        )
        self.import_progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.import_progress_dialog.show()

        self.import_thread = ImportWorkerThread(self.library.data_dir_name, mode=mode, parent=self)
        self.import_thread.progress.connect(
            lambda payload, worker_mode=mode: self._on_import_thread_progress(
                payload, worker_mode
            )
        )
        self.import_thread.completed.connect(
            lambda payload, auto_import=auto, worker_mode=mode: self._on_import_thread_completed(
                payload, auto_import, worker_mode
            )
        )
        self.import_thread.failed.connect(
            lambda message, auto_import=auto, worker_mode=mode: self._on_import_thread_failed(
                message, auto_import, worker_mode
            )
        )
        self.import_thread.finished.connect(self._on_import_thread_finished)
        self.import_thread.start()

    def import_games(self, _checked: bool = False, *, auto: bool = False) -> None:
        self._run_worker(mode="import", auto=auto)

    def refresh_metadata_from_sgdb(self) -> None:
        self._run_worker(mode="refresh_metadata", auto=False)

    def open_preferences(self) -> None:
        dialog = PreferencesDialog(self.runtime_settings, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        dialog.apply()
        self.apply_filters(select_game_id=self.selected_game_id())
        if dialog.refresh_requested:
            self.refresh_metadata_from_sgdb()
            return
        self.statusBar().showMessage("Preferences updated")

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            "About Klay",
            (
                "Klay\n"
                f"Version {shared.VERSION}\n\n"
                "A KDE-focused standalone fork of Cartridges.\n\n"
                "License: GPL-3.0-or-later"
            ),
        )

    def activate_selected_game(self) -> None:
        if self.navigation_stack.currentWidget() != self.library_page:
            return
        game = self.selected_game()
        if game is None:
            return
        if self.runtime_settings.get_bool(
            "cover-launches-game", GENERAL_BOOL_KEYS["cover-launches-game"]
        ):
            self.launch_game(game)
            return
        self.open_game_details(game)

    def open_selected_details(self) -> None:
        if self.navigation_stack.currentWidget() != self.library_page:
            return
        game = self.selected_game()
        if game is None:
            return
        self.open_game_details(game)

    def _refresh_details_backdrop(self) -> None:
        if not self.active_details_cover:
            return
        target_size = self.details_page.size()
        if target_size.width() <= 0 or target_size.height() <= 0:
            return

        pixmap = self.active_details_cover.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = max(0, (pixmap.width() - target_size.width()) // 2)
        y = max(0, (pixmap.height() - target_size.height()) // 2)
        pixmap = pixmap.copy(x, y, target_size.width(), target_size.height())

        overlay = QPixmap(pixmap.size())
        overlay.fill(Qt.GlobalColor.transparent)
        painter = QPainter(overlay)
        painter.drawPixmap(0, 0, pixmap)
        painter.fillRect(overlay.rect(), QColor(14, 14, 18, 148))
        painter.end()

        self.details_backdrop.setPixmap(overlay)

    def open_game_details(self, game: GameEntry) -> None:
        self.active_game_id = game.game_id

        self.details_header_title.setText(game.name)
        self.details_title.setText(game.name)
        self.details_developer.setText(game.developer or "")
        self.details_developer.setVisible(bool(game.developer))
        self.details_added.setText(f"Added: {_fmt_timestamp(game.added)}")
        self.details_last_played.setText(f"Last played: {_fmt_timestamp(game.last_played)}")
        self.details_executable.setText(game.executable_text() or "-")
        self.details_hide_btn.setText("Unhide" if game.hidden else "Hide")

        cover = self._game_cover(game)
        self.details_cover.setPixmap(
            cover.scaled(
                self.details_cover.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

        self.active_details_cover = cover
        self._refresh_details_backdrop()
        self.navigation_stack.setCurrentWidget(self.details_page)
        self.search_row.setVisible(False)

    def show_context_menu(self, position: QPoint) -> None:
        item = self.games_list.itemAt(position)
        if item is None:
            return

        game = self.game_by_id(str(item.data(ROLE_GAME_ID)))
        if game is None:
            return

        menu = QMenu(self)
        menu.addAction("Play", lambda: self.launch_game(game))
        menu.addAction("Details", lambda: self.open_game_details(game))
        menu.addAction("Edit", lambda: self.edit_game(game))
        menu.addAction("Unhide" if game.hidden else "Hide", lambda: self.toggle_hide_game(game))
        menu.addAction("Remove", lambda: self.remove_game(game))

        search_menu = menu.addMenu("Search on...")
        for label, engine in (
            ("IGDB", "igdb"),
            ("SteamGridDB", "sgdb"),
            ("ProtonDB", "protondb"),
            ("PCGamingWiki", "pcgw"),
            ("Lutris", "lutris"),
            ("HowLongToBeat", "hltb"),
        ):
            search_menu.addAction(
                label,
                lambda checked=False, selected_engine=engine: self.search_web(game, selected_engine),
            )

        menu.exec(self.games_list.mapToGlobal(position))
