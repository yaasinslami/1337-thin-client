"""Desktop menu integration on isolated school-image fixtures."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import test_safety as fixtures
import test_release as release

tool = fixtures.tool
application_dirs = tool.system_application_dirs


class DesktopMenuTests(unittest.TestCase):
    setUp = fixtures.SafetyTests.setUp
    icon_fixture = fixtures.SafetyTests.icon_fixture
    native_fixture = fixtures.SafetyTests.native_fixture
    fake_mount = fixtures.SafetyTests.fake_mount
    xdg_mock = release.ReleaseTests.xdg_mock

    def entry(self, directory, name, fields):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('[Desktop Entry]\nType=Application\n' + fields + '\n')
        return path

    def test_machine_a_b_c_mask_actual_code_ids_without_overmatching(self):
        self.icon_fixture()
        for machine, names in [('A', ['code.desktop']),
                               ('B', ['com.visualstudio.code.desktop']),
                               ('C', ['code.desktop', 'com.visualstudio.code.desktop'])]:
            with self.subTest(machine=machine):
                system = self.base / machine / 'usr/share/applications'
                flatpak = self.base / machine / 'var/lib/flatpak/exports/share/applications'
                for name in names:
                    self.entry(flatpak if name.startswith('com.') else system, name,
                               'Name=Visual Studio Code\nExec=/usr/share/code/code %F')
                for name, fields in {
                        'Code::Blocks.desktop': 'Name=Code::Blocks IDE\nExec=codeblocks',
                        'vscode-like-random.desktop': 'Name=Some Code Tool\nExec=other-code',
                        'code-native.desktop': 'Name=Visual Studio Code\nExec=code-native %F',
                        'code-native-url-handler.desktop': 'Name=Visual Studio Code\nExec=code-native --open-url %U',
                        'action-only.desktop': 'Name=Other Tool\nExec=other\n[Desktop Action code]\nName=Visual Studio Code\nExec=code',
                        }.items():
                    self.entry(system, name, fields)
                originals = {p: p.read_bytes() for root in (system, flatpak)
                             for p in root.glob('*.desktop')}
                state, _, execute = self.xdg_mock()
                with mock.patch.object(tool, 'system_application_dirs', return_value=[system, flatpak]), \
                     mock.patch.object(tool, 'run', side_effect=execute):
                    self.assertEqual(tool.system_code_desktop_ids(), sorted(names))
                    tool.desktop_integration(self.root)
                apps = self.home / '.local/share/applications'
                for name in names + ['com.brave.Browser.desktop', 'org.mozilla.firefox.desktop']:
                    self.assertEqual(tool.desktop_fields(apps / name).get('Hidden'), 'true')
                for name in ['Code::Blocks.desktop', 'vscode-like-random.desktop', 'action-only.desktop']:
                    self.assertFalse((apps / name).exists())
                for name in ['code-native.desktop', 'code-native-url-handler.desktop']:
                    self.assertNotEqual(tool.desktop_fields(apps / name).get('Hidden'), 'true')
                self.assertEqual(state['vscode'], 'code-native-url-handler.desktop')
                for path, payload in originals.items():
                    self.assertEqual(path.read_bytes(), payload)

    def test_flatpak_identity_nested_id_and_renamed_native_guard(self):
        system = self.base / 'system/applications'
        self.entry(system, 'vendor/alternate.desktop',
                   'Name=Localized editor\nX-Flatpak=com.visualstudio.code\nExec=flatpak run com.visualstudio.code')
        self.entry(system, 'renamed-native.desktop',
                   f'Name=Visual Studio Code\nExec="{self.home}/.local/bin/code-native" %F')
        self.entry(system, 'almost.desktop', 'Name=Visual Studio Code Extra\nExec=codeish')
        self.assertEqual(tool.system_code_desktop_ids(), ['vendor-alternate.desktop'])

    def test_existing_minimal_hides_preserved_and_repeated_setup_idempotent(self):
        self.icon_fixture()
        apps = self.home / '.local/share/applications'
        apps.mkdir(parents=True)
        for name in ['com.brave.Browser.desktop', 'org.mozilla.firefox.desktop', 'com.visualstudio.code.desktop']:
            (apps / name).write_text('[Desktop Entry]\nHidden=true\n')
        with mock.patch.object(tool, 'run'):
            tool.desktop_integration(self.root)
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in apps.iterdir()}
            tool.desktop_integration(self.root)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in apps.iterdir()})
        for name in ['com.brave.Browser.desktop', 'org.mozilla.firefox.desktop', 'com.visualstudio.code.desktop']:
            self.assertEqual((apps / name).read_text(), '[Desktop Entry]\nHidden=true\n')

    def test_unmanaged_launcher_and_symlink_overrides_refused(self):
        apps = self.home / '.local/share/applications'
        apps.mkdir(parents=True)
        system = self.base / 'system/applications'
        self.entry(system, 'code.desktop', 'Name=Visual Studio Code\nExec=code')
        path = apps / 'code.desktop'
        path.write_text('[Desktop Entry]\nName=Personal launcher\n')
        original = path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'unmanaged'):
            tool.hide_system_applications(apps)
        self.assertEqual(path.read_bytes(), original)
        path.unlink()
        external = self.base / 'external.desktop'
        external.write_bytes(original)
        path.symlink_to(external)
        with self.assertRaisesRegex(RuntimeError, 'Unexpected symlink'):
            tool.hide_system_applications(apps)
        self.assertEqual(external.read_bytes(), original)

    def test_optional_cache_failure_or_absence_does_not_block_callback(self):
        self.icon_fixture()
        for available in (False, True):
            state, _, execute = self.xdg_mock()
            def run(args, **kwargs):
                if args[0] in ('gtk-update-icon-cache', 'update-desktop-database'):
                    raise subprocess.CalledProcessError(1, args)
                return execute(args, **kwargs)
            with self.subTest(available=available), \
                 mock.patch.object(tool.shutil, 'which', return_value='/fixture/tool' if available else None), \
                 mock.patch.object(tool, 'run', side_effect=run):
                tool.desktop_integration(self.root)
            self.assertEqual(state['vscode'], 'code-native-url-handler.desktop')
            self.assertEqual(state['https'], 'other.desktop')

    def test_menu_diagnostics_detects_unmasked_then_fixed_entry(self):
        self.icon_fixture()
        system = self.base / 'system/applications'
        self.entry(system, 'code.desktop', 'Name=Visual Studio Code\nExec=code')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(tool.desktop_menu_diagnostics())
        self.assertIn('code.desktop: MISMATCH', output.getvalue())
        with mock.patch.object(tool, 'run'):
            tool.desktop_integration(self.root)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(tool.desktop_menu_diagnostics())
        self.assertIn('code.desktop: hidden', output.getvalue())
        self.assertIn('Native Code icon: present', output.getvalue())

    def test_prepare_reconstructs_desktop_on_fresh_post(self):
        source = self.icon_fixture()
        self.native_fixture('brave')
        self.native_fixture('code')
        # Persistent markers/tooling survived; fresh workstation payload is staged.
        tool.SELF.parent.mkdir(parents=True)
        tool.SELF.write_text('# Managed by thin-client-setup\n')
        state, _, execute = self.xdg_mock()
        def run(args, **kwargs):
            if str(args[0]).endswith('/bin/code'):
                return mock.Mock(stdout='')
            return execute(args, **kwargs)
        with self.fake_mount(), mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'install', side_effect=fixtures.staged_install), mock.patch.object(tool, 'activate_release'), mock.patch.object(tool, 'install_self'), \
             mock.patch.object(tool, 'stopped'), mock.patch.object(tool, 'run', side_effect=run):
            tool.main(['prepare'])
        icon = self.home / '.local/share/icons/hicolor/1x1/apps/code-native.png'
        self.assertEqual(icon.read_bytes(), source.read_bytes())
        fields = tool.desktop_fields(self.home / '.local/share/applications/code-native.desktop')
        self.assertEqual(fields['Icon'], str(icon))
        self.assertEqual(state['http'], 'other.desktop')
        self.assertEqual(state['vscode'], 'code-native-url-handler.desktop')

    def test_xdg_directories_are_standard_and_exclude_user_launchers(self):
        extra = self.base / 'xdg/share'
        with mock.patch.dict(os.environ, {'XDG_DATA_DIRS': f'{extra}:relative',
                                         'XDG_DATA_HOME': str(self.home / '.local/share')}):
            roots = application_dirs()
        self.assertIn(extra / 'applications', roots)
        self.assertIn(self.home / '.local/share/flatpak/exports/share/applications', roots)
        self.assertNotIn(self.home / '.local/share/applications', roots)
        self.assertTrue(all(p.is_absolute() for p in roots))
