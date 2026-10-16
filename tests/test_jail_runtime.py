from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from klay.qt import jail_helper as helper


def _elf(path: Path, *, needed=(), soname=None, bits=64) -> Path:
    """Minimal ELF dynamic metadata, so dependency tests need no compiler."""
    strings = bytearray(b"\0")
    entries = []
    for tag, names in ((1, needed), (14, (soname,) if soname else ())):
        for name in names:
            offset = len(strings)
            strings.extend(name.encode() + b"\0")
            entries.append((tag, offset))
    dynamic_offset, string_offset = 256, 512
    entries += [(5, string_offset), (10, len(strings)), (0, 0)]
    wide = bits == 64
    dynamic = b"".join(struct.pack("<QQ" if wide else "<II", *item) for item in entries)
    size = string_offset + len(strings)
    image = bytearray(size)
    ident = b"\x7fELF" + bytes((2 if wide else 1, 1, 1)) + b"\0" * 9
    if wide:
        header = struct.pack("<16sHHIQQQIHHHHHH", ident, 3, 62, 1, 0, 64, 0, 0, 64, 56, 2, 64, 0, 0)
        load = struct.pack("<IIQQQQQQ", 1, 4, 0, 0, 0, size, size, 4096)
        dyn = struct.pack("<IIQQQQQQ", 2, 4, dynamic_offset, dynamic_offset, 0, len(dynamic), len(dynamic), 8)
    else:
        header = struct.pack("<16sHHIIIIIHHHHHH", ident, 3, 3, 1, 0, 52, 0, 0, 52, 32, 2, 40, 0, 0)
        load = struct.pack("<IIIIIIII", 1, 0, 0, 0, size, size, 4, 4096)
        dyn = struct.pack("<IIIIIIII", 2, dynamic_offset, dynamic_offset, 0, len(dynamic), len(dynamic), 4, 4)
    image[:len(header)] = header
    image[len(header):len(header) + len(load) + len(dyn)] = load + dyn
    image[dynamic_offset:dynamic_offset + len(dynamic)] = dynamic
    image[string_offset:] = strings
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image)
    return path


class ProtonRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="klay-runtime-test-")
        self.root = Path(self.temporary.name)
        self.runner = self.root / "proton"
        self.runner.mkdir()
        self.plugin_root = self.runner / "files/lib/x86_64-linux-gnu/gstreamer-1.0"
        self.runtime = self.root / "installed-runtime/lib/x86_64-linux-gnu"
        self.runtime.mkdir(parents=True)
        self.host = self.root / "host-lib"
        self.host.mkdir()
        self.config = {"runner_type": "proton", "runner": str(self.runner)}
        self.addCleanup(self.temporary.cleanup)

    def args(self):
        with patch.object(helper, "_runtime_library_roots", return_value=[self.runtime]), patch.object(helper, "_host_library_roots", return_value=[self.host]):
            return helper.proton_runtime_args(self.config)

    def mounts(self, arguments):
        return {arguments[index + 1]: arguments[index + 2] for index, value in enumerate(arguments) if value == "--ro-bind"}

    def test_wine_never_searches_or_shares_installed_proton_runtimes(self):
        with patch.object(helper, "_runtime_library_roots") as roots:
            self.assertEqual(helper.proton_runtime_args({"runner_type": "wine", "runner": "/usr/bin/wine"}), ([], {}, []))
        roots.assert_not_called()

    def test_mounts_only_missing_codec_and_its_missing_dependency(self):
        _elf(self.plugin_root / "libgstvpx.so", needed=["libvpx.so.6"])
        codec = _elf(self.runtime / "libvpx.so.6.3.0", soname="libvpx.so.6", needed=["libcrypto.so.1.1"])
        crypto = _elf(self.runtime / "libcrypto.so.1.1", soname="libcrypto.so.1.1")
        _elf(self.runtime / "libunrelated.so.1", soname="libunrelated.so.1")
        arguments, environment, readonly = self.args()
        self.assertEqual(self.mounts(arguments), {
            str(codec): "/runtime-libs/x86_64/libvpx.so.6",
            str(crypto): "/runtime-libs/x86_64/libcrypto.so.1.1",
        })
        self.assertIn("/runtime-libs/x86_64", environment["LD_LIBRARY_PATH"].split(":"))
        self.assertTrue(set(environment["LD_LIBRARY_PATH"].split(":")) <= {"/runtime-libs/x86_64", "/runtime-libs/i386"})
        self.assertEqual(set(readonly), set(self.mounts(arguments).values()))
        self.assertNotIn(str(self.runtime), self.mounts(arguments))

    def test_preserves_libraries_already_supplied_by_host_or_runner(self):
        _elf(self.plugin_root / "libgstfake.so", needed=["libvpx.so.6", "libwebp.so.6"])
        _elf(self.host / "libvpx.so.6", soname="libvpx.so.6")
        _elf(self.plugin_root.parent / "libwebp.so.6", soname="libwebp.so.6")
        _elf(self.runtime / "libvpx.so.6", soname="libvpx.so.6")
        _elf(self.runtime / "libwebp.so.6", soname="libwebp.so.6")
        self.assertEqual(self.args(), ([], {}, []))

    def test_follows_bundled_library_dependencies_without_replacing_bundled_library(self):
        _elf(self.plugin_root / "libgstlibav.so", needed=["libavcodec.so.58"])
        _elf(self.plugin_root.parent / "libavcodec.so.58", soname="libavcodec.so.58", needed=["libbz2.so.1.0"])
        codec = _elf(self.runtime / "libbz2.so.1.0.4", soname="libbz2.so.1.0")
        arguments, _, _ = self.args()
        self.assertEqual(self.mounts(arguments), {str(codec): "/runtime-libs/x86_64/libbz2.so.1.0"})

    def test_legacy_dist_plugins_follow_bundled_dependencies(self):
        bundled = self.runner / "dist/lib64"
        _elf(bundled / "gstreamer-1.0/libgstlibav.so", needed=["libavcodec.so.58"])
        _elf(bundled / "libavcodec.so.58", soname="libavcodec.so.58", needed=["libbz2.so.1.0"])
        codec = _elf(self.runtime / "libbz2.so.1.0.4", soname="libbz2.so.1.0")
        arguments, _, _ = self.args()
        self.assertEqual(self.mounts(arguments), {str(codec): "/runtime-libs/x86_64/libbz2.so.1.0"})

    def test_host_lib32_prevents_unnecessary_runtime_substitution(self):
        _elf(self.plugin_root / "libgstflac.so", needed=["libFLAC.so.8"], bits=32)
        _elf(self.runtime / "libFLAC.so.8", soname="libFLAC.so.8", bits=32)
        # Map the production search paths into a temporary filesystem so this
        # covers both Arch-style and separate /lib32 installations on any host.
        with patch.object(helper, "Path", side_effect=lambda value: self.root / value.lstrip("/")):
            host_roots = helper._host_library_roots()
        for directory in ("usr/lib32", "lib32"):
            with self.subTest(directory=directory):
                library = _elf(self.root / directory / "libFLAC.so.8", soname="libFLAC.so.8", bits=32)
                try:
                    with patch.object(helper, "_host_library_roots", return_value=host_roots), patch.object(helper, "_runtime_library_roots", return_value=[self.runtime]):
                        self.assertEqual(helper.proton_runtime_args(self.config), ([], {}, []))
                finally:
                    library.unlink()

    def test_does_not_replace_core_or_host_graphics_libraries(self):
        names = ["libc.so.6", "ld-linux-x86-64.so.2", "libstdc++.so.6", "libgcc_s.so.1", "libGL.so.1", "libEGL.so.1", "libvulkan.so.1", "libGLX.so.0", "libdrm.so.2", "libgbm.so.1"]
        _elf(self.plugin_root / "libgstfake.so", needed=names)
        for name in names:
            _elf(self.runtime / name, soname=name)
        self.assertEqual(self.args(), ([], {}, []))

    def test_missing_runtime_or_wrong_abi_does_not_substitute_unrelated_file(self):
        _elf(self.plugin_root / "libgstvpx.so", needed=["libvpx.so.6"])
        _elf(self.runtime / "libvpx.so.6", soname="libvpx.so.6", bits=32)
        self.assertEqual(self.args(), ([], {}, []))
        with patch.object(helper, "_runtime_library_roots", return_value=[]), patch.object(helper, "_host_library_roots", return_value=[]):
            self.assertEqual(helper.proton_runtime_args(self.config), ([], {}, []))

    def test_supports_32_bit_plugins_without_mixing_architectures(self):
        plugin = self.runner / "files/lib/i386-linux-gnu/gstreamer-1.0/libgstflac.so"
        _elf(plugin, needed=["libFLAC.so.8"], bits=32)
        codec = _elf(self.runtime / "libFLAC.so.8.3.0", soname="libFLAC.so.8", bits=32)
        arguments, environment, readonly = self.args()
        self.assertEqual(self.mounts(arguments), {str(codec): "/runtime-libs/i386/libFLAC.so.8"})
        self.assertIn("/runtime-libs/i386", environment["LD_LIBRARY_PATH"].split(":"))
        self.assertTrue(set(environment["LD_LIBRARY_PATH"].split(":")) <= {"/runtime-libs/x86_64", "/runtime-libs/i386"})
        self.assertEqual(readonly, ["/runtime-libs/i386/libFLAC.so.8"])

    def test_rejects_runtime_symlinks_outside_discovered_library_directory(self):
        _elf(self.plugin_root / "libgstvpx.so", needed=["libvpx.so.6"])
        private = _elf(self.root / "private/libvpx.so.6", soname="libvpx.so.6")
        (self.runtime / "libvpx.so.6").symlink_to(private)
        self.assertEqual(self.args(), ([], {}, []))

    def test_allows_in_directory_soname_symlink_but_mounts_regular_target(self):
        _elf(self.plugin_root / "libgstvpx.so", needed=["libvpx.so.6"])
        codec = _elf(self.runtime / "libvpx.so.6.3.0", soname="libvpx.so.6")
        (self.runtime / "libvpx.so.6").symlink_to(codec.name)
        arguments, _, _ = self.args()
        self.assertEqual(self.mounts(arguments), {str(codec): "/runtime-libs/x86_64/libvpx.so.6"})

    def test_dependency_names_cannot_be_used_as_paths(self):
        _elf(self.plugin_root / "libgstfake.so", needed=["../private.so", "/private.so"])
        _elf(self.root / "private.so", soname="../private.so")
        self.assertEqual(self.args(), ([], {}, []))

    def test_dot_sonames_cannot_select_parent_or_current_mount_directory(self):
        for name in (".", ".."):
            with self.subTest(name=name):
                _elf(self.plugin_root / "libgstfake.so", needed=[name])
                _elf(self.runtime / "libinvalid.so", soname=name)
                self.assertEqual(self.args(), ([], {}, []))

    def test_malformed_optional_steam_metadata_does_not_prevent_runtime_discovery(self):
        home = self.root / "home"
        metadata = home / ".local/share/Steam/steamapps/libraryfolders.vdf"
        metadata.parent.mkdir(parents=True)
        library = self.root / "extra-library"
        runtime = library / "steamapps/common/SteamLinuxRuntime_sniper/platform/files/lib/x86_64-linux-gnu"
        runtime.mkdir(parents=True)
        metadata.write_bytes(b'\xff\n"path" "' + str(library).encode() + b'"\n')
        with patch.object(helper, "_host_home", return_value=home):
            self.assertIn(runtime, helper._runtime_library_roots())

    def test_optional_steam_metadata_is_bounded(self):
        home = self.root / "home"
        metadata = home / ".local/share/Steam/steamapps/libraryfolders.vdf"
        metadata.parent.mkdir(parents=True)
        library = self.root / "beyond-metadata-limit"
        runtime = library / "steamapps/common/SteamLinuxRuntime_sniper/platform/files/lib/x86_64-linux-gnu"
        runtime.mkdir(parents=True)
        metadata.write_bytes(b" " * (1024 * 1024) + b'\n"path" "' + str(library).encode() + b'"\n')
        with patch.object(helper, "_host_home", return_value=home):
            self.assertNotIn(runtime, helper._runtime_library_roots())

    def test_discovers_existing_native_and_flatpak_umu_runtimes(self):
        home = self.root / "home"
        runtimes = []
        for base in (".local/share/umu", ".var/app/net.lutris.Lutris/data/umu"):
            for version, platform_name in (("steamrt3", "sniper_platform_3.0.test"), ("steamrt4", "steamrt4_platform_4.0.test")):
                runtime = home / base / version / platform_name / "files/lib/x86_64-linux-gnu"
                runtime.mkdir(parents=True)
                runtimes.append(runtime)
        with patch.object(helper, "_host_home", return_value=home):
            self.assertEqual(set(helper._runtime_library_roots()), set(runtimes))

    def test_truncated_elf_plugin_is_ignored(self):
        self.plugin_root.mkdir(parents=True)
        (self.plugin_root / "libgstbroken.so").write_bytes(b"\x7fELF\x02\x01\x01")
        self.assertEqual(self.args(), ([], {}, []))


@unittest.skipUnless(os.environ.get("KLAY_LIVE_JAIL_TESTS") == "1", "set KLAY_LIVE_JAIL_TESTS=1 for real sandbox runtime checks")
class LiveProtonRuntimeTests(unittest.TestCase):
    def test_real_bundled_gstreamer_decodes_a_synthetic_vp8_video(self):
        if platform.machine() != "x86_64" or not shutil.which("ffmpeg"):
            self.skipTest("requires x86_64 host ffmpeg")
        runner = helper._host_home() / ".local/share/Steam/compatibilitytools.d/GE-Proton10-34"
        bundled = runner / "files/lib/x86_64-linux-gnu"
        if not (bundled / "gstreamer-1.0/libgstvpx.so").is_file():
            self.skipTest("installed trusted GE-Proton10-34 VP8 plugin unavailable")
        with tempfile.TemporaryDirectory(prefix="klay-synthetic-video-") as temporary:
            root = Path(temporary)
            game = root / "source"
            game.mkdir()
            (game / "Game.exe").write_bytes(b"trusted temporary placeholder")
            generated = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi", "-i", "color=c=red:s=32x32:r=2", "-t", "1", "-c:v", "libvpx", "-threads", "1", "-b:v", "50k", "-an", str(game / "probe.webm")],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            )
            if generated.returncode:
                self.skipTest("host ffmpeg cannot generate trusted VP8 fixture: " + generated.stderr.decode(errors="replace")[-500:])
            config = helper.validate_config({"enabled": True, "game_dir": str(game), "exe": "Game.exe", "runner_type": "proton", "runner": str(runner), "arguments": []})
            store = helper.Store("trusted-video-decode-smoke", "klay-test", home=root)
            try:
                helper.prepare_game(store, config)
                command = helper.build_command(config, store.path, store.path)
                runtime_path = ""
                for index, argument in enumerate(command):
                    if argument == "--setenv" and command[index + 1] == "LD_LIBRARY_PATH":
                        runtime_path = command[index + 2]
                libraries = ["/runner/files/lib/x86_64-linux-gnu"]
                libraries += [runtime_path] if runtime_path else []
                insertion = command.index("--")
                command[insertion:insertion] = [
                    "--setenv", "LD_LIBRARY_PATH", ":".join(libraries),
                    "--setenv", "GST_PLUGIN_SYSTEM_PATH_1_0", "/runner/files/lib/x86_64-linux-gnu/gstreamer-1.0",
                    "--setenv", "GST_REGISTRY_FORK", "no",
                ]
                code = """import ctypes
from ctypes import c_char_p, c_int, c_uint64, c_void_p, POINTER, byref
gst = ctypes.CDLL('/runner/files/lib/x86_64-linux-gnu/libgstreamer-1.0.so.0', mode=ctypes.RTLD_GLOBAL)
gst.gst_init.argtypes = [c_void_p, c_void_p]
gst.gst_parse_launch.argtypes = [c_char_p, POINTER(c_void_p)]
gst.gst_parse_launch.restype = c_void_p
gst.gst_element_set_state.argtypes = [c_void_p, c_int]
gst.gst_element_set_state.restype = c_int
gst.gst_element_get_bus.argtypes = [c_void_p]
gst.gst_element_get_bus.restype = c_void_p
gst.gst_bus_timed_pop_filtered.argtypes = [c_void_p, c_uint64, c_int]
gst.gst_bus_timed_pop_filtered.restype = c_void_p
gst.gst_mini_object_unref.argtypes = [c_void_p]
gst.gst_object_unref.argtypes = [c_void_p]
gst.gst_init(None, None)
error = c_void_p()
pipeline = gst.gst_parse_launch(b'filesrc location=/game/probe.webm ! matroskademux ! vp8dec ! fakesink sync=false', byref(error))
assert pipeline and not error.value, 'Bundled GStreamer could not construct VP8 decoder pipeline'
bus = gst.gst_element_get_bus(pipeline)
try:
    assert gst.gst_element_set_state(pipeline, 4) != 0, 'Bundled GStreamer rejected PLAYING'
    # EOS-only filtering drops errors; absence of EOS still fails within the
    # bounded wait and retains GStreamer's diagnostic stderr in the test output.
    message = gst.gst_bus_timed_pop_filtered(bus, 10_000_000_000, 1)
    assert message, 'Bundled GStreamer did not decode the synthetic video to EOS'
    gst.gst_mini_object_unref(message)
finally:
    gst.gst_element_set_state(pipeline, 1)
    gst.gst_object_unref(bus)
    gst.gst_object_unref(pipeline)
print('trusted bundled VP8 video decoded to EOS', flush=True)
"""
                spec = json.loads(command[-1])
                spec.update(argv=["/usr/bin/python3", "-I", "-c", code], proton_prefix=False)
                command[-1] = json.dumps(spec)
                result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
                output = result.stdout.decode(errors="replace")
                self.assertEqual(result.returncode, 0, output)
                self.assertIn("trusted bundled VP8 video decoded to EOS", output)
                self.assertEqual(sorted(path.name for path in game.iterdir()), ["Game.exe", "probe.webm"])
            finally:
                store.close()

    def test_real_installed_proton_codec_plugins_load_inside_production_jail(self):
        if platform.machine() != "x86_64":
            self.skipTest("requires x86_64 host")
        runner = helper._host_home() / ".local/share/Steam/compatibilitytools.d/GE-Proton10-34"
        plugins = ("libgstvpx.so", "libgstwebp.so", "libgstaes.so", "libgstflac.so", "libgstdtls.so", "libgstlibav.so", "libgstbz2.so")
        plugin_root = runner / "files/lib/x86_64-linux-gnu/gstreamer-1.0"
        if not all((plugin_root / name).is_file() for name in plugins):
            self.skipTest("installed trusted GE-Proton10-34 codec plugins unavailable")
        with tempfile.TemporaryDirectory(prefix="klay-installed-codecs-") as temporary:
            root = Path(temporary)
            game = root / "source"
            game.mkdir()
            (game / "Game.exe").write_bytes(b"trusted temporary placeholder")
            config = helper.validate_config({"enabled": True, "game_dir": str(game), "exe": "Game.exe", "runner_type": "proton", "runner": str(runner), "arguments": []})
            store = helper.Store("trusted-installed-codec-smoke", "klay-test", home=root)
            try:
                helper.prepare_game(store, config)
                command = helper.build_command(config, store.path, store.path)
                # Emulate the exact library prefix added by proton's init_wine,
                # then load each plugin in a fresh process to avoid satisfying
                # another plugin's dependencies through a previous dlopen.
                runtime_path = ""
                for index, argument in enumerate(command):
                    if argument == "--setenv" and command[index + 1] == "LD_LIBRARY_PATH":
                        runtime_path = command[index + 2]
                libraries = ["/runner/files/lib/" + abi for abi in ("x86_64-linux-gnu", "aarch64-linux-gnu", "i386-linux-gnu")]
                libraries += [runtime_path] if runtime_path else []
                insertion = command.index("--")
                command[insertion:insertion] = ["--setenv", "LD_LIBRARY_PATH", ":".join(libraries)]
                code = """import pathlib, subprocess, sys
for name in sys.argv[1:]:
    path = '/runner/files/lib/x86_64-linux-gnu/gstreamer-1.0/' + name
    child = subprocess.run([sys.executable, '-I', '-c', 'import ctypes,sys; ctypes.CDLL(sys.argv[1]); print(sys.argv[1])', path], capture_output=True, text=True, timeout=10)
    assert child.returncode == 0, name + ': ' + child.stdout + child.stderr
    print('trusted installed codec loaded:', name, flush=True)
assert not pathlib.Path('/run/dbus/system_bus_socket').exists()
"""
                spec = json.loads(command[-1])
                spec.update(argv=["/usr/bin/python3", "-I", "-c", code, *plugins], proton_prefix=False)
                command[-1] = json.dumps(spec)
                result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
                output = result.stdout.decode(errors="replace")
                self.assertEqual(result.returncode, 0, output)
                self.assertEqual(output.count("trusted installed codec loaded:"), len(plugins), output)
                self.assertEqual(sorted(path.name for path in game.iterdir()), ["Game.exe"])
            finally:
                store.close()

    def test_real_missing_codec_dependency_loads_with_readonly_narrow_fallback(self):
        if platform.machine() != "x86_64" or not shutil.which("gcc"):
            self.skipTest("requires x86_64 host GCC")
        with tempfile.TemporaryDirectory(prefix="klay-live-runtime-") as temporary:
            root = Path(temporary)
            runner = root / "trusted-proton"
            plugin_root = runner / "files/lib/x86_64-linux-gnu/gstreamer-1.0"
            plugin_root.mkdir(parents=True)
            runtime = root / "installed-runtime/lib/x86_64-linux-gnu"
            runtime.mkdir(parents=True)
            game = root / "source"
            game.mkdir()
            (game / "Game.exe").write_bytes(b"trusted temporary placeholder")
            (root / "codec.c").write_text("int klay_codec_value(void) { return 42; }\n")
            (root / "plugin.c").write_text("extern int klay_codec_value(void); int klay_probe(void) { return klay_codec_value(); }\n")
            codec = runtime / "libklay_codec_test.so.6.3.0"
            plugin = plugin_root / "libgstklay-test.so"
            commands = [
                ["gcc", "-shared", "-fPIC", "-nostdlib", "-Wl,-soname,libklay_codec_test.so.6", str(root / "codec.c"), "-o", str(codec)],
                ["gcc", "-shared", "-fPIC", "-nostdlib", "-Wl,--no-as-needed", str(root / "plugin.c"), str(codec), "-o", str(plugin)],
            ]
            for command in commands:
                subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            script = """#!/usr/bin/python3
import ctypes, os, pathlib
library = ctypes.CDLL('/runner/files/lib/x86_64-linux-gnu/gstreamer-1.0/libgstklay-test.so')
assert library.klay_probe() == 42
assert os.statvfs('/runtime-libs/x86_64/libklay_codec_test.so.6').f_flag & os.ST_RDONLY
assert sorted(path.name for path in pathlib.Path('/runtime-libs/x86_64').iterdir()) == ['libklay_codec_test.so.6']
assert not pathlib.Path(%r).exists()
assert not pathlib.Path('/run/dbus/system_bus_socket').exists()
assert [line.split(':')[0].strip() for line in open('/proc/net/dev') if ':' in line] == ['lo']
print('trusted missing codec loaded through narrow readonly fallback', flush=True)
""" % str(runtime)
            (runner / "proton").write_text(script)
            (runner / "proton").chmod(0o700)
            config = helper.validate_config({"enabled": True, "game_dir": str(game), "exe": "Game.exe", "runner_type": "proton", "runner": str(runner), "arguments": []})
            store = helper.Store("trusted-runtime-smoke", "klay-test", home=root)
            try:
                helper.prepare_game(store, config)
                with patch.object(helper, "_runtime_library_roots", return_value=[]):
                    baseline = helper.build_command(config, store.path, store.path)
                missing = subprocess.run(baseline, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
                self.assertNotEqual(missing.returncode, 0, missing.stdout.decode(errors="replace"))
                self.assertIn(b"libklay_codec_test.so.6", missing.stdout)
                # This uses the production sandbox and supervisor. Only the
                # discovery fixture points at our locally compiled dependency.
                with patch.object(helper, "_runtime_library_roots", return_value=[runtime]):
                    command = helper.build_command(config, store.path, store.path)
                result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
                output = result.stdout.decode(errors="replace")
                self.assertEqual(result.returncode, 0, output)
                self.assertIn("trusted missing codec loaded", output)
                self.assertEqual((game / "Game.exe").read_bytes(), b"trusted temporary placeholder")
                self.assertEqual(sorted(path.name for path in game.iterdir()), ["Game.exe"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
