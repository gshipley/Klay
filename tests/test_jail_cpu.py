"""CPU topology metadata stays exact, read-only, and separate from controls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from klay.qt import jail_helper as helper


class CpuTopologyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="klay-cpu-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sys = self.root / "sys"
        self.sys.mkdir()
        self.trusted = self.root / "trusted"
        self.trusted.mkdir(mode=0o700)

    def file(self, relative, contents="0\n"):
        path = self.sys / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        return path

    @staticmethod
    def bindings(command):
        return [(command[i + 1], command[i + 2]) for i, value in enumerate(command)
                if value == "--ro-bind"]

    def args(self):
        return helper.cpu_sysfs_args(self.trusted, self.sys)

    def snapshot_files(self):
        snapshots = list(self.trusted.glob("cpu-metadata-*"))
        self.assertEqual(len(snapshots), 1)
        return {str(path.relative_to(snapshots[0])): path.read_bytes()
                for path in snapshots[0].rglob("*") if path.is_file()}

    def test_only_wine_cpu_and_numa_topology_files_are_shared(self):
        selected = ["devices/system/cpu/online", "devices/system/cpu/present",
                    "devices/system/node/online", "devices/system/node/node0/cpumap",
                    "devices/cpu_core/cpus"]
        for cpu in (0, 31):
            selected += [f"devices/system/cpu/cpu{cpu}/topology/{name}"
                         for name in ("physical_package_id", "thread_siblings", "thread_siblings_list")]
            selected += [f"devices/system/cpu/cpu{cpu}/cache/index0/{name}"
                         for name in ("shared_cpu_map", "level", "ways_of_associativity",
                                      "coherency_line_size", "size", "type")]
        excluded = ["devices/system/cpu/cpu0/online", "devices/system/cpu/smt/control",
                    "devices/system/cpu/cpu0/topology/unrelated", "devices/system/cpu/cpu0/cache/index0/uevent",
                    "devices/system/cpu/cpu0/cpufreq/scaling_max_freq", "devices/system/node/node0/compact",
                    "devices/system/node/node0/meminfo", "devices/virtual/dmi/id/product_uuid",
                    "devices/cpu_core/uevent", "devices/system/cpu/cpu1fake/topology/thread_siblings",
                    "devices/system/cpu/cpu0/cache/index1fake/level", "devices/system/node/node1fake/cpumap"]
        for relative in selected + excluded:
            self.file(relative)
        command, readonly = self.args()
        self.assertEqual(self.snapshot_files(), {path: b"0\n" for path in selected})
        self.assertEqual(set(readonly), {"/sys/devices/system/cpu", "/sys/devices/system/node", "/sys/devices/cpu_core"})
        self.assertEqual({destination for _, destination in self.bindings(command)}, set(readonly))
        self.assertTrue(all(source.startswith(str(self.trusted) + "/cpu-metadata-")
                            for source, _ in self.bindings(command)))
        self.assertEqual(set(command[::3]), {"--ro-bind"})
        self.assertEqual(len(readonly), 3)

    def test_symlink_files_and_ancestors_cannot_expose_other_paths(self):
        target = self.root / "outside"
        target.mkdir()
        (target / "cpumap").write_text("ffffffff\n")
        (target / "thread_siblings").write_text("ffffffff\n")
        online = self.file("devices/system/cpu/online")
        online.unlink()
        online.symlink_to(target / "cpumap")
        topology = self.sys / "devices/system/cpu/cpu0/topology"
        topology.parent.mkdir(parents=True)
        topology.symlink_to(target, target_is_directory=True)
        node = self.sys / "devices/system/node/node0"
        node.parent.mkdir(parents=True)
        node.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.args(), ([], []))

    def test_symlink_cpu_or_cache_directory_is_not_followed(self):
        self.file("devices/system/cpu/cpu1/topology/thread_siblings")
        cpu = self.sys / "devices/system/cpu/cpu0"
        cpu.symlink_to(cpu.parent / "cpu1", target_is_directory=True)
        cache = self.sys / "devices/system/cpu/cpu1/cache"
        cache.mkdir()
        target = self.root / "other-cache"
        target.mkdir()
        (target / "level").write_text("1\n")
        (cache / "index0").symlink_to(target, target_is_directory=True)
        bindings = self.bindings(self.args()[0])
        self.assertEqual(len(bindings), 1)
        self.assertEqual(self.snapshot_files(), {"devices/system/cpu/cpu1/topology/thread_siblings": b"0\n"})

    def test_special_files_unreadable_files_and_missing_sysfs_are_optional(self):
        fifo = self.sys / "devices/system/cpu/online"
        fifo.parent.mkdir(parents=True)
        os.mkfifo(fifo)
        present = self.file("devices/system/cpu/present")
        with patch.object(helper.os, "access", return_value=False):
            self.assertEqual(self.args(), ([], []))
        present.unlink()
        self.assertEqual(self.args(), ([], []))
        self.assertEqual(helper.cpu_sysfs_args(self.trusted, self.root / "missing"), ([], []))

    def test_snapshot_reuse_and_changed_topology_do_not_mutate_active_snapshot(self):
        online = self.file("devices/system/cpu/online", "0-31\n")
        first, _ = self.args()
        self.assertEqual(self.args()[0], first)
        self.assertEqual(len(list(self.trusted.iterdir())), 1)
        first_source = Path(self.bindings(first)[0][0])
        online.write_text("0-15\n")
        second, _ = self.args()
        self.assertNotEqual(second, first)
        self.assertEqual((first_source / "online").read_text(), "0-31\n")
        self.assertEqual((Path(self.bindings(second)[0][0]) / "online").read_text(), "0-15\n")

    def test_thousand_cpu_snapshot_keeps_all_metadata_and_constant_mount_count(self):
        self.file("devices/system/cpu/online", "0-1023\n")
        for cpu in range(1024):
            self.file(f"devices/system/cpu/cpu{cpu}/topology/thread_siblings", f"{1 << cpu:x}\n")
        command, readonly = self.args()
        self.assertEqual(len(command), 3)
        self.assertEqual(readonly, ["/sys/devices/system/cpu"])
        self.assertLess(len(json.dumps({"readonly": readonly})), 128)
        self.assertEqual(len(self.snapshot_files()), 1025)

    def test_oversize_metadata_fails_explicitly_without_publishing_snapshot(self):
        self.file("devices/system/cpu/online", "0" * 65537)
        with self.assertRaisesRegex(helper.JailError, "64 KiB"):
            self.args()
        self.assertEqual(list(self.trusted.iterdir()), [])

    def test_build_command_checks_every_topology_mount_is_readonly(self):
        mount = "/sys/devices/system/cpu/online"
        args = ["--ro-bind", "/synthetic/online", mount]
        config = {"runner_type": "wine", "runner": "/usr/bin/wine", "exe": "unused.exe", "arguments": []}
        with (patch.object(helper, "check_dependencies", return_value=("/usr/bin/bwrap", "/usr/bin/python3")),
              patch.object(helper, "cpu_sysfs_args", return_value=(args, [mount]))):
            command = helper.build_command(config, self.root, self.root)
        self.assertIn(("/synthetic/online", mount), self.bindings(command))
        self.assertIn(mount, json.loads(command[-1])["readonly"])
        self.assertIn("--unshare-all", command)


if __name__ == "__main__":
    unittest.main()
