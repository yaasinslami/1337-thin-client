"""Official-installer boundary tests: no real downloads or installations."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import test_safety as fixtures

tool = fixtures.tool

# A harmless installer fixture emulates the documented package layout and checks
# that even an installer writing HOME cannot touch the real persistent account.
INSTALLER = '''#!/bin/sh
set -eu
mkdir -p "$CODEX_HOME/packages/standalone/releases/fixture/bin" "$CODEX_INSTALL_DIR"
printf '#!/bin/sh\\nexit 0\\n' > "$CODEX_HOME/packages/standalone/releases/fixture/bin/codex"
chmod 755 "$CODEX_HOME/packages/standalone/releases/fixture/bin/codex"
ln -sfn releases/fixture "$CODEX_HOME/packages/standalone/current"
ln -sfn "$CODEX_HOME/packages/standalone/current/bin/codex" "$CODEX_INSTALL_DIR/codex"
printf 'installer rc' > "$HOME/.zshrc"
'''


class CodexInstallTests(unittest.TestCase):
    setUp = fixtures.SafetyTests.setUp
    fake_mount = fixtures.SafetyTests.fake_mount

    def fake_commands(self, argv, **kwargs):
        argv = [str(arg) for arg in argv]
        if argv[0] == 'curl':
            self.assertEqual(argv[-1], 'https://chatgpt.com/codex/install.sh')
            Path(argv[argv.index('-o') + 1]).write_text(INSTALLER)
            return mock.Mock(returncode=0)
        self.assertEqual(argv[0], 'sh')
        env = kwargs['env']
        self.assertEqual(env['CODEX_HOME'], str(self.root.parent / 'codex-storage'))
        self.assertEqual(env['CODEX_INSTALL_DIR'], str(self.root.parent / 'codex-bin'))
        self.assertNotEqual(env['HOME'], str(self.home))
        self.assertTrue(Path(env['HOME']).is_relative_to(self.root.parent))
        self.assertEqual(env['CODEX_NON_INTERACTIVE'], '1')
        self.assertNotIn('CODEX_INSTALL_DAEMON_ONLY', env)
        self.assertNotIn('CODEX_INSTALL_DEFER_SELECTION', env)
        return subprocess.run(argv, **kwargs, check=True, capture_output=True)

    def test_install_heavy_payload_only_in_goinfre_state_preserved_and_env_local(self):
        state = self.home / '.codex'
        (state / 'sessions').mkdir(parents=True)
        for name in ('auth.json', 'config.toml', 'sessions/session.jsonl'):
            (state / name).write_bytes(b'sensitive persistent fixture\x00\xff')
        (self.home / '.zshrc').write_bytes(b'alias vssh=keep\n')
        (self.home / '.bashrc').write_bytes(b'untouched bash\n')
        (self.home / '.zsh_history').write_bytes(b'untouched history\n')
        expected = {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(state), 'CODEX_INSTALL_DAEMON_ONLY': '1'}):
            before_env = dict(os.environ)
            with self.fake_mount(), mock.patch.object(tool, 'run', side_effect=self.fake_commands):
                tool.install_codex(self.root)
            self.assertEqual(dict(os.environ), before_env)
        self.assertTrue(tool.codex_installed(self.root))
        self.assertEqual(expected, {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()})
        storage, launchers = tool.codex_paths(self.root)
        self.assertFalse(storage.is_relative_to(self.home))
        self.assertTrue((launchers / 'codex').resolve().is_relative_to(storage))
        with self.fake_mount(), mock.patch.object(tool, 'run', side_effect=AssertionError('idempotent: no downloads')):
            tool.install_codex(self.root)

    def test_missing_launcher_repaired_without_persistent_state_changes(self):
        with self.fake_mount(), mock.patch.object(tool, 'run', side_effect=self.fake_commands):
            tool.install_codex(self.root)
            (self.root.parent / 'codex-bin/codex').unlink()
            tool.install_codex(self.root)
        self.assertTrue(tool.codex_installed(self.root))
        self.assertFalse((self.home / '.codex').exists())

    def test_unknown_launcher_and_symlink_destinations_refused(self):
        launchers = self.root.parent / 'codex-bin'
        launchers.mkdir()
        binary = launchers / 'codex'
        binary.write_text('user command')
        with self.fake_mount(), mock.patch.object(tool, 'run', side_effect=AssertionError('no downloads')):
            with self.assertRaisesRegex(RuntimeError, 'Unrecognized'):
                tool.install_codex(self.root)
            self.assertEqual(binary.read_text(), 'user command')
            binary.unlink()
            binary.symlink_to('/usr/bin/true')
            with self.assertRaisesRegex(RuntimeError, 'Unrelated'):
                tool.install_codex(self.root)
        self.assertEqual(os.readlink(binary), '/usr/bin/true')
        binary.unlink()
        launchers.rmdir()
        launchers.symlink_to(self.home)
        with self.fake_mount(), self.assertRaises(RuntimeError):
            tool.install_codex(self.root)

    def test_home_cannot_be_used_as_package_storage(self):
        root = self.home / 'thin-client'
        with mock.patch.object(tool, 'run', side_effect=AssertionError('no installer')), \
             self.assertRaises(RuntimeError):
            tool.install_codex(root)
        self.assertFalse(root.exists())

    def test_missing_post_reports_absent_without_mutating_persistent_state(self):
        state = self.home / '.codex'
        state.mkdir()
        (state / 'auth.json').write_text('PRIVATE_DO_NOT_PRINT')
        before = sorted(self.base.rglob('*'))
        with mock.patch.object(tool, 'goinfre', side_effect=RuntimeError('post storage absent')), \
             mock.patch.object(tool, 'run', side_effect=AssertionError('status is local')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            tool.main(['codex', 'status'])
        self.assertIn('not installed', output.getvalue())
        self.assertNotIn('PRIVATE', output.getvalue())
        self.assertEqual(before, sorted(self.base.rglob('*')))
        self.assertEqual((state / 'auth.json').read_text(), 'PRIVATE_DO_NOT_PRINT')
