"""Brave desktop icons and system masks without host icon-theme dependencies."""
import contextlib
import io
from pathlib import Path
import unittest
from unittest import mock

import test_desktop_menu as menu
import test_safety as fixtures

tool = fixtures.tool


class BraveDesktopTests(unittest.TestCase):
    setUp = fixtures.SafetyTests.setUp
    icon_fixture = fixtures.SafetyTests.icon_fixture
    native_fixture = fixtures.SafetyTests.native_fixture
    fake_mount = fixtures.SafetyTests.fake_mount
    entry = menu.DesktopMenuTests.entry
    xdg_mock = menu.DesktopMenuTests.xdg_mock

    def test_official_brave_icon_persistent_absolute_and_idempotent(self):
        code = self.icon_fixture()
        current = self.root / 'apps/brave/current'
        self.assertTrue(current.is_symlink())
        source = current.resolve() / 'product_logo_256.png'
        with mock.patch.object(tool, 'run'):
            tool.desktop_integration(self.root)
            icon = self.home / '.local/share/icons/hicolor/1x1/apps/brave-native.png'
            self.assertEqual(icon.read_bytes(), source.read_bytes())
            self.assertFalse(icon.is_symlink())
            self.assertTrue(icon.is_relative_to(self.home))
            self.assertEqual(icon.stat().st_mode & 0o777, 0o644)
            before = (icon.stat().st_ino, icon.stat().st_mtime_ns)
            tool.desktop_integration(self.root)
            self.assertEqual(before, (icon.stat().st_ino, icon.stat().st_mtime_ns))
            icon.unlink()
            tool.desktop_integration(self.root)
            self.assertEqual(icon.read_bytes(), source.read_bytes())
        apps = self.home / '.local/share/applications'
        self.assertEqual(tool.desktop_fields(apps / 'brave-native.desktop')['Icon'], str(icon))
        code_icon = self.home / '.local/share/icons/hicolor/1x1/apps/code-native.png'
        self.assertEqual(code_icon.read_bytes(), code.read_bytes())
        self.assertEqual(tool.desktop_fields(apps / 'code-native.desktop')['Icon'], str(code_icon))
        callback = tool.desktop_fields(apps / 'code-native-url-handler.desktop')
        self.assertEqual(callback['NoDisplay'], 'true')
        self.assertTrue(callback['Exec'].endswith('--open-url %U'))
        self.assertEqual(callback['MimeType'], 'x-scheme-handler/vscode;')

    def test_brave_icon_rejects_external_release(self):
        self.icon_fixture()
        current = self.root / 'apps/brave/current'
        current.unlink()
        current.symlink_to(self.base)
        with self.assertRaisesRegex(RuntimeError, 'outside managed releases'):
            tool.install_brave_icon(self.root)
        self.assertFalse((self.home / '.local/share/icons').exists())

    def test_brave_icon_rejects_symlink_source(self):
        self.icon_fixture()
        source = (self.root / 'apps/brave/current').resolve() / 'product_logo_256.png'
        external = self.base / 'external.png'
        source.rename(external)
        source.symlink_to(external)
        with self.assertRaisesRegex(RuntimeError, 'Unexpected symlink'):
            tool.install_brave_icon(self.root)
        self.assertTrue(external.is_file())

    def test_brave_icon_rejects_symlink_destination(self):
        self.icon_fixture()
        external = self.base / 'external.png'
        external.write_bytes(b'unrelated')
        destination = self.home / '.local/share/icons/hicolor/1x1/apps/brave-native.png'
        destination.parent.mkdir(parents=True)
        destination.symlink_to(external)
        with self.assertRaisesRegex(RuntimeError, 'Unexpected symlink'):
            tool.install_brave_icon(self.root)
        self.assertEqual(external.read_bytes(), b'unrelated')

    def test_missing_invalid_brave_icon_refuses_without_downloading(self):
        self.icon_fixture()
        source = (self.root / 'apps/brave/current').resolve() / 'product_logo_256.png'
        with mock.patch.object(tool, 'run') as run:
            source.unlink()
            with self.assertRaisesRegex(RuntimeError, 'Bundled Brave icon missing'):
                tool.install_brave_icon(self.root)
            source.write_bytes(b'not PNG')
            with self.assertRaisesRegex(RuntimeError, 'Invalid bundled Brave PNG'):
                tool.install_brave_icon(self.root)
            run.assert_not_called()

    def test_system_brave_ids_masked_across_images_without_hiding_other_browsers(self):
        self.icon_fixture()
        for image, ids in [('A', ['brave-browser.desktop']),
                           ('B', ['com.brave.Browser.desktop']),
                           ('C', ['brave-browser.desktop', 'com.brave.Browser.desktop'])]:
            with self.subTest(image=image):
                directory = self.base / image / 'applications'
                for name in ids:
                    self.entry(directory, name, 'Name=Brave Web Browser\nExec=/usr/bin/brave-browser-stable %U')
                for name, fields in {
                        'google-chrome.desktop': 'Name=Google Chrome\nExec=google-chrome %U',
                        'chromium.desktop': 'Name=Chromium\nExec=chromium %U',
                        'brave-tool.desktop': 'Name=Brave Helper\nExec=brave-helper',
                        'not-brave.desktop': 'Name=Brave\nExec=chromium %U',
                        'brave-native.desktop': 'Name=Brave\nExec=brave-native %U',
                        'renamed-native.desktop': 'Name=Brave Web Browser\nExec=brave-native %U',
                        }.items():
                    self.entry(directory, name, fields)
                originals = {p: p.read_bytes() for p in directory.iterdir()}
                with mock.patch.object(tool, 'system_application_dirs', return_value=[directory]), \
                     mock.patch.object(tool, 'run'):
                    self.assertEqual(tool.system_brave_desktop_ids(), sorted(ids))
                    tool.desktop_integration(self.root)
                apps = self.home / '.local/share/applications'
                for name in ids:
                    self.assertEqual(tool.desktop_fields(apps / name)['Hidden'], 'true')
                for name in ['google-chrome.desktop', 'chromium.desktop', 'brave-tool.desktop',
                             'not-brave.desktop', 'renamed-native.desktop']:
                    self.assertFalse((apps / name).exists())
                self.assertNotIn('Hidden', tool.desktop_fields(apps / 'brave-native.desktop'))
                self.assertEqual(tool.desktop_fields(apps / 'org.mozilla.firefox.desktop')['Hidden'], 'true')
                for p, content in originals.items():
                    self.assertEqual(p.read_bytes(), content)

    def test_flatpak_brave_identity_without_exact_name(self):
        directory = self.base / 'system/applications'
        self.entry(directory, 'vendor-brave.desktop',
                   'Name=Localized Browser\nX-Flatpak=com.brave.Browser\nExec=flatpak run com.brave.Browser')
        self.assertEqual(tool.system_brave_desktop_ids(), ['vendor-brave.desktop'])

    def test_diagnostics_reports_missing_referenced_brave_icon(self):
        self.icon_fixture()
        with mock.patch.object(tool, 'run'):
            tool.desktop_integration(self.root)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertTrue(tool.desktop_menu_diagnostics())
        self.assertIn('Native Brave icon: present', output.getvalue())
        icon = self.home / '.local/share/icons/hicolor/1x1/apps/brave-native.png'
        icon.unlink()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(tool.desktop_menu_diagnostics())
        self.assertIn('Native Brave icon: MISMATCH', output.getvalue())
        self.assertIn('Native Code icon: present', output.getvalue())

    def test_fresh_post_prepare_restores_icons_and_matches_desktop(self):
        self.icon_fixture()
        self.native_fixture('brave')
        self.native_fixture('code')
        state, _, execute = self.xdg_mock()
        def run(args, **kwargs):
            if str(args[0]).endswith('/bin/code'):
                return mock.Mock(stdout='')
            return execute(args, **kwargs)
        with self.fake_mount(), mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'install'), mock.patch.object(tool, 'install_self'), \
             mock.patch.object(tool, 'stopped'), mock.patch.object(tool, 'run', side_effect=run):
            tool.main(['desktop'])
            apps = self.home / '.local/share/applications'
            entries = {p.name: p.read_bytes() for p in apps.iterdir()}
            brave_icon = self.home / '.local/share/icons/hicolor/1x1/apps/brave-native.png'
            brave_icon.unlink()
            tool.main(['prepare'])
            self.assertTrue(brave_icon.is_file())
            self.assertEqual(entries, {p.name: p.read_bytes() for p in apps.iterdir()})
        source = (self.root / 'apps/brave/current').resolve() / 'product_logo_256.png'
        self.assertEqual(brave_icon.read_bytes(), source.read_bytes())
        self.assertEqual(state['vscode'], 'code-native-url-handler.desktop')
        self.assertEqual(state['http'], 'other.desktop')
        self.assertEqual(state['https'], 'other.desktop')
