import os
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QSettings, QTimer, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLineEdit
    from klay.qt.jail_dialog import JailConfigEditor, JailGameDialog, JailReportDialog
except ImportError:
    QT_AVAILABLE = False
else:
    QT_AVAILABLE = True


def valid_config(**changes):
    return {
        "enabled": True,
        "game_dir": "/home/player/My Game",
        "exe": "bin/My Game.exe",
        "runner_type": "wine",
        "runner": "wine",
        "arguments": ["--language", "English US"],
        **changes,
    }


class QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not QT_AVAILABLE:
            raise unittest.SkipTest("PySide6 is not installed")
        cls.app = QApplication.instance() or QApplication([])

    def wait_for(self, predicate):
        for _ in range(200):
            QTest.qWait(10)
            if predicate():
                return
        self.assertTrue(predicate(), "Qt worker did not finish within two seconds")


@unittest.skipIf(not QT_AVAILABLE, "PySide6 is not installed")
class JailEditorTests(QtTestCase):
    def setUp(self):
        self.widgets = []
        self.detections = []

    def tearDown(self):
        for release, _editor in self.detections:
            release.set()
        for _release, editor in self.detections:
            if editor.worker is not None:
                editor.worker.wait(3500)
        self.app.processEvents()
        for widget in self.widgets:
            widget.deleteLater()
        self.app.processEvents()

    def editor(self, **changes):
        editor = JailConfigEditor(valid_config(**changes))
        self.widgets.append(editor)
        return editor

    def lookup_editor(self, config=None, *, error="", parent=None):
        release = threading.Event()
        calls = []

        class Library:
            def detect_jail_config(self, game):
                calls.append((game, threading.get_ident()))
                if not release.wait(3):
                    raise OSError("Test detection timed out")
                if error:
                    raise OSError(error)
                return valid_config(
                    runner_dir="/home/player/Runners/wine-build", working_dir="bin",
                )

        editor = JailConfigEditor(
            config, parent, library=Library(),
            game=SimpleNamespace(source="lutris_wine", game_id="lutris_wine_42"),
        )
        self.widgets.append(editor)
        self.detections.append((release, editor))
        return editor, release, calls

    def test_argument_boundaries_and_explicit_opt_out(self):
        editor = self.editor()
        editor.arguments.setText('--name "two words" --literal \'$(touch should-not-exist)\'')
        config = editor.configuration()
        self.assertEqual(config["arguments"], [
            "--name", "two words", "--literal", "$(touch should-not-exist)",
        ])
        self.assertEqual(config["exe"], "bin/My Game.exe")
        editor.enabled.setChecked(False)
        self.assertFalse(editor.configuration()["enabled"])
        self.assertEqual(editor.configuration()["game_dir"], "/home/player/My Game")

    def assert_invalid(self, **changes):
        editor = self.editor(**changes)
        with self.assertRaises(ValueError):
            editor.configuration()
        self.assertTrue(editor.validation_message.text())

    def test_rejects_relative_game_folder(self):
        self.assert_invalid(game_dir="relative")

    def test_rejects_root_game_folder(self):
        self.assert_invalid(game_dir="/")

    def test_rejects_absolute_exe(self):
        self.assert_invalid(exe="/outside.exe")

    def test_rejects_exe_outside_game_folder(self):
        self.assert_invalid(exe="../outside.exe")

    def test_rejects_non_windows_executable(self):
        self.assert_invalid(exe="game.sh")

    def test_rejects_relative_proton_folder(self):
        self.assert_invalid(runner_type="proton", runner="relative-proton")

    def test_unbalanced_argument_quoting_blocks_save(self):
        editor = self.editor()
        editor.arguments.setText('--name "unfinished')
        with self.assertRaisesRegex(ValueError, "invalid quoting"):
            editor.configuration()

    def assert_corrupt_enabled_is_fail_closed(self, enabled):
        editor = self.editor(enabled=enabled, arguments=[42, "two words"])
        self.assertTrue(editor.enabled.isChecked())
        self.assertIs(editor.configuration()["enabled"], True)
        editor.enabled.setChecked(False)
        self.assertIs(editor.configuration()["enabled"], False)

    def test_corrupt_string_enabled_requires_explicit_opt_out(self):
        self.assert_corrupt_enabled_is_fail_closed("false")

    def test_corrupt_number_enabled_requires_explicit_opt_out(self):
        self.assert_corrupt_enabled_is_fail_closed(0)

    def test_corrupt_null_enabled_requires_explicit_opt_out(self):
        self.assert_corrupt_enabled_is_fail_closed(None)

    def test_corrupt_list_enabled_requires_explicit_opt_out(self):
        self.assert_corrupt_enabled_is_fail_closed([])

    def test_checkbox_detects_asynchronously_and_collapses_advanced_settings(self):
        editor, release, calls = self.lookup_editor()
        self.assertFalse(calls)
        editor.enabled.setChecked(True)
        self.assertTrue(editor.detecting)
        self.assertIn("Detecting", editor.validation_message.text())
        with self.assertRaisesRegex(ValueError, "Wait"):
            editor.configuration()
        self.wait_for(lambda: bool(calls))
        self.assertNotEqual(calls[0][1], threading.get_ident())
        release.set()
        self.wait_for(lambda: not editor.detecting)
        self.assertEqual(editor.configuration()["exe"], "bin/My Game.exe")
        self.assertIn("ready", editor.validation_message.text())
        self.assertFalse(editor.advanced.isChecked())
        self.assertTrue(editor.fields.isHidden())
        self.assertEqual(editor.configuration()["runner_dir"], "/home/player/Runners/wine-build")
        self.assertEqual(editor.configuration()["working_dir"], "bin")
        self.assertEqual(editor.working_dir.text(), "bin")

    def test_detection_error_expands_manual_settings_without_fallback(self):
        editor, release, calls = self.lookup_editor(error="Lutris game folder is missing.")
        editor.enabled.setChecked(True)
        release.set()
        self.wait_for(lambda: not editor.detecting)
        self.assertTrue(editor.enabled.isChecked())
        self.assertTrue(editor.advanced.isChecked())
        self.assertIn("Lutris game folder is missing", editor.validation_message.text())
        self.assertIn("Advanced settings", editor.validation_message.text())
        with self.assertRaises(ValueError):
            editor.configuration()
        editor.game_dir.setText("/home/player/Manual Game")
        editor.exe.setText("Manual.exe")
        self.assertEqual(editor.configuration()["exe"], "Manual.exe")
        self.assertEqual(len(calls), 1)

    def test_delayed_detection_preserves_toggle_off_and_reenable_settings(self):
        editor, release, calls = self.lookup_editor()
        editor.enabled.setChecked(True)
        editor.enabled.setChecked(False)
        release.set()
        self.wait_for(lambda: not editor.detecting)
        self.assertFalse(editor.enabled.isChecked())
        self.assertFalse(editor.configuration()["enabled"])
        editor.enabled.setChecked(True)
        self.assertEqual(editor.configuration()["exe"], "bin/My Game.exe")
        editor.exe.setText("Manual.exe")
        editor.enabled.setChecked(False)
        self.assertEqual(editor.configuration()["exe"], "Manual.exe")
        editor.enabled.setChecked(True)
        self.assertEqual(editor.configuration()["exe"], "Manual.exe")
        self.assertEqual(len(calls), 1)

    def test_existing_settings_skip_detection_and_preserve_custom_runner_root(self):
        editor, release, calls = self.lookup_editor(valid_config(
            enabled=False, runner_dir="/home/player/Runners/custom",
        ))
        editor.enabled.setChecked(True)
        self.assertFalse(editor.detecting)
        self.assertFalse(calls)
        self.assertEqual(editor.configuration()["runner_dir"], "/home/player/Runners/custom")
        editor.enabled.setChecked(False)
        editor.enabled.setChecked(True)
        self.assertFalse(calls)
        editor.runner.setText("/usr/bin/wine")
        self.assertNotIn("runner_dir", editor.configuration())

    def test_manual_working_directory_survives_game_path_edits_and_toggle(self):
        for field in ("game_dir", "exe"):
            with self.subTest(field=field):
                editor = self.editor()
                editor.working_dir.setText("content/assets")
                editor.arguments.setText("--mode safe")
                editor.runner.setText("/usr/bin/wine")
                editor.enabled.setChecked(False)
                self.assertEqual(editor.configuration()["working_dir"], "content/assets")
                editor.enabled.setChecked(True)
                self.assertEqual(editor.configuration()["working_dir"], "content/assets")
                if field == "game_dir":
                    editor.game_dir.setText("/home/player/Other Game")
                else:
                    editor.exe.setText("Other.exe")
                config = editor.configuration()
                self.assertEqual(config["working_dir"], "content/assets")
                reopened = JailConfigEditor(config)
                self.widgets.append(reopened)
                self.assertEqual(reopened.working_dir.text(), "content/assets")

    def test_blank_working_directory_uses_executable_folder_default(self):
        editor = self.editor()
        self.assertEqual(editor.working_dir.text(), "")
        self.assertNotIn("working_dir", editor.configuration())
        editor = self.editor(working_dir=".")
        self.assertEqual(editor.configuration()["working_dir"], ".")
        editor.working_dir.clear()
        editor.exe.setText("other/bin/Game.exe")
        self.assertNotIn("working_dir", editor.configuration())
        editor.enabled.setChecked(False)
        self.assertNotIn("working_dir", editor.configuration())

    def test_invalid_working_directory_blocks_configuration(self):
        editor = self.editor()
        for path in ("/tmp", "..", "../bin", "bin/../other", "bad\x00path", "bad\npath", "bad\rpath", "bin\\other"):
            with self.subTest(path=path):
                editor.working_dir.setText(path)
                with self.assertRaisesRegex(ValueError, "Working folder"):
                    editor.configuration()

    def test_game_dialog_defers_cancel_until_detection_finishes(self):
        dialog = JailGameDialog()
        self.widgets.append(dialog)
        editor, release, calls = self.lookup_editor(parent=dialog)
        dialog.set_jail_editor(editor)
        dialog.show()
        editor.enabled.setChecked(True)
        dialog.reject()
        self.assertTrue(dialog.isVisible())
        dialog.accept()
        self.assertTrue(dialog.isVisible())
        release.set()
        self.wait_for(lambda: not editor.detecting)
        self.assertFalse(dialog.isVisible())

    def test_report_is_plain_bounded_and_safe_to_close_during_worker(self):
        release = threading.Event()

        class Library:
            def jail_report(self, game):
                if not release.wait(2):
                    raise OSError("Test report timed out")
                return {
                    "status": "failed", "exit_code": 3, "error": "Probe failed",
                    "log_text": "x" * 70_000 + "<script>plain text</script>",
                    "log_path": "/tmp/jail log.txt", "jail_dir": "/tmp/jail",
                }

        class Game:
            name = "Test Game"

        dialog = JailReportDialog(Library(), Game())
        self.widgets.append(dialog)
        try:
            dialog.show()
            dialog.reject()
            self.assertIsNotNone(dialog.worker)
            self.assertTrue(dialog.isVisible())
            release.set()
            self.wait_for(lambda: dialog.worker is None)
            self.assertFalse(dialog.isVisible())
            self.assertIn("Status: failed", dialog.status.text())
            self.assertIn("Exit code: 3", dialog.status.text())
            self.assertIn("<script>plain text</script>", dialog.log.toPlainText())
            self.assertLess(len(dialog.log.toPlainText()), 64_100)
            self.assertIn("/tmp/jail log.txt", dialog.paths.text())
        finally:
            release.set()
            if dialog.worker is not None:
                dialog.worker.wait(2500)
                self.app.processEvents()


@unittest.skipIf(not QT_AVAILABLE, "PySide6 is not installed")
class JailWindowTests(QtTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            from klay.qt.library import GameEntry, GameLibrary
            from klay.qt.settings import SettingsBackend
            from klay.qt.window import KlayMainWindow
        except ImportError as error:
            raise unittest.SkipTest(f"Generated shared module/GI runtime unavailable: {error}")
        except AttributeError as error:
            if "gi" not in str(error):
                raise
            raise unittest.SkipTest(f"GI runtime unavailable: {error}")
        cls.GameEntry = GameEntry
        cls.GameLibrary = GameLibrary
        cls.SettingsBackend = SettingsBackend
        cls.KlayMainWindow = KlayMainWindow

    def setUp(self):
        # Real Qt window/event loop; persistent settings and the host helper are isolated.
        temporary = tempfile.TemporaryDirectory(prefix="klay-jail-ui-test-")
        self.addCleanup(temporary.cleanup)
        from pathlib import Path
        test_root = Path(temporary.name)
        env = mock.patch.dict(os.environ, {
            "XDG_DATA_HOME": str(test_root / "data"),
            "XDG_CONFIG_HOME": str(test_root / "config"),
        })
        env.start()
        self.addCleanup(env.stop)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(test_root))

        def isolated_settings(settings, data_dir_name="klay"):
            settings.schema = settings.state_schema = None
            settings.fallback = {"auto-import": False}
            settings.fallback_state = {}
            settings.fallback_path = test_root / "settings.json"
            settings.fallback_state_path = test_root / "state.json"

        settings_patch = mock.patch.object(self.SettingsBackend, "__init__", isolated_settings)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)

        class Process:
            returncode = None

            def poll(self):
                return self.returncode

        class Library(self.GameLibrary):
            def __init__(library):
                super().__init__("test-jail")
                library.release = threading.Event()
                library.calls = []
                library.process = Process()
                library.failure = ""
                library.stop_release = threading.Event()
                library.stop_calls = []
                library.stop_failure = ""
                library.stop_result = {"status": "stopped"}

            def stop_jail(library, game):
                library.stop_calls.append((game.game_id, threading.get_ident()))
                if not library.stop_release.wait(3):
                    raise OSError("Test stop timed out")
                if library.stop_failure:
                    raise OSError(library.stop_failure)
                return library.stop_result

            def launch(library, game):
                library.calls.append((game.game_id, threading.get_ident()))
                if not library.release.wait(3):
                    raise OSError("Test helper timed out")
                if library.failure:
                    raise OSError(library.failure)
                library.mark_played(game)
                return library.process

            def jail_report(library, game):
                return {"status": "failed", "error": "Runner stopped", "log_path": "/tmp/jail.log"}

        self.library = Library()
        self.game = self.GameEntry(
            self.library.games_dir / "imported_1.json",
            {"game_id": "imported_1", "name": "Test Jail Game", "source": "imported",
             "executable": "xdg-open launcher://game", "jail": valid_config()},
        )
        self.library.save_game(self.game)
        self.window = self.KlayMainWindow(self.library)
        self.errors = []
        self.window._show_error = lambda title, text: self.errors.append((title, text))
        self.window.show()
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        self.library.release.set()
        self.library.stop_release.set()
        if self.window.jail_launch_thread is not None:
            self.window.jail_launch_thread.wait(3500)
        for worker in list(self.window._jail_report_workers):
            worker.wait(3500)
        for worker in list(self.window._jail_folder_workers):
            worker.wait(3500)
        for worker in list(self.window._jail_stop_workers):
            worker.wait(3500)
        self.app.processEvents()
        self.window._jail_processes.clear()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_launch_prepares_off_thread_then_supervises_errors(self):
        ticks = []
        heartbeat = QTimer(self.window)
        heartbeat.setInterval(5)
        heartbeat.timeout.connect(lambda: ticks.append(True))
        heartbeat.start()
        self.assertTrue(self.window.launch_game(self.game))
        self.wait_for(lambda: bool(self.library.calls))
        QTest.qWait(30)
        self.assertTrue(ticks)
        self.assertNotEqual(self.library.calls[0][1], threading.get_ident())
        self.assertIsNotNone(self.window.jail_launch_thread)
        self.assertFalse(self.window.launch_game(self.game))
        self.assertEqual(len(self.library.calls), 1)
        self.assertEqual(self.game.last_played, 0)
        self.library.release.set()
        self.wait_for(lambda: self.window.jail_launch_thread is None)
        self.assertFalse(self.errors)
        self.assertIn(self.game.game_id, self.window._jail_processes)
        self.assertGreater(self.library.load_game_by_id(self.game.game_id).last_played, 0)
        self.library.process.returncode = 5
        self.window._poll_jail_processes()
        self.wait_for(lambda: not self.window._jail_report_workers)
        self.assertTrue(self.errors)
        self.assertIn("Runner stopped", self.errors[-1][1])
        self.assertIn("/tmp/jail.log", self.errors[-1][1])

    def test_launch_failure_never_runs_normal_command(self):
        self.library.failure = "Isolation probe failed"
        self.assertTrue(self.window.launch_game(self.game))
        self.library.release.set()
        self.wait_for(lambda: self.window.jail_launch_thread is None)
        self.assertEqual(len(self.library.calls), 1)
        self.assertTrue(self.errors)
        self.assertIn("Isolation probe failed", self.errors[-1][1])
        self.assertFalse(self.window._jail_processes)
        self.assertTrue(self.window.details_play_btn.isEnabled())

    def test_stale_ordinary_ui_entry_dispatches_current_jail_off_thread(self):
        stale = self.GameEntry(self.game.path, {**self.game.data, "jail": {"enabled": False}})
        self.assertTrue(self.window.launch_game(stale))
        self.wait_for(lambda: bool(self.library.calls))
        self.assertNotEqual(self.library.calls[0][1], threading.get_ident())
        self.assertTrue(self.window.jail_launch_thread.game.jail_enabled)
        self.library.release.set()
        self.wait_for(lambda: self.window.jail_launch_thread is None)
        self.assertIn(stale.game_id, self.window._jail_processes)

    def test_jail_enabled_between_ui_dispatch_and_launch_retries_off_thread(self):
        self.game.set_value("jail", {"enabled": False})
        self.library.save_game(self.game)
        refresh = self.library.refresh_launch_entry
        calls = []

        def refresh_then_enable(game):
            refresh(game)
            calls.append(True)
            if len(calls) == 1:
                current = self.library.load_game_by_id(game.game_id)
                current.set_value("jail", valid_config())
                self.library.save_game(current)

        def ready(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), main_thread)
            return self.library.process

        main_thread = threading.get_ident()
        with (
            mock.patch.object(self.library, "refresh_launch_entry", side_effect=refresh_then_enable),
            mock.patch.object(self.library, "launch", side_effect=lambda game, **kwargs: self.GameLibrary.launch(self.library, game, **kwargs)),
            mock.patch("klay.qt.library.jail.launch_game", side_effect=ready) as jailed,
            mock.patch("klay.qt.library.subprocess.Popen") as ordinary,
        ):
            self.assertTrue(self.window.launch_game(self.game))
            self.wait_for(lambda: self.window.jail_launch_thread is None)
        jailed.assert_called_once()
        ordinary.assert_not_called()
        self.assertIn(self.game.game_id, self.window._jail_processes)
        self.assertFalse(self.errors)

    def test_launch_warning_preserves_supervisor_tracking(self):
        self.library.process.jail_warning = "Play history could not be saved: disk full."
        self.assertTrue(self.window.launch_game(self.game))
        self.library.release.set()
        self.wait_for(lambda: self.window.jail_launch_thread is None)
        self.assertEqual(len(self.library.calls), 1)
        self.assertIs(self.window._jail_processes[self.game.game_id][1], self.library.process)
        self.assertTrue(self.window._jail_monitor_timer.isActive())
        self.assertIn("disk full", self.window.statusBar().currentMessage())
        self.assertFalse(self.errors)

    def test_escape_during_preparation_keeps_supervisor_tracking(self):
        self.assertTrue(self.window.launch_game(self.game))
        dialog = self.window.launch_feedback_dialog
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.wait_for(lambda: self.window.launch_feedback_dialog is None)
        self.library.release.set()
        self.wait_for(lambda: self.window.jail_launch_thread is None)
        self.assertIn(self.game.game_id, self.window._jail_processes)
        self.assertTrue(self.window._jail_monitor_timer.isActive())
        self.assertTrue(self.window.details_play_btn.isEnabled())
        self.assertFalse(self.errors)

    def test_stop_runs_off_thread_then_allows_relaunch_after_helper_exits(self):
        self.window.open_game_details(self.game)
        self.window._jail_processes[self.game.game_id] = (self.game, self.library.process)
        self.window._jail_monitor_timer.start()
        self.assertTrue(self.window.details_jail_stop_button.isEnabled())
        self.window.stop_jail(self.game)
        self.wait_for(lambda: bool(self.library.stop_calls))
        self.assertNotEqual(self.library.stop_calls[0][1], threading.get_ident())
        self.assertFalse(self.window.details_jail_stop_button.isEnabled())
        self.window.stop_jail(self.game)
        self.assertEqual(len(self.library.stop_calls), 1)
        self.assertFalse(self.window.launch_game(self.game))
        self.assertIn("Stopping", self.window.statusBar().currentMessage())

        # A nonzero exit is expected only once the stop operation confirms it.
        self.library.process.returncode = -15
        self.window._poll_jail_processes()
        self.assertIn(self.game.game_id, self.window._jail_processes)
        self.assertFalse(self.window._jail_report_workers)
        self.library.stop_release.set()
        self.wait_for(lambda: not self.window._jail_stop_workers and not self.window._jail_report_workers)
        self.assertNotIn(self.game.game_id, self.window._jail_processes)
        self.assertFalse(self.errors)
        self.assertTrue(self.window.details_jail_stop_button.isEnabled())
        self.library.process.returncode = None
        self.library.release.set()
        self.assertTrue(self.window.launch_game(self.game))
        self.wait_for(lambda: self.window.jail_launch_thread is None)
        self.assertIn(self.game.game_id, self.window._jail_processes)
        self.assertFalse(self.errors)

    def test_stop_acknowledgement_keeps_helper_tracking_until_it_exits(self):
        self.window._jail_processes[self.game.game_id] = (self.game, self.library.process)
        self.window._jail_monitor_timer.start()
        self.library.stop_release.set()
        self.window.stop_jail(self.game)
        self.wait_for(lambda: not self.window._jail_stop_workers)
        self.assertIn(self.game.game_id, self.window._jail_processes)
        self.assertTrue(self.window._jail_monitor_timer.isActive())
        self.assertFalse(self.window.launch_game(self.game))
        self.assertIn("Stop Jail", self.window.statusBar().currentMessage())
        self.library.process.returncode = -15
        self.window._poll_jail_processes()
        self.wait_for(lambda: not self.window._jail_report_workers)
        self.assertFalse(self.errors)

    def test_stop_failure_preserves_process_and_later_exit_diagnostics(self):
        self.window._jail_processes[self.game.game_id] = (self.game, self.library.process)
        self.window._jail_monitor_timer.start()
        self.library.stop_failure = "The running jail could not be verified"
        self.library.stop_release.set()
        self.window.stop_jail(self.game)
        self.wait_for(lambda: not self.window._jail_stop_workers)
        self.assertIn(self.game.game_id, self.window._jail_processes)
        self.assertTrue(self.window._jail_monitor_timer.isActive())
        self.assertEqual(self.errors[-1][0], "Cannot Stop Jail")
        self.assertIn("could not be verified", self.errors[-1][1])
        self.assertNotIn(self.game.game_id, self.window._jail_requested_stops)
        self.library.process.returncode = 5
        self.window._poll_jail_processes()
        self.wait_for(lambda: not self.window._jail_report_workers)
        self.assertIn("Runner stopped", self.errors[-1][1])

    def test_durable_stopped_report_survives_lost_stop_acknowledgement(self):
        self.window._jail_processes[self.game.game_id] = (self.game, self.library.process)
        self.window._jail_monitor_timer.start()
        self.library.stop_failure = "Host helper response was interrupted"
        self.library.stop_release.set()
        self.window.stop_jail(self.game)
        self.wait_for(lambda: not self.window._jail_stop_workers)
        self.assertEqual(self.errors[-1][0], "Cannot Stop Jail")
        self.assertNotIn(self.game.game_id, self.window._jail_requested_stops)
        self.errors.clear()
        self.library.process.returncode = -9
        with mock.patch.object(self.library, "jail_report", return_value={"status": "stopped"}):
            self.window._poll_jail_processes()
            self.wait_for(lambda: not self.window._jail_report_workers)
        self.assertNotIn(self.game.game_id, self.window._jail_processes)
        self.assertFalse(self.errors)
        self.assertIn("Stopped the jail", self.window.statusBar().currentMessage())

    def test_stop_available_for_previous_session_and_idempotent_if_not_running(self):
        self.window.open_game_details(self.game)
        self.assertTrue(self.window.details_jail_stop_button.isVisible())
        self.assertTrue(self.window.details_jail_stop_button.isEnabled())
        self.library.stop_result = {"status": "not_running"}
        self.library.stop_release.set()
        self.window.details_jail_stop_button.click()
        self.wait_for(lambda: not self.window._jail_stop_workers)
        self.assertEqual(len(self.library.stop_calls), 1)
        self.assertIn("not running", self.window.statusBar().currentMessage())
        self.assertFalse(self.window._jail_requested_stops)
        self.assertFalse(self.errors)

    def test_close_defers_until_stop_worker_finishes(self):
        self.window.stop_jail(self.game)
        self.wait_for(lambda: bool(self.library.stop_calls))
        self.window.close()
        self.assertTrue(self.window.isVisible())
        self.assertTrue(self.window._close_after_jail_work)
        self.library.stop_release.set()
        self.wait_for(lambda: not self.window._jail_stop_workers)
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.errors)

    def test_already_running_error_explains_stop_control(self):
        self.library.failure = "This game is already running in its jail"
        self.library.release.set()
        self.assertTrue(self.window.launch_game(self.game))
        self.wait_for(lambda: self.window.jail_launch_thread is None)
        self.assertIn("Stop Jail", self.errors[-1][1])
        self.assertIn("right-click menu", self.errors[-1][1])

    def test_close_defers_until_jail_worker_finishes(self):
        self.assertTrue(self.window.launch_game(self.game))
        self.window.close()
        self.assertTrue(self.window.isVisible())
        self.assertTrue(self.window._close_after_jail_work)
        self.library.release.set()
        self.wait_for(lambda: self.window.jail_launch_thread is None)
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.errors)

    def test_add_dialog_allows_jail_without_normal_command(self):
        assertions = []
        callback_errors = []

        def fill_dialog():
            dialog = self.app.activeModalWidget()
            try:
                edits = dialog.findChildren(QLineEdit)
                name, normal_command = edits[:2]
                name.setText("Direct Jail Game")
                editor = dialog.findChild(JailConfigEditor)
                editor.enabled.setChecked(True)
                editor.game_dir.setText("/home/player/Games/Game")
                editor.exe.setText("Game.exe")
                buttons = dialog.findChild(QDialogButtonBox)
                ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
                assertions.append(ok.isEnabled() and not normal_command.text())
                editor.enabled.setChecked(False)
                assertions.append(not ok.isEnabled())
                editor.enabled.setChecked(True)
                QTest.mouseClick(ok, Qt.MouseButton.LeftButton)
            except Exception as error:
                callback_errors.append(error)
                dialog.reject()

        QTimer.singleShot(0, fill_dialog)
        result = self.window._game_form_dialog(title="Add Game")
        self.assertFalse(callback_errors)
        self.assertEqual(assertions, [True, True])
        self.assertEqual(result[0], "Direct Jail Game")
        self.assertEqual(result[1], "")
        self.assertIs(result[-1]["enabled"], True)

    def test_edit_checkbox_waits_for_detection_then_saves_detected_settings(self):
        self.game.data.pop("jail")
        self.game.data["source"] = "lutris_wine"
        self.library.save_game(self.game)
        callback_errors = []
        calls = []
        assertions = []

        def detect(game):
            calls.append((game.name, threading.get_ident()))
            if not self.library.release.wait(3):
                raise OSError("Test detection timed out")
            return valid_config(runner_dir="/home/player/Runners/custom")

        def begin():
            dialog = self.app.activeModalWidget()
            editor = dialog.findChild(JailConfigEditor)
            ok = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)

            def ready():
                try:
                    assertions.append(ok.isEnabled())
                    assertions.append(not editor.advanced.isChecked())
                    QTest.mouseClick(ok, Qt.MouseButton.LeftButton)
                except Exception as error:
                    callback_errors.append(error)
                    dialog.reject()

            try:
                assertions.append(not calls)
                editor.detection_finished.connect(ready)
                editor.enabled.setChecked(True)
                assertions.append(editor.detecting and not ok.isEnabled())
                self.game.data["name"] = "Changed after lookup started"
                QTimer.singleShot(30, self.library.release.set)
            except Exception as error:
                callback_errors.append(error)
                dialog.reject()

        with mock.patch.object(self.library, "detect_jail_config", side_effect=detect):
            QTimer.singleShot(0, begin)
            result = self.window._game_form_dialog(title="Edit Game", game=self.game)
        self.assertFalse(callback_errors)
        self.assertEqual(assertions, [True, True, True, True])
        self.assertEqual(calls[0][0], "Test Jail Game")
        self.assertNotEqual(calls[0][1], threading.get_ident())
        self.assertEqual(result[-1]["exe"], "bin/My Game.exe")
        self.assertEqual(result[-1]["runner_dir"], "/home/player/Runners/custom")

    def test_window_close_waits_for_edit_detection(self):
        self.game.data.pop("jail")
        callback_errors = []

        def detect(game):
            if not self.library.release.wait(3):
                raise OSError("Test detection timed out")
            return valid_config()

        def begin():
            dialog = self.app.activeModalWidget()
            try:
                editor = dialog.findChild(JailConfigEditor)
                editor.enabled.setChecked(True)
                self.window.close()
                self.assertTrue(self.window.isVisible())
                self.assertTrue(self.window._close_after_jail_work)
                self.assertTrue(dialog.isVisible())
                QTimer.singleShot(30, self.library.release.set)
            except Exception as error:
                callback_errors.append(error)
                self.library.release.set()
                dialog.reject()

        with mock.patch.object(self.library, "detect_jail_config", side_effect=detect):
            QTimer.singleShot(0, begin)
            result = self.window._game_form_dialog(title="Edit Game", game=self.game)
        self.assertFalse(callback_errors)
        self.assertIsNone(result)
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.window._jail_game_dialogs)

    def test_edit_dialog_sizes_wrapped_notice_after_detection_and_advanced_toggles(self):
        self.game.data.pop("jail")
        callback_errors = []
        heights = {}

        def begin():
            dialog = self.app.activeModalWidget()
            editor = dialog.findChild(JailConfigEditor)

            def check_notice():
                self.assertGreaterEqual(
                    editor.note.height(), editor.note.heightForWidth(editor.note.width()),
                )
                self.assertLessEqual(dialog.width(), 700)

            def collapsed_again():
                try:
                    check_notice()
                    self.assertLessEqual(dialog.height(), heights["collapsed"] + 10)
                except Exception as error:
                    callback_errors.append(error)
                dialog.reject()

            def expanded():
                try:
                    check_notice()
                    self.assertGreater(dialog.height(), heights["collapsed"])
                    self.assertLess(dialog.height(), 700)
                    editor.advanced.setChecked(False)
                    QTimer.singleShot(15, collapsed_again)
                except Exception as error:
                    callback_errors.append(error)
                    dialog.reject()

            def collapsed():
                try:
                    check_notice()
                    heights["collapsed"] = dialog.height()
                    self.assertLess(dialog.height(), 500)
                    editor.advanced.setChecked(True)
                    QTimer.singleShot(15, expanded)
                except Exception as error:
                    callback_errors.append(error)
                    dialog.reject()

            editor.detection_finished.connect(lambda: QTimer.singleShot(15, collapsed))
            editor.enabled.setChecked(True)

        with mock.patch.object(self.library, "detect_jail_config", return_value=valid_config()):
            QTimer.singleShot(0, begin)
            self.window._game_form_dialog(title="Edit Game", game=self.game)
        self.assertFalse(callback_errors)
        self.assertIn("collapsed", heights)

    def test_automatic_exit_waits_for_jail_readiness(self):
        self.window.runtime_settings.set_bool("exit-after-launch", True)
        self.assertTrue(self.window.launch_game(self.game))
        self.assertTrue(self.window.isVisible())
        QTest.qWait(30)
        self.assertTrue(self.window.isVisible())
        self.library.release.set()
        self.wait_for(lambda: self.window.jail_launch_thread is None)
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.errors)

    def test_open_folder_is_responsive_and_close_waits_for_worker(self):
        calls = []

        def open_folder(game):
            calls.append((game.game_id, threading.get_ident()))
            if not self.library.release.wait(3):
                raise OSError("Test folder request timed out")

        with mock.patch.object(self.library, "open_jail_folder", side_effect=open_folder):
            ticks = []
            heartbeat = QTimer(self.window)
            heartbeat.setInterval(5)
            heartbeat.timeout.connect(lambda: ticks.append(True))
            heartbeat.start()
            self.window.open_jail_folder(self.game)
            self.wait_for(lambda: bool(calls))
            QTest.qWait(30)
            self.assertTrue(ticks)
            self.assertNotEqual(calls[0][1], threading.get_ident())
            self.window.open_jail_folder(self.game)
            self.assertEqual(len(calls), 1)
            self.window.close()
            self.assertTrue(self.window.isVisible())
            self.assertTrue(self.window._close_after_jail_work)
            self.library.release.set()
            self.wait_for(lambda: not self.window._jail_folder_workers)
            self.assertFalse(self.window.isVisible())
            self.assertFalse(self.errors)

    def test_open_folder_reports_helper_failure(self):
        with mock.patch.object(
            self.library, "open_jail_folder", side_effect=OSError("Host helper unavailable")
        ):
            self.window.open_jail_folder(self.game)
            self.wait_for(lambda: not self.window._jail_folder_workers)
        self.assertTrue(self.errors)
        self.assertEqual(self.errors[-1][0], "Cannot Open Jail Folder")
        self.assertIn("Host helper unavailable", self.errors[-1][1])


if __name__ == "__main__":
    unittest.main()
