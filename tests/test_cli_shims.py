"""Generic commands use managed native wrappers without shell configuration edits."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import test_safety as fixtures

tool = fixtures.tool


class CliShimTests(unittest.TestCase):
    setUp = fixtures.SafetyTests.setUp
    icon_fixture = fixtures.SafetyTests.icon_fixture

    def test_missing_links_and_idempotency(self):
        tool.install_cli_shims()
        paths = [self.home / '.local/bin' / app for app in ('code', 'brave')]
        before = [p.lstat().st_ino for p in paths]
        tool.install_cli_shims()
        self.assertEqual(before, [p.lstat().st_ino for p in paths])
        for p in paths:
            self.assertEqual(os.readlink(p), str(p.with_name(p.name + '-native')))

    def test_stale_exact_user_bin_link_is_repaired(self):
        directory = self.home / '.local/bin'
        directory.mkdir(parents=True)
        for app in ('code', 'brave'):
            (directory / app).symlink_to(self.home / 'bin' / (app + '-native'))
        tool.install_cli_shims()
        for app in ('code', 'brave'):
            self.assertEqual(os.readlink(directory / app), str(directory / (app + '-native')))

    def test_collisions_refused_without_partial_changes(self):
        directory = self.home / '.local/bin'
        directory.mkdir(parents=True)
        path = directory / 'brave'
        outside = self.base / 'user-code-native'
        outside.write_bytes(b'user owned')
        for kind in ('file', 'directory', 'link'):
            with self.subTest(kind=kind):
                if kind == 'file':
                    path.write_bytes(b'keep')
                elif kind == 'directory':
                    path.mkdir()
                else:
                    path.symlink_to(outside)
                with self.assertRaisesRegex(RuntimeError, 'Refusing'):
                    tool.install_cli_shims()
                self.assertFalse((directory / 'code').is_symlink())
                self.assertEqual(outside.read_bytes(), b'user owned')
                if kind == 'directory':
                    path.rmdir()
                else:
                    if kind == 'file':
                        self.assertEqual(path.read_bytes(), b'keep')
                    path.unlink()

    def test_symlinked_bin_parent_refused(self):
        (self.home / '.local').mkdir()
        (self.home / '.local/bin').symlink_to(self.base)
        with self.assertRaises(RuntimeError):
            tool.install_cli_shims()

    def test_relative_correct_link_unchanged(self):
        directory = self.home / '.local/bin'
        directory.mkdir(parents=True)
        path = directory / 'code'
        path.symlink_to('code-native')
        before = path.lstat().st_ino
        tool.install_cli_shims()
        self.assertEqual(path.lstat().st_ino, before)
        self.assertEqual(os.readlink(path), 'code-native')

    def test_old_location_with_unmanaged_wrapper_refused(self):
        directory = self.home / '.local/bin'
        directory.mkdir(parents=True)
        old = self.home / 'bin/code-native'
        old.parent.mkdir()
        old.write_text('unrelated user wrapper')
        path = directory / 'code'
        path.symlink_to(old)
        with self.assertRaisesRegex(RuntimeError, 'unmanaged wrapper'):
            tool.install_cli_shims()
        self.assertEqual(os.readlink(path), str(old))
        self.assertEqual(old.read_text(), 'unrelated user wrapper')

    def test_arguments_and_wrappers_unchanged(self):
        directory = self.home / '.local/bin'
        directory.mkdir(parents=True)
        contents = b'#!/bin/sh\nprintf "%s\\n" "$@"\n'
        for app in ('code', 'brave'):
            wrapper = directory / (app + '-native')
            wrapper.write_bytes(contents)
            wrapper.chmod(0o755)
        tool.install_cli_shims()
        for app in ('code', 'brave'):
            result = subprocess.run([str(directory / app), '--new-window', 'a b', ''],
                                    check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout, '--new-window\na b\n\n')
            self.assertEqual((directory / (app + '-native')).read_bytes(), contents)

    def test_prepare_reaches_shims_without_unrelated_shell_or_system_writes(self):
        sentinels = [self.home / name for name in ('.zshrc', '.bashrc', '.zsh_history')]
        sentinels += [self.base / 'system-code']
        for p in sentinels:
            p.write_bytes(b"alias vssh='keep'; history sentinel\x00\xff\n")
        expected = [p.read_bytes() for p in sentinels]
        self.icon_fixture()
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'prepare_core', side_effect=lambda root: tool.desktop_integration(root)), \
             mock.patch.object(tool, 'run'):
            tool.main(['prepare'])
        self.assertEqual(expected, [p.read_bytes() for p in sentinels])
        self.assertTrue((self.home / '.local/bin/code').is_symlink())
        (self.home / '.local/bin/code').unlink()
        tool.install_self()
        self.assertTrue((self.home / '.local/bin/code').is_symlink())

    def test_diagnostics_warn_about_path_without_editing_it(self):
        tool.install_cli_shims()
        output = io.StringIO()
        with mock.patch.object(tool.shutil, 'which', return_value='/usr/bin/code'), \
             contextlib.redirect_stdout(output):
            tool.cli_diagnostics()
        self.assertIn('CLI code:', output.getvalue())
        self.assertIn('put ~/.local/bin first in PATH', output.getvalue())
        with mock.patch.object(tool.shutil, 'which', side_effect=lambda app: str(self.home / '.local/bin' / app)), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            tool.cli_diagnostics()
        self.assertNotIn('MISMATCH', output.getvalue())
