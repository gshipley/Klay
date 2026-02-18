from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from html import escape
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSettings, QSize, QThread, QTimer, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QImageReader,
    QKeySequence,
    QMovie,
    QPalette,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
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
    QSizePolicy,
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
ROLE_PLAYTIME = Qt.ItemDataRole.UserRole + 7

COVER_SIZE = QSize(200, 300)
CATEGORY_FILTER_PREFIX = "category:"


def _fmt_timestamp(timestamp: int) -> str:
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def _fmt_playtime_minutes(minutes: int | None) -> str:
    if minutes is None or minutes <= 0:
        return ""
    days, rem_minutes = divmod(int(minutes), 60 * 24)
    hours, rem_minutes = divmod(rem_minutes, 60)
    if days:
        if hours:
            return f"Played: {days}d {hours}h"
        return f"Played: {days}d"
    if hours:
        if rem_minutes:
            return f"Played: {hours}h {rem_minutes}m"
        return f"Played: {hours}h"
    return f"Played: {rem_minutes}m"


def _normalize_name(name: str) -> str:
    normalized = name.strip().lower()
    if normalized.startswith("the "):
        return normalized[4:]
    return normalized


def _clean_category_name(value: str) -> str:
    return " ".join(value.split()).strip()


def _category_filter_key(category_name: str) -> str:
    return f"{CATEGORY_FILTER_PREFIX}{category_name.casefold()}"


def _category_from_filter_key(filter_key: str) -> str | None:
    if not filter_key.startswith(CATEGORY_FILTER_PREFIX):
        return None
    name = filter_key[len(CATEGORY_FILTER_PREFIX) :]
    return name or None


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _asset_path(*segments: str) -> Path:
    pkgdatadir = getattr(shared, "PKGDATADIR", "")
    if pkgdatadir:
        candidate = Path(pkgdatadir).joinpath("assets", *segments)
        if candidate.exists():
            return candidate
    return _project_root().joinpath("assets", *segments)


def _source_icon(source: str) -> QIcon:
    if source.startswith(CATEGORY_FILTER_PREFIX):
        icon = QIcon.fromTheme("tag-symbolic")
        if not icon.isNull():
            return icon

    source_icon_candidates = {
        "steam": ["steam_logo.png"],
        "lutris": ["lutris.svg"],
        "heroic": ["heroic.webp"],
        "flatpak": ["flatpak-logo.png"],
    }.get(source, [])
    source_icon_candidates.extend(
        [
            f"{source}_logo.png",
            f"{source}_logo.svg",
            f"{source}_logo.webp",
            f"{source}_logo.jpg",
            f"{source}_logo.jpeg",
            f"{source}-logo.png",
            f"{source}-logo.svg",
            f"{source}-logo.webp",
            f"{source}-logo.jpg",
            f"{source}-logo.jpeg",
        ]
    )
    for icon_name in dict.fromkeys(source_icon_candidates):
        candidates = (
            _asset_path("images", icon_name),
            _project_root() / icon_name,
        )
        for candidate in candidates:
            if candidate.is_file():
                custom_icon = QIcon(str(candidate))
                if not custom_icon.isNull():
                    return custom_icon

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


def _app_icon() -> QIcon:
    icon = QIcon.fromTheme(shared.APP_ID)
    if not icon.isNull():
        return icon
    icon = QIcon.fromTheme("com.grantshipley.Klay")
    if not icon.isNull():
        return icon
    for candidate in (_asset_path("images", "Klay.png"), _project_root() / "Klay.png"):
        if candidate.is_file():
            return QIcon(str(candidate))
    return QIcon.fromTheme("applications-games")


def _fit_cover(pixmap: QPixmap, target_size: QSize) -> QPixmap:
    scaled = pixmap.scaled(
        target_size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    x = max(0, (scaled.width() - target_size.width()) // 2)
    y = max(0, (scaled.height() - target_size.height()) // 2)
    return scaled.copy(x, y, target_size.width(), target_size.height())


def _link_html(url: str, *, color: str = "#8fd2ff") -> str:
    safe = escape(url.strip(), quote=True)
    if not safe:
        return ""
    return (
        f'<a href="{safe}" '
        f'style="color: {color}; text-decoration: underline; font-weight: 500;">'
        f"{safe}</a>"
    )


class GameCardDelegate(QStyledItemDelegate):
    OUTER_MARGIN = 6
    INNER_BORDER = 1
    TEXT_PAD_H = 11
    TEXT_PAD_V = 8
    BASE_CARD_WIDTH = 216
    BASE_COVER_HEIGHT = 300
    MIN_CARD_HEIGHT = 368
    MIN_COVER_HEIGHT = 140
    MIN_TITLE_PANEL_HEIGHT = 56

    def __init__(self, window: "KlayMainWindow", parent: QWidget) -> None:
        super().__init__(parent)
        self.window = window

    @staticmethod
    def _meta_font(base_font: QFont) -> QFont:
        meta_font = QFont(base_font)
        point_size = meta_font.pointSizeF()
        if point_size > 0:
            meta_font.setPointSizeF(max(8.0, point_size - 0.8))
        return meta_font

    def _title_panel_height(
        self,
        option: QStyleOptionViewItem,
        index,
        *,
        text_width: int,
    ) -> tuple[int, int]:
        title = str(index.data(ROLE_TITLE) or "")
        playtime = str(index.data(ROLE_PLAYTIME) or "").strip()
        metrics = option.fontMetrics
        title_bounds = metrics.boundingRect(
            QRect(0, 0, max(1, text_width), 10_000),
            Qt.TextFlag.TextWordWrap,
            title,
        )
        title_height = max(metrics.lineSpacing(), title_bounds.height())

        meta_height = 0
        if playtime:
            meta_height = QFontMetrics(self._meta_font(option.font)).height()

        gap = max(4, metrics.lineSpacing() // 5) if meta_height else 0
        panel_height = max(
            self.MIN_TITLE_PANEL_HEIGHT,
            self.TEXT_PAD_V * 2 + title_height + gap + meta_height,
        )
        return panel_height, meta_height

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        inner_width = (
            self.BASE_CARD_WIDTH - self.OUTER_MARGIN * 2 - self.INNER_BORDER * 2
        )
        text_width = inner_width - self.TEXT_PAD_H * 2
        title_panel_height, _meta_height = self._title_panel_height(
            option, index, text_width=text_width
        )
        card_height = max(
            self.MIN_CARD_HEIGHT,
            self.BASE_COVER_HEIGHT
            + title_panel_height
            + self.OUTER_MARGIN * 2
            + self.INNER_BORDER * 2,
        )
        return QSize(self.BASE_CARD_WIDTH, card_height)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = option.rect.adjusted(
            self.OUTER_MARGIN,
            self.OUTER_MARGIN,
            -self.OUTER_MARGIN,
            -self.OUTER_MARGIN,
        )
        hovered = self.window.hovered_card_row == index.row()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)

        background = QColor("#f9f9fb")
        title_background = QColor("#ffffff")
        border = QColor("#d8d8df")
        title_color = QColor("#2e2e34")
        meta_color = QColor("#565663")
        if option.palette.window().color().lightness() < 128:
            background = QColor("#2f2f35")
            title_background = QColor("#34343b")
            border = QColor("#4b4b56")
            title_color = QColor("#f3f3f6")
            meta_color = QColor("#c4c4ce")

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

        inner_rect = rect.adjusted(
            self.INNER_BORDER,
            self.INNER_BORDER,
            -self.INNER_BORDER,
            -self.INNER_BORDER,
        )
        text_width = max(1, inner_rect.width() - self.TEXT_PAD_H * 2)
        title_panel_height, _meta_height = self._title_panel_height(
            option, index, text_width=text_width
        )
        max_title_panel_height = max(
            self.MIN_TITLE_PANEL_HEIGHT, inner_rect.height() - self.MIN_COVER_HEIGHT
        )
        title_panel_height = min(title_panel_height, max_title_panel_height)
        cover_height = max(self.MIN_COVER_HEIGHT, inner_rect.height() - title_panel_height)
        cover_rect = QRect(
            inner_rect.left(),
            inner_rect.top(),
            inner_rect.width(),
            cover_height,
        )
        title_rect = QRect(
            inner_rect.left(),
            inner_rect.top() + cover_height,
            inner_rect.width(),
            inner_rect.height() - cover_height,
        )

        clip_path = QPainterPath()
        clip_path.addRoundedRect(rect, 10, 10)
        painter.setClipPath(clip_path)

        cover = index.data(ROLE_COVER_PIXMAP)
        if isinstance(cover, QPixmap):
            painter.drawPixmap(cover_rect, cover)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(title_background)
        painter.drawRect(title_rect)
        painter.setPen(QPen(border, 1))
        painter.drawLine(
            title_rect.left(),
            title_rect.top(),
            title_rect.right(),
            title_rect.top(),
        )
        painter.setClipping(False)

        text_rect = title_rect.adjusted(
            self.TEXT_PAD_H,
            self.TEXT_PAD_V,
            -self.TEXT_PAD_H,
            -self.TEXT_PAD_V,
        )
        title_text = str(index.data(ROLE_TITLE) or "")
        playtime_text = str(index.data(ROLE_PLAYTIME) or "")
        if playtime_text:
            meta_font = self._meta_font(option.font)
            meta_metrics = QFontMetrics(meta_font)
            gap = max(4, option.fontMetrics.lineSpacing() // 5)
            title_text_rect = QRect(
                text_rect.left(),
                text_rect.top(),
                text_rect.width(),
                max(0, text_rect.height() - meta_metrics.height() - gap),
            )
            painter.setPen(title_color)
            painter.drawText(
                title_text_rect,
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                title_text,
            )
            painter.setFont(meta_font)
            painter.setPen(meta_color)
            playtime_rect = QRect(
                text_rect.left(),
                text_rect.bottom() - meta_metrics.height() + 1,
                text_rect.width(),
                meta_metrics.height(),
            )
            painter.drawText(
                playtime_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                playtime_text,
            )
            painter.setFont(option.font)
        else:
            painter.setPen(title_color)
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
                title_text,
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
    cancel_requested = Signal()

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

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        layout.addWidget(self.cancel_button, alignment=Qt.AlignmentFlag.AlignRight)

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
        fast_mode: bool = False,
        startup_auto: bool = False,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.data_dir_name = data_dir_name
        self.mode = mode
        self.fast_mode = fast_mode
        self.startup_auto = startup_auto
        self._cancel_requested = False
        self._process: subprocess.Popen[str] | None = None

    def stop(self) -> None:
        self._cancel_requested = True
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            process.kill()

    def run(self) -> None:  # type: ignore[override]
        env = os.environ.copy()
        env["KLAY_DATA_DIR_NAME"] = self.data_dir_name
        env["KLAY_IMPORT_MODE"] = self.mode
        env["KLAY_IMPORT_FAST"] = "1" if self.fast_mode else "0"
        env["KLAY_IMPORT_STARTUP_AUTO"] = "1" if self.startup_auto else "0"

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
        self._process = process

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
        self._process = None

        if self._cancel_requested:
            self.completed.emit({"canceled": True})
            return

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
        self.category_definitions = self._load_category_definitions()
        self.category_icon_paths = self._load_category_icon_paths()
        self.initial_search = initial_search
        app = QApplication.instance()
        self._default_palette = QPalette(app.palette()) if app is not None else QPalette()

        self.games: list[GameEntry] = []
        self.filtered: list[GameEntry] = []
        self.current_filter = "all"
        self.sort_mode = "last_played"
        self.show_hidden = False
        self.cover_cache: dict[str, QPixmap] = {}
        self.cover_movies: dict[str, QMovie] = {}
        self.cover_search_cache: dict[
            tuple[str, str, str, bool, str],
            tuple[float, list[dict[str, Any]]],
        ] = {}
        self.undo_stack: list[UndoEntry] = []
        self.hovered_card_row = -1
        self.active_game_id: str | None = None
        self.active_details_cover: QPixmap | None = None
        self.details_cover_movie: QMovie | None = None
        self.import_thread: ImportWorkerThread | None = None
        self.import_progress_dialog: WorkerProgressDialog | None = None
        self.last_import_session: ImportSession | None = None
        self._close_after_import_cancel = False

        self.setWindowTitle("Klay")
        icon = _app_icon()
        if app is not None and app.windowIcon().isNull() and not icon.isNull():
            app.setWindowIcon(icon)
        self.setWindowIcon(icon)
        self.resize(1170, 795)

        self._apply_color_mode()
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
        app_icon.setPixmap(_app_icon().pixmap(QSize(16, 16)))
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
        self.games_list.itemClicked.connect(lambda _item: self.open_selected_details())
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
        self.details_backdrop.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        blur_effect = QGraphicsBlurEffect(self.details_backdrop)
        blur_effect.setBlurRadius(40)
        self.details_backdrop.setGraphicsEffect(blur_effect)
        stack_layout.addWidget(self.details_backdrop)

        foreground = QWidget()
        self.details_foreground = foreground
        foreground.installEventFilter(self)
        stack_layout.addWidget(foreground)
        stack_layout.setCurrentWidget(foreground)

        details_layout = QVBoxLayout(foreground)
        details_layout.setContentsMargins(18, 14, 18, 18)
        details_layout.setSpacing(12)

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

        details_layout.addStretch(1)

        body_row = QHBoxLayout()
        body_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body_row.addStretch(1)
        body_frame = QFrame()
        self.details_body_frame = body_frame
        body_frame.setObjectName("DetailsBody")
        body_frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        body_frame.setMaximumWidth(980)
        body_layout = QHBoxLayout(body_frame)
        body_layout.setContentsMargins(26, 24, 26, 24)
        body_layout.setSpacing(28)
        body_row.addWidget(body_frame, 0, Qt.AlignmentFlag.AlignCenter)
        body_row.addStretch(1)
        details_layout.addLayout(body_row, 0)
        details_layout.addStretch(1)

        self.details_cover = QLabel()
        self.details_cover.setObjectName("DetailsCover")
        self.details_cover.setMinimumSize(180, 270)
        self.details_cover.setMaximumSize(220, 330)
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
        self.details_title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        right.addWidget(self.details_title)

        self.details_developer = QLabel("")
        dev_font = QFont(self.font())
        dev_font.setBold(True)
        self.details_developer.setFont(dev_font)
        self.details_developer.setWordWrap(True)
        right.addWidget(self.details_developer)

        self.details_publisher = QLabel("")
        self.details_publisher.setWordWrap(True)
        right.addWidget(self.details_publisher)

        self.details_genres = QLabel("")
        self.details_genres.setWordWrap(True)
        right.addWidget(self.details_genres)

        self.details_categories = QLabel("")
        self.details_categories.setWordWrap(True)
        right.addWidget(self.details_categories)

        self.details_platforms = QLabel("")
        self.details_platforms.setWordWrap(True)
        right.addWidget(self.details_platforms)

        self.details_release_date = QLabel("")
        self.details_release_date.setWordWrap(True)
        right.addWidget(self.details_release_date)

        self.details_summary = QLabel("")
        self.details_summary.setWordWrap(True)
        self.details_summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        right.addWidget(self.details_summary)

        self.details_metacritic = QLabel("")
        self.details_metacritic.setWordWrap(True)
        right.addWidget(self.details_metacritic)

        self.details_igdb_rating = QLabel("")
        self.details_igdb_rating.setWordWrap(True)
        right.addWidget(self.details_igdb_rating)

        self.details_igdb_url = QLabel("")
        self.details_igdb_url.setOpenExternalLinks(True)
        self.details_igdb_url.setWordWrap(True)
        right.addWidget(self.details_igdb_url)

        self.details_website = QLabel("")
        self.details_website.setOpenExternalLinks(True)
        self.details_website.setWordWrap(True)
        right.addWidget(self.details_website)

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

        button_row = QHBoxLayout()
        self.details_play_btn = QPushButton("Play")
        self.details_play_btn.clicked.connect(self.launch_active_game)
        button_row.addWidget(self.details_play_btn)

        self.details_edit_btn = QToolButton()
        self.details_edit_btn.setText("Edit")
        self.details_edit_btn.clicked.connect(self.edit_active_game)
        button_row.addWidget(self.details_edit_btn)

        self.details_categories_btn = QToolButton()
        self.details_categories_btn.setText("Categories")
        self.details_categories_btn.clicked.connect(self.edit_active_game_categories)
        button_row.addWidget(self.details_categories_btn)

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

        self.details_refresh_metadata_btn = QToolButton()
        self.details_refresh_metadata_btn.setText("Refresh Metadata")
        self.details_refresh_metadata_btn.clicked.connect(
            self.refresh_active_game_metadata_from_igdb
        )
        button_row.addWidget(self.details_refresh_metadata_btn)

        self.details_cover_picker_btn = QToolButton()
        self.details_cover_picker_btn.setText("Change Cover")
        self.details_cover_picker_btn.clicked.connect(self.choose_cover_for_active_game)
        button_row.addWidget(self.details_cover_picker_btn)

        button_row.addStretch(1)
        right.addLayout(button_row)
        return page

    def _build_dark_palette(self) -> QPalette:
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(33, 35, 41))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(239, 240, 241))
        palette.setColor(QPalette.ColorRole.Base, QColor(24, 26, 31))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(40, 43, 51))
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(30, 32, 36))
        palette.setColor(QPalette.ColorRole.ToolTipText, QColor(239, 240, 241))
        palette.setColor(QPalette.ColorRole.Text, QColor(239, 240, 241))
        palette.setColor(QPalette.ColorRole.Button, QColor(49, 54, 62))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(239, 240, 241))
        palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 90, 90))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(61, 174, 233))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(20, 20, 20))
        palette.setColor(QPalette.ColorRole.Mid, QColor(77, 82, 92))
        palette.setColor(QPalette.ColorRole.Midlight, QColor(97, 103, 116))
        palette.setColor(QPalette.ColorRole.Shadow, QColor(8, 8, 9))
        palette.setColor(QPalette.ColorRole.Link, QColor(116, 183, 255))
        palette.setColor(QPalette.ColorRole.LinkVisited, QColor(177, 142, 255))

        disabled_text = QColor(144, 148, 156)
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_text)
        palette.setColor(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled_text
        )
        palette.setColor(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled_text
        )
        palette.setColor(
            QPalette.ColorGroup.Disabled,
            QPalette.ColorRole.HighlightedText,
            QColor(80, 84, 92),
        )
        return palette

    def _apply_color_mode(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        dark_mode = self.runtime_settings.get_bool(
            "dark-mode", GENERAL_BOOL_KEYS["dark-mode"]
        )
        if dark_mode:
            app.setPalette(self._build_dark_palette())
        else:
            app.setPalette(QPalette(self._default_palette))
        app.setStyleSheet("")

        if hasattr(self, "sidebar_frame"):
            self._apply_styles()
            if hasattr(self, "games_list"):
                self.games_list.viewport().update()
            if hasattr(self, "sidebar_list"):
                self.sidebar_list.viewport().update()
            self.update()

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
                border-radius: 14px;
                border: 1px solid rgba(255, 255, 255, 28);
                background: rgba(124, 123, 117, 214);
            }
            QLabel#DetailsCover {
                border-radius: 8px;
                border: 1px solid rgba(255, 255, 255, 32);
                background: rgba(14, 15, 18, 220);
            }
            QFrame#DetailsBody QLabel {
                color: #f2f3f5;
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
            answer = QMessageBox.question(
                self,
                "Import In Progress",
                "An import is still running. Cancel it and close Klay?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._cancel_import(close_after=True)
            event.ignore()
            return

        self._save_state()
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._refresh_details_backdrop()
        self._update_details_card_size()

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
        elif watched is self.details_foreground:
            if (
                event.type() == QEvent.Type.MouseButtonPress
                and self.navigation_stack.currentWidget() == self.details_page
            ):
                if hasattr(event, "position"):
                    point = event.position().toPoint()  # type: ignore[attr-defined]
                elif hasattr(event, "pos"):
                    point = event.pos()  # type: ignore[attr-defined]
                else:
                    point = QPoint(-1, -1)

                if self.details_body_frame.geometry().contains(point):
                    return super().eventFilter(watched, event)

                self.show_library_page()
                return True
        return super().eventFilter(watched, event)

    def _load_category_icon_paths(self) -> dict[str, str]:
        raw_mapping = self.runtime_settings.get_string("category-icons", "").strip()
        if not raw_mapping:
            return {}
        try:
            parsed = json.loads(raw_mapping)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}

        icon_paths: dict[str, str] = {}
        for key, value in parsed.items():
            category_key = _clean_category_name(str(key)).casefold()
            icon_path = str(value).strip()
            if not category_key or not icon_path:
                continue
            icon_paths[category_key] = icon_path
        return icon_paths

    def _load_category_definitions(self) -> dict[str, str]:
        raw_mapping = self.runtime_settings.get_string("category-definitions", "").strip()
        if not raw_mapping:
            return {}
        try:
            parsed = json.loads(raw_mapping)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}

        labels: dict[str, str] = {}
        for key, value in parsed.items():
            category_label = _clean_category_name(str(value))
            category_key = _clean_category_name(str(key)).casefold()
            if not category_key or not category_label:
                continue
            labels[category_key] = category_label
        return labels

    def _save_category_definitions(self, labels: dict[str, str]) -> None:
        cleaned = {
            _clean_category_name(key).casefold(): _clean_category_name(value)
            for key, value in labels.items()
            if _clean_category_name(key) and _clean_category_name(value)
        }
        self.runtime_settings.set_string("category-definitions", json.dumps(cleaned, sort_keys=True))
        self.category_definitions = cleaned

    def _category_sidebar_icon(self, category_key: str) -> QIcon:
        if icon_path := self.category_icon_paths.get(category_key, "").strip():
            icon = QIcon(icon_path)
            if not icon.isNull():
                return icon
        return _source_icon(_category_filter_key(category_key))

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
            if (category_key := _category_from_filter_key(key)) is not None:
                item.setIcon(self._category_sidebar_icon(category_key))
            else:
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
        elif (category_key := _category_from_filter_key(self.current_filter)) is not None:
            category_name = self._category_labels_by_key().get(category_key, category_key)
            self.page_title.setText(f"Category: {category_name}")
        else:
            self.page_title.setText(self.library.source_label(self.current_filter))

    def reload_games(self) -> None:
        selected_game_id = self.selected_game_id()
        self._stop_cover_movies()
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
        category_labels = self._category_labels_by_key(visible_games)
        category_counts: dict[str, int] = {}
        for game in visible_games:
            for category in game.categories:
                key = _clean_category_name(category).casefold()
                if not key:
                    continue
                category_counts[key] = category_counts.get(key, 0) + 1

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
        if category_counts:
            self._add_sidebar_item(label="Categories", heading=True)
            for category_key in sorted(
                category_counts, key=lambda key: category_labels.get(key, key).casefold()
            ):
                self._add_sidebar_item(
                    label=category_labels.get(category_key, category_key),
                    count=category_counts[category_key],
                    key=_category_filter_key(category_key),
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

    def _category_labels_by_key(self, games: list[GameEntry] | None = None) -> dict[str, str]:
        by_key: dict[str, str] = dict(self.category_definitions)
        for game in games or self.games:
            for category in game.categories:
                cleaned = _clean_category_name(category)
                if not cleaned:
                    continue
                key = cleaned.casefold()
                if key not in by_key:
                    by_key[key] = cleaned
        return by_key

    def _normalize_categories(self, categories: list[str]) -> list[str]:
        by_key: dict[str, str] = {}
        for category in categories:
            cleaned = _clean_category_name(category)
            if not cleaned:
                continue
            key = cleaned.casefold()
            if key not in by_key:
                by_key[key] = cleaned
        return [by_key[key] for key in sorted(by_key.keys())]

    def _set_game_categories(self, game: GameEntry, categories: list[str]) -> bool:
        normalized = self._normalize_categories(categories)
        if normalized == self._normalize_categories(game.categories):
            return False
        game.set_value("categories", normalized)
        self.library.save_game(game)
        return True

    @staticmethod
    def _resolve_category_redirect(key: str, redirects: dict[str, str]) -> str:
        current = key
        seen = {current}
        while current in redirects:
            next_key = redirects[current]
            if not next_key or next_key in seen:
                break
            seen.add(next_key)
            current = next_key
        return current

    def _apply_category_definition_changes(
        self,
        *,
        labels_by_key: dict[str, str],
        redirects: dict[str, str],
        deleted_keys: set[str],
    ) -> int:
        if not self.games:
            return 0

        normalized_labels = {
            _clean_category_name(key).casefold(): _clean_category_name(label)
            for key, label in labels_by_key.items()
            if _clean_category_name(key) and _clean_category_name(label)
        }
        normalized_redirects = {
            _clean_category_name(source).casefold(): _clean_category_name(target).casefold()
            for source, target in redirects.items()
            if _clean_category_name(source) and _clean_category_name(target)
        }
        normalized_deleted = {
            _clean_category_name(key).casefold()
            for key in deleted_keys
            if _clean_category_name(key)
        }

        changed_games = 0
        for game in self.games:
            updated_categories: list[str] = []
            for category in game.categories:
                cleaned = _clean_category_name(category)
                if not cleaned:
                    continue
                key = cleaned.casefold()
                key = self._resolve_category_redirect(key, normalized_redirects)
                if key in normalized_deleted and key not in normalized_labels:
                    continue
                updated_categories.append(normalized_labels.get(key, cleaned))

            if self._set_game_categories(game, updated_categories):
                changed_games += 1

        return changed_games

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
        if self.details_cover_movie is not None:
            self.details_cover_movie.stop()
            self.details_cover_movie = None
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

    def _stop_cover_movies(self) -> None:
        for movie in self.cover_movies.values():
            try:
                movie.stop()
            except RuntimeError:
                pass
        self.cover_movies.clear()

    def _attach_cover_animation(self, game: GameEntry, item: QListWidgetItem) -> None:
        cover_path = self.library.cover_path(game)
        if cover_path is None or cover_path.suffix.lower() not in {".gif", ".webp"}:
            return

        movie = QMovie(str(cover_path))
        if not movie.isValid():
            return

        movie.setCacheMode(QMovie.CacheMode.CacheAll)
        self.cover_movies[game.game_id] = movie

        def _on_frame_changed(_frame: int, game_id: str = game.game_id, list_item: QListWidgetItem = item, m: QMovie = movie) -> None:
            frame = m.currentPixmap()
            if frame.isNull():
                return
            try:
                list_item.setData(ROLE_COVER_PIXMAP, _fit_cover(frame, COVER_SIZE))
            except RuntimeError:
                # Item no longer exists (list refreshed).
                m.stop()
                self.cover_movies.pop(game_id, None)
                return
            self.games_list.viewport().update()

        movie.frameChanged.connect(_on_frame_changed)
        movie.start()

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
        self._stop_cover_movies()
        term = self.search_entry.text().strip().lower()

        if self.show_hidden:
            games = [game for game in self.games if game.hidden]
        else:
            games = [game for game in self.games if not game.hidden]
            if self.current_filter == "imported":
                games = [game for game in games if game.base_source == "imported"]
            elif (category_key := _category_from_filter_key(self.current_filter)) is not None:
                games = [
                    game
                    for game in games
                    if any(category.casefold() == category_key for category in game.categories)
                ]
            elif self.current_filter != "all":
                games = [game for game in games if game.base_source == self.current_filter]

        if term:
            games = [
                game
                for game in games
                if term in game.name.lower()
                or term in game.developer.lower()
                or term in game.publisher.lower()
                or term in game.source.lower()
                or any(term in category.lower() for category in game.categories)
                or any(term in genre.lower() for genre in game.genres)
                or any(term in platform.lower() for platform in game.platforms)
                or term in game.release_date.lower()
            ]

        self.filtered = self._sorted_games(games)
        self.games_list.clear()

        for game in self.filtered:
            item = QListWidgetItem()
            item.setData(ROLE_GAME_ID, game.game_id)
            item.setData(ROLE_TITLE, game.name)
            item.setData(ROLE_COVER_PIXMAP, self._game_cover(game))
            item.setData(ROLE_PLAYTIME, _fmt_playtime_minutes(game.playtime_minutes))
            item.setToolTip(game.name)
            self.games_list.addItem(item)
            self._attach_cover_animation(game, item)

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

    def choose_cover_for_active_game(self) -> None:
        game = self.active_game()
        if game is None:
            return
        api_key = self.runtime_settings.get_string("sgdb-key", "").strip()
        igdb_client_id = self.runtime_settings.get_string("igdb-client-id", "").strip()
        igdb_token = self.runtime_settings.get_string("igdb-key", "").strip()
        igdb_secret = self.runtime_settings.get_string("igdb-client-secret", "").strip()
        sgdb_sig = api_key[:10] if api_key else ""
        igdb_sig = (
            f"{igdb_client_id[:10]}:{(igdb_token or igdb_secret)[:10]}"
            if igdb_client_id and (igdb_token or igdb_secret)
            else ""
        )
        cache_key = (game.name.strip().lower(), sgdb_sig, igdb_sig, True, "v2")
        cache_ttl_s = 300.0
        picker_button = self.details_cover_picker_btn
        picker_button_original_text = picker_button.text()
        picker_button.setEnabled(False)
        picker_button.setText("Loading...")
        self.statusBar().showMessage(f"Searching covers for {game.name}...")

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Choose Cover: {game.name}")
        dialog.resize(1120, 760)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        hint_label = QLabel(
            "Click a cover card to apply it. Animated options are marked and shown first."
        )
        hint_label.setWordWrap(True)
        layout.addWidget(hint_label)

        loading_row = QWidget()
        loading_layout = QHBoxLayout(loading_row)
        loading_layout.setContentsMargins(0, 0, 0, 0)
        loading_layout.setSpacing(8)
        loading_label = QLabel("Searching cover providers...")
        loading_bar = QProgressBar()
        loading_bar.setRange(0, 0)
        loading_bar.setTextVisible(False)
        loading_layout.addWidget(loading_label, 1)
        loading_layout.addWidget(loading_bar)
        layout.addWidget(loading_row)

        list_widget = QListWidget()
        list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        list_widget.setFlow(QListWidget.Flow.LeftToRight)
        list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        list_widget.setMovement(QListWidget.Movement.Static)
        list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        list_widget.setUniformItemSizes(True)
        list_widget.setWrapping(True)
        list_widget.setWordWrap(True)
        list_widget.setSpacing(14)
        thumb_size = QSize(170, 255)
        list_widget.setIconSize(thumb_size)
        list_widget.setGridSize(QSize(220, 332))
        list_widget.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        list_widget.setEnabled(False)
        layout.addWidget(list_widget, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        role_url = Qt.ItemDataRole.UserRole
        role_animated = Qt.ItemDataRole.UserRole + 1
        role_mime = Qt.ItemDataRole.UserRole + 2
        role_local_path = Qt.ItemDataRole.UserRole + 3

        placeholder = _fit_cover(self._placeholder_cover(), thumb_size)
        selected: dict[str, Any] = {}

        def _path_animated(path: Path) -> bool:
            suffix = path.suffix.lower()
            if suffix == ".gif":
                return True
            if suffix not in {".webp", ".apng"}:
                return False
            reader = QImageReader(str(path))
            frame_count = reader.imageCount()
            if frame_count > 1:
                return True
            if frame_count == 1:
                return False
            return bool(reader.supportsAnimation())

        def _choose_item(item: QListWidgetItem | None) -> None:
            if item is None:
                return
            selected["url"] = str(item.data(role_url) or "").strip()
            selected["local_path"] = str(item.data(role_local_path) or "").strip()
            selected["mime"] = str(item.data(role_mime) or "").strip().lower()
            selected["animated"] = bool(item.data(role_animated))
            if selected["url"] or selected["local_path"]:
                dialog.accept()

        loader = ThreadPoolExecutor(max_workers=4)
        search_loader = ThreadPoolExecutor(max_workers=1)
        thumb_jobs: dict[Future[Path | None], QListWidgetItem] = {}
        movie_jobs: dict[Future[Path | None], tuple[QListWidgetItem, str, str]] = {}
        active_movies: dict[str, QMovie] = {}
        icon_cache: dict[str, QIcon] = {}
        job_timer = QTimer(dialog)
        job_timer.setInterval(14)
        search_timer = QTimer(dialog)
        search_timer.setInterval(24)
        search_future: Future[list[dict[str, Any]]] | None = None

        def _make_thumb(url: str, mime: str, animated: bool) -> Path | None:
            return self.library.cached_remote_thumbnail_path(
                url=url,
                mime=mime,
                animated=animated,
                width=thumb_size.width(),
                height=thumb_size.height(),
                timeout=8,
            )

        def _make_movie_source(url: str, mime: str) -> Path | None:
            return self.library.cached_remote_cover_path(
                url=url,
                mime=mime,
                animated=True,
                timeout=8,
            )

        def _set_item_icon(item: QListWidgetItem, pixmap: QPixmap, *, cache_key: str = "") -> None:
            if pixmap.isNull():
                return
            if pixmap.size() != thumb_size:
                pixmap = _fit_cover(pixmap, thumb_size)
            icon = QIcon(pixmap)
            if cache_key:
                icon_cache[cache_key] = icon
            item.setIcon(icon)

        def _start_item_movie(item: QListWidgetItem, source: Path, *, movie_key: str) -> bool:
            existing = active_movies.pop(movie_key, None)
            if existing is not None:
                existing.stop()
                existing.deleteLater()
            movie = QMovie(str(source))
            if not movie.isValid():
                movie.deleteLater()
                return False
            movie.setCacheMode(QMovie.CacheMode.CacheNone)

            def _on_frame_changed(
                _frame: int,
                *,
                list_item: QListWidgetItem = item,
                current_movie: QMovie = movie,
                key: str = movie_key,
            ) -> None:
                frame = current_movie.currentPixmap()
                if frame.isNull():
                    return
                _set_item_icon(list_item, frame, cache_key=key)

            movie.frameChanged.connect(_on_frame_changed)
            _on_frame_changed(0)
            movie.start()
            active_movies[movie_key] = movie
            return True

        def _poll_thumb_jobs() -> None:
            done = [future for future in list(thumb_jobs) if future.done()]
            for future in done[:24]:
                item = thumb_jobs.pop(future, None)
                if item is None:
                    continue
                try:
                    thumb_path = future.result()
                except Exception:
                    thumb_path = None
                if thumb_path is None:
                    continue
                url = str(item.data(role_url) or "").strip()
                if url in icon_cache:
                    item.setIcon(icon_cache[url])
                    continue
                pixmap = QPixmap(str(thumb_path))
                _set_item_icon(item, pixmap, cache_key=url)

            movie_done = [future for future in list(movie_jobs) if future.done()]
            for future in movie_done[:12]:
                entry = movie_jobs.pop(future, None)
                if entry is None:
                    continue
                item, url, mime = entry
                try:
                    movie_source = future.result()
                except Exception:
                    movie_source = None
                if movie_source is not None and _start_item_movie(item, movie_source, movie_key=url):
                    continue
                thumb_jobs[loader.submit(_make_thumb, url, mime, True)] = item

            if not thumb_jobs and not movie_jobs:
                job_timer.stop()

        def _search_options() -> list[dict[str, Any]]:
            now = time.monotonic()
            cached = self.cover_search_cache.get(cache_key)
            if cached is not None and (now - cached[0]) <= cache_ttl_s:
                return [dict(entry) for entry in cached[1]]

            options: list[dict[str, Any]] = []
            if api_key:
                options.extend(
                    self.library.search_sgdb_cover_options(
                        game_name=game.name,
                        api_key=api_key,
                        animated=True,
                        limit=60,
                    )
                )
            if igdb_client_id and (igdb_token or igdb_secret):
                options.extend(
                    self.library.search_igdb_cover_options(
                        game_name=game.name,
                        client_id=igdb_client_id,
                        access_token=igdb_token,
                        client_secret=igdb_secret,
                        limit=24,
                    )
                )

            deduped_options: list[dict[str, Any]] = []
            seen_urls: set[str] = set()
            for option in options:
                url = str(option.get("url") or "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                deduped_options.append(option)
            ordered = sorted(
                deduped_options,
                key=lambda option: not bool(option.get("animated", False)),
            )
            self.cover_search_cache[cache_key] = (now, [dict(entry) for entry in ordered])
            if len(self.cover_search_cache) > 64:
                oldest_key = min(
                    self.cover_search_cache.items(),
                    key=lambda pair: pair[1][0],
                )[0]
                self.cover_search_cache.pop(oldest_key, None)
            return ordered

        def _append_option_item(option: dict[str, Any], index: int) -> None:
            url = str(option.get("url") or "").strip()
            if not url:
                return
            mime = str(option.get("mime") or "").strip().lower()
            animated = bool(option.get("animated"))
            provider = str(option.get("provider") or "").strip().upper()
            detail = str(option.get("label") or f"Option {index}").strip()
            state_text = "ANIMATED" if animated else "STATIC"
            label_parts = [part for part in (provider, state_text, detail) if part]
            label = "\n".join([label_parts[0], " | ".join(label_parts[1:])]) if label_parts else f"Option {index}"
            item = QListWidgetItem(QIcon(placeholder), label)
            item.setData(role_url, url)
            item.setData(role_animated, animated)
            item.setData(role_mime, mime)
            item.setData(role_local_path, "")
            item.setToolTip(label.replace("\n", " | "))
            list_widget.addItem(item)
            if animated:
                movie_jobs[loader.submit(_make_movie_source, url, mime)] = (item, url, mime)
            else:
                thumb_jobs[loader.submit(_make_thumb, url, mime, False)] = item

        def _append_current_cover_item() -> int:
            cover_path = self.library.cover_path(game)
            if cover_path is None or not cover_path.is_file():
                return 0
            animated = _path_animated(cover_path)
            suffix = cover_path.suffix.lower().lstrip(".")
            mime = {
                "gif": "image/gif",
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "webp": "image/webp",
                "tiff": "image/tiff",
            }.get(suffix, "")
            state_text = "ANIMATED" if animated else "STATIC"
            detail = f"{state_text} | Local cover | {mime or suffix or 'image'}"
            label = f"CURRENT\n{detail}"
            pixmap = QPixmap(str(cover_path))
            icon = QIcon(placeholder)
            if not pixmap.isNull():
                if pixmap.size() != thumb_size:
                    pixmap = _fit_cover(pixmap, thumb_size)
                icon = QIcon(pixmap)
            item = QListWidgetItem(icon, label)
            item.setData(role_url, "")
            item.setData(role_animated, animated)
            item.setData(role_mime, mime)
            item.setData(role_local_path, str(cover_path))
            item.setToolTip(f"{label.replace(chr(10), ' | ')} | {cover_path.name}")
            list_widget.addItem(item)
            if animated:
                _start_item_movie(item, cover_path, movie_key=f"local:{cover_path}")
            return 1

        def _apply_options(options: list[dict[str, Any]]) -> None:
            if not options:
                loading_label.setText(
                    "No online cover options found. Check SteamGridDB/IGDB credentials in Preferences."
                )
                loading_bar.hide()
                return
            animated_count = sum(1 for option in options if bool(option.get("animated")))
            loading_label.setText(
                f"Found {len(options)} covers ({animated_count} animated). Click one to apply."
            )
            loading_bar.hide()
            list_widget.setEnabled(True)
            for index, option in enumerate(options, start=1):
                _append_option_item(option, index)
            if (thumb_jobs or movie_jobs) and not job_timer.isActive():
                job_timer.start()

        def _poll_search() -> None:
            if search_future is None or not search_future.done():
                return
            search_timer.stop()
            try:
                options = search_future.result()
            except Exception:
                options = []
            _apply_options(options)

        def _shutdown_loader() -> None:
            if job_timer.isActive():
                job_timer.stop()
            if search_timer.isActive():
                search_timer.stop()
            if search_future is not None:
                search_future.cancel()
            for future in list(thumb_jobs):
                future.cancel()
            for future in list(movie_jobs):
                future.cancel()
            for movie in list(active_movies.values()):
                movie.stop()
                movie.deleteLater()
            active_movies.clear()
            loader.shutdown(wait=False, cancel_futures=True)
            search_loader.shutdown(wait=False, cancel_futures=True)

        def _start_search() -> None:
            nonlocal search_future
            if search_future is not None:
                return
            search_future = search_loader.submit(_search_options)
            if not search_timer.isActive():
                search_timer.start()

        def _restore_picker_button() -> None:
            picker_button.setEnabled(True)
            picker_button.setText(picker_button_original_text)

        def _on_dialog_finished(_result: int) -> None:
            _shutdown_loader()
            _restore_picker_button()

        current_item_count = _append_current_cover_item()
        if current_item_count > 0:
            list_widget.setEnabled(True)
            loading_label.setText("Current cover shown. Searching cover providers...")

        list_widget.itemClicked.connect(_choose_item)
        list_widget.itemActivated.connect(_choose_item)
        dialog.finished.connect(_on_dialog_finished)
        job_timer.timeout.connect(_poll_thumb_jobs)
        search_timer.timeout.connect(_poll_search)
        QTimer.singleShot(0, _start_search)

        result = dialog.exec()
        if result != QDialog.DialogCode.Accepted:
            return

        selected_url = str(selected.get("url") or "").strip()
        selected_local_path = str(selected.get("local_path") or "").strip()
        selected_mime = str(selected.get("mime") or "").strip().lower()
        selected_animated = bool(selected.get("animated"))
        if not selected_url and not selected_local_path:
            current = list_widget.currentItem()
            if current is not None:
                selected_url = str(current.data(role_url) or "").strip()
                selected_local_path = str(current.data(role_local_path) or "").strip()
                selected_mime = str(current.data(role_mime) or "").strip().lower()
                selected_animated = bool(current.data(role_animated))
        if not selected_url and not selected_local_path:
            return

        if selected_local_path:
            selected_path = Path(selected_local_path)
            current_path = self.library.cover_path(game)
            try:
                same_cover = (
                    current_path is not None
                    and current_path.is_file()
                    and selected_path.is_file()
                    and selected_path.resolve() == current_path.resolve()
                )
            except OSError:
                same_cover = False
            if same_cover:
                self.statusBar().showMessage("Cover unchanged")
                return
            try:
                self.library.set_cover(game, selected_path)
            except OSError:
                self._show_error("Cover Selection", "Failed to apply selected cover.")
                return
        elif not self.library.set_cover_from_url(
            game,
            selected_url,
            mime=selected_mime,
            animated=selected_animated,
        ):
            self._show_error("Cover Selection", "Failed to download selected cover.")
            return

        self.cover_cache.pop(game.game_id, None)
        self.reload_games()
        refreshed = self.game_by_id(game.game_id)
        if refreshed is not None:
            self.open_game_details(refreshed)
        self.statusBar().showMessage("Cover updated")

    def refresh_active_game_metadata_from_igdb(self) -> None:
        game = self.active_game()
        if game is None:
            return

        client_id = self.runtime_settings.get_string("igdb-client-id", "").strip()
        token = self.runtime_settings.get_string("igdb-key", "").strip()
        client_secret = self.runtime_settings.get_string("igdb-client-secret", "").strip()
        if not client_id or (not token and not client_secret):
            QMessageBox.warning(
                self,
                "IGDB Metadata",
                "Set IGDB Client ID and Client Secret (or Access Token) in Preferences.",
            )
            return

        button = self.details_refresh_metadata_btn
        original_text = button.text()
        button.setEnabled(False)
        button.setText("Refreshing...")
        self.statusBar().showMessage(f"Refreshing metadata for {game.name}...")
        try:
            metadata = self.library.search_igdb_metadata(
                game_name=game.name,
                client_id=client_id,
                access_token=token,
                client_secret=client_secret,
            )
        except Exception as error:
            button.setEnabled(True)
            button.setText(original_text)
            self._show_error("IGDB Metadata", f"Refresh failed: {error}")
            return

        button.setEnabled(True)
        button.setText(original_text)
        if not metadata:
            self.statusBar().showMessage("No IGDB metadata found")
            return

        changed = False
        for key, value in metadata.items():
            if value in (None, "", []):
                continue
            if game.data.get(key) != value:
                game.set_value(key, value)
                changed = True

        if not changed:
            self.statusBar().showMessage("Metadata already up to date")
            return

        self.library.save_game(game)
        self.reload_games()
        refreshed = self.game_by_id(game.game_id)
        if refreshed is not None:
            self.open_game_details(refreshed)
        self.statusBar().showMessage("Metadata refreshed from IGDB")

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

    def edit_game_categories(self, game: GameEntry) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Categories: {game.name}")
        dialog.setMinimumWidth(420)
        dialog.setMinimumHeight(360)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        hint = QLabel("Create categories and check the ones to assign to this game.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        add_row = QHBoxLayout()
        category_input = QLineEdit()
        category_input.setPlaceholderText("New category")
        add_row.addWidget(category_input, 1)
        add_button = QPushButton("Add")
        add_row.addWidget(add_button)
        layout.addLayout(add_row)

        category_list = QListWidget()
        layout.addWidget(category_list, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(buttons)

        assigned_keys = {category.casefold() for category in game.categories}
        categories_by_key = self._category_labels_by_key()

        def _refresh_list() -> None:
            category_list.clear()
            for key in sorted(categories_by_key, key=lambda item: categories_by_key[item].casefold()):
                item = QListWidgetItem(categories_by_key[key])
                item.setData(Qt.ItemDataRole.UserRole, key)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if key in assigned_keys else Qt.CheckState.Unchecked
                )
                category_list.addItem(item)

        def _add_category() -> None:
            assigned_keys.clear()
            for row in range(category_list.count()):
                item = category_list.item(row)
                key = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
                if key and item.checkState() == Qt.CheckState.Checked:
                    assigned_keys.add(key)
            name = _clean_category_name(category_input.text())
            if not name:
                return
            key = name.casefold()
            if key not in categories_by_key:
                categories_by_key[key] = name
            assigned_keys.add(key)
            category_input.clear()
            _refresh_list()

        _refresh_list()
        add_button.clicked.connect(_add_category)
        category_input.returnPressed.connect(_add_category)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        selected_categories: list[str] = []
        for row in range(category_list.count()):
            item = category_list.item(row)
            key = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if not key or item.checkState() != Qt.CheckState.Checked:
                continue
            selected_categories.append(categories_by_key.get(key, item.text()))

        if not self._set_game_categories(game, selected_categories):
            return

        self.statusBar().showMessage(f"Updated categories for {game.name}")
        selected_game_id = game.game_id
        self.reload_games()
        self.apply_filters(select_game_id=selected_game_id)
        if self.navigation_stack.currentWidget() == self.details_page:
            refreshed = self.game_by_id(selected_game_id)
            if refreshed is not None:
                self.open_game_details(refreshed)

    def edit_active_game_categories(self) -> None:
        game = self.active_game()
        if game is None:
            return
        self.edit_game_categories(game)

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
        self.empty_import_button.setEnabled(not importing)

    def _close_import_progress(self) -> None:
        if self.import_progress_dialog is None:
            return
        try:
            self.import_progress_dialog.cancel_requested.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.import_progress_dialog.close()
        self.import_progress_dialog.deleteLater()
        self.import_progress_dialog = None

    def _cancel_import(self, *, close_after: bool = False) -> None:
        if self.import_thread is None or not self.import_thread.isRunning():
            if close_after:
                self.close()
            return

        self._close_after_import_cancel = self._close_after_import_cancel or close_after
        if self.import_progress_dialog is not None:
            self.import_progress_dialog.set_summary_message("Canceling import...")
            self.import_progress_dialog.cancel_button.setEnabled(False)
        self.statusBar().showMessage("Canceling background task...")
        self.import_thread.stop()

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
                    self._details_cover_target_size(),
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

        if payload.get("canceled"):
            self.last_import_session = None
            self.statusBar().showMessage("Import canceled")
            return

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
            include_cover_refresh = self.runtime_settings.get_bool(
                "refresh-covers-on-metadata", False
            )
            if metadata_updates == 0 and cover_updates == 0:
                summary = (
                    "No metadata or cover changes"
                    if include_cover_refresh
                    else "No metadata changes"
                )
            else:
                parts: list[str] = []
                if metadata_updates:
                    parts.append(f"{metadata_updates} metadata updates")
                if include_cover_refresh and cover_updates:
                    parts.append(f"{cover_updates} cover updates")
                if include_cover_refresh and new_cover_updates:
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
        if message == "Import canceled":
            self.statusBar().showMessage("Import canceled")
            return
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
        if self._close_after_import_cancel:
            self._close_after_import_cancel = False
            self.close()

    def _run_worker(self, *, mode: str, auto: bool) -> None:
        if self.import_thread is not None and self.import_thread.isRunning():
            self.statusBar().showMessage("A background task is already running")
            return

        self._set_import_in_progress(True)
        include_cover_refresh = self.runtime_settings.get_bool(
            "refresh-covers-on-metadata", False
        )
        progress_text = (
            (
                "Refreshing metadata and covers…"
                if include_cover_refresh
                else "Refreshing metadata…"
            )
            if mode == "refresh_metadata"
            else "Importing games…"
        )
        progress_title = "Refresh Metadata" if mode == "refresh_metadata" else "Import"
        if auto:
            self.import_progress_dialog = None
            self.statusBar().showMessage(progress_text)
        else:
            self.import_progress_dialog = WorkerProgressDialog(
                title=progress_title,
                message=progress_text,
                parent=self,
            )
            self.import_progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
            self.import_progress_dialog.cancel_requested.connect(self._cancel_import)
            self.import_progress_dialog.show()

        fast_mode = bool(auto and mode == "import")
        self.import_thread = ImportWorkerThread(
            self.library.data_dir_name,
            mode=mode,
            fast_mode=fast_mode,
            startup_auto=bool(auto and mode == "import"),
            parent=self,
        )
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
        current_category_labels = self._category_labels_by_key()
        dialog = PreferencesDialog(
            self.runtime_settings,
            category_labels=current_category_labels,
            category_icons=self.category_icon_paths,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        dialog.apply()
        new_category_labels = dialog.category_labels()
        self._save_category_definitions(new_category_labels)
        self._apply_category_definition_changes(
            labels_by_key=new_category_labels,
            redirects=dialog.category_redirects(),
            deleted_keys=dialog.deleted_category_keys(),
        )

        self.category_icon_paths = self._load_category_icon_paths()
        self.category_icon_paths = {
            key: path
            for key, path in self.category_icon_paths.items()
            if key in self.category_definitions
        }
        self.runtime_settings.set_string(
            "category-icons",
            json.dumps(self.category_icon_paths, sort_keys=True),
        )
        self._apply_color_mode()
        self.reload_games()
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
                "A KDE-focused standalone game launcher.\n\n"
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
        accent = self._cover_accent_color(self.active_details_cover)
        painter.fillRect(overlay.rect(), QColor(accent.red(), accent.green(), accent.blue(), 120))
        painter.fillRect(overlay.rect(), QColor(10, 10, 14, 84))
        painter.end()

        self.details_backdrop.setPixmap(overlay)
        self._apply_details_accent(accent)

    def _details_cover_target_size(self) -> QSize:
        width = max(180, min(220, self.details_cover.width() or 200))
        height = int(width * 1.5)
        return QSize(width, height)

    def _update_details_card_size(self) -> None:
        if not hasattr(self, "details_body_frame"):
            return
        available = max(340, self.details_page.width() - 96)
        self.details_body_frame.setMaximumWidth(min(980, available))

    def _cover_accent_color(self, pixmap: QPixmap) -> QColor:
        image = pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        step_x = max(1, image.width() // 18)
        step_y = max(1, image.height() // 18)
        red = green = blue = samples = 0
        for y in range(0, image.height(), step_y):
            for x in range(0, image.width(), step_x):
                color = image.pixelColor(x, y)
                red += color.red()
                green += color.green()
                blue += color.blue()
                samples += 1
        if samples == 0:
            return QColor(52, 72, 96)
        return QColor(red // samples, green // samples, blue // samples)

    def _apply_details_accent(self, accent: QColor) -> None:
        # Keep card tone consistent for readability across all covers.
        _unused = accent
        light_border = QColor(210, 210, 205, 88)
        background = QColor(124, 123, 117, 214)
        self.details_body_frame.setStyleSheet(
            (
                "QFrame#DetailsBody {"
                f"border-radius: 14px; border: 1px solid rgba({light_border.red()}, {light_border.green()}, {light_border.blue()}, {light_border.alpha()}); "
                f"background: rgba({background.red()}, {background.green()}, {background.blue()}, {background.alpha()});"
                "}"
                "QFrame#DetailsBody QLabel { color: #f2f3f5; }"
            )
        )

    def open_game_details(self, game: GameEntry) -> None:
        self.active_game_id = game.game_id

        self.details_header_title.setText(game.name)
        self.details_title.setText(game.name)
        self.details_developer.setText(game.developer or "")
        self.details_developer.setVisible(bool(game.developer))
        self.details_publisher.setText(
            f"Publisher: {game.publisher}" if game.publisher else ""
        )
        self.details_publisher.setVisible(bool(game.publisher))
        self.details_genres.setText(
            f"Genres: {', '.join(game.genres)}" if game.genres else ""
        )
        self.details_genres.setVisible(bool(game.genres))
        self.details_categories.setText(
            f"Categories: {', '.join(game.categories)}" if game.categories else ""
        )
        self.details_categories.setVisible(bool(game.categories))
        self.details_platforms.setText(
            f"Platforms: {', '.join(game.platforms)}" if game.platforms else ""
        )
        self.details_platforms.setVisible(bool(game.platforms))
        self.details_release_date.setText(
            f"Release Date: {game.release_date}" if game.release_date else ""
        )
        self.details_release_date.setVisible(bool(game.release_date))
        self.details_summary.setText(game.summary or "")
        self.details_summary.setVisible(bool(game.summary))
        self.details_metacritic.setText(
            f"Metacritic: {game.metacritic_score}" if game.metacritic_score is not None else ""
        )
        self.details_metacritic.setVisible(game.metacritic_score is not None)
        self.details_igdb_rating.setText(
            f"IGDB Rating: {game.igdb_rating:.1f}" if game.igdb_rating is not None else ""
        )
        self.details_igdb_rating.setVisible(game.igdb_rating is not None)
        self.details_igdb_url.setText(_link_html(game.igdb_url) if game.igdb_url else "")
        self.details_igdb_url.setVisible(bool(game.igdb_url))
        self.details_website.setText(_link_html(game.website) if game.website else "")
        self.details_website.setVisible(bool(game.website))
        self.details_added.setText(f"Added: {_fmt_timestamp(game.added)}")
        self.details_last_played.setText(f"Last played: {_fmt_timestamp(game.last_played)}")
        self.details_executable.setText(game.executable_text() or "-")
        self.details_hide_btn.setText("Unhide" if game.hidden else "Hide")

        if self.details_cover_movie is not None:
            self.details_cover_movie.stop()
            self.details_cover_movie = None

        cover_path = self.library.cover_path(game)
        if cover_path is not None and cover_path.suffix.lower() in {".gif", ".webp"}:
            movie = QMovie(str(cover_path))
            target = self._details_cover_target_size()
            movie.setScaledSize(target)
            self.details_cover.setMovie(movie)
            movie.start()
            self.details_cover_movie = movie
            frame = movie.currentPixmap()
            cover = frame if not frame.isNull() else self._game_cover(game)
        else:
            cover = self._game_cover(game)
            self.details_cover.setPixmap(
                cover.scaled(
                    self._details_cover_target_size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

        self.active_details_cover = cover
        self._refresh_details_backdrop()
        self.navigation_stack.setCurrentWidget(self.details_page)
        self.search_row.setVisible(False)
        self._update_details_card_size()

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
        menu.addAction("Categories...", lambda: self.edit_game_categories(game))
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
