from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import select
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from klay.qt import jail_helper as helper


def lifecycle_functions():
    """Use the actual embedded supervisor's process classification functions."""
    wanted = {
        "WINE_SERVICES", "WINE_WRAPPERS", "read_process", "live_processes",
        "game_path", "is_game_process", "signal_processes",
    }
    nodes = []
    for node in ast.parse(helper.INNER).body:
        names = {node.name} if isinstance(node, ast.FunctionDef) else set()
        if isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        if names & wanted:
            nodes.append(node)
    namespace = {"os": os, "spec": {"proton_prefix": True}, "child": types.SimpleNamespace(pid=2)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<jail-lifecycle>", "exec"), namespace)
    return namespace


class LifecycleProcessTests(unittest.TestCase):
    def setUp(self):
        self.functions = lifecycle_functions()

    def application(self, name, arguments=(), *, pid=3, executable=None):
        process = {"pid": pid, "started": "123", "name": name, "arguments": list(arguments), "executable": executable}
        return self.functions["is_game_process"](process)

    def test_proton_and_umu_are_infrastructure_even_with_target_arguments(self):
        self.assertFalse(self.application("python3", ["/usr/bin/python3", "/runner/proton", "run", "/game/Game.exe"], pid=2))
        self.assertFalse(self.application("umu.exe", [r"C:\windows\system32\umu.exe", "/game/Game.exe"]))
        self.assertFalse(self.application("xalia.exe", [r"C:\windows\system32\xalia.exe"]))

    def test_game_children_are_retained_regardless_of_name_or_launcher_handoff(self):
        self.assertTrue(self.application("ActualGame.exe", [r"Z:\game\bin\ActualGame.exe"]))
        self.assertTrue(self.application("python3", ["/usr/bin/python3", "-c", "a native child"]))
        self.assertTrue(self.application("services.exe", ["/game/services.exe"]))
        self.assertTrue(self.application("umu.exe", ["/game/umu.exe"]))
        self.assertTrue(self.application("rpcss.exe", [r"C:\windows\system32\rpcss.exe", r"Z:\game\plugin.dll"]))

    def test_direct_wine_process_remains_an_application(self):
        self.functions["spec"]["proton_prefix"] = False
        self.assertTrue(self.application("wine", ["/runner/bin/wine", "/game/Game.exe"], pid=2))

    def test_non_system_windows_image_overrides_service_and_wrapper_names(self):
        for name in ("services.exe", "explorer.exe", "umu.exe"):
            for location in ("C:\\Games\\", "C:\\Games\\windows\\system32\\", "\\\\?\\C:\\Games\\"):
                with self.subTest(name=name, location=location):
                    self.assertTrue(self.application(name, [location + name]))
        for location in ("C:\\windows\\system32\\", "C:\\windows\\syswow64\\", "C:\\windows\\"):
            self.assertFalse(self.application("services.exe", [location + "services.exe"]))
        for location in ("Z:\\runner\\files\\share\\xalia\\", "Z:\\usr\\share\\xalia\\"):
            self.assertFalse(self.application("xalia.exe", [location + "xalia.exe"]))
        self.assertTrue(self.application("xalia.exe", [r"Z:\runner-game\xalia.exe"]))

    def test_private_dbus_helpers_require_the_system_executable(self):
        for executable in ("/usr/bin/dbus-daemon", "/usr/bin/dbus-launch"):
            with self.subTest(executable=executable):
                self.assertFalse(self.application(Path(executable).name, [executable, "--session"], executable=executable))
                self.assertTrue(self.application(Path(executable).name, ["/game/" + Path(executable).name], executable=executable))
                self.assertTrue(self.application(Path(executable).name, [executable], executable="/usr/bin/python3"))

    def test_known_services_and_zombies_are_not_applications(self):
        self.assertFalse(self.application("wineserver"))
        self.assertFalse(self.application("services.exe", [r"C:\windows\system32\services.exe"]))
        self.assertFalse(self.application("winemenubuilder", [r"C:\windows\system32\winemenubuilder.exe"]))
        with tempfile.TemporaryDirectory(prefix="klay-proc-test-") as temporary:
            root = Path(temporary)
            self.write_process(root, 52, state="Z")
            self.assertIsNone(self.functions["read_process"](52, str(root)))

    def write_process(self, root, pid, *, state="S", started="123", name="services.exe", arguments=("services.exe",)):
        directory = root / str(pid)
        directory.mkdir(exist_ok=True)
        # /proc stat fields after the process name begin with state (field 3).
        fields = [state, "1", *("0" for _ in range(17)), started]
        (directory / "stat").write_text(f"{pid} ({name}) " + " ".join(fields))
        (directory / "comm").write_text(name + "\n")
        (directory / "cmdline").write_bytes(b"\0".join(argument.encode() for argument in arguments) + b"\0")

    def test_signals_require_the_same_process_start_time(self):
        with tempfile.TemporaryDirectory(prefix="klay-proc-test-") as temporary:
            root = Path(temporary)
            self.write_process(root, 52, started="123")
            snapshot = self.functions["live_processes"](str(root))
            self.assertEqual([process["pid"] for process in snapshot], [52])
            self.write_process(root, 52, started="456")
            with patch.object(os, "kill") as kill:
                self.functions["signal_processes"](snapshot, signal.SIGTERM, proc_root=str(root))
            kill.assert_not_called()

    def test_idle_shutdown_preserves_waiting_wrappers_initially(self):
        with tempfile.TemporaryDirectory(prefix="klay-proc-test-") as temporary:
            root = Path(temporary)
            for pid, name in ((2, "python3"), (3, "umu.exe"), (4, "services.exe")):
                self.write_process(root, pid, name=name)
            processes = self.functions["live_processes"](str(root))
            with patch.object(os, "kill") as kill:
                self.functions["signal_processes"](processes, signal.SIGTERM, preserve_wrappers=True, proc_root=str(root))
            kill.assert_called_once_with(4, signal.SIGTERM)


@unittest.skipUnless(os.environ.get("KLAY_LIVE_JAIL_TESTS") == "1" and shutil.which("bwrap"), "opt in to benign live lifecycle tests with KLAY_LIVE_JAIL_TESTS=1")
class LiveLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="klay-lifecycle-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.game = self.root / "source"
        self.game.mkdir()
        self.runner = self.root / "trusted-proton"
        self.runner.mkdir()
        self.wrapper = self.runner / "proton"
        self.wrapper.write_text("#!/usr/bin/python3\n")
        self.wrapper.chmod(0o700)
        self.config = {"enabled": True, "game_dir": str(self.game), "exe": "Game.exe", "runner_type": "proton", "runner": str(self.runner), "arguments": []}

    def helper_process(self, operation, *, application_start_timeout=None):
        source = Path(helper.__file__).read_text()
        if application_start_timeout is not None:
            source = source.replace('"application_start_timeout": 600',
                                    '"application_start_timeout": ' + repr(application_start_timeout))
        wrapper = "import pwd,types\npwd.getpwuid = lambda uid: types.SimpleNamespace(pw_dir=" + repr(str(self.home)) + ")\n" + source.replace("from __future__ import annotations", "")
        process = subprocess.Popen([sys.executable, "-I", "-c", wrapper, "--operation", operation, "--game-id", "lifecycle-test", "--data-dir-name", "klay"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.addCleanup(self.ensure_stopped, process)
        return process

    def ensure_stopped(self, process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    def configure(self, application, *, startup_delay=0, runner_exit=0, dbus=False):
        (self.game / "Game.exe").write_text(application)
        # The fake Proton wrapper waits for a persistent Wine service after
        # its game exits, reproducing the stale running state without Wine.
        service_command = ["/usr/bin/dbus-daemon", "--session", "--nofork"] if dbus else ["/usr/bin/python3", "-c", "import ctypes,time; ctypes.CDLL(None).prctl(15,b'services.exe',0,0,0); time.sleep(60)"]
        self.wrapper.write_text("#!/usr/bin/python3\n" + f"""
import subprocess, sys, time
service = subprocess.Popen({service_command!r})
time.sleep({startup_delay!r})
game = subprocess.Popen(['/usr/bin/python3', sys.argv[2]])
game.wait()
{'sys.exit(' + str(runner_exit) + ')' if runner_exit else 'service.wait()'}
""")

    def launch(self, *, timeout=18, application_start_timeout=None):
        process = self.helper_process("launch", application_start_timeout=application_start_timeout)
        started = time.monotonic()
        output, error = process.communicate(json.dumps(self.config).encode(), timeout=timeout)
        response = json.loads(output)
        self.assertTrue(response["ok"], response.get("error", error.decode()))
        reporter = self.helper_process("report")
        report_output, report_error = reporter.communicate(json.dumps(self.config).encode(), timeout=10)
        self.assertEqual(reporter.returncode, 0, report_error.decode())
        return process.returncode, json.loads(report_output), time.monotonic() - started

    def test_game_exit_stops_persistent_services_and_allows_another_launch(self):
        self.configure("import time\ntime.sleep(.6)\nprint('benign game exited', flush=True)\n")
        for _ in range(2):
            code, report, elapsed = self.launch()
            self.assertEqual(code, 0, report["log_text"])
            self.assertEqual(report["status"], "exited")
            self.assertIn("stopping remaining Wine services", report["log_text"])
            self.assertLess(elapsed, 12)

    def test_delayed_initialization_is_not_mistaken_for_game_exit(self):
        self.configure("import pathlib,time\ntime.sleep(.6)\npathlib.Path('/game/started').write_text('started')\n", startup_delay=4)
        code, report, elapsed = self.launch(application_start_timeout=6)
        self.assertEqual(code, 0, report["log_text"])
        self.assertGreater(elapsed, 7)
        self.assertFalse((self.game / "started").exists())
        self.assertTrue((Path(report["jail_dir"]) / "game/started").is_file())

    def test_only_services_have_bounded_startup_and_can_relaunch(self):
        self.configure("pass\n", startup_delay=.3)
        # The stable process state after an unobserved short child is identical
        # to a wrapper that starts services without ever starting an application.
        self.wrapper.write_text(self.wrapper.read_text().replace(
            "game = subprocess.Popen(['/usr/bin/python3', sys.argv[2]])\ngame.wait()",
            "# No application process"))
        code, report, elapsed = self.launch(application_start_timeout=1)
        self.assertEqual(code, 124, report["log_text"])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["exit_code"], 124)
        self.assertIn("application startup timed out", report["log_text"])
        self.assertLess(elapsed, 15)
        self.configure("import time\ntime.sleep(.6)\n")
        # A relaunch uses the persistent private snapshot, not the source file
        # configure() writes. Update only this temporary test jail's payload.
        (Path(report["jail_dir"]) / "game/Game.exe").write_text("import time\ntime.sleep(.6)\n")
        code, report, _ = self.launch(application_start_timeout=3)
        self.assertEqual(code, 0, report["log_text"])

    def test_silent_quick_application_cannot_leave_a_waiting_wrapper(self):
        self.configure("pass\n", startup_delay=.3)
        code, report, elapsed = self.launch(application_start_timeout=1)
        # Depending on scheduling, the quick child either appears in a /proc
        # snapshot and exits normally, or receives the explicit startup timeout.
        self.assertIn((code, report["exit_code"]), ((0, 0), (124, 124)))
        self.assertLess(elapsed, 15)

    def test_observed_application_can_outlive_startup_budget(self):
        self.configure("import time\ntime.sleep(2)\n")
        code, report, elapsed = self.launch(application_start_timeout=1)
        self.assertEqual(code, 0, report["log_text"])
        self.assertEqual(report["status"], "exited")
        self.assertGreater(elapsed, 4)

    def test_differently_named_native_child_survives_launcher_exit(self):
        self.configure("""import subprocess,time
subprocess.Popen(['/usr/bin/python3', '-c', "import pathlib,time; time.sleep(4); pathlib.Path('/game/child-finished').write_text('finished')"])
time.sleep(.6)
""")
        code, report, elapsed = self.launch()
        self.assertEqual(code, 0, report["log_text"])
        self.assertGreater(elapsed, 7)
        self.assertTrue((Path(report["jail_dir"]) / "game/child-finished").is_file())

    def test_nonzero_runner_exit_is_preserved(self):
        self.configure("import time\ntime.sleep(.6)\n", runner_exit=7)
        code, report, _elapsed = self.launch()
        self.assertEqual(code, 7, report["log_text"])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["exit_code"], 7)

    @unittest.skipUnless(Path("/usr/bin/dbus-daemon").is_file(), "host has no dbus-daemon")
    def test_private_dbus_service_does_not_keep_the_game_running(self):
        self.configure("import time\ntime.sleep(.6)\n", dbus=True)
        code, report, elapsed = self.launch()
        self.assertEqual(code, 0, report["log_text"])
        self.assertEqual(report["status"], "exited")
        self.assertLess(elapsed, 12)

    def test_signal_cleanup_is_bounded_when_game_and_wrapper_ignore_term(self):
        self.configure("import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n")
        self.wrapper.write_text(self.wrapper.read_text().replace("import subprocess, sys, time", "import signal, subprocess, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)"))
        process = self.helper_process("launch")
        process.stdin.write(json.dumps(self.config).encode())
        process.stdin.close()
        process.stdin = None
        self.assertTrue(select.select([process.stdout], [], [], 20)[0], "Jail launch did not report readiness")
        response = json.loads(process.stdout.readline())
        self.assertTrue(response["ok"], response)
        time.sleep(1)
        started = time.monotonic()
        process.terminate()
        process.communicate(timeout=8)
        self.assertLess(time.monotonic() - started, 7)


if __name__ == "__main__":
    unittest.main()
