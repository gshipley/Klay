"""Graphics diagnostics run trusted tools inside the production jail boundary."""
import json
import os
from pathlib import Path
import subprocess
import socket
import stat
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from klay.qt import jail_helper as helper


class GraphicsChecksTests(unittest.TestCase):
    def test_local_authority_keeps_auth_hostname_in_separate_namespace(self):
        with tempfile.TemporaryDirectory(prefix='klay-xauth-test-') as temporary:
            root = Path(temporary)
            authority = root / 'authority'
            authority.write_bytes(b'synthetic authority metadata')
            original_lstat = Path.lstat
            def lstat(path, *args, **kwargs):
                if path == Path('/tmp/.X11-unix/X17391'):
                    return SimpleNamespace(st_mode=stat.S_IFSOCK)
                return original_lstat(path, *args, **kwargs)
            config = {'runner_type': 'wine', 'runner': '/usr/bin/wine', 'exe': 'unused.exe', 'arguments': []}
            with (patch.object(Path, 'lstat', lstat),
                  patch.object(helper, 'check_dependencies', return_value=('/usr/bin/bwrap', '/usr/bin/python3')),
                  patch.object(helper.os, 'uname', return_value=SimpleNamespace(nodename='auth-host')),
                  patch.dict(os.environ, {'DISPLAY': ':17391', 'XAUTHORITY': str(authority)})):
                command = helper.build_command(config, root, root)
                self.assertEqual(command[command.index('--hostname') + 1], 'auth-host')
                self.assertIn('--unshare-all', command)
                self.assertIn('uts', json.loads(command[-1])['namespaces'])
                authority.unlink()
                command = helper.build_command(config, root, root)
                self.assertEqual(command[command.index('--hostname') + 1], 'klay-jail')

    def command(self, *args, **kwargs):
        return ['bwrap', '--unshare-all', '--clearenv', '--', 'python3', '-I', '-c', 'trusted',
                json.dumps({'argv': ['unused-game.exe'], 'cwd': '/game/bin', 'proton_prefix': True,
                            'hidden': ['/home/user/.ssh'], 'readonly': ['/usr']})]

    def run_checks(self, outputs):
        with (patch.object(Path, 'is_file', return_value=True),
              patch.object(helper.os, 'access', return_value=True),
              patch.dict(helper.os.environ, {'DISPLAY': ':0'}),
              patch.object(helper, 'build_command', side_effect=self.command),
              patch.object(helper.subprocess, 'run', side_effect=outputs) as run):
            result = helper.check_graphics({}, Path('/private/test'), Path('/private/state'))
        return result, run

    def test_diagnostics_use_exact_system_tools_and_keep_boundary(self):
        result, run = self.run_checks([
            subprocess.CompletedProcess([], 0, b'OpenGL renderer string: NVIDIA test GPU\n'),
            subprocess.CompletedProcess([], 0, b'deviceName = NVIDIA test GPU\ndeviceName = Intel test GPU\n'),
        ])
        self.assertEqual(result['opengl'], {'status': 'passed', 'renderers': ['NVIDIA test GPU']})
        self.assertEqual(result['vulkan']['renderers'], ['NVIDIA test GPU', 'Intel test GPU'])
        for call, executable in zip(run.call_args_list, ('glxinfo', 'vulkaninfo')):
            command = call.args[0]
            self.assertIn('--unshare-all', command)
            self.assertIn('--clearenv', command)
            spec = json.loads(command[-1])
            self.assertEqual(spec['argv'][0], '/usr/bin/' + executable)
            self.assertEqual(spec['cwd'], '/game')
            self.assertFalse(spec['proton_prefix'])
            self.assertEqual(spec['hidden'], ['/home/user/.ssh'])
            self.assertNotIn('unused-game.exe', spec['argv'])

    def test_failed_glx_and_missing_vulkan_device_are_not_success(self):
        result, _ = self.run_checks([
            subprocess.CompletedProcess([], 1, b'X_GLXCreateContext BadValue'),
            subprocess.CompletedProcess([], 0, b'No Vulkan devices'),
        ])
        self.assertEqual(result['opengl']['status'], 'failed')
        self.assertIn('BadValue', result['opengl']['detail'])
        self.assertEqual(result['vulkan']['status'], 'failed')

    def test_timeouts_are_explicit_and_bounded(self):
        result, _ = self.run_checks([subprocess.TimeoutExpired('glxinfo', 10),
                                     subprocess.TimeoutExpired('vulkaninfo', 10)])
        self.assertEqual(result['opengl']['status'], 'failed')
        self.assertIn('timed out', result['vulkan']['detail'])

    def test_optional_diagnostics_do_not_require_additional_host_packages(self):
        with patch.object(Path, 'is_file', return_value=False), patch.object(helper.subprocess, 'run') as run:
            result = helper.check_graphics({}, Path('/private/test'), Path('/private/state'))
        self.assertEqual(result['opengl']['status'], 'unavailable')
        self.assertEqual(result['vulkan']['status'], 'unavailable')
        run.assert_not_called()

    def test_no_x_display_skips_only_opengl(self):
        with (patch.object(Path, 'is_file', return_value=True),
              patch.object(helper.os, 'access', return_value=True),
              patch.dict(helper.os.environ, {'DISPLAY': ''}),
              patch.object(helper, 'build_command', side_effect=self.command),
              patch.object(helper.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'deviceName = test GPU')) as run):
            result = helper.check_graphics({}, Path('/private/test'), Path('/private/state'))
        self.assertEqual(result['opengl']['status'], 'unavailable')
        self.assertEqual(result['vulkan']['status'], 'passed')
        self.assertEqual(run.call_count, 1)


@unittest.skipUnless(os.environ.get('KLAY_LIVE_JAIL_TESTS') == '1', 'live graphics checks are opt-in')
class LiveGraphicsChecks(unittest.TestCase):
    def test_family_local_cookie_reaches_synthetic_x_server(self):
        with tempfile.TemporaryDirectory(prefix='klay-live-xauth-') as temporary:
            root = Path(temporary)
            authority = root / 'authority'
            cookie = bytes(range(16))
            fields = [socket.gethostname().encode(), b'17391', b'MIT-MAGIC-COOKIE-1', cookie]
            authority.write_bytes(struct.pack('>H', 256) + b''.join(struct.pack('>H', len(value)) + value for value in fields))
            server = socket.socket(socket.AF_UNIX)
            self.addCleanup(server.close)
            socket_path = root / 'X17391'
            server.bind(str(socket_path))
            server.listen()
            server.settimeout(10)
            captured, errors = [], []
            def receive():
                try:
                    connection, _ = server.accept()
                    with connection:
                        connection.settimeout(5)
                        captured.append(connection.recv(4096))
                except OSError as error:
                    errors.append(str(error))
            thread = threading.Thread(target=receive, daemon=True)
            thread.start()
            store = helper.Store('synthetic-xauth', 'klay-test', root)
            self.addCleanup(store.close)
            for name in ('game', 'prefix', 'home'):
                descriptor = store.directory(name)
                os.close(descriptor)
            original_lstat = Path.lstat
            def lstat(path, *args, **kwargs):
                if path == Path('/tmp/.X11-unix/X17391'):
                    return socket_path.lstat()
                return original_lstat(path, *args, **kwargs)
            config = {'runner_type': 'wine', 'runner': '/usr/bin/python3', 'exe': 'unused.exe', 'arguments': []}
            with patch.object(Path, 'lstat', lstat), patch.dict(os.environ, {'DISPLAY': ':17391', 'XAUTHORITY': str(authority)}):
                command = helper.build_command(config, store.path, store.path)
            source_index = command.index('/tmp/.X11-unix/X17391')
            command[source_index] = str(socket_path)
            spec = json.loads(command[-1])
            spec['argv'] = ['/usr/bin/python3', '-c', "import ctypes; lib=ctypes.CDLL('libX11.so.6'); lib.XOpenDisplay.argtypes=[ctypes.c_char_p]; lib.XOpenDisplay.restype=ctypes.c_void_p; lib.XOpenDisplay(None)"]
            command[-1] = json.dumps(spec)
            result = subprocess.run(command, capture_output=True, timeout=12)
            thread.join(10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertFalse(errors, errors)
            self.assertTrue(captured, 'X11 client never contacted synthetic server')
            data = captured[0]
            self.assertGreaterEqual(len(data), 12)
            name_length, cookie_length = struct.unpack_from('<HH' if data[:1] == b'l' else '>HH', data, 6)
            self.assertEqual((name_length, cookie_length), (18, 16))
            cookie_offset = 12 + (name_length + 3) // 4 * 4
            self.assertEqual(data[cookie_offset:cookie_offset + cookie_length], cookie)

    def test_host_renderer_remains_available_inside_jail(self):
        if not Path('/usr/bin/glxinfo').is_file() or not os.environ.get('DISPLAY'):
            self.skipTest('host has no glxinfo or X11 display')
        baseline = subprocess.run(['/usr/bin/glxinfo', '-B'], capture_output=True, text=True, timeout=10)
        if baseline.returncode:
            self.skipTest('host OpenGL is unavailable outside the jail')
        with tempfile.TemporaryDirectory(prefix='klay-live-graphics-') as temporary:
            root = Path(temporary)
            home, source, runner = (root / name for name in ('account', 'source', 'runner'))
            for directory in (home, source, runner):
                directory.mkdir(mode=0o700)
            (source / 'unused.exe').write_bytes(b'This fixture is never executed')
            (runner / 'proton').write_text('#!/bin/sh\nexit 0\n')
            (runner / 'proton').chmod(0o700)
            config = helper.validate_config({'enabled': True, 'game_dir': str(source),
                'exe': 'unused.exe', 'runner_type': 'proton', 'runner': str(runner), 'arguments': []})
            store = helper.Store('graphics-test', 'klay', home)
            try:
                result = helper.check(config, store)
            finally:
                store.close()
        self.assertEqual(result['graphics']['opengl']['status'], 'passed')
        if 'NVIDIA' in baseline.stdout:
            self.assertIn('NVIDIA', ' '.join(result['graphics']['opengl']['renderers']))
            if Path('/usr/bin/vulkaninfo').is_file():
                self.assertIn('NVIDIA', ' '.join(result['graphics']['vulkan']['renderers']))
