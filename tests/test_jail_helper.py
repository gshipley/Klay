from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from klay.qt import jail_helper as helper


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="klay-jail-test-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.game = self.root / "source"
        self.game.mkdir()
        (self.game / "Game.exe").write_bytes(b"benign test placeholder")
        self.runner = self.root / "trusted-proton"
        self.runner.mkdir()
        (self.runner / "proton").write_text("#!/usr/bin/python3\nprint('benign runner')\n")
        (self.runner / "proton").chmod(0o700)
        self.config = {"enabled": True, "game_dir": str(self.game), "exe": "Game.exe", "runner_type": "proton", "runner": str(self.runner), "arguments": []}
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.temp.cleanup()

    def store(self, game_id="test"):
        result = helper.Store(game_id, "klay", self.home)
        self.stores.append(result)
        return result

    def config_ok(self):
        return helper.validate_config(self.config)

    def test_config_rejects_parent_traversal_and_runner_from_game(self):
        for exe in ("../Game.exe", "/Game.exe", "sub/../../Game.exe", "Game.bat"):
            with self.subTest(exe=exe), self.assertRaises(helper.JailError):
                helper.validate_config(dict(self.config, exe=exe))
        local_runner = self.game / "proton"
        local_runner.write_text("#!/usr/bin/python3\n")
        local_runner.chmod(0o700)
        with self.assertRaisesRegex(helper.JailError, "outside"):
            helper.validate_config(dict(self.config, runner=str(self.game)))

    def test_selected_executable_symlink_is_rejected(self):
        (self.game / "Game.exe").unlink()
        (self.game / "Game.exe").symlink_to(self.runner / "proton")
        with self.assertRaisesRegex(helper.JailError, "symlink"):
            helper.prepare_game(self.store(), self.config_ok())

    def test_copy_rejects_symlinks_without_writing_their_target(self):
        target = self.root / "host-file"
        target.write_text("keep me")
        (self.game / "_bootstrap.bat").symlink_to(target)
        store = self.store()
        with self.assertRaisesRegex(helper.JailError, "symlink"):
            helper.prepare_game(store, self.config_ok())
        self.assertEqual(target.read_text(), "keep me")
        self.assertFalse((store.path / "game").exists())
        self.assertFalse(list(store.path.glob(".copy-*")))

    def test_copy_rejects_special_files(self):
        os.mkfifo(self.game / "fifo")
        with self.assertRaisesRegex(helper.JailError, "special file"):
            helper.prepare_game(self.store(), self.config_ok())

    def test_streamed_copy_size_limit_stops_growth_and_discards_partial_copy(self):
        store = self.store()
        with patch.object(helper, "MAX_COPY_BYTES", 20):
            with self.assertRaisesRegex(helper.JailError, "150 GiB"):
                helper.prepare_game(store, self.config_ok())
        self.assertFalse((store.path / "game").exists())
        self.assertFalse(list(store.path.glob(".copy-*")))

    def test_failed_unpublished_setup_can_be_retried(self):
        store = self.store()
        (store.path / "game").mkdir()
        (store.path / "game/partial").write_text("not yet published")
        (store.path / "prefix").mkdir()
        helper.prepare_game(store, self.config_ok())
        self.assertFalse((store.path / "game/partial").exists())
        self.assertTrue((store.path / "game/Game.exe").is_file())

    def test_selected_source_symlink_is_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.game, target_is_directory=True)
        with self.assertRaisesRegex(helper.JailError, "symlink"):
            helper.prepare_game(self.store(), helper.validate_config(dict(self.config, game_dir=str(alias))))

    def test_ancestor_alias_is_allowed_and_missing_original_relaunches(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        config = helper.validate_config(dict(self.config, game_dir=str(alias / "source")))
        store = self.store()
        helper.prepare_game(store, config)
        shutil.rmtree(self.game)
        helper.prepare_game(store, helper.validate_config(config))
        self.assertTrue((store.path / "game/Game.exe").exists())

    def test_fifo_selected_executable_is_rejected_without_blocking(self):
        (self.game / "Game.exe").unlink()
        os.mkfifo(self.game / "Game.exe")
        fd = helper._open_directory(self.game)
        try:
            with self.assertRaisesRegex(helper.JailError, "regular file"):
                helper._regular_at(fd, "Game.exe")
        finally:
            os.close(fd)

    def test_wine_name_resolves_on_host_and_must_stay_under_usr(self):
        with patch.object(helper.shutil, "which", return_value="/usr/bin/true"):
            config = helper.validate_config(dict(self.config, runner_type="wine", runner="wine"))
            self.assertEqual(config["runner"], str(Path("/usr/bin/true").resolve()))
        with patch.object(helper.shutil, "which", return_value=str(self.runner / "proton")):
            with self.assertRaisesRegex(helper.JailError, "system Wine"):
                helper.validate_config(dict(self.config, runner_type="wine", runner="wine"))

    def custom_wine(self, root=None):
        root = root or self.runner
        binary = root / "bin/wine"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/usr/bin/python3\nprint('benign custom wine')\n")
        binary.chmod(0o700)
        return dict(self.config, runner_type="wine", runner=str(binary), runner_dir=str(root))

    def test_custom_wine_build_mount_is_readonly_and_exact_executable_is_rewritten(self):
        config = helper.validate_config(self.custom_wine())
        store = self.store()
        with patch.object(helper, "check_dependencies", return_value=("/usr/bin/bwrap", "/usr/bin/python3")):
            command = helper.build_command(config, store.path, store.path)
        self.assertIn(str(self.runner), command)
        spec = json.loads(command[-1])
        self.assertEqual(spec["argv"][0], "/runner/bin/wine")
        self.assertIn("/runner", spec["readonly"])
        bindings = [(command[index + 1], command[index + 2]) for index, word in enumerate(command) if word == "--ro-bind"]
        self.assertIn((str(self.runner), "/runner"), bindings)
        self.assertNotIn((str(self.runner.parent), "/runner"), bindings)

    def test_custom_wine_aliases_are_canonical_and_executable_stays_in_build(self):
        config = self.custom_wine()
        alias = self.root / "wine-alias"
        alias.symlink_to(self.runner, target_is_directory=True)
        config.update(runner_dir=str(alias), runner=str(alias / "bin/wine"))
        checked = helper.validate_config(config)
        self.assertEqual(checked["runner_dir"], str(self.runner))
        self.assertEqual(checked["runner"], str(self.runner / "bin/wine"))
        with self.assertRaisesRegex(helper.JailError, "inside"):
            helper.validate_config(dict(config, runner="/usr/bin/true"))

    def test_custom_wine_rejects_home_parent_and_game_overlap(self):
        home_config = self.custom_wine(self.home)
        with patch.object(helper, "_host_home", return_value=self.home), self.assertRaisesRegex(helper.JailError, "specific Wine build"):
            helper.validate_config(home_config)
        parent_config = self.custom_wine(self.root)
        with self.assertRaisesRegex(helper.JailError, "separate"):
            helper.validate_config(parent_config)
        inside_config = self.custom_wine(self.game / "untrusted-runner")
        with self.assertRaisesRegex(helper.JailError, "outside"):
            helper.validate_config(inside_config)

    def test_custom_wine_rejects_jail_storage_overlap(self):
        store = self.store()
        config = helper.validate_config(self.custom_wine(store.path / "runner-build"))
        with self.assertRaisesRegex(helper.JailError, "outside jail storage"):
            helper.prepare_game(store, config)

    def test_bind_alias_identity_cannot_bypass_broad_wine_root_check(self):
        config = self.custom_wine()
        original = helper.os.path.samefile
        def identity(left, right):
            if {Path(left), Path(right)} == {self.runner, self.home}:
                return True
            return original(left, right)
        with patch.object(helper, "_host_home", return_value=self.home), patch.object(helper.os.path, "samefile", side_effect=identity):
            with self.assertRaisesRegex(helper.JailError, "specific Wine build"):
                helper.validate_config(config)

    def test_saved_proton_wine_settings_use_the_same_validated_build_wrapper(self):
        for layout in ("files/bin/wine", "files/bin/wine64", "dist/bin/wine", "dist/bin/wine64"):
            with self.subTest(layout=layout):
                wine = self.runner / layout
                wine.parent.mkdir(parents=True, exist_ok=True)
                wine.write_text("#!/bin/true\n")
                wine.chmod(0o700)
                config = helper.validate_config(dict(self.config, runner_type="wine", runner=str(wine), runner_dir=str(self.runner)))
                self.assertEqual(config["runner_type"], "proton")
                self.assertEqual(config["runner"], str(self.runner))
                self.assertIsNone(config["runner_dir"])

    def test_saved_proton_wine_cannot_use_an_escaping_or_nonexecutable_wrapper(self):
        wine = self.runner / "files/bin/wine"
        wine.parent.mkdir(parents=True)
        wine.write_text("#!/bin/true\n")
        wine.chmod(0o700)
        config = dict(self.config, runner_type="wine", runner=str(wine), runner_dir=str(self.runner))
        wrapper = self.runner / "proton"
        wrapper.chmod(0o600)
        with self.assertRaisesRegex(helper.JailError, "entry point"):
            helper.validate_config(config)
        outside = self.root / "unrelated-proton"
        outside.write_text("#!/bin/true\n")
        outside.chmod(0o700)
        wrapper.unlink()
        wrapper.symlink_to(outside)
        with self.assertRaisesRegex(helper.JailError, "entry point"):
            helper.validate_config(config)

    def test_standard_wine_is_not_promoted_by_an_unrelated_proton_wrapper(self):
        config = helper.validate_config(self.custom_wine())
        self.assertEqual(config["runner_type"], "wine")
        self.assertEqual(config["runner"], str(self.runner / "bin/wine"))
        self.assertEqual(config["runner_dir"], str(self.runner))
        wine = self.runner / "files/bin/wine"
        wine.parent.mkdir(parents=True)
        wine.write_text("#!/bin/true\n")
        wine.chmod(0o700)
        (self.runner / "proton").unlink()
        config = helper.validate_config(dict(self.config, runner_type="wine", runner=str(wine), runner_dir=str(self.runner)))
        self.assertEqual(config["runner_type"], "wine")
        self.assertEqual(config["runner_dir"], str(self.runner))

    def test_legacy_distribution_wine_requires_its_proton_wrapper(self):
        wine = self.runner / "dist/bin/wine"
        wine.parent.mkdir(parents=True)
        wine.write_text("#!/bin/true\n")
        wine.chmod(0o700)
        (self.runner / "proton").unlink()
        with self.assertRaisesRegex(helper.JailError, "Proton entry point"):
            helper.validate_config(dict(self.config, runner_type="wine", runner=str(wine), runner_dir=str(self.runner)))

    def test_saved_proton_build_cannot_contain_the_original_game(self):
        wine = self.runner / "files/bin/wine"
        wine.parent.mkdir(parents=True)
        wine.write_text("#!/bin/true\n")
        wine.chmod(0o700)
        game = self.runner / "original-game"
        game.mkdir()
        with self.assertRaisesRegex(helper.JailError, "separate"):
            helper.validate_config(dict(self.config, game_dir=str(game), runner_type="wine", runner=str(wine), runner_dir=str(self.runner)))

    def test_working_directory_is_relative_and_defaults_to_executable_parent(self):
        config = helper.validate_config(dict(self.config, exe="sub/Game.exe"))
        self.assertEqual(config["working_dir"], "sub")
        for invalid in ("/tmp", "../other", "sub/../../other", "bad\x00path", [], 3, ""):
            with self.subTest(invalid=invalid), self.assertRaises(helper.JailError):
                helper.validate_config(dict(self.config, working_dir=invalid))
        self.assertEqual(helper.validate_config(dict(self.config, working_dir="."))["working_dir"], ".")

    def test_explicit_working_directory_controls_sandbox_cwd(self):
        store = self.store()
        config = helper.validate_config(dict(self.config, exe="bin/Game.exe", working_dir="assets"))
        with patch.object(helper, "check_dependencies", return_value=("/usr/bin/bwrap", "/usr/bin/python3")):
            command = helper.build_command(config, store.path, store.path)
        self.assertEqual(json.loads(command[-1])["cwd"], "/game/assets")
        config = helper.validate_config(dict(self.config, exe="bin/Game.exe", working_dir="."))
        with patch.object(helper, "check_dependencies", return_value=("/usr/bin/bwrap", "/usr/bin/python3")):
            command = helper.build_command(config, store.path, store.path)
        self.assertEqual(json.loads(command[-1])["cwd"], "/game")

    def test_working_directory_symlink_cannot_escape_selected_game(self):
        (self.game / "assets").symlink_to(self.root, target_is_directory=True)
        root_fd = helper._open_directory(self.game)
        try:
            with self.assertRaises(OSError):
                helper._directory_at(root_fd, "assets")
        finally:
            os.close(root_fd)

    def test_snapshot_is_separate_persistent_and_source_unchanged(self):
        store = self.store()
        config = self.config_ok()
        helper.prepare_game(store, config)
        self.assertEqual((store.path / "game/Game.exe").read_bytes(), (self.game / "Game.exe").read_bytes())
        (store.path / "game/Game.exe").write_bytes(b"jail-only change")
        host_file = self.root / "host-target"
        host_file.write_bytes(b"unchanged")
        # Subsequent preparation does no bootstrap/host writes in mutable trees.
        (store.path / "game/_bootstrap.bat").symlink_to(host_file)
        (store.path / "prefix/system.reg").symlink_to(host_file)
        helper.prepare_game(store, config)
        self.assertEqual(host_file.read_bytes(), b"unchanged")
        self.assertEqual((self.game / "Game.exe").read_bytes(), b"benign test placeholder")
        self.assertEqual((store.path / "game/Game.exe").read_bytes(), b"jail-only change")

    def test_different_games_have_separate_storage_and_nonblocking_locks(self):
        one = self.store("one")
        two = self.store("two")
        self.assertNotEqual(one.path, two.path)
        one.lock()
        same = self.store("one")
        with self.assertRaisesRegex(helper.JailError, "already"):
            same.lock()
        two.lock()

    def test_trusted_storage_symlink_rejected(self):
        destination = self.root / "other"
        destination.mkdir()
        (self.home / ".local").symlink_to(destination)
        with self.assertRaises(OSError):
            self.store()
        self.assertFalse(list(destination.iterdir()))

    def test_dependency_error_fails_closed(self):
        with patch.object(helper.shutil, "which", return_value=None):
            with self.assertRaisesRegex(helper.JailError, "bubblewrap"):
                helper.check_dependencies()

    def test_mount_policy_and_arguments_do_not_inherit_host_environment(self):
        store = self.store()
        config = self.config_ok()
        config["arguments"] = ["literal ; $(touch /tmp/forbidden)", "two words"]
        with patch.dict(os.environ, {"GITHUB_TOKEN": "private", "SSH_AUTH_SOCK": "/tmp/agent", "DISPLAY": "", "WAYLAND_DISPLAY": ""}), patch.object(helper, "check_dependencies", return_value=("/usr/bin/bwrap", "/usr/bin/python3")):
            command = helper.build_command(config, store.path, store.path)
        self.assertIn("--unshare-all", command)
        self.assertIn("--clearenv", command)
        self.assertIn("--cap-drop", command)
        self.assertNotIn("private", command)
        self.assertNotIn("GITHUB_TOKEN", command)
        self.assertNotIn("/tmp/agent", command)
        bindings = [(command[index + 1], command[index + 2]) for index, word in enumerate(command) if word in ("--bind", "--ro-bind", "--dev-bind")]
        self.assertNotIn((str(self.home), str(self.home)), bindings)
        self.assertNotIn(("/etc", "/etc"), bindings)
        self.assertNotIn(("/run", "/run"), bindings)
        self.assertNotIn((str(store.path), str(store.path)), bindings)
        self.assertIn((str(store.path / "game"), "/game"), bindings)
        spec = json.loads(command[-1])
        self.assertEqual(spec["argv"][-2:], config["arguments"])
        self.assertNotIn("sh", spec["argv"])

    def test_gpu_metadata_binds_only_selected_physical_gpu_and_aliases(self):
        sysfs = self.root / "sys"
        physical = sysfs / "devices/pci0000:00/0000:00:02.0"
        render = physical / "drm/renderD128"
        render.mkdir(parents=True)
        (render / "device").symlink_to(physical, target_is_directory=True)
        drm_class = sysfs / "class/drm"
        drm_class.mkdir(parents=True)
        (drm_class / "renderD128").symlink_to(render, target_is_directory=True)
        unrelated = sysfs / "devices/pci0000:00/unselected/drm/renderD129"
        unrelated.mkdir(parents=True)
        (drm_class / "renderD129").symlink_to(unrelated, target_is_directory=True)
        char = sysfs / "dev/char"
        char.mkdir(parents=True)
        (char / "226:128").symlink_to(render, target_is_directory=True)
        bus = sysfs / "bus/pci"
        driver = bus / "drivers/test_gpu"
        driver.mkdir(parents=True)
        (physical / "subsystem").symlink_to(bus, target_is_directory=True)
        (physical / "driver").symlink_to(driver, target_is_directory=True)
        command, readonly = helper.gpu_sysfs_args([(Path("/dev/dri/renderD128"), os.makedev(226, 128))], sysfs)
        bindings = [(command[index + 1], command[index + 2]) for index, word in enumerate(command) if word == "--ro-bind"]
        self.assertEqual(bindings, [(str(physical), "/sys/devices/pci0000:00/0000:00:02.0")])
        self.assertEqual(readonly, ["/sys/devices/pci0000:00/0000:00:02.0"])
        self.assertIn("/sys/class/drm/renderD128", command)
        self.assertIn("/sys/dev/char/226:128", command)
        self.assertNotIn("/sys/class/drm/renderD129", command)
        self.assertNotIn("/sys/class/net", command)

    def test_nvidia_module_metadata_is_exact_readonly_and_requires_a_shared_gpu(self):
        sysfs = self.root / "sys"
        module = sysfs / "module/nvidia"
        module.mkdir(parents=True)
        (module / "version").write_text("test-driver\n")
        (module / "holders").mkdir()
        (module / "holders/nvidia_uvm").symlink_to("../../nvidia_uvm")
        for devices in ([], [Path("/dev/dri/renderD128")], [Path("/dev/nvidiactl"), Path("/dev/nvidia-uvm")], [Path("/dev/nvidia0-unrelated")]):
            with self.subTest(devices=devices):
                self.assertEqual(helper.nvidia_module_args(devices, sysfs), ([], []))
        command, readonly = helper.nvidia_module_args([Path("/dev/nvidia0")], sysfs)
        self.assertEqual(command, ["--ro-bind", str(module), "/sys/module/nvidia"])
        self.assertEqual(readonly, ["/sys/module/nvidia"])
        self.assertNotIn("/sys/module", command)
        self.assertNotIn("/sys", command)
        self.assertNotIn("/sys/module/nvidia_uvm", command)
        self.assertNotIn("/proc/driver/nvidia", command)

    def test_nvidia_module_metadata_refuses_missing_or_escaping_symlink(self):
        sysfs = self.root / "sys"
        (sysfs / "module").mkdir(parents=True)
        devices = [Path("/dev/nvidia0")]
        self.assertEqual(helper.nvidia_module_args(devices, sysfs), ([], []))
        outside = self.root / "outside-module"
        outside.mkdir()
        (sysfs / "module/nvidia").symlink_to(outside, target_is_directory=True)
        self.assertEqual(helper.nvidia_module_args(devices, sysfs), ([], []))
        (sysfs / "module/nvidia").unlink()
        (sysfs / "module").rmdir()
        (outside / "nvidia").mkdir()
        (sysfs / "module").symlink_to(outside, target_is_directory=True)
        self.assertEqual(helper.nvidia_module_args(devices, sysfs), ([], []))

    def test_nvidia_uvm_metadata_requires_both_shared_gpu_and_uvm_device(self):
        sysfs = self.root / "sys"
        core = sysfs / "module/nvidia"
        uvm = sysfs / "module/nvidia_uvm"
        core.mkdir(parents=True)
        uvm.mkdir()
        for devices in ([Path("/dev/nvidia-uvm")], [Path("/dev/nvidiactl"), Path("/dev/nvidia-uvm")]):
            with self.subTest(devices=devices):
                self.assertEqual(helper.nvidia_module_args(devices, sysfs), ([], []))
        for devices in ([Path("/dev/nvidia0")], [Path("/dev/nvidia0"), Path("/tmp/nvidia-uvm")]):
            with self.subTest(devices=devices):
                command, readonly = helper.nvidia_module_args(devices, sysfs)
                self.assertEqual(command, ["--ro-bind", str(core), "/sys/module/nvidia"])
                self.assertEqual(readonly, ["/sys/module/nvidia"])
        command, readonly = helper.nvidia_module_args([Path("/dev/nvidia0"), Path("/dev/nvidia-uvm")], sysfs)
        self.assertEqual(command, ["--ro-bind", str(core), "/sys/module/nvidia", "--ro-bind", str(uvm), "/sys/module/nvidia_uvm"])
        self.assertEqual(readonly, ["/sys/module/nvidia", "/sys/module/nvidia_uvm"])
        self.assertNotIn("/sys/module", command)
        self.assertNotIn("/sys", command)

    def test_nvidia_uvm_metadata_refuses_missing_or_escaping_module(self):
        sysfs = self.root / "sys"
        core = sysfs / "module/nvidia"
        core.mkdir(parents=True)
        devices = [Path("/dev/nvidia0"), Path("/dev/nvidia-uvm")]
        expected = (["--ro-bind", str(core), "/sys/module/nvidia"], ["/sys/module/nvidia"])
        self.assertEqual(helper.nvidia_module_args(devices, sysfs), expected)
        outside = self.root / "unrelated-module"
        outside.mkdir()
        (sysfs / "module/nvidia_uvm").symlink_to(outside, target_is_directory=True)
        self.assertEqual(helper.nvidia_module_args(devices, sysfs), expected)

    def test_machine_id_is_stable_per_jail_and_exposed_only_as_a_readonly_file(self):
        store = self.store("one")
        identity = (store.path / "account-machine-id").read_text()
        self.assertRegex(identity, r"^[0-9a-f]{32}\n$")
        self.assertEqual((self.store("one").path / "account-machine-id").read_text(), identity)
        self.assertNotEqual((self.store("two").path / "account-machine-id").read_text(), identity)
        with patch.object(helper, "check_dependencies", return_value=("/usr/bin/bwrap", "/usr/bin/python3")):
            command = helper.build_command(self.config_ok(), store.path, store.path)
        bindings = [(command[index + 1], command[index + 2]) for index, word in enumerate(command) if word == "--ro-bind"]
        self.assertIn((str(store.path / "account-machine-id"), "/etc/machine-id"), bindings)
        self.assertNotIn(("/etc/machine-id", "/etc/machine-id"), bindings)

    def test_report_reads_only_bounded_trusted_log_and_never_prefix(self):
        store = self.store()
        name = "run-1-012345abcdef.log"
        (store.path / "logs" / name).write_bytes(b"x" * (helper.REPORT_LOG + 30))
        store.write_json("state.json", {"status": "failed", "exit_code": 3, "log_file": name, "log_path": "/etc/shadow"})
        result = helper.report(store)
        self.assertEqual(len(result["log_text"]), helper.REPORT_LOG)
        self.assertTrue(result["log_truncated"])
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(result["log_path"], str(store.path / "logs" / name))
        store.write_json("state.json", {"status": "exited", "log_file": "../../host-file"})
        with self.assertRaisesRegex(helper.JailError, "metadata"):
            helper.report(store)

    def test_failed_probe_has_no_normal_launch_fallback(self):
        store = self.store()
        failed = subprocess.CompletedProcess([], 1, b"bwrap: namespaces unavailable")
        with patch.object(helper, "check_dependencies", return_value=("/usr/bin/bwrap", "/usr/bin/python3")), patch.object(helper.subprocess, "run", return_value=failed) as run:
            with self.assertRaisesRegex(helper.JailError, "Sandbox checks failed"):
                helper.check(self.config_ok(), store)
        self.assertEqual(run.call_count, 1)
        self.assertFalse((store.path / "game").exists())


@unittest.skipUnless(os.environ.get("KLAY_LIVE_JAIL_TESTS") == "1" and shutil.which("bwrap"), "opt in to benign live bubblewrap tests with KLAY_LIVE_JAIL_TESTS=1")
class LiveHelperTests(unittest.TestCase):
    setUp = HelperTests.setUp
    tearDown = HelperTests.tearDown
    store = HelperTests.store
    custom_wine = HelperTests.custom_wine
    def helper_process(self, operation):
        # The real CLI runs isolated Python; patch only its storage home for test.
        source = Path(helper.__file__).read_text()
        wrapper = "import pwd,types\npwd.getpwuid = lambda uid: types.SimpleNamespace(pw_dir=" + repr(str(self.home)) + ")\n" + source.replace("from __future__ import annotations", "")
        return subprocess.Popen([sys.executable, "-I", "-c", wrapper, "--operation", operation, "--game-id", "live-test", "--data-dir-name", "klay"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)

    def request(self, operation):
        process = self.helper_process(operation)
        output, error = process.communicate(json.dumps(self.config).encode(), timeout=30)
        response = json.loads(output)
        self.assertEqual(process.returncode, 0, response.get("error", error.decode()))
        return response

    def run_to_completion(self):
        process = self.helper_process("launch")
        output, error = process.communicate(json.dumps(self.config).encode(), timeout=30)
        response = json.loads(output)
        self.assertTrue(response["ok"], response.get("error", error.decode()))
        self.assertEqual(response["status"], "running")
        return process.returncode, self.request("report")

    def test_real_runner_exit_failure_is_preserved(self):
        (self.runner / "proton").write_text("#!/usr/bin/python3\nimport sys\nprint('benign expected failure')\nsys.exit(7)\n")
        code, reported = self.run_to_completion()
        self.assertEqual(code, 7)
        self.assertEqual(reported["status"], "failed")
        self.assertEqual(reported["exit_code"], 7)

    def test_real_custom_wine_build_is_selected_and_readonly(self):
        self.config = self.custom_wine()
        Path(self.config["runner"]).write_text("""#!/usr/bin/python3
import os, pathlib, sys
assert os.statvfs('/runner').f_flag & os.ST_RDONLY
assert os.environ['WINE'] == '/runner/bin/wine'
assert os.environ['WINEPREFIX'] == '/prefix'
assert sys.argv[1] == '/game/Game.exe'
pathlib.Path('/game/wine-marker').write_text('benign selected custom Wine')
print('benign selected custom Wine', flush=True)
""")
        code, reported = self.run_to_completion()
        self.assertEqual(code, 0, reported["log_text"])
        self.assertIn("selected custom Wine", reported["log_text"])
        self.assertFalse((self.game / "wine-marker").exists())

    def assert_proton_preserves_prefix(self, nested):
        store = self.store("live-test")
        helper.prepare_game(store, helper.validate_config(self.config))
        prefix = store.path / "prefix"
        if nested:
            prefix /= "pfx"
            prefix.mkdir()
        (prefix / "system.reg").write_text("benign existing registry")
        (prefix / "old-save").write_text("existing jail save")
        original_save = (prefix / "old-save").stat()
        (self.runner / "proton").write_text("""#!/usr/bin/python3
import errno, os, pathlib, re
compat = pathlib.Path(os.environ['STEAM_COMPAT_DATA_PATH'])
pfx = pathlib.Path(os.environ['WINEPREFIX'])
assert compat == pathlib.Path('/home/klay/.local/share/klay-proton')
assert pfx == compat / 'pfx'
assert pfx.is_symlink()
assert (pfx / 'old-save').read_text() == 'existing jail save'
assert (pfx / 'system.reg').read_text() == 'benign existing registry'
assert os.environ['STEAM_COMPAT_INSTALL_PATH'] == '/game'
assert os.environ['PROTONFIXES_DISABLE'] == '1'
assert os.environ['UMU_ID'] == 'umu-default'
assert os.environ['UMU_USE_STEAM'] == '0'
assert not pathlib.Path('/run/dbus/system_bus_socket').exists()
count = pfx / 'launch-count'
count.write_text(str(int(count.read_text()) + 1) if count.exists() else '1')
identity = pathlib.Path('/etc/machine-id').read_text()
assert re.fullmatch('[0-9a-f]{32}\\n', identity)
try:
    os.open('/etc/machine-id', os.O_WRONLY)
except OSError as error:
    assert error.errno in (errno.EROFS, errno.EACCES), error
else:
    raise AssertionError('machine ID is writable')
print('benign existing prefix preserved', pfx.resolve(), flush=True)
""")
        for count in (1, 2):
            code, reported = self.run_to_completion()
            self.assertEqual(code, 0, reported["log_text"])
            self.assertEqual(reported["status"], "exited")
            self.assertIn("existing prefix preserved", reported["log_text"])
            self.assertEqual((prefix / "launch-count").read_text(), str(count))
            self.assertEqual((prefix / "old-save").read_text(), "existing jail save")
            self.assertEqual((prefix / "system.reg").read_text(), "benign existing registry")
            current_save = (prefix / "old-save").stat()
            self.assertEqual((current_save.st_dev, current_save.st_ino), (original_save.st_dev, original_save.st_ino))
        self.assertFalse((self.game / "launch-count").exists())
        self.assertFalse((self.game / "old-save").exists())
        self.assertEqual(os.readlink(store.path / "home/.local/share/klay-proton/pfx"), "/prefix/pfx" if nested else "/prefix")

    def test_real_proton_preserves_existing_direct_wine_prefix_and_relaunches(self):
        self.assert_proton_preserves_prefix(nested=False)

    def test_real_proton_preserves_older_nested_prefix_and_relaunches(self):
        self.assert_proton_preserves_prefix(nested=True)

    def test_real_proton_bootstrap_rejects_a_mutable_home_component_symlink(self):
        store = self.store("live-test")
        helper.prepare_game(store, helper.validate_config(self.config))
        target = self.root / "host-target"
        target.mkdir()
        (target / "keep").write_text("unchanged host data")
        (store.path / "home/.local").symlink_to(target, target_is_directory=True)
        process = self.helper_process("launch")
        output, error = process.communicate(json.dumps(self.config).encode(), timeout=30)
        response = json.loads(output)
        self.assertFalse(response["ok"], response)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual((target / "keep").read_text(), "unchanged host data")
        self.assertEqual(list(target.iterdir()), [target / "keep"])

    def test_real_proton_bootstrap_rejects_a_replaced_private_prefix_link(self):
        store = self.store("live-test")
        helper.prepare_game(store, helper.validate_config(self.config))
        compat = store.path / "home/.local/share/klay-proton"
        compat.mkdir(parents=True)
        (compat / "pfx").symlink_to("/game", target_is_directory=True)
        process = self.helper_process("launch")
        output, error = process.communicate(json.dumps(self.config).encode(), timeout=30)
        response = json.loads(output)
        self.assertFalse(response["ok"], response)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(os.readlink(compat / "pfx"), "/game")
        self.assertEqual((self.game / "Game.exe").read_bytes(), b"benign test placeholder")

    @unittest.skipUnless(Path('/dev/nvidia0').exists() and Path('/sys/module/nvidia').is_dir(), "host has no NVIDIA module/device")
    def test_real_nvidia_module_is_readonly_without_other_module_exposure(self):
        (self.runner / "proton").write_text("""#!/usr/bin/python3
import errno, os, pathlib
module = pathlib.Path('/sys/module/nvidia')
assert (module / 'version').read_text().strip()
assert os.statvfs(module).f_flag & os.ST_RDONLY
try:
    os.open(module / 'version', os.O_WRONLY)
except OSError as error:
    assert error.errno in (errno.EROFS, errno.EACCES), error
else:
    raise AssertionError('NVIDIA module is writable')
expected = ['nvidia']
if pathlib.Path('/dev/nvidia-uvm').exists():
    uvm = module.parent / 'nvidia_uvm'
    assert uvm.is_dir()
    assert os.statvfs(uvm).f_flag & os.ST_RDONLY
    try:
        os.open(uvm / 'initstate', os.O_WRONLY)
    except OSError as error:
        assert error.errno in (errno.EROFS, errno.EACCES), error
    else:
        raise AssertionError('NVIDIA UVM module is writable')
    expected.append('nvidia_uvm')
assert sorted(path.name for path in module.parent.iterdir()) == expected
assert not pathlib.Path('/sys/class/net').exists()
print('benign narrow NVIDIA metadata confirmed', flush=True)
""")
        code, reported = self.run_to_completion()
        self.assertEqual(code, 0, reported["log_text"])
        self.assertIn("narrow NVIDIA metadata confirmed", reported["log_text"])

    def test_real_explicit_working_directory_stays_inside_copied_game(self):
        (self.game / "assets").mkdir()
        self.config["working_dir"] = "assets"
        (self.runner / "proton").write_text("""#!/usr/bin/python3
import os, pathlib
assert os.getcwd() == '/game/assets'
pathlib.Path('cwd-marker').write_text('benign working directory')
print('benign explicit working directory', flush=True)
""")
        code, reported = self.run_to_completion()
        self.assertEqual(code, 0, reported["log_text"])
        self.assertIn("explicit working directory", reported["log_text"])
        self.assertFalse((self.game / "assets/cwd-marker").exists())

    def test_real_descendant_stdout_is_drained_for_bounded_time(self):
        (self.runner / "proton").write_text("#!/usr/bin/python3\nimport subprocess\nsubprocess.Popen(['/usr/bin/python3', '-c', \"import ctypes,time; ctypes.CDLL(None).prctl(15,b'services.exe',0,0,0); time.sleep(30)\"])\nprint('benign background service')\n")
        code, reported = self.run_to_completion()
        self.assertEqual(code, 0)
        self.assertEqual(reported["status"], "exited")
        self.assertIn("background service", reported["log_text"])

    def test_real_pid_one_reaps_orphans_without_losing_runner_failure(self):
        (self.runner / "proton").write_text("""#!/usr/bin/python3
import os, pathlib, sys, time
intermediate = os.fork()
if intermediate == 0:
    orphan = os.fork()
    if orphan == 0:
        os._exit(0)
    os._exit(0)
os.waitpid(intermediate, 0)
time.sleep(.6)
zombies = []
for entry in pathlib.Path('/proc').iterdir():
    if entry.name.isdecimal():
        try:
            status = (entry / 'status').read_text()
        except FileNotFoundError:
            continue
        if 'State:\\tZ' in status:
            zombies.append(entry.name)
assert not zombies, zombies
print('benign orphan reaping confirmed', flush=True)
sys.exit(7)
""")
        code, reported = self.run_to_completion()
        self.assertEqual(code, 7)
        self.assertEqual(reported["exit_code"], 7)
        self.assertIn("orphan reaping confirmed", reported["log_text"])

    @unittest.skipUnless(any(Path("/dev/dri").glob("renderD[0-9]*")), "host has no accessible render nodes")
    def test_real_libudev_identifies_only_shared_gpu_metadata(self):
        (self.runner / "proton").write_text("""#!/usr/bin/python3
import ctypes, os, pathlib
lib = ctypes.CDLL('libudev.so.1')
lib.udev_new.restype = ctypes.c_void_p
lib.udev_device_new_from_devnum.argtypes = [ctypes.c_void_p, ctypes.c_char, ctypes.c_ulong]
lib.udev_device_new_from_devnum.restype = ctypes.c_void_p
lib.udev_device_get_parent_with_subsystem_devtype.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
lib.udev_device_get_parent_with_subsystem_devtype.restype = ctypes.c_void_p
lib.udev_device_get_sysattr_value.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
lib.udev_device_get_sysattr_value.restype = ctypes.c_char_p
lib.udev_device_get_syspath.argtypes = [ctypes.c_void_p]
lib.udev_device_get_syspath.restype = ctypes.c_char_p
lib.udev_device_unref.argtypes = [ctypes.c_void_p]
lib.udev_unref.argtypes = [ctypes.c_void_p]
context = lib.udev_new()
assert context
nodes = list(pathlib.Path('/dev/dri').glob('renderD*'))
assert nodes
for node in nodes:
    device = lib.udev_device_new_from_devnum(context, b'c', node.stat().st_rdev)
    assert device, str(node)
    gpu = lib.udev_device_get_parent_with_subsystem_devtype(device, b'pci', None)
    assert gpu, str(node)
    vendor = lib.udev_device_get_sysattr_value(gpu, b'vendor')
    identity = lib.udev_device_get_sysattr_value(gpu, b'device')
    assert vendor and identity, str(node)
    syspath = lib.udev_device_get_syspath(gpu).decode()
    assert os.statvfs(syspath).f_flag & os.ST_RDONLY
    print('benign GPU identification:', node.name, vendor.decode(), identity.decode(), flush=True)
    lib.udev_device_unref(device)
lib.udev_unref(context)
assert not pathlib.Path('/sys/class/net').exists()
cpu = pathlib.Path('/sys/devices/system/cpu')
assert (cpu / 'online').is_file()
assert os.statvfs(cpu).f_flag & os.ST_RDONLY
for control in (cpu / 'smt/control', cpu / 'cpu0/online', cpu / 'cpu0/cpufreq',
                pathlib.Path('/sys/devices/system/node/node0/compact')):
    assert not control.exists(), str(control)
""")
        code, reported = self.run_to_completion()
        self.assertEqual(code, 0, reported["log_text"])
        self.assertIn("GPU identification", reported["log_text"])

    def test_real_probe_and_managed_supervised_launch(self):
        (self.runner / "proton").write_text("""#!/usr/bin/python3
import json, os, pathlib, time
interfaces = [line.split(':')[0].strip() for line in open('/proc/net/dev') if ':' in line]
assert interfaces == ['lo'], interfaces
assert not os.path.exists('/run/dbus/system_bus_socket')
assert not os.path.exists('/run/user/%d/bus' % os.getuid())
assert 'GITHUB_TOKEN' not in os.environ
pathlib.Path('/game/jail-only-save').write_text('save')
pathlib.Path('/prefix/probe-save').write_text('prefix')
pathlib.Path('/home/klay/home-save').write_text('home')
try:
    os.open('/proc/1/fd/1', os.O_WRONLY)
except PermissionError:
    print('supervisor startup pipe protected', flush=True)
else:
    raise AssertionError('startup pipe accessible to runner')
print('benign confinement probe passed', flush=True)
time.sleep(.5)
""")
        checked = self.request("check")
        self.assertEqual(checked["status"], "ready")
        process = self.helper_process("launch")
        try:
            process.stdin.write(json.dumps(self.config).encode())
            process.stdin.close()
            response = json.loads(process.stdout.readline())
            self.assertTrue(response["ok"], response)
            self.assertEqual(response["status"], "running")
            self.assertIsNone(process.poll())
            self.assertEqual(process.wait(timeout=30), 0, process.stderr.read().decode())
            self.assertEqual(process.stdout.read(), b"")
            reported = self.request("report")
            self.assertEqual(reported["status"], "exited")
            self.assertEqual(reported["exit_code"], 0)
            self.assertIn("confinement probe passed", reported["log_text"])
            self.assertIn("startup pipe protected", reported["log_text"])
            self.assertFalse((self.game / "jail-only-save").exists())
            self.assertTrue((Path(response["jail_dir"]) / "game/jail-only-save").is_file())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


if __name__ == "__main__":
    unittest.main()
