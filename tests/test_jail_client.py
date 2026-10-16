from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from klay.qt import jail


class JailClientTests(unittest.TestCase):
    def helper(self, program: str) -> list[str]:
        return [sys.executable, "-I", "-c", program]

    def test_ready_supervisor_stays_alive_and_preserves_exit_failure(self) -> None:
        command = self.helper(
            "import json,sys,time; json.load(sys.stdin); "
            "print(json.dumps({'ok':True,'status':'running'}),flush=True); "
            "time.sleep(.3); sys.exit(7)"
        )
        with patch.object(jail, "_helper_command", return_value=command):
            process = jail.launch_game({"enabled": True}, game_id="test")
        self.assertIsNone(process.poll())
        self.assertEqual(process.wait(timeout=5), 7)

    def test_game_arguments_are_json_data(self) -> None:
        value = "; $(touch /tmp/should-not-exist) ' \" \\ unicode 雪"
        command = self.helper(
            "import json,sys; data=json.load(sys.stdin); "
            "print(json.dumps({'ok':True,'status':'not_started','received':data}),flush=True)"
        )
        with patch.object(jail, "_helper_command", return_value=command):
            report = jail.game_report({"arguments": [value]}, game_id="test")
        self.assertEqual(report["received"]["arguments"], [value])

    def test_failed_preflight_raises(self) -> None:
        command = self.helper(
            "import json,sys; json.load(sys.stdin); "
            "print(json.dumps({'ok':False,'error':'network namespace unavailable'}),flush=True); "
            "sys.exit(1)"
        )
        with patch.object(jail, "_helper_command", return_value=command):
            with self.assertRaisesRegex(jail.JailError, "network namespace unavailable"):
                jail.launch_game({"enabled": True}, game_id="test")

    def test_success_without_readiness_is_rejected(self) -> None:
        command = self.helper(
            "import json,sys; json.load(sys.stdin); print(json.dumps({'ok':True}),flush=True)"
        )
        with patch.object(jail, "_helper_command", return_value=command):
            with self.assertRaisesRegex(jail.JailError, "did not confirm"):
                jail.launch_game({"enabled": True}, game_id="test")

    def test_timeout_terminates_helper(self) -> None:
        command = self.helper("import sys,time; sys.stdin.read(); time.sleep(30)")
        real_popen = subprocess.Popen
        children: list[subprocess.Popen] = []

        def start(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            return process

        with patch.object(jail, "_helper_command", return_value=command), patch.object(
            jail.subprocess, "Popen", side_effect=start
        ):
            with self.assertRaisesRegex(jail.JailError, "timed out"):
                jail._start("launch", {}, game_id="test", data_dir_name="klay", timeout=.1)
        self.assertIsNotNone(children[0].poll())

    def test_flatpak_forwards_trusted_source_without_shell(self) -> None:
        with patch.dict(os.environ, {"FLATPAK_ID": "com.grantshipley.Klay"}), patch.object(
            Path, "read_text", return_value="print('trusted helper')"
        ):
            command = jail._helper_command("launch", "game ; $()", "klay")
        self.assertEqual(command[:7], ["flatpak-spawn", "--host", "--watch-bus", "python3", "-I", "-c", "print('trusted helper')"])
        self.assertIn("game ; $()", command)
        self.assertNotIn("sh", command)

    def test_interruption_during_preparation_reaps_helper(self) -> None:
        command = self.helper("import sys,time; sys.stdin.read(); time.sleep(30)")
        real_popen = subprocess.Popen
        children = []

        def start(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            return process

        with (
            patch.object(jail, "_helper_command", return_value=command),
            patch.object(jail.subprocess, "Popen", side_effect=start),
            patch.object(jail, "_read_response", side_effect=KeyboardInterrupt),
        ):
            with self.assertRaises(KeyboardInterrupt):
                jail.launch_game({}, game_id="test")
        self.assertIsNotNone(children[0].poll())

    def test_unsupported_platform_refuses_jail(self) -> None:
        with patch.object(jail.sys, "platform", "darwin"):
            with self.assertRaisesRegex(jail.JailError, "Linux only"):
                jail._helper_command("launch", "test", "klay")


if __name__ == "__main__":
    unittest.main()
