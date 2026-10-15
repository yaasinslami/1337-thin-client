"""Process-local Brave runtime and permanent hands-off Service Worker policy."""
import contextlib
import io
import os
from pathlib import Path
import shutil
import unittest
from unittest import mock

import test_safety as fixtures

tool = fixtures.tool


class BraveRuntimeTests(unittest.TestCase):
    setUp = fixtures.SafetyTests.setUp
    native_fixture = fixtures.SafetyTests.native_fixture
    fake_mount = fixtures.SafetyTests.fake_mount

    def test_brave_launch_runtime_and_persistent_profile_idempotently(self):
        self.native_fixture('brave')
        before = dict(os.environ)
        with self.fake_mount(), mock.patch.object(tool.os, 'execve') as launch:
            for _ in range(2):
                tool.open_app(self.root, 'brave', [])
                _, argv, env = launch.call_args.args
                for name in ('config', 'cache'):
                    path = self.root / 'runtime/brave' / name
                    self.assertTrue(path.is_dir())
                    self.assertEqual(env[f'XDG_{name.upper()}_HOME'], str(path))
                    self.assertTrue(path.resolve().is_relative_to(self.root.resolve()))
                    self.assertFalse(path.resolve().is_relative_to(self.home.resolve()))
                self.assertIn(f'--user-data-dir={tool.BRAVE}', argv)
                self.assertNotIn('--no-sandbox', argv)
        self.assertEqual(dict(os.environ), before)

    def test_runtime_rejects_symlink_ancestor(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (self.root / 'runtime').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Unexpected symlink'):
            tool.environment(self.root, 'brave')
        self.assertEqual(list(outside.iterdir()), [])

    def test_code_keeps_its_own_environment(self):
        self.native_fixture('code')
        with mock.patch.object(tool.os, 'execve') as launch:
            tool.open_app(self.root, 'code', [])
        _, argv, env = launch.call_args.args
        self.assertEqual(env['XDG_CONFIG_HOME'], str(self.home / '.config'))
        self.assertEqual(env['XDG_CACHE_HOME'], str(self.root / 'cache/vscode'))
        self.assertIn(str(tool.CODE), [str(arg) for arg in argv])
        self.assertFalse((self.root / 'runtime/brave').exists())

    def test_prepare_reconstructs_fresh_runtime_without_importing_worker_state(self):
        self.native_fixture('brave')
        self.native_fixture('code')
        profile = tool.BRAVE / 'Default'
        profile.mkdir()
        # Old data still exists, but preparation must never restore it to HOME.
        old = self.root / 'cache/brave-profile/Default/Service Worker/ScriptCache'
        old.mkdir(parents=True)
        (old / 'persistent').write_bytes(b'old worker state')
        with self.fake_mount(), mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'stopped'), mock.patch.object(tool, 'install_self'), \
             mock.patch.object(tool, 'install'), mock.patch.object(tool, 'run'), \
             mock.patch.object(tool, 'desktop_integration'):
            for _ in range(2):
                tool.main(['prepare'])
                for name in ('config', 'cache'):
                    self.assertTrue((self.root / 'runtime/brave' / name).is_dir())
                self.assertFalse((profile / 'Service Worker').exists())
                self.assertEqual((old / 'persistent').read_bytes(), b'old worker state')
            shutil.rmtree(self.root)
            self.root.mkdir()
            tool.main(['prepare'])
            for name in ('config', 'cache'):
                self.assertTrue((self.root / 'runtime/brave' / name).is_dir())
            self.assertFalse((profile / 'Service Worker').exists())

    def test_repair_command_and_status_advice_removed(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            tool.cli_parser().parse_args(['repair-legacy'])
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'script_status'), mock.patch.object(tool, 'desktop_diagnostics'), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            tool.main(['status'])
        self.assertNotIn('repair-legacy', output.getvalue())
        self.assertNotIn('Legacy Service Worker', output.getvalue())

    def test_runtime_has_no_machine_specific_branches_or_sandbox_override(self):
        source = Path(tool.__file__).read_text()
        for forbidden in ('gethostname', 'uname().nodename', '/etc/hostname', 'E3', '--no-sandbox'):
            self.assertNotIn(forbidden, source)
