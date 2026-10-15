"""Release checks: temporary accounts only; no live installers or desktop actions."""
import contextlib
import io
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock

import test_safety as fixtures


tool = fixtures.tool


class ReleaseTests(unittest.TestCase):
    setUp = fixtures.SafetyTests.setUp
    fake_mount = fixtures.SafetyTests.fake_mount
    native_fixture = fixtures.SafetyTests.native_fixture
    desktop_icon_fixtures = fixtures.SafetyTests.desktop_icon_fixtures
    spotify_fixture = fixtures.SafetyTests.spotify_fixture
    spotify_download = fixtures.SafetyTests.spotify_download
    fake_theme_install = fixtures.SafetyTests.fake_theme_install

    def xdg_mock(self, *, ignore_https=False):
        state = {'browser': 'other.desktop', 'http': 'other.desktop',
                 'https': 'other.desktop', 'vscode': 'flatpak-code.desktop',
                 'spotify': 'other-spotify.desktop'}
        calls = []
        def execute(args, **kwargs):
            argv = [str(arg) for arg in args]
            calls.append(argv)
            self.assertEqual(kwargs['env']['XDG_CONFIG_HOME'], str(self.home / '.config'))
            self.assertEqual(kwargs['env']['XDG_DATA_HOME'], str(self.home / '.local/share'))
            if argv[:3] == ['xdg-settings', 'get', 'default-web-browser']:
                return mock.Mock(stdout=state['browser'] + '\n')
            if argv[:3] == ['xdg-settings', 'set', 'default-web-browser']:
                state['browser'] = argv[3]
            elif argv[:3] == ['xdg-mime', 'query', 'default']:
                return mock.Mock(stdout=state[argv[3].split('/')[-1]] + '\n')
            elif argv[:2] == ['xdg-mime', 'default']:
                scheme = argv[3].split('/')[-1]
                if not (ignore_https and scheme == 'https'):
                    state[scheme] = argv[2]
            elif argv[0] != 'update-desktop-database':
                self.fail(f'Unexpected external command: {argv}')
            return mock.Mock(stdout='')
        return state, calls, execute

    def test_explicit_defaults_register_all_routes_idempotently(self):
        state, calls, execute = self.xdg_mock()
        self.desktop_icon_fixtures()
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'install_self'), mock.patch.object(tool, 'run', side_effect=execute):
            tool.main(['defaults', 'brave'])
            entries = self.home / '.local/share/applications'
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in entries.iterdir()}
            tool.main(['defaults', 'brave'])
            self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                                     for p in entries.iterdir()})
        self.assertEqual(state, {'browser': 'brave-native.desktop', 'http': 'brave-native.desktop',
                                'https': 'brave-native.desktop',
                                'vscode': 'code-native-url-handler.desktop',
                                'spotify': 'spotify-thin.desktop'})
        callback = (entries / 'code-native-url-handler.desktop').read_text()
        self.assertIn('--open-url %U', callback)
        self.assertIn('MimeType=x-scheme-handler/vscode;', callback)
        self.assertNotIn('x-scheme-handler/http', callback)
        self.assertNotIn('x-scheme-handler/vscode', (entries / 'code-native.desktop').read_text())
        self.assertIn(['xdg-settings', 'get', 'default-web-browser'], calls)

    def test_desktop_does_not_take_over_browser_defaults(self):
        state, _, execute = self.xdg_mock()
        self.desktop_icon_fixtures()
        with mock.patch.object(tool, 'run', side_effect=execute):
            tool.desktop_integration(self.root)
        self.assertEqual(state['browser'], 'other.desktop')
        self.assertEqual(state['http'], 'other.desktop')
        self.assertEqual(state['https'], 'other.desktop')
        self.assertEqual(state['vscode'], 'code-native-url-handler.desktop')
        self.assertEqual(state['spotify'], 'spotify-thin.desktop')

    def test_spotify_uses_official_snap_with_isolated_persistent_profile(self):
        executable = self.spotify_fixture()
        tool.migrate_spotify()
        with mock.patch.object(tool.os, 'execve') as execute, \
             mock.patch.object(tool, 'start_spotify_browser_bridge',
                               return_value=Path('/run/user/fixture/open.fifo')):
            tool.open_app(self.root, 'spotify',
                          ['spotify:track:4uLU6hMCjMI75M1A2tKUQC'])
        path, argv, env = execute.call_args.args
        self.assertEqual(path, tool.BWRAP)
        self.assertEqual(argv[0], str(tool.BWRAP))
        self.assertIn('/snap/spotify/current/usr/share/spotify/spotify', argv)
        self.assertIn('spotify:track:4uLU6hMCjMI75M1A2tKUQC', argv)
        self.assertNotIn(str(tool.BRAVE), argv)
        self.assertEqual(env['HOME'], str(tool.SPOTIFY))
        self.assertEqual(env['SNAP_NAME'], 'spotify')
        self.assertEqual(env['XDG_CACHE_HOME'], str(tool.SPOTIFY / '.cache'))
        self.assertIn(str(self.root / 'cache/spotify'), argv)
        self.assertTrue((tool.SPOTIFY / '.thin-client-import').is_file())
        self.assertTrue((self.root / 'cache/spotify').is_dir())
        self.assertTrue(executable.is_file())

    def test_spotify_prepare_is_idempotent_and_registers_only_its_scheme(self):
        calls = []
        def execute(args, **kwargs):
            argv = [str(value) for value in args]
            if argv[0] in ('snap', 'unsquashfs'):
                return self.spotify_download(args, **kwargs)
            calls.append(argv)
            return mock.Mock(stdout='')
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'install_self'), \
             mock.patch.object(tool, 'run', side_effect=execute), \
             mock.patch.object(tool.shutil, 'which', side_effect=lambda name:
                               f'/usr/bin/{name}' if name in ('snap', 'unsquashfs') else None):
            tool.main(['spotify', 'prepare'])
            before = {(path.relative_to(self.home), path.read_bytes())
                      for path in self.home.rglob('*') if path.is_file()}
            tool.main(['spotify', 'prepare'])
            after = {(path.relative_to(self.home), path.read_bytes())
                     for path in self.home.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        entry = self.home / '.local/share/applications/spotify-thin.desktop'
        self.assertIn('MimeType=x-scheme-handler/spotify;', entry.read_text())
        self.assertNotIn('x-scheme-handler/http', entry.read_text())
        self.assertIn(['xdg-mime', 'default', 'spotify-thin.desktop',
                       'x-scheme-handler/spotify'], calls)
        self.assertTrue((self.root / 'apps/spotify/current').is_symlink())

    def test_defaults_refuse_to_report_success_when_xdg_ignores_update(self):
        _, _, execute = self.xdg_mock(ignore_https=True)
        self.desktop_icon_fixtures()
        with mock.patch.object(tool, 'run', side_effect=execute):
            tool.desktop_integration(self.root)
            with self.assertRaisesRegex(RuntimeError, 'did not match'):
                tool.make_default_browser()

    def test_diagnostics_are_read_only_and_report_mismatches(self):
        state, calls, execute = self.xdg_mock()
        expected = dict(state)
        output = io.StringIO()
        with mock.patch.object(tool, 'run', side_effect=execute), contextlib.redirect_stdout(output):
            self.assertFalse(tool.desktop_diagnostics())
        self.assertEqual(state, expected)
        self.assertEqual(len(calls), 5)
        self.assertEqual(output.getvalue().count('MISMATCH'), 5)
        self.assertIn('MISSING', output.getvalue())
        self.assertFalse((self.home / '.config').exists())

    def test_diagnostics_report_query_failures(self):
        with mock.patch.object(tool, 'run', side_effect=FileNotFoundError('fixture')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertFalse(tool.desktop_diagnostics())
        self.assertIn('unavailable (query failed)', output.getvalue())

    def test_code_callback_args_and_browser_environment_preserved_correctly(self):
        exe = self.native_fixture('code')
        callback = 'vscode://vscode.github-authentication/did-authenticate?code=fixture&state=fixture'
        with mock.patch.dict(os.environ, {'BROWSER': '/stale/browser'}), \
             mock.patch.object(tool.os, 'execve') as execute:
            tool.open_app(self.root, 'code', ['--open-url', callback])
            path, argv, env = execute.call_args.args
            self.assertEqual(path, exe)
            self.assertEqual(argv[-2:], ['--open-url', callback])
            self.assertIn(str(tool.CODE), argv)
            self.assertNotIn('BROWSER', env)
            self.assertEqual(os.environ['BROWSER'], '/stale/browser')
            brave_env = tool.environment(self.root, 'brave')
            self.assertEqual(brave_env['BROWSER'], '/stale/browser')

    def test_readonly_post_switches_code_caches_to_private_runtime(self):
        exe = self.native_fixture('code')
        tool.vscode_caches(self.root)
        transient = self.base / 'post-runtime'
        with mock.patch.object(tool, 'runtime_storage', return_value=transient), \
             mock.patch.object(tool, 'transient_runtime_path', return_value=transient), \
             mock.patch.object(tool.os, 'execve') as execute:
            tool.open_app(self.root, 'code', [])
        path, _, env = execute.call_args.args
        self.assertEqual(path, exe)
        self.assertEqual(env['TMPDIR'], str(transient / 'tmp'))
        self.assertEqual((tool.CODE / 'GPUCache').resolve(),
                         transient / 'cache/vscode/GPUCache')
        self.assertEqual((tool.CODE / 'Cache').resolve(), transient / 'cache/vscode/Cache')

    def test_code_extensions_are_persistent_across_workstation_goinfre(self):
        argv = [str(value) for value in tool.code_args(self.root)]
        self.assertEqual(argv[argv.index('--extensions-dir') + 1],
                         str(tool.STATE / 'vscode-extensions'))
        self.assertNotIn(str(self.root / 'vscode-extensions'), argv)

    def test_theme_command_accepts_readonly_goinfre(self):
        writable_requirements = []
        def locate(require_writable=True):
            writable_requirements.append(require_writable)
            return self.root
        with mock.patch.object(tool, 'goinfre', side_effect=locate), \
             mock.patch.object(tool, 'install_self'), \
             mock.patch.object(tool, 'install_vscode_theme') as install:
            tool.main(['theme'])
        self.assertEqual(writable_requirements, [False])
        install.assert_called_once_with(self.root)

    def test_readonly_post_switches_only_existing_legacy_brave_cache_links(self):
        self.native_fixture('brave')
        profile = tool.BRAVE / 'Default'
        profile.mkdir()
        legacy_link = profile / 'GraphiteDawnCache'
        legacy_target = self.root / 'cache/brave-profile/Default/GraphiteDawnCache'
        legacy_target.mkdir(parents=True)
        legacy_link.symlink_to(legacy_target)
        real_directory = profile / 'GPUPersistentCache'
        real_directory.mkdir()
        (real_directory / 'keep').write_text('persistent fixture')
        transient = self.base / 'post-runtime'
        with mock.patch.object(tool, 'runtime_storage', return_value=transient), \
             mock.patch.object(tool, 'transient_runtime_path', return_value=transient), \
             mock.patch.object(tool.os, 'execve'):
            tool.open_app(self.root, 'brave', [])
        self.assertEqual(legacy_link.resolve(),
                         transient / 'cache/brave-profile/Default/GraphiteDawnCache')
        self.assertFalse(real_directory.is_symlink())
        self.assertEqual((real_directory / 'keep').read_text(), 'persistent fixture')

    def test_gpu_flags_are_added_only_with_usable_render_node(self):
        exe = self.native_fixture('brave')
        with mock.patch.object(tool, 'gpu_render_available', return_value=True), \
             mock.patch.object(tool.os, 'execve') as execute:
            tool.open_app(self.root, 'brave', [])
        path, argv, _ = execute.call_args.args
        self.assertEqual(path, exe)
        self.assertIn('--ignore-gpu-blocklist', argv)
        self.assertIn('--enable-gpu-rasterization', argv)
        self.assertIn('--enable-zero-copy', argv)

    def test_friend_brave_launch_relocates_cache_before_exec(self):
        exe = self.native_fixture('brave')
        path = tool.BRAVE / 'Default/DawnGraphiteCache'
        path.mkdir(parents=True)
        (path / 'generated').write_text('friend cache fixture')
        with self.fake_mount(), mock.patch.object(tool, 'stopped') as stopped, \
             mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool.os, 'execve') as execute:
            tool.main(['open', 'brave'])
            stopped.assert_called_once_with(('brave',))
            execute.assert_called_once()
            self.assertEqual(execute.call_args.args[0], exe)
        self.assertEqual(path.resolve(), self.root / 'cache/brave-profile/Default/DawnGraphiteCache')
        backups = list((self.root / 'cache-backups').glob('*/DawnGraphiteCache/generated'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), 'friend cache fixture')
        self.assertFalse((tool.STATE / 'cache-backups').exists())

    def test_profile_number_cache_relocation_and_launch_idempotent(self):
        self.native_fixture('brave')
        for profile in ['Profile 1', 'Profile 12']:
            for name in ['DawnGraphiteCache', 'DawnWebGPUCache']:
                path = tool.BRAVE / profile / name
                path.mkdir(parents=True)
                (path / 'generated').write_text(f'{profile}/{name}')
        with self.fake_mount(), mock.patch.object(tool, 'stopped'), mock.patch.object(tool.os, 'execve'):
            tool.open_app(self.root, 'brave', [])
            tool.open_app(self.root, 'brave', [])
        self.assertEqual({p.read_text() for p in (self.root / 'cache-backups').glob('*/*/generated')},
                         {f'{profile}/{name}' for profile in ['Profile 1', 'Profile 12']
                          for name in ['DawnGraphiteCache', 'DawnWebGPUCache']})

    def test_prepare_relocates_real_dawn_webgpu_cache_idempotently(self):
        self.native_fixture('brave')
        self.native_fixture('code')
        path = tool.BRAVE / 'Default/DawnWebGPUCache'
        path.mkdir(parents=True)
        (path / 'some-test-file').write_bytes(b'production reproduction fixture')
        with self.fake_mount(), mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'stopped') as stopped, mock.patch.object(tool, 'install_self'), \
             mock.patch.object(tool, 'install'), mock.patch.object(tool, 'run'), \
             mock.patch.object(tool, 'install_vscode_theme', side_effect=self.fake_theme_install), \
             mock.patch.object(tool, 'desktop_integration'), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            tool.main(['prepare'])
            self.assertIn(mock.call(('brave',)), stopped.call_args_list)
            self.assertTrue(path.is_symlink())
            self.assertEqual(path.resolve(), self.root / 'cache/brave-profile/Default/DawnWebGPUCache')
            backups = list((self.root / 'cache-backups').glob('*/DawnWebGPUCache/some-test-file'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b'production reproduction fixture')
            self.assertTrue(backups[0].is_relative_to(self.root))
            self.assertFalse(backups[0].is_relative_to(self.home))
            self.assertFalse(any(p.name == 'cache-backups' for p in self.home.rglob('*')))
            timestamp = path.lstat().st_mtime_ns
            tool.main(['prepare'])
            self.assertEqual(timestamp, path.lstat().st_mtime_ns)
            self.assertEqual(backups, list((self.root / 'cache-backups').glob('*/DawnWebGPUCache/some-test-file')))
            with mock.patch.object(tool.os, 'execve') as execute:
                tool.open_app(self.root, 'brave', [])
                execute.assert_called_once()
        self.assertIn('Relocating stopped Brave cache:', output.getvalue())
        self.assertIn(str(backups[0].parent), output.getvalue())

    def test_unknown_and_unknown_dawn_cache_names_refused(self):
        for name in ['Unknown', 'DawnFutureCache', 'DawnWebGPUCacheExtra']:
            path = tool.BRAVE / 'Default' / name
            path.mkdir(parents=True)
            (path / 'important').write_text('keep')
            with self.subTest(name=name), mock.patch.object(tool, 'stopped') as stopped:
                with self.assertRaisesRegex(RuntimeError, 'left intact'):
                    tool.brave_cache_link(path, self.root / 'cache/test', self.root)
                stopped.assert_not_called()
            self.assertFalse(path.is_symlink())
            self.assertEqual((path / 'important').read_text(), 'keep')
        self.assertFalse((self.root / 'cache-backups').exists())

    def browser_site_state_fixture(self, profile_root):
        persistent = {
            'Default/Service Worker/Database/persistent-db': b'registrations\x00\xff',
            'Default/Service Worker/ScriptCache/service-worker-script': b'installed worker script',
            'Default/Service Worker/CacheStorage/site-cache-data': b'offline site data',
            'Default/Some Application Data/Cache/important': b'application state',
            'Default/Foo/ScriptCache/important': b'user script',
            'Default/Whatever/GPUCache/important': b'user data with cache-like basename',
            'Default/IndexedDB/site/Cache/important': b'indexed database',
            'Default/Local Storage/important': b'local storage',
            'Default/Session Storage/important': b'session storage',
            'Default/WebStorage/important': b'web storage',
            'Profile 12/Service Worker/ScriptCache/important': b'numbered profile script',
        }
        for relative, contents in persistent.items():
            path = profile_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        for name in ['DawnWebGPUCache', 'GPUCache']:
            path = profile_root / 'Default' / name
            path.mkdir(parents=True)
            (path / 'gpu-file').write_bytes(name.encode())
        return persistent

    def test_prepare_and_open_ignore_persistent_site_storage_idempotently(self):
        self.native_fixture('brave')
        self.native_fixture('code')
        persistent = self.browser_site_state_fixture(tool.BRAVE)
        persistent_dirs = {tool.BRAVE / relative for relative in
                           ['Default/Service Worker', 'Default/Some Application Data',
                            'Default/Foo', 'Default/Whatever', 'Default/IndexedDB',
                            'Default/Local Storage', 'Default/Session Storage', 'Default/WebStorage',
                            'Profile 12/Service Worker']}
        before = {relative: (tool.BRAVE / relative).lstat() for relative in persistent}
        original_stat = Path.stat
        def guard(path, *args, **kwargs):
            if any(path == directory or directory in path.parents for directory in persistent_dirs):
                self.fail(f'Cache setup inspected persistent site state: {path}')
            return original_stat(path, *args, **kwargs)
        with self.fake_mount(), mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'install_self'), mock.patch.object(tool, 'install'), \
             mock.patch.object(tool, 'stopped'), mock.patch.object(tool, 'run'), \
             mock.patch.object(tool, 'install_vscode_theme', side_effect=self.fake_theme_install), \
             mock.patch.object(tool, 'desktop_integration'), mock.patch.object(tool.os, 'execve') as execute:
            original_stat = Path.stat  # retain the fixture's separate-filesystem simulation
            with mock.patch.object(Path, 'stat', guard):
                tool.main(['prepare'])
                tool.main(['prepare'])
                tool.open_app(self.root, 'brave', [])
                execute.assert_called_once()
        for relative, contents in persistent.items():
            path = tool.BRAVE / relative
            self.assertEqual(path.read_bytes(), contents)
            self.assertFalse(path.parent.is_symlink())
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.lstat().st_ino, before[relative].st_ino)
            self.assertEqual(path.lstat().st_mtime_ns, before[relative].st_mtime_ns)
        backups = list((self.root / 'cache-backups').glob('*/*'))
        self.assertEqual({path.name for path in backups}, {'DawnWebGPUCache', 'GPUCache'})
        self.assertEqual(len(backups), 2)
        for name in ['DawnWebGPUCache', 'GPUCache']:
            path = tool.BRAVE / 'Default' / name
            self.assertTrue(path.is_symlink())
            self.assertEqual(path.resolve(), self.root / 'cache/brave-profile/Default' / name)
            backup = next(path for path in backups if path.name == name)
            self.assertEqual((backup / 'gpu-file').read_bytes(), name.encode())
        self.assertFalse((tool.STATE / 'cache-backups').exists())

    def test_unapproved_nested_paths_refused_before_cache_validation(self):
        for relative in ['Default/Service Worker/ScriptCache', 'Default/Service Worker/CacheStorage',
                         'Default/Foo/ScriptCache', 'Default/Whatever/GPUCache',
                         'Default/Some Application Data/Cache']:
            with self.subTest(relative=relative), \
                 mock.patch.object(tool, 'safe', side_effect=AssertionError('must not validate as cache')):
                with self.assertRaisesRegex(RuntimeError, 'Unapproved Brave cache path'):
                    tool.brave_cache_link(tool.BRAVE / relative, self.root / 'cache/test', self.root)
        self.assertFalse((self.root / 'cache').exists())

    def test_root_rendering_caches_use_only_their_exact_root_locations(self):
        self.native_fixture('brave')
        for name in ['GraphiteDawnCache', 'GPUPersistentCache']:
            path = tool.BRAVE / name
            path.mkdir()
            (path / 'gpu-file').write_bytes(name.encode())
            nested = tool.BRAVE / 'Default/Whatever' / name
            nested.mkdir(parents=True)
            (nested / 'important').write_bytes(b'keep nested state')
        with self.fake_mount(), mock.patch.object(tool, 'stopped'), mock.patch.object(tool.os, 'execve'):
            tool.open_app(self.root, 'brave', [])
        for name in ['GraphiteDawnCache', 'GPUPersistentCache']:
            self.assertTrue((tool.BRAVE / name).is_symlink())
            self.assertEqual((tool.BRAVE / 'Default/Whatever' / name / 'important').read_bytes(),
                             b'keep nested state')
            backup = next((self.root / 'cache-backups').glob(f'*/{name}/gpu-file'))
            self.assertEqual(backup.read_bytes(), name.encode())

    def test_existing_service_worker_symlink_is_not_validated_or_rewritten(self):
        self.native_fixture('brave')
        profile = tool.BRAVE / 'Default'
        profile.mkdir()
        state = profile / 'Service Worker'
        state.symlink_to(self.base / 'missing-site-state')
        with mock.patch.object(tool.os, 'execve') as execute:
            tool.open_app(self.root, 'brave', [])
            execute.assert_called_once()
        self.assertEqual(os.readlink(state), str(self.base / 'missing-site-state'))
        self.assertFalse((self.base / 'missing-site-state').exists())
        self.assertFalse((self.root / 'cache-backups').exists())

    @unittest.skipUnless(shutil.which('rsync'), 'rsync is required for profile copy integration')
    def test_real_brave_migration_preserves_site_state_and_nested_cache_names(self):
        tool.OLD_BRAVE.mkdir(parents=True)
        (tool.OLD_BRAVE / 'Local State').write_text('{}')
        persistent = self.browser_site_state_fixture(tool.OLD_BRAVE)
        # A file at an approved directory location is unexpected data, not a cache tree.
        (tool.OLD_BRAVE / 'Cache').write_bytes(b'keep unexpected file')
        with mock.patch.dict(os.environ, {'LC_ALL': 'C'}), \
             mock.patch.object(tool.shutil, 'disk_usage', return_value=mock.Mock(free=10 * 1024**3)):
            tool.migrate_brave(tool.BRAVE.with_name('Brave-Browser.thin-client-partial'))
        for relative, contents in persistent.items():
            self.assertEqual((tool.BRAVE / relative).read_bytes(), contents)
            self.assertEqual((tool.OLD_BRAVE / relative).read_bytes(), contents)
        self.assertEqual((tool.BRAVE / 'Cache').read_bytes(), b'keep unexpected file')
        for name in ['DawnWebGPUCache', 'GPUCache']:
            self.assertFalse((tool.BRAVE / 'Default' / name).exists())
            self.assertTrue((tool.OLD_BRAVE / 'Default' / name / 'gpu-file').is_file())

    @unittest.skipUnless(shutil.which('rsync'), 'rsync is required for profile backup integration')
    def test_real_brave_retirement_backup_preserves_site_state(self):
        tool.OLD_BRAVE.mkdir(parents=True)
        (tool.OLD_BRAVE / 'Local State').write_text('{}')
        persistent = self.browser_site_state_fixture(tool.OLD_BRAVE)
        with mock.patch.dict(os.environ, {'LC_ALL': 'C'}):
            backup = tool.backup_flatpak('brave', tool.flatpak_path('brave'), self.root)
        for relative, contents in persistent.items():
            self.assertEqual((backup / 'persistent-data' / relative).read_bytes(), contents)
            self.assertEqual((tool.OLD_BRAVE / relative).read_bytes(), contents)
        for name in ['DawnWebGPUCache', 'GPUCache']:
            self.assertFalse((backup / 'persistent-data/Default' / name).exists())

    def test_running_brave_blocks_open_without_losing_contents(self):
        self.native_fixture('brave')
        path = tool.BRAVE / 'Default/DawnGraphiteCache'
        path.mkdir(parents=True)
        (path / 'generated').write_text('keep')
        with self.fake_mount(), mock.patch.object(tool, 'stopped', side_effect=RuntimeError('Quit Brave')), \
             mock.patch.object(tool.os, 'execve') as execute:
            with self.assertRaisesRegex(RuntimeError, 'Quit Brave'):
                tool.open_app(self.root, 'brave', [])
            execute.assert_not_called()
        self.assertEqual((path / 'generated').read_text(), 'keep')
        self.assertFalse(path.is_symlink())

    def test_unmanaged_profile_directory_ignored_and_wrong_cache_link_refused(self):
        self.native_fixture('brave')
        unmanaged = tool.BRAVE / 'Default/CrashpadMetadata'
        unmanaged.mkdir(parents=True)
        (unmanaged / 'generated').write_text('keep')
        with mock.patch.object(tool.os, 'execve') as execute:
            tool.open_app(self.root, 'brave', [])
            execute.assert_called_once()
        self.assertEqual((unmanaged / 'generated').read_text(), 'keep')
        path = tool.BRAVE / 'Default/GPUCache'
        path.unlink()  # only the correctly generated fixture link
        path.symlink_to(self.base / 'unexpected')
        with mock.patch.object(tool.os, 'execve') as execute:
            with self.assertRaisesRegex(RuntimeError, 'Unexpected cache link'):
                tool.open_app(self.root, 'brave', [])
            execute.assert_not_called()
        self.assertEqual(os.readlink(path), str(self.base / 'unexpected'))

    def test_new_post_refuses_download_on_launch_and_prepare_restores_targets(self):
        with mock.patch.object(tool, 'stopped'), mock.patch.object(tool.os, 'execve'):
            self.native_fixture('brave')
            self.native_fixture('code')
            tool.open_app(self.root, 'brave', [])
            tool.open_app(self.root, 'code', [])
        shutil.rmtree(self.root)  # only disposable fixture storage
        with mock.patch.object(tool, 'install') as install:
            with self.assertRaisesRegex(RuntimeError, 'prepare explicitly'):
                tool.open_app(self.root, 'brave', [])
            install.assert_not_called()
        def restore_binaries(root):
            self.native_fixture('brave')
            self.native_fixture('code')
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'install_self'), mock.patch.object(tool, 'stopped'), \
             mock.patch.object(tool, 'install', side_effect=restore_binaries), \
             mock.patch.object(tool, 'install_vscode_theme', side_effect=self.fake_theme_install), \
             mock.patch.object(tool, 'run'), mock.patch.object(tool, 'desktop_integration'):
            tool.main(['prepare'])
        self.assertTrue((tool.BRAVE / 'GPUCache').is_dir())
        self.assertTrue((tool.CODE / 'GPUCache').is_dir())
        self.assertTrue(tool.vscode_extensions().is_dir())

    def test_mount_escape_in_profile_or_inside_cache_is_refused(self):
        path = tool.BRAVE / 'Default/DawnGraphiteCache'
        path.mkdir(parents=True)
        nested = path / 'nested'
        nested.mkdir()
        (nested / 'generated').write_text('keep')
        for mount in [path.parent, nested]:
            with self.subTest(mount=mount), self.fake_mount(), \
                 mock.patch.object(tool, 'stopped'), \
                 mock.patch.object(Path, 'is_mount', autospec=True, side_effect=lambda p: p == mount):
                with self.assertRaisesRegex(RuntimeError, 'Mount'):
                    tool.brave_cache_link(path, self.root / 'cache/test', self.root)
            self.assertEqual((nested / 'generated').read_text(), 'keep')

    def test_status_detects_new_post_without_mutation(self):
        before = sorted(self.base.rglob('*'))
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'script_status'), mock.patch.object(tool, 'desktop_diagnostics'), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            tool.main(['status'])
        self.assertIn('New/unprepared workstation detected', output.getvalue())
        self.assertIn('thin-client-setup prepare', output.getvalue())
        self.assertEqual(before, sorted(self.base.rglob('*')))

    def test_repo_vs_stale_installed_script_warning_then_exact_update(self):
        tool.SELF.parent.mkdir(parents=True)
        tool.SELF.write_text('#!/usr/bin/env python3\n# Managed by thin-client-setup\n# old fixture\n')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            tool.script_status()
        self.assertIn('UPDATE REQUIRED', output.getvalue())
        with mock.patch.object(tool, 'goinfre', side_effect=AssertionError('install-self needs no goinfre')):
            tool.main(['install-self'])
        self.assertEqual(tool.SELF.read_bytes(), fixtures.SCRIPT.read_bytes())
        self.assertEqual(tool.SELF.stat().st_mode & 0o777, 0o755)
        timestamp = tool.SELF.stat().st_mtime_ns
        tool.main(['install-self'])
        self.assertEqual(tool.SELF.stat().st_mtime_ns, timestamp)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            tool.script_status()
        self.assertNotIn('UPDATE REQUIRED', output.getvalue())
        self.assertIn('matches', output.getvalue())

    def test_install_self_dry_run_does_not_modify_files(self):
        before = sorted(self.base.rglob('*'))
        with mock.patch.object(tool, 'goinfre', side_effect=AssertionError('unexpected')):
            tool.main(['install-self', '--dry-run'])
        self.assertEqual(before, sorted(self.base.rglob('*')))

    def test_install_self_repairs_executable_mode_without_changing_bytes(self):
        tool.main(['install-self'])
        expected = tool.SELF.read_bytes()
        tool.SELF.chmod(0o600)
        tool.main(['install-self'])
        self.assertEqual(tool.SELF.read_bytes(), expected)
        self.assertEqual(tool.SELF.stat().st_mode & 0o777, 0o755)

    def test_install_self_symlink_to_checkout_refused(self):
        tool.SELF.parent.mkdir(parents=True)
        tool.SELF.symlink_to(fixtures.SCRIPT)
        with self.assertRaisesRegex(RuntimeError, 'Unexpected symlink'):
            tool.main(['install-self'])
        self.assertTrue(tool.SELF.is_symlink())

    def test_home_goinfre_link_selects_generic_validated_storage(self):
        (self.home / 'goinfre').symlink_to(self.root.parent)
        with self.fake_mount(), mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(tool.goinfre(), self.root)

    def test_actual_cli_install_self_updates_temporary_account_only(self):
        env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}
        tool.SELF.parent.mkdir(parents=True)
        tool.SELF.write_text('# Managed by thin-client-setup\n# old fixture\n')
        result = subprocess.run([sys.executable, '-B', str(fixtures.SCRIPT), 'install-self'],
                                env=env, capture_output=True, text=True, check=True)
        self.assertIn('Installed setup', result.stdout)
        self.assertEqual(tool.SELF.read_bytes(), fixtures.SCRIPT.read_bytes())
        result = subprocess.run([sys.executable, '-B', str(tool.SELF), '--version'],
                                env=env, capture_output=True, text=True, check=True)
        self.assertIn(tool.VERSION, result.stdout)
        self.assertFalse((self.home / '.config').exists())
        self.assertFalse((self.root / 'apps').exists())

    def test_upgraded_installed_copy_handles_friend_cache_scenario(self):
        tool.SELF.parent.mkdir(parents=True)
        tool.SELF.write_text('# Managed by thin-client-setup\n# old fixture\n')
        tool.main(['install-self'])
        with mock.patch.dict(os.environ, {'HOME': str(self.home)}):
            loader = importlib.machinery.SourceFileLoader('installed_fixture', str(tool.SELF))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            installed = importlib.util.module_from_spec(spec)
            loader.exec_module(installed)
        self.native_fixture('brave')
        path = installed.BRAVE / 'Default/DawnGraphiteCache'
        path.mkdir(parents=True)
        (path / 'generated').write_text('upgraded fixture')
        with self.fake_mount(), mock.patch.object(installed, 'goinfre', return_value=self.root), \
             mock.patch.object(installed, 'stopped'), mock.patch.object(installed.os, 'execve') as execute:
            installed.main(['open', 'brave'])
            execute.assert_called_once()
        self.assertTrue(path.is_symlink())
        backup = next((self.root / 'cache-backups').glob('*/DawnGraphiteCache/generated'))
        self.assertEqual(backup.read_text(), 'upgraded fixture')
        self.assertFalse((installed.STATE / 'cache-backups').exists())

    def test_generated_callback_wrapper_preserves_url_as_one_argument(self):
        self.desktop_icon_fixtures()
        with mock.patch.object(tool, 'run'):
            tool.desktop_integration(self.root)
        # A recording setup fixture intercepts the generated launcher's exec.
        tool.SELF.write_text('#!/usr/bin/python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n')
        tool.SELF.chmod(0o755)
        callback = 'vscode://fixture/callback?code=fixture&state=fixture'
        result = subprocess.run(['/bin/sh', str(self.home / '.local/bin/code-native'),
                                 '--open-url', callback],
                                env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin'},
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), ['open', 'code', '--open-url', callback])

    def test_unmanaged_installed_script_never_overwritten(self):
        tool.SELF.parent.mkdir(parents=True)
        tool.SELF.write_text('my script\n')
        with self.assertRaisesRegex(RuntimeError, 'unmanaged'):
            tool.main(['install-self'])
        self.assertEqual(tool.SELF.read_text(), 'my script\n')

    def test_codex_status_metadata_only_and_fixed_persistent_state_root(self):
        codex_home = self.home / '.codex'
        codex_home.mkdir()
        (codex_home / 'auth.json').write_text('PRIVATE_FIXTURE_DO_NOT_PRINT')
        (codex_home / 'config.toml').write_text('PRIVATE_CONFIG_FIXTURE')
        before = sorted(self.base.rglob('*'))
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'run', side_effect=AssertionError('must not execute Codex')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            tool.main(['codex', 'status'])
        self.assertIn(str(codex_home), output.getvalue())
        self.assertIn('auth.json: present', output.getvalue())
        self.assertNotIn('PRIVATE_', output.getvalue())
        self.assertEqual(before, sorted(self.base.rglob('*')))

    def test_individual_cli_commands_install_only_requested_cli(self):
        for name in ['codex', 'claude']:
            with self.subTest(name=name), mock.patch.object(tool, 'goinfre', return_value=self.root), \
                 mock.patch.object(tool, 'install_self'), \
                 mock.patch.object(tool, 'install_coding_clis') as install:
                tool.main([name, 'prepare'])
                install.assert_called_once_with(self.root, only=name)

    def test_cli_launchers_keep_state_in_home_and_temporary_data_in_goinfre(self):
        for name, relative in [('codex', 'apps/codex/bin/codex'),
                               ('claude', 'apps/claude/current/claude')]:
            with self.subTest(name=name):
                executable = self.root / relative
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_text('fixture')
                executable.chmod(0o755)
                if name == 'codex':
                    packages = self.root / 'apps/codex/installer-home/packages'
                    packages.mkdir(parents=True)
                    state = self.home / '.codex'
                    state.mkdir()
                    (state / 'packages').symlink_to(packages)
                with mock.patch.object(tool.os, 'execve') as execute:
                    tool.open_cli(self.root, name, ['--version'])
                path, argv, env = execute.call_args.args
                self.assertEqual(path, executable)
                self.assertEqual(argv[-1], '--version')
                self.assertEqual(env['TMPDIR'], str(self.root / 'tmp' / name))
                if name == 'codex':
                    self.assertIn('--no-daemon', argv)
                    self.assertEqual(env['CODEX_HOME'], str(self.home / '.codex'))
                else:
                    self.assertEqual(env['CLAUDE_CONFIG_DIR'], str(self.home / '.claude'))
                    self.assertEqual(env['DISABLE_UPDATES'], '1')

    def test_codex_official_home_launcher_is_safely_replaced_but_packages_remain(self):
        target = self.home / '.codex/packages/standalone/current/bin/codex'
        target.parent.mkdir(parents=True)
        target.write_text('old package')
        command = self.home / '.local/bin/codex'
        command.parent.mkdir(parents=True)
        command.symlink_to(target)
        tool.cli_wrapper('codex')
        self.assertTrue(target.exists())
        self.assertFalse(command.is_symlink())
        self.assertIn('open codex "$@"', command.read_text())

    def test_codex_uses_daemon_when_managed_daemon_package_exists(self):
        executable = self.root / 'apps/codex/bin/codex'
        executable.parent.mkdir(parents=True)
        executable.write_text('fixture')
        executable.chmod(0o755)
        packages = self.root / 'apps/codex/installer-home/packages'
        daemon = packages / 'app-server-daemon/current/bin/codex'
        daemon.parent.mkdir(parents=True)
        daemon.write_text('fixture')
        daemon.chmod(0o755)
        state = self.home / '.codex'
        state.mkdir()
        (state / 'packages').symlink_to(packages)
        with mock.patch.object(tool.os, 'execve') as execute:
            tool.open_cli(self.root, 'codex', ['resume'])
        self.assertEqual(execute.call_args.args[1], [str(executable), 'resume'])


if __name__ == '__main__':
    unittest.main()
