from __future__ import annotations

from copy import deepcopy
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

from PySide6.QtCore import QThread, QTimer, Signal, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QPlainTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class JailDetectionThread(QThread):
    def __init__(self, library, game, parent=None) -> None:
        super().__init__(parent)
        self.library = library
        self.game = deepcopy(game)
        self.config: dict[str, Any] = {}
        self.error = ""

    def run(self) -> None:
        try:
            config = self.library.detect_jail_config(self.game)
            if not isinstance(config, dict):
                raise OSError("Lutris did not provide usable jail settings.")
            self.config = config
        except Exception as error:
            self.error = str(error) or "Jail settings could not be detected."


class JailConfigEditor(QWidget):
    """Infer a direct launch on opt-in, while retaining manual configuration."""

    changed = Signal()
    detection_finished = Signal()

    def __init__(
        self, config: dict[str, Any] | None = None, parent=None, *, library=None, game=None,
    ) -> None:
        super().__init__(parent)
        self.library = library
        self.game = game
        self._base_config = deepcopy(config or {})
        self._setting_values = False
        self._manual_revision = 0
        self._detection_revision = 0
        self._detection_error = ""
        self.worker: JailDetectionThread | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.enabled = QCheckBox("Run in a jail (offline)")
        enabled = self._base_config.get("enabled", False)
        self.enabled.setChecked(enabled if isinstance(enabled, bool) else True)
        layout.addWidget(self.enabled)

        self.validation_message = QLabel()
        self.validation_message.setWordWrap(True)
        self.validation_message.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.validation_message)
        self.advanced = QToolButton()
        self.advanced.setText("Advanced settings")
        self.advanced.setCheckable(True)
        self.advanced.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced.toggled.connect(self._advanced_toggled)
        layout.addWidget(self.advanced)

        self.fields = QWidget()
        form = QFormLayout(self.fields)
        form.setContentsMargins(0, 0, 0, 0)
        self.game_dir = QLineEdit(str(self._base_config.get("game_dir") or ""))
        self.game_dir.setPlaceholderText("Folder containing the whole game")
        self.exe = QLineEdit(str(self._base_config.get("exe") or ""))
        self.exe.setPlaceholderText("Relative path, e.g. bin/Game.exe")
        self.working_dir = QLineEdit(str(self._base_config.get("working_dir") or ""))
        self.working_dir.setPlaceholderText("Relative to game folder; blank uses the .exe folder")
        self.runner_type = QComboBox()
        self.runner_type.addItem("Wine", "wine")
        self.runner_type.addItem("Proton", "proton")
        self.runner_type.setCurrentIndex(
            1 if self._base_config.get("runner_type") == "proton" else 0
        )
        self.runner = QLineEdit(str(self._base_config.get("runner") or "wine"))
        arguments = self._base_config.get("arguments", [])
        self.arguments = QLineEdit(
            shlex.join([str(argument) for argument in arguments])
            if isinstance(arguments, list) else str(arguments or "")
        )
        self.arguments.setPlaceholderText('Optional arguments, e.g. --language "English"')
        form.addRow("Game folder", self._browse_row(self.game_dir, self._browse_game_dir))
        form.addRow("Game .exe", self._browse_row(self.exe, self._browse_exe))
        form.addRow("Working folder", self.working_dir)
        form.addRow("Runner type", self.runner_type)
        form.addRow("Runner", self._browse_row(self.runner, self._browse_runner))
        form.addRow("Game arguments", self.arguments)
        layout.addWidget(self.fields)

        self.note = QLabel(
            "Offline mode uses a copy of the whole game folder, with its own prefix "
            "and saves. Changes stay in that copy. Online games are not supported. "
            "Compatible gamepads connected before launch are available. "
            "The desktop and Linux kernel are shared; this does not "
            "prove a game is safe."
        )
        self.note.setWordWrap(True)
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.note)
        self.enabled.toggled.connect(self._enabled_toggled)
        self.runner_type.currentIndexChanged.connect(self._runner_type_changed)
        for edit in (self.game_dir, self.exe, self.working_dir):
            edit.textChanged.connect(self._field_changed)
        self.arguments.textChanged.connect(self._field_changed)
        self.runner.textChanged.connect(self._runner_changed)
        self.advanced.setChecked(self.enabled.isChecked() and not self._has_valid_settings())
        self._on_changed()

    @property
    def detecting(self) -> bool:
        return self.worker is not None

    def _browse_row(self, edit: QLineEdit, callback) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        button = QPushButton("Browse")
        button.clicked.connect(callback)
        layout.addWidget(button)
        return row

    def _browse_game_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "Select the whole game folder", self.game_dir.text()
        )
        if selected:
            self.game_dir.setText(selected)

    def _browse_exe(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self, "Select an executable inside the game folder", self.game_dir.text(),
            "Windows executables (*.exe *.EXE)",
        )
        if not selected:
            return
        try:
            relative = Path(selected).relative_to(Path(self.game_dir.text()).expanduser())
        except ValueError:
            self.validation_message.setText("Select an .exe inside the chosen game folder.")
            return
        self.exe.setText(relative.as_posix())

    def _browse_runner(self) -> None:
        if self.runner_type.currentData() == "proton":
            selected = QFileDialog.getExistingDirectory(self, "Select a Proton build folder")
        else:
            selected, _filter = QFileDialog.getOpenFileName(self, "Select a host Wine executable")
        if selected:
            self.runner.setText(selected)

    def _advanced_toggled(self, checked: bool) -> None:
        self.advanced.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        self.fields.setVisible(checked and self.enabled.isChecked())

    def _field_changed(self) -> None:
        if not self._setting_values:
            self._manual_revision += 1
            self._detection_error = ""
        self._on_changed()

    def _runner_changed(self) -> None:
        if not self._setting_values:
            self._base_config.pop("runner_dir", None)
        self._field_changed()

    def _runner_type_changed(self) -> None:
        if not self._setting_values:
            self._base_config.pop("runner_dir", None)
            if self.runner_type.currentData() == "proton" and self.runner.text() == "wine":
                self.runner.clear()
            elif self.runner_type.currentData() == "wine" and not self.runner.text():
                self.runner.setText("wine")
        self._field_changed()

    def _has_valid_settings(self) -> bool:
        try:
            self._configured_values()
        except ValueError:
            return False
        return True

    def _enabled_toggled(self, enabled: bool) -> None:
        if (
            enabled and not self.detecting and not self._has_valid_settings()
            and self._manual_revision == 0
        ):
            if self.library is not None and self.game is not None:
                self._detection_error = ""
                self._detection_revision = self._manual_revision
                self.worker = JailDetectionThread(self.library, self.game, self)
                self.worker.finished.connect(self._detection_ready)
                self.advanced.setChecked(False)
                self._on_changed()
                self.worker.start()
                return
            self.advanced.setChecked(True)
        self._on_changed()

    def _detection_ready(self) -> None:
        worker = self.worker
        if worker is None:
            return
        self.worker = None
        if self._manual_revision == self._detection_revision:
            if worker.error:
                self._detection_error = worker.error
                if "Advanced settings" not in worker.error:
                    self._detection_error += " Choose settings under Advanced settings."
                self.advanced.setChecked(True)
            else:
                self._setting_values = True
                try:
                    self._base_config = deepcopy(worker.config)
                    self.game_dir.setText(str(worker.config.get("game_dir") or ""))
                    self.exe.setText(str(worker.config.get("exe") or ""))
                    self.working_dir.setText(str(worker.config.get("working_dir") or ""))
                    self.runner_type.setCurrentIndex(
                        1 if worker.config.get("runner_type") == "proton" else 0
                    )
                    self.runner.setText(str(worker.config.get("runner") or "wine"))
                    self.arguments.setText(shlex.join(worker.config.get("arguments", [])))
                finally:
                    self._setting_values = False
                self.advanced.setChecked(not self._has_valid_settings())
        worker.deleteLater()
        self._on_changed()
        self.detection_finished.emit()

    def _on_changed(self) -> None:
        enabled = self.enabled.isChecked()
        self.advanced.setVisible(enabled)
        self.fields.setVisible(enabled and self.advanced.isChecked())
        self.fields.setEnabled(enabled and not self.detecting)
        self.note.setVisible(enabled)
        self.validation_message.setVisible(enabled)
        self.runner.setPlaceholderText(
            "Proton build folder containing proton"
            if self.runner_type.currentData() == "proton"
            else "wine, or an absolute host Wine executable path"
        )
        if self.detecting:
            message = "Detecting jail settings from Lutris…"
        elif self._detection_error:
            message = self._detection_error
        elif enabled:
            try:
                self._configured_values()
            except ValueError as error:
                message = str(error)
            else:
                message = "Jail settings are ready."
        else:
            message = ""
        self.validation_message.setText(message)
        self.changed.emit()

    def _configured_values(self) -> dict[str, Any]:
        folder = Path(self.game_dir.text().strip()).expanduser()
        if not self.game_dir.text().strip() or not folder.is_absolute() or folder == Path("/"):
            raise ValueError("Choose an absolute folder path containing the whole game.")
        exe = Path(self.exe.text().strip())
        if (
            not self.exe.text().strip() or exe.is_absolute() or ".." in exe.parts
            or exe.suffix.lower() != ".exe"
        ):
            raise ValueError("Choose an .exe relative to the game folder, without '..'.")
        working_text = self.working_dir.text()
        working_dir = PurePosixPath(working_text.strip())
        if (
            working_dir.is_absolute() or ".." in working_dir.parts
            or any(character in working_text for character in ("\x00", "\n", "\r", "\\"))
        ):
            raise ValueError("Working folder must be relative to the game folder, without '..' or backslashes.")
        runner_type = str(self.runner_type.currentData())
        runner = self.runner.text().strip()
        if not runner:
            raise ValueError("Choose a Proton build folder or a Wine executable.")
        runner_path = Path(runner).expanduser()
        if runner_type == "proton" and not runner_path.is_absolute():
            raise ValueError("The Proton build folder must be an absolute path.")
        if runner_type == "wine" and not runner_path.is_absolute() and (
            "/" in runner or any(character.isspace() for character in runner)
        ):
            raise ValueError("Use a Wine command name or an absolute executable path.")
        try:
            arguments = shlex.split(self.arguments.text())
        except ValueError as error:
            raise ValueError(f"Game arguments have invalid quoting: {error}") from error
        config = {
            **self._base_config, "enabled": True,
            "game_dir": str(folder), "exe": exe.as_posix(),
            "runner_type": runner_type, "runner": str(runner_path), "arguments": arguments,
        }
        if working_text.strip():
            config["working_dir"] = working_dir.as_posix()
        else:
            config.pop("working_dir", None)
        return config

    def configuration(self) -> dict[str, Any]:
        if self.detecting:
            raise ValueError("Wait for jail settings detection to finish.")
        if self.enabled.isChecked():
            return self._configured_values()
        try:
            config = self._configured_values()
        except ValueError:
            config = dict(self._base_config)
        return {**config, "enabled": False}


class JailGameDialog(QDialog):
    """Keep a lookup worker alive when an Add/Edit dialog is dismissed."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.editor: JailConfigEditor | None = None
        self._reject_after_detection = False
        self._resize_queued = False

    @property
    def detecting(self) -> bool:
        return self.editor is not None and self.editor.detecting

    def set_jail_editor(self, editor: JailConfigEditor) -> None:
        self.editor = editor
        editor.detection_finished.connect(self._detection_finished)
        editor.changed.connect(self._request_resize)
        editor.advanced.toggled.connect(self._request_resize)
        self._request_resize()

    def _request_resize(self) -> None:
        if self._resize_queued:
            return
        self._resize_queued = True
        QTimer.singleShot(0, self._fit_to_editor)

    def _fit_to_editor(self) -> None:
        self._resize_queued = False
        layout = self.layout()
        if layout is not None:
            layout.invalidate()
            self.adjustSize()

    def _detection_finished(self) -> None:
        if self._reject_after_detection:
            super().reject()

    def accept(self) -> None:
        if not self.detecting:
            super().accept()

    def reject(self) -> None:
        if self.detecting:
            self._reject_after_detection = True
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self.detecting:
            self._reject_after_detection = True
            event.ignore()
            return
        super().closeEvent(event)


class JailReportThread(QThread):
    def __init__(self, library, game, parent=None) -> None:
        super().__init__(parent)
        self.library = library
        self.game = game
        self.report: dict[str, Any] = {}

    def run(self) -> None:
        try:
            self.report = self.library.jail_report(self.game)
        except Exception as error:
            self.report = {"status": "unavailable", "error": str(error)}


class JailFolderThread(QThread):
    def __init__(self, library, game, parent=None) -> None:
        super().__init__(parent)
        self.library = library
        self.game = game
        self.error = ""

    def run(self) -> None:
        try:
            self.library.open_jail_folder(self.game)
        except Exception as error:
            self.error = str(error) or "The host file manager could not open the jail folder."


class JailStopThread(QThread):
    """Stop one identified host jail without blocking Qt's event loop."""

    def __init__(self, library, game, parent=None) -> None:
        super().__init__(parent)
        self.library = library
        self.game = deepcopy(game)
        self.result: dict[str, Any] = {}
        self.error = ""

    def run(self) -> None:
        try:
            self.result = self.library.stop_jail(self.game)
            if self.result.get("status") not in {"stopped", "not_running"}:
                raise OSError("The jail helper did not confirm that the jail stopped.")
        except Exception as error:
            self.error = str(error) or "The jail could not be stopped."


class JailReportDialog(QDialog):
    def __init__(self, library, game, parent=None) -> None:
        super().__init__(parent)
        self.library = library
        self.game = game
        self.worker: JailReportThread | None = None
        self._close_when_ready = False
        self.setWindowTitle(f"Jail log — {game.name}")
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        self.status = QLabel("Loading jail report…")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.paths = QLabel()
        self.paths.setTextFormat(Qt.TextFormat.PlainText)
        self.paths.setWordWrap(True)
        self.paths.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.paths)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1500)
        layout.addWidget(self.log, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.refresh = buttons.addButton("Refresh", QDialogButtonBox.ButtonRole.ActionRole)
        self.refresh.clicked.connect(self.load_report)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.load_report()

    def load_report(self) -> None:
        if self.worker is not None:
            return
        self.refresh.setEnabled(False)
        self.status.setText("Loading jail report…")
        self.worker = JailReportThread(self.library, self.game, self)
        self.worker.finished.connect(self._report_ready)
        self.worker.start()

    def _report_ready(self) -> None:
        worker = self.worker
        if worker is None:
            return
        report = worker.report
        self.worker = None
        worker.deleteLater()
        self.refresh.setEnabled(True)
        status = str(report.get("status") or "unknown")
        error = str(report.get("error") or "")
        exit_code = report.get("exit_code")
        self.status.setText(
            f"Status: {status}"
            + (f" · Exit code: {exit_code}" if exit_code is not None else "")
            + (f"\n{error}" if error else "")
        )
        self.paths.setText("\n".join(
            f"{label}: {report[key]}" for label, key in
            (("Jail folder", "jail_dir"), ("Log file", "log_path")) if report.get(key)
        ))
        text = str(report.get("log_text") or "No jail log is available yet.")
        if len(text) > 64_000:
            text = "[Showing the last 64,000 characters]\n" + text[-64_000:]
        self.log.setPlainText(text)
        if self._close_when_ready:
            super().reject()

    def reject(self) -> None:
        if self.worker is not None:
            self._close_when_ready = True
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self.worker is not None:
            self._close_when_ready = True
            event.ignore()
            return
        super().closeEvent(event)
