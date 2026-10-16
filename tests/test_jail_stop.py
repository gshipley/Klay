from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from klay.qt import jail_helper as helper


class StopFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="klay-stop-test-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.temp.cleanup()

    def store(self, game_id="stop-test"):
        store = helper.Store(game_id, "klay", self.home)
        self.stores.append(store)
        return store


class StopTests(StopFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.pid = 987654
        self.ticks = 4321
        self.holder = self.store()
        self.holder.lock()
        self.holder.write_json("owner.json", {"pid": self.pid, "start_ticks": self.ticks})
        self.holder.write_json("state.json", {"status": "running", "supervisor_pid": self.pid})
        self.target = self.store()
        self.proc = self.root / "proc"
        (self.proc / str(self.pid)).mkdir(parents=True)
        self.arguments = [b"/usr/bin/python3", b"-I", b"/trusted/klay/qt/jail_helper.py", b"--operation", b"launch",
                          b"--game-id", b"stop-test", b"--data-dir-name", b"klay"]
        self.write_arguments()

    def write_arguments(self):
        (self.proc / str(self.pid) / "cmdline").write_bytes(b"\0".join(self.arguments) + b"\0")

    @contextlib.contextmanager
    def verified_process(self, *, unlock_on=signal.SIGTERM, events=None):
        waiter = MagicMock()
        waiter.select.side_effect = events or [[(None, selectors.EVENT_READ)]]
        def send(process_fd, signum):
            if signum == unlock_on:
                fcntl.flock(self.holder.lock_fd, fcntl.LOCK_UN)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(helper, "Path", side_effect=lambda name: self.proc if name == "/proc" else Path(name)))
            stack.enter_context(patch.object(helper, "_process_start_ticks", return_value=self.ticks))
            opened = stack.enter_context(patch.object(helper.os, "pidfd_open", side_effect=lambda pid: os.open("/dev/null", os.O_RDONLY)))
            signals = stack.enter_context(patch.object(helper.signal, "pidfd_send_signal", side_effect=send))
            stack.enter_context(patch.object(helper.selectors, "DefaultSelector", return_value=waiter))
            yield opened, signals, waiter

    def test_idle_jail_does_not_signal_stale_saved_owner_or_rewrite_state(self):
        fcntl.flock(self.holder.lock_fd, fcntl.LOCK_UN)
        with self.verified_process() as (opened, signals, waiter):
            result = helper.stop(self.target)
        self.assertEqual(result["status"], "not_running")
        opened.assert_not_called()
        signals.assert_not_called()
        waiter.register.assert_not_called()
        self.assertEqual(self.target.read_json("state.json")["status"], "running")

    def test_only_verified_matching_supervisor_is_signaled_and_marked_stopped(self):
        with self.verified_process() as (opened, signals, waiter):
            result = helper.stop(self.target)
        self.assertEqual(result["status"], "stopped")
        opened.assert_called_once_with(self.pid)
        self.assertEqual([call.args[1] for call in signals.call_args_list], [signal.SIGTERM])
        waiter.close.assert_called_once()
        self.assertEqual(self.target.read_json("state.json")["status"], "stopped")

    def test_invalid_saved_owner_is_rejected_before_opening_any_process(self):
        for pid in (None, True, 0, 1, -2, "1234", os.getpid()):
            with self.subTest(pid=pid):
                self.holder.write_json("owner.json", {"pid": pid, "start_ticks": self.ticks})
                with self.verified_process() as (opened, signals, waiter), self.assertRaisesRegex(helper.JailError, "identified safely"):
                    helper.stop(self.target)
                opened.assert_not_called()
                signals.assert_not_called()

    def test_reused_process_id_is_rejected_without_signal(self):
        self.holder.write_json("owner.json", {"pid": self.pid, "start_ticks": self.ticks - 1})
        with self.verified_process() as (opened, signals, waiter), self.assertRaisesRegex(helper.JailError, "no longer matches"):
            helper.stop(self.target)
        signals.assert_not_called()

    def test_unrelated_game_data_directory_or_program_is_never_signaled(self):
        original = self.arguments[:]
        replacements = ((b"stop-test", b"another-game"), (b"klay", b"other-app"), (b"launch", b"report"),
                        (b"/trusted/klay/qt/jail_helper.py", b"/trusted/other.py"), (b"-I", b"-u"))
        for old, new in replacements:
            with self.subTest(old=old, new=new):
                self.arguments = [new if value == old else value for value in original]
                self.write_arguments()
                with self.verified_process() as (opened, signals, waiter), self.assertRaisesRegex(helper.JailError, "not this game's"):
                    helper.stop(self.target)
                signals.assert_not_called()

    def test_embedded_flatpak_helper_source_must_match_program_and_game(self):
        self.arguments[2:3] = [b"-c", b'READY_PREFIX = "KLAY_JAIL_READY:"\nclass Store:\n    pass\n']
        self.write_arguments()
        with self.verified_process() as (opened, signals, waiter):
            self.assertEqual(helper.stop(self.target)["status"], "stopped")
        self.assertEqual(signals.call_count, 1)

    def test_legacy_supervisor_uses_saved_state_identity_when_owner_is_absent(self):
        (self.holder.path / "owner.json").unlink()
        self.holder.write_json("state.json", {"status": "running", "supervisor_pid": self.pid, "supervisor_start_ticks": self.ticks})
        with self.verified_process() as (opened, signals, waiter):
            self.assertEqual(helper.stop(self.target)["status"], "stopped")
        self.assertEqual(signals.call_count, 1)

    def test_missing_safe_pid_handles_fail_closed(self):
        with self.verified_process() as (opened, signals, waiter), patch.object(helper.os, "pidfd_open", new=None):
            # Availability is checked with hasattr, as on unsupported Python.
            with patch.object(helper, "os", wraps=helper.os) as platform_os:
                del platform_os.pidfd_open
                with self.assertRaisesRegex(helper.JailError, "safe jail process termination"):
                    helper.stop(self.target)
        signals.assert_not_called()

    def test_supervisor_exits_before_pid_handle_opens_without_signaling_other_pid(self):
        with self.verified_process() as (opened, signals, waiter):
            opened.side_effect = ProcessLookupError
            with self.assertRaisesRegex(helper.JailError, "previous supervisor ended"):
                helper.stop(self.target)
        signals.assert_not_called()

    def test_stubborn_verified_supervisor_gets_bounded_sigkill_escalation(self):
        with self.verified_process(unlock_on=signal.SIGKILL, events=[[], [(None, selectors.EVENT_READ)]]) as (opened, signals, waiter):
            self.assertEqual(helper.stop(self.target)["status"], "stopped")
        self.assertEqual([call.args[1] for call in signals.call_args_list], [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual([call.args[0] for call in waiter.select.call_args_list], [5, 5])

    def test_process_that_remains_busy_after_termination_is_not_reported_stopped(self):
        with self.verified_process(unlock_on=None) as (opened, signals, waiter), self.assertRaisesRegex(helper.JailError, "still busy"):
            helper.stop(self.target)
        self.assertEqual(self.target.read_json("state.json")["status"], "running")

    def test_pid_handle_is_closed_when_a_verified_supervisor_does_not_exit(self):
        descriptors = []
        with self.verified_process(unlock_on=None, events=[[], []]) as (opened, signals, waiter):
            def open_handle(pid):
                descriptor = os.open("/dev/null", os.O_RDONLY)
                descriptors.append(descriptor)
                return descriptor
            opened.side_effect = open_handle
            with self.assertRaisesRegex(helper.JailError, "not finished stopping"):
                helper.stop(self.target)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        waiter.close.assert_called_once()


@unittest.skipUnless(os.environ.get("KLAY_LIVE_JAIL_TESTS") == "1" and shutil.which("bwrap"),
                     "opt in to benign live bubblewrap tests with KLAY_LIVE_JAIL_TESTS=1")
class LiveStopTests(StopFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.processes = []
        self.game = self.root / "source"
        self.game.mkdir()
        (self.game / "Game.exe").write_bytes(b"benign stop test placeholder")
        self.runner = self.root / "runner"
        (self.runner / "bin").mkdir(parents=True)
        wine = self.runner / "bin/wine"
        wine.write_text("#!/usr/bin/python3\nimport pathlib, time\npathlib.Path('/game/runner-started').write_text('benign test runner')\ntime.sleep(120)\n")
        wine.chmod(0o700)
        self.config = {"enabled": True, "game_dir": str(self.game), "exe": "Game.exe", "runner_type": "wine",
                       "runner": str(wine), "runner_dir": str(self.runner), "arguments": []}

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()
        super().tearDown()

    def helper_process(self, operation, game_id="stop-test", *, stubborn=False):
        source = Path(helper.__file__).read_text().replace("from __future__ import annotations", "")
        source = "import pwd,types\npwd.getpwuid = lambda uid: types.SimpleNamespace(pw_dir=" + repr(str(self.home)) + ")\n" + source
        if stubborn:
            # This fixture owns only a temporary store. No game or sandbox is run.
            source = source.replace('if __name__ == "__main__":', 'if False:')
            source += "\nstore = Store('stop-test', 'klay')\nstore.lock()\nstore.write_json('state.json', {'status':'running', 'supervisor_pid':os.getpid()})\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint(json.dumps({'ok':True, 'status':'fixture-ready'}), flush=True)\ntime.sleep(120)\n"
        process = subprocess.Popen([sys.executable, "-I", "-c", source, "--operation", operation,
                                    "--game-id", game_id, "--data-dir-name", "klay"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.processes.append(process)
        return process

    def request(self, operation, game_id="stop-test"):
        process = self.helper_process(operation, game_id)
        output, error = process.communicate(json.dumps(self.config).encode(), timeout=30)
        result = json.loads(output)
        self.assertEqual(process.returncode, 0, result.get("error", error.decode()))
        return result

    def launch(self, game_id="stop-test"):
        process = self.helper_process("launch", game_id)
        process.stdin.write(json.dumps(self.config).encode())
        process.stdin.close()
        with selectors.DefaultSelector() as waiter:
            waiter.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(waiter.select(30), "fixture did not acknowledge startup")
        result = json.loads(process.stdout.readline())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "running")
        self.assertIsNone(process.poll())
        return process

    def test_live_stop_only_selected_jail_and_immediate_relaunch(self):
        selected = self.launch()
        other = self.launch("another-stop-test")
        self.assertEqual(self.request("stop")["status"], "stopped")
        self.assertEqual(selected.wait(timeout=10), 0, selected.stderr.read().decode())
        self.assertIsNone(other.poll(), "stopping one jail also stopped another")
        self.assertEqual(self.request("report")["status"], "stopped")
        relaunched = self.launch()
        self.assertEqual(self.request("stop")["status"], "stopped")
        self.assertEqual(relaunched.wait(timeout=10), 0, relaunched.stderr.read().decode())
        self.assertEqual(self.request("stop")["status"], "not_running")
        self.assertEqual(self.request("stop", "another-stop-test")["status"], "stopped")
        self.assertEqual(other.wait(timeout=10), 0, other.stderr.read().decode())
        self.assertFalse((self.game / "runner-started").exists())
        self.assertEqual((self.game / "Game.exe").read_bytes(), b"benign stop test placeholder")

    def test_live_legacy_owner_file_missing_uses_matching_supervisor_state(self):
        process = self.launch()
        store = self.store()
        (store.path / "owner.json").unlink()
        self.assertEqual(self.request("stop")["status"], "stopped")
        self.assertEqual(process.wait(timeout=10), 0, process.stderr.read().decode())
        self.assertEqual(self.request("report")["status"], "stopped")

    def test_live_stubborn_temporary_supervisor_is_killed_within_bounded_wait(self):
        process = self.helper_process("launch", stubborn=True)
        self.assertEqual(json.loads(process.stdout.readline())["status"], "fixture-ready")
        started = time.monotonic()
        self.assertEqual(self.request("stop")["status"], "stopped")
        self.assertLess(time.monotonic() - started, 12)
        self.assertEqual(process.wait(timeout=2), -signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
