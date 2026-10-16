"""Opt-in XDG Autostart and one-time removal of the uncommitted zsh trigger."""
import contextlib
import io
from pathlib import Path
import shlex
import shutil
import subprocess
import unittest
from unittest import mock

import test_safety as fixtures

tool = fixtures.tool
LEGACY = (b'# >>> 1337 thin-client session >>>\n'
          b'if [[ -o interactive ]] && command -v thin-client-setup >/dev/null; then\n'
          b'    thin-client-setup session-init || :\n'
          b'fi\n# <<< 1337 thin-client session <<<\n')


class AutostartTests(unittest.TestCase):
    setUp = fixtures.SafetyTests.setUp

    def test_enable_correct_desktop_fields_absolute_exec_and_idempotency(self):
        tool.main(['session', 'enable'])
        path = tool.session_entry()
        fields = tool.desktop_fields(path)
        self.assertEqual(shlex.split(fields['Exec']), [str(tool.SELF), 'session-init'])
        self.assertEqual(fields['Type'], 'Application')
        for name in ('Terminal', 'StartupNotify'):
            self.assertEqual(fields[name], 'false')
        for name in ('NoDisplay', 'X-GNOME-Autostart-enabled'):
            self.assertEqual(fields[name], 'true')
        self.assertNotIn('/home/yslami', path.read_text())
        before = path.read_bytes(), path.stat().st_mtime_ns
        tool.main(['session', 'enable'])
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_disable_only_managed_entry_and_preserves_other_autostart(self):
        tool.session_enable()
        other = tool.session_entry().with_name('unrelated.desktop')
        other.write_bytes(b'user entry\x00\xff')
        tool.main(['session', 'disable'])
        self.assertFalse(tool.session_entry().exists())
        self.assertEqual(other.read_bytes(), b'user entry\x00\xff')
        tool.session_disable()

    @unittest.skipUnless(shutil.which('desktop-file-validate'), 'desktop validator available')
    def test_desktop_entry_valid_with_spaces_in_home(self):
        home = self.base / 'another user'
        home.mkdir()
        with mock.patch.multiple(tool, HOME=home, SELF=home / '.local/bin/thin-client-setup'):
            tool.session_enable()
            subprocess.run(['desktop-file-validate', str(tool.session_entry())],
                           capture_output=True, check=True)
            self.assertEqual(shlex.split(tool.desktop_fields(tool.session_entry())['Exec']),
                             [str(tool.SELF), 'session-init'])

    def test_unmanaged_collision_refused_enable_and_disable(self):
        path = tool.session_entry()
        path.parent.mkdir(parents=True)
        for contents in ('[Desktop Entry]\nName=My App\n',
                         '[Desktop Entry]\n# Managed by thin-client-setup\nName=Different Feature\n'):
            path.write_text(contents)
            for action in ('enable', 'disable'):
                with self.assertRaisesRegex(RuntimeError, 'unrelated'):
                    tool.main(['session', action])
                self.assertEqual(path.read_text(), contents)

    def test_symlinked_file_or_directory_refused(self):
        path = tool.session_entry()
        path.parent.mkdir(parents=True)
        other = self.base / 'other'
        other.write_text('unchanged')
        path.symlink_to(other)
        for command in (tool.session_enable, tool.session_disable):
            with self.assertRaises(RuntimeError):
                command()
        self.assertEqual(other.read_text(), 'unchanged')
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(self.base)
        with self.assertRaises(RuntimeError):
            tool.session_enable()

    def test_install_self_does_not_enable_and_upgrades_only_existing_opt_in(self):
        tool.install_self()
        self.assertFalse(tool.session_entry().exists())
        self.assertFalse((self.home / '.zshrc').exists())
        tool.session_enable()
        path = tool.session_entry()
        path.write_text(path.read_text().replace('session-init', 'obsolete-command'))
        tool.install_self()
        self.assertEqual(path.read_text(), tool.session_entry_contents())
        tool.session_disable()
        tool.install_self()
        self.assertFalse(path.exists())

    def test_upgrade_preserves_explicit_hidden_or_disabled_entry(self):
        for setting in ('Hidden=true', 'X-GNOME-Autostart-enabled=false'):
            tool.session_enable()
            path = tool.session_entry()
            data = path.read_text().replace('X-GNOME-Autostart-enabled=true\n', '') + setting + '\n'
            path.write_text(data)
            tool.install_self()
            self.assertEqual(path.read_text(), data)
        path.write_text('[Desktop Entry]\nName=User Data\n')
        tool.install_self()
        self.assertEqual(path.read_text(), '[Desktop Entry]\nName=User Data\n')

    def test_all_normal_commands_leave_shell_files_byte_identical(self):
        paths = [self.home / name for name in ('.zshrc', '.bashrc', '.zprofile', '.profile', '.zsh_history')]
        for p in paths:
            p.write_bytes(b'alias vssh=keep\xff\n' + LEGACY + b'# suffix\n')
        expected = [p.read_bytes() for p in paths]
        tool.session_enable()
        tool.install_self()
        tool.session_disable()
        self.assertEqual(expected, [p.read_bytes() for p in paths])

    def test_exact_legacy_block_removal_preserves_outside_bytes_and_mode(self):
        path = self.home / '.zshrc'
        prefix = b"alias vssh='fixture value'\nSAVEHIST=100000\n# nonutf8 \xff\n"
        suffix = b"alias other='unchanged'\n"
        path.write_bytes(prefix + LEGACY + suffix)
        path.chmod(0o640)
        history = self.home / '.zsh_history'
        history.write_bytes(b'protected history\x00\xff')
        tool.remove_legacy_zsh_trigger()
        self.assertEqual(path.read_bytes(), prefix + suffix)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        stamp = path.stat().st_mtime_ns
        tool.remove_legacy_zsh_trigger()
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertEqual(history.read_bytes(), b'protected history\x00\xff')

    def test_modified_legacy_block_and_symlinked_rc_are_never_changed(self):
        path = self.home / '.zshrc'
        data = LEGACY.replace(b'    thin-client-setup session-init || :', b'    personal-command')
        path.write_bytes(data)
        with self.assertRaisesRegex(RuntimeError, 'Unrecognized'):
            tool.remove_legacy_zsh_trigger()
        self.assertEqual(path.read_bytes(), data)
        path.unlink()
        other = self.base / 'other-rc'
        other.write_bytes(LEGACY)
        path.symlink_to(other)
        with self.assertRaises(RuntimeError):
            tool.remove_legacy_zsh_trigger()
        self.assertEqual(other.read_bytes(), LEGACY)
