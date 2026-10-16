from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from klay.qt import jail_helper as helper


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="klay-controller-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dev = self.root / "dev"
        self.sys = self.root / "sys"
        self.udev = self.root / "udev/data"
        for path in (self.dev / "input", self.sys / "devices", self.sys / "class/input",
                     self.sys / "dev/char", self.sys / "bus/usb", self.udev):
            path.mkdir(parents=True, exist_ok=True)
        self.physical = self.sys / "devices/pci0/usb1/1-1"
        self.interface = self.physical / "1-1:1.0"
        self.physical.mkdir(parents=True)
        (self.physical / "uevent").write_text("DEVTYPE=usb_device\n")
        (self.physical / "idVendor").write_text("2dc8\n")
        (self.physical / "subsystem").symlink_to(self.sys / "bus/usb", target_is_directory=True)
        self.devices = {}
        self.inaccessible = set()
        original = Path.lstat

        def node_stat(path, *args, **kwargs):
            if path in self.devices:
                return SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_rdev=self.devices[path])
            return original(path, *args, **kwargs)

        self.patchers = [patch.object(Path, "lstat", node_stat),
                         patch.object(helper.os, "access", side_effect=lambda path, mode: path not in self.inaccessible)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def controller(self, name="event258", minor=258, *, props="", input_name="input45"):
        node = self.dev / "input" / name
        node.touch()
        self.devices[node] = os.makedev(13, minor)
        root = self.interface / "input" / input_name
        entry = root / name
        entry.mkdir(parents=True)
        (entry / "dev").write_text("13:" + str(minor) + "\n")
        (entry / "uevent").write_text(f"MAJOR=13\nMINOR={minor}\nDEVNAME=input/{name}\n")
        (self.sys / "class/input" / name).symlink_to(entry, target_is_directory=True)
        (self.sys / "dev/char" / ("13:" + str(minor))).symlink_to(entry, target_is_directory=True)
        (self.udev / ("c13:" + str(minor))).write_text("E:ID_INPUT=1\nE:ID_INPUT_JOYSTICK=1\n" + props)
        return node, entry, root

    def args(self):
        return helper.controller_args(self.dev, self.sys, self.udev)

    @staticmethod
    def mounts(args, option):
        return [(args[i + 1], args[i + 2]) for i, arg in enumerate(args) if arg == option]

    def test_shares_only_gamepad_nodes_and_exact_identification_metadata(self):
        event, entry, root = self.controller()
        joystick, _, _ = self.controller("js1", 1)
        (self.dev / "hidraw0").touch()
        keyboard = self.interface / "input/input46/event259"
        keyboard.mkdir(parents=True)
        args, readonly = self.args()
        self.assertEqual(self.mounts(args, "--dev-bind"),
                         [(str(event), "/dev/input/event258"), (str(joystick), "/dev/input/js1")])
        bindings = self.mounts(args, "--ro-bind")
        inside = "/sys/devices/pci0/usb1/1-1/1-1:1.0/input/input45"
        self.assertEqual(bindings.count((str(root), inside)), 1)
        self.assertIn((str(self.udev / "c13:258"), "/run/udev/data/c13:258"), bindings)
        self.assertIn((str(self.physical / "idVendor"), "/sys/devices/pci0/usb1/1-1/idVendor"), bindings)
        self.assertIn(inside, readonly)
        for broad in (self.dev / "input", self.physical, self.interface, self.udev, self.sys):
            self.assertNotIn(str(broad), [source for source, _ in bindings])
        self.assertNotIn(str(keyboard), args)
        self.assertNotIn(str(self.dev / "hidraw0"), args)

    def test_rejects_keyboard_mouse_and_combined_interfaces(self):
        for kind in ("KEY", "KEYBOARD", "MOUSE", "TOUCHPAD", "TOUCHSCREEN", "TABLET"):
            with self.subTest(kind=kind):
                node, _, _ = self.controller("event" + str(300 + len(self.devices)), 300 + len(self.devices),
                                             props="E:ID_INPUT_" + kind + "=1\n")
        self.assertEqual(self.args(), ([], []))

    def test_joystick_classification_is_required(self):
        self.controller()
        record = self.udev / "c13:258"
        for properties in ("E:ID_INPUT=1\n", "E:ID_INPUT_JOYSTICK=0\n", "E:ID_INPUT_JOYSTICK=10\n"):
            record.write_text(properties)
            self.assertEqual(self.args(), ([], []))

    def test_skips_unreadable_controller_without_requiring_root(self):
        node, _, _ = self.controller()
        self.inaccessible.add(node)
        self.assertEqual(self.args(), ([], []))

    def test_regular_files_wrong_major_and_symlink_devices_are_not_shared(self):
        node, _, _ = self.controller()
        del self.devices[node]
        self.assertEqual(self.args(), ([], []))
        self.devices[node] = os.makedev(1, 3)
        self.assertEqual(self.args(), ([], []))
        del self.devices[node]
        node.unlink()
        node.symlink_to("/dev/null")
        self.assertEqual(self.args(), ([], []))

    def test_rejects_database_symlink_even_if_target_classifies_as_gamepad(self):
        self.controller()
        record = self.udev / "c13:258"
        target = self.udev / "other-record"
        record.rename(target)
        record.symlink_to(target)
        self.assertEqual(self.args(), ([], []))

    def test_rejects_reused_device_number_and_class_target_outside_sysfs(self):
        _, entry, _ = self.controller()
        (entry / "dev").write_text("13:64\n")
        self.assertEqual(self.args(), ([], []))
        (entry / "dev").write_text("13:258\n")
        alias = self.sys / "class/input/event258"
        alias.unlink()
        outside = self.root / "outside/input99/event258"
        outside.mkdir(parents=True)
        (outside / "dev").write_text("13:258\n")
        alias.symlink_to(outside)
        self.assertEqual(self.args(), ([], []))

    def test_char_alias_must_match_selected_sysfs_device(self):
        self.controller()
        char = self.sys / "dev/char/13:258"
        char.unlink()
        char.symlink_to(self.sys / "devices")
        self.assertEqual(self.args(), ([], []))

    def test_rejects_oversize_metadata(self):
        self.controller()
        (self.udev / "c13:258").write_text("E:ID_INPUT_JOYSTICK=1\n" + "x" * 65536)
        self.assertEqual(self.args(), ([], []))

    def test_missing_disconnected_or_unclassified_devices_do_not_block_launch(self):
        self.assertEqual(helper.controller_args(self.root / "missing", self.sys, self.udev), ([], []))
        _, entry, _ = self.controller()
        (entry / "dev").unlink()
        self.assertEqual(self.args(), ([], []))


if __name__ == "__main__":
    unittest.main()
