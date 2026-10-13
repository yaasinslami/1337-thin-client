import errno
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / 'thin-client-setup'
loader = importlib.machinery.SourceFileLoader('thin_client', str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
tool = importlib.util.module_from_spec(spec)
loader.exec_module(tool)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.home = self.base / 'home'
        self.root = self.base / 'goinfre/thin-client'
        self.home.mkdir()
        self.root.mkdir(parents=True)
        self.patch = mock.patch.multiple(tool, HOME=self.home,
            BRAVE=self.home / '.config/BraveSoftware/Brave-Browser',
            CODE=self.home / '.config/Code-Thin',
            OLD_BRAVE=self.home / '.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser',
            OLD_CODE=self.home / '.var/app/com.visualstudio.code/config/Code/User',
            STATE=self.home / '.local/share/thin-client',
            SELF=self.home / '.local/bin/thin-client-setup')
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def tar(self, members):
        path = self.base / 'input.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name, kind, value in members:
                info = tarfile.TarInfo(name)
                if kind == 'file':
                    data = value.encode()
                    info.size = len(data)
                    info.mode = 0o4755
                    archive.addfile(info, io.BytesIO(data))
                else:
                    info.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
                    info.linkname = value
                    archive.addfile(info)
        return path

    def extraction(self, archive, kind='tar'):
        dest = self.base / 'extract'
        dest.mkdir()
        tool.extract(archive, dest, kind)
        return dest

    def test_zip_traversal_rejected(self):
        path = self.base / 'input.zip'
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('../escape', 'x')
        with self.assertRaises(RuntimeError):
            self.extraction(path, 'zip')
        self.assertFalse((self.base / 'escape').exists())

    def test_tar_absolute_path_rejected(self):
        with self.assertRaises(RuntimeError):
            self.extraction(self.tar([('/escape', 'file', 'x')]))

    def test_tar_parent_traversal_rejected(self):
        with self.assertRaises(RuntimeError):
            self.extraction(self.tar([('../escape', 'file', 'x')]))

    def test_escaping_symlink_rejected(self):
        with self.assertRaises(RuntimeError):
            self.extraction(self.tar([('link', 'symlink', '../../escape')]))

    def test_escaping_hardlink_rejected(self):
        with self.assertRaises(RuntimeError):
            self.extraction(self.tar([('link', 'hardlink', '../escape')]))

    def test_symlink_ancestor_rejected(self):
        with self.assertRaises(RuntimeError):
            self.extraction(self.tar([('link', 'symlink', 'directory'), ('link/file', 'file', 'x')]))

    def test_link_chain_rejected(self):
        with self.assertRaises(RuntimeError):
            self.extraction(self.tar([('a', 'symlink', 'b'), ('b', 'symlink', 'file')]))

    def test_safe_internal_link_and_strip_setuid(self):
        dest = self.extraction(self.tar([('file', 'file', 'x'), ('link', 'symlink', 'file')]))
        self.assertEqual((dest / 'link').read_text(), 'x')
        self.assertFalse((dest / 'file').stat().st_mode & stat.S_ISUID)

    def test_duplicate_archive_entry_rejected(self):
        with self.assertRaises(RuntimeError):
            self.extraction(self.tar([('file', 'file', 'x'), ('file', 'file', 'y')]))

    def test_nonempty_extraction_destination_refused(self):
        dest = self.base / 'extract'
        dest.mkdir()
        (dest / 'important').write_text('keep')
        with self.assertRaises(RuntimeError):
            tool.extract(self.tar([('file', 'file', 'x')]), dest, 'tar')
        self.assertEqual((dest / 'important').read_text(), 'keep')

    def test_missing_goinfre(self):
        with mock.patch.dict(os.environ, {'GOINFRE': str(self.base / 'missing')}):
            with self.assertRaises(RuntimeError):
                tool.goinfre()

    def test_home_goinfre_refused(self):
        with self.assertRaises(RuntimeError):
            tool.validate_goinfre(self.home, self.home)

    def test_same_filesystem_refused(self):
        with self.assertRaises(RuntimeError):
            tool.validate_goinfre(self.root.parent, self.home)

    def fake_mount(self, uid=None):
        original = Path.stat
        root = self.root.parent
        def replacement(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == root:
                data = list(result)
                data[2] += 1
                if uid is not None:
                    data[4] = uid
                return os.stat_result(data)
            return result
        return mock.patch.object(Path, 'stat', replacement)

    def test_valid_goinfre_without_passwd_lookup(self):
        with self.fake_mount(), mock.patch.dict(os.environ, {'GOINFRE': str(self.root.parent)}):
            self.assertEqual(tool.goinfre(), self.root)

    def test_unowned_goinfre_refused(self):
        with self.fake_mount(uid=os.getuid() + 1):
            with self.assertRaises(RuntimeError):
                tool.validate_goinfre(self.root.parent, self.home)

    def test_unwritable_goinfre_refused(self):
        with self.fake_mount(), mock.patch.object(tool.os, 'access', return_value=False):
            with self.assertRaises(RuntimeError):
                tool.validate_goinfre(self.root.parent, self.home)

    def test_unmanaged_launcher_preserved(self):
        path = self.home / 'launcher'
        path.write_text('my important launcher')
        with self.assertRaises(RuntimeError):
            tool.managed_file(path, '# Managed by thin-client-setup\nreplacement', 0o755)
        self.assertEqual(path.read_text(), 'my important launcher')

    def test_managed_launcher_idempotent(self):
        path = self.home / 'launcher'
        text = '# Managed by thin-client-setup\nexample'
        tool.managed_file(path, text, 0o755)
        timestamp = path.stat().st_mtime_ns
        tool.managed_file(path, text, 0o755)
        self.assertEqual(path.stat().st_mtime_ns, timestamp)

    def test_cache_link_idempotency(self):
        path = self.home / 'cache'
        target = self.root / 'cache'
        tool.cache_link(path, target)
        tool.cache_link(path, target)
        self.assertEqual(path.resolve(), target)

    def test_cache_existing_data_preserved(self):
        path = self.home / 'cache'
        path.mkdir()
        (path / 'important').write_text('keep')
        with self.assertRaises(RuntimeError):
            tool.cache_link(path, self.root / 'cache')
        self.assertEqual((path / 'important').read_text(), 'keep')

    def native_fixture(self, app):
        profile = tool.BRAVE if app == 'brave' else tool.CODE
        profile.mkdir(parents=True, exist_ok=True)
        (profile / '.thin-client-import').write_text('prepared fixture')
        exe = self.root / ('apps/brave/current/brave' if app == 'brave'
                           else 'apps/vscode/current/bin/code')
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text('fixture')
        exe.chmod(0o755)
        return exe

    def test_open_code_friend_reproduction_never_inspects_brave(self):
        self.native_fixture('code')
        self.native_fixture('brave')
        cache = tool.BRAVE / 'Default/DawnGraphiteCache'
        cache.mkdir(parents=True)
        (cache / 'generated').write_text('keep')
        original_stat = Path.stat
        def guard(path, *args, **kwargs):
            if path == tool.BRAVE or tool.BRAVE in path.parents:
                self.fail(f'Code inspected Brave: {path}')
            return original_stat(path, *args, **kwargs)
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(Path, 'stat', guard), \
             mock.patch.object(tool, 'brave_caches', side_effect=AssertionError('Brave touched')), \
             mock.patch.object(tool.os, 'execve') as execute:
            tool.main(['open', 'code'])
            execute.assert_called_once()
        self.assertFalse(cache.is_symlink())
        self.assertEqual((cache / 'generated').read_text(), 'keep')
        self.assertTrue((self.root / 'vscode-extensions').is_dir())

    def test_open_brave_never_inspects_code(self):
        self.native_fixture('brave')
        tool.CODE.mkdir(parents=True)
        (tool.CODE / 'GPUCache').mkdir()
        # Even an invalid extension destination must not block Brave.
        (self.root / 'vscode-extensions').write_text('unexpected')
        original_stat = Path.stat
        def guard(path, *args, **kwargs):
            if path == tool.CODE or tool.CODE in path.parents:
                self.fail(f'Brave inspected Code: {path}')
            return original_stat(path, *args, **kwargs)
        with mock.patch.object(Path, 'stat', guard), \
             mock.patch.object(tool, 'vscode_caches', side_effect=AssertionError('Code touched')), \
             mock.patch.object(tool.os, 'execve') as execute:
            tool.open_app(self.root, 'brave', [])
            execute.assert_called_once()

    def test_code_launches_without_brave_profile_or_binary(self):
        self.native_fixture('code')
        with mock.patch.object(tool.os, 'execve') as execute:
            tool.open_app(self.root, 'code', [])
            execute.assert_called_once()
        self.assertFalse(tool.BRAVE.exists())
        self.assertFalse((self.root / 'apps/brave').exists())

    def test_brave_launches_without_code_profile_or_binary(self):
        self.native_fixture('brave')
        with mock.patch.object(tool.os, 'execve') as execute:
            tool.open_app(self.root, 'brave', [])
            execute.assert_called_once()
        self.assertFalse(tool.CODE.exists())
        self.assertFalse((self.root / 'apps/vscode').exists())
        self.assertFalse((self.root / 'vscode-extensions').exists())

    def test_known_brave_caches_preserved_then_linked(self):
        cache_names = {p.as_posix() for p in tool.BRAVE_PROFILE_DISPOSABLE_CACHE_PATHS}
        for name in sorted(cache_names):
            cache = tool.BRAVE / 'Default' / name
            cache.mkdir(parents=True)
            (cache / 'generated').write_text(name)
        with self.fake_mount(), mock.patch.object(tool, 'stopped') as stopped:
            tool.brave_caches(self.root)
            self.assertEqual(stopped.call_count, len(cache_names))
            stopped.assert_called_with(('brave',))
            tool.brave_caches(self.root)
            self.assertEqual(stopped.call_count, len(cache_names))
        # Every original cache payload is retained outside the browser profile.
        preserved = {p.read_text() for p in (self.root / 'cache-backups').glob('*/*/generated')}
        self.assertEqual(preserved, cache_names)
        for backup in (self.root / 'cache-backups').glob('*/*'):
            self.assertTrue(backup.is_relative_to(self.root))
            self.assertFalse(backup.is_relative_to(self.home))
        self.assertFalse((tool.STATE / 'cache-backups').exists())
        for name in cache_names:
            self.assertTrue((tool.BRAVE / 'Default' / name).is_symlink())

    def test_brave_backup_rejects_arbitrary_home_root(self):
        cache = tool.BRAVE / 'Default/GPUCache'
        cache.mkdir(parents=True)
        (cache / 'generated').write_text('keep')
        for root in [self.home / 'thin-client', self.home / 'arbitrary/thin-client']:
            root.mkdir(parents=True)
            with self.subTest(root=root), mock.patch.object(tool, 'stopped'):
                with self.assertRaises(RuntimeError):
                    tool.brave_cache_link(cache, self.root / 'cache/test', root)
            self.assertEqual((cache / 'generated').read_text(), 'keep')
            self.assertFalse((root / 'cache-backups').exists())

    def test_brave_backup_symlink_into_home_refused(self):
        cache = tool.BRAVE / 'Default/GPUCache'
        cache.mkdir(parents=True)
        (cache / 'generated').write_text('keep')
        destination = self.home / 'arbitrary-backups'
        destination.mkdir()
        (self.root / 'cache-backups').symlink_to(destination)
        with self.fake_mount(), mock.patch.object(tool, 'stopped'):
            with self.assertRaises(RuntimeError):
                tool.brave_cache_link(cache, self.root / 'cache/test', self.root)
        self.assertEqual((cache / 'generated').read_text(), 'keep')
        self.assertEqual(list(destination.iterdir()), [])

    def test_brave_backup_cross_filesystem_move_preserves_contents(self):
        cache = tool.BRAVE / 'Default/GPUCache'
        cache.mkdir(parents=True)
        (cache / 'generated').write_text('keep')
        (cache / 'nested-link').symlink_to('generated')
        with self.fake_mount(), mock.patch.object(tool, 'stopped'), \
             mock.patch.object(tool.os, 'rename', side_effect=OSError(errno.EXDEV, 'cross-device')):
            tool.brave_cache_link(cache, self.root / 'cache/test', self.root)
        backup = next((self.root / 'cache-backups').glob('*/GPUCache'))
        self.assertEqual((backup / 'generated').read_text(), 'keep')
        self.assertEqual(os.readlink(backup / 'nested-link'), 'generated')
        self.assertTrue(cache.is_symlink())

    def test_failed_brave_backup_copy_preserves_source(self):
        cache = tool.BRAVE / 'Default/GPUCache'
        cache.mkdir(parents=True)
        (cache / 'generated').write_text('keep')
        with self.fake_mount(), mock.patch.object(tool, 'stopped'), \
             mock.patch.object(tool.os, 'rename', side_effect=OSError(errno.EXDEV, 'cross-device')), \
             mock.patch.object(tool.shutil, 'copytree', side_effect=OSError('copy failed')):
            with self.assertRaises(OSError):
                tool.brave_cache_link(cache, self.root / 'cache/test', self.root)
        self.assertFalse(cache.is_symlink())
        self.assertEqual((cache / 'generated').read_text(), 'keep')

    def test_running_brave_cache_relocation_refused(self):
        cache = tool.BRAVE / 'Default/GPUCache'
        cache.mkdir(parents=True)
        (cache / 'generated').write_text('keep')
        with self.fake_mount(), mock.patch.object(tool, 'stopped', side_effect=RuntimeError('running')):
            with self.assertRaises(RuntimeError):
                tool.brave_caches(self.root)
        self.assertFalse(cache.is_symlink())
        self.assertEqual((cache / 'generated').read_text(), 'keep')

    def test_brave_unknown_or_persistent_paths_refused(self):
        for relative in ['Default/Unknown', 'Default/IndexedDB', 'Default/Local Storage',
                         'Default/Service Worker/CacheStorage', 'Default/Extensions/Cache',
                         'Profile custom/GPUCache']:
            with self.subTest(relative=relative):
                path = tool.BRAVE / relative
                path.mkdir(parents=True)
                (path / 'important').write_text('keep')
                with self.assertRaises(RuntimeError):
                    tool.brave_cache_link(path, self.root / 'cache/test', self.root)
                self.assertFalse(path.is_symlink())
                self.assertEqual((path / 'important').read_text(), 'keep')

    def test_brave_persistent_data_and_nested_cache_names_untouched(self):
        tool.BRAVE.mkdir(parents=True)
        persistent = ['Local State', 'Default/History', 'Default/Bookmarks',
                      'Default/Preferences', 'Default/Login Data',
                      'Default/IndexedDB/site/Cache/important',
                      'Default/Local Storage/important',
                      'Default/Extensions/id/Cache/important']
        for relative in persistent:
            path = tool.BRAVE / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('persistent fixture')
        with self.fake_mount(), mock.patch.object(tool, 'stopped') as stopped:
            tool.brave_caches(self.root)
            stopped.assert_not_called()
        for relative in persistent:
            self.assertEqual((tool.BRAVE / relative).read_text(), 'persistent fixture')
        self.assertFalse((tool.STATE / 'cache-backups').exists())

    def test_brave_cache_mount_refused(self):
        cache = tool.BRAVE / 'GPUCache'
        cache.mkdir(parents=True)
        with mock.patch.object(Path, 'is_mount', return_value=True):
            with self.assertRaises(RuntimeError):
                tool.brave_cache_link(cache, self.root / 'cache/test', self.root)
        self.assertFalse(cache.is_symlink())

    def test_brave_cache_file_refused(self):
        tool.BRAVE.mkdir(parents=True)
        path = tool.BRAVE / 'GPUCache'
        path.write_text('keep')
        with self.assertRaises(RuntimeError):
            tool.brave_cache_link(path, self.root / 'cache/test', self.root)
        self.assertEqual(path.read_text(), 'keep')

    def test_brave_symlink_ancestor_refused(self):
        tool.BRAVE.mkdir(parents=True)
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'GPUCache').mkdir()
        (tool.BRAVE / 'Default').symlink_to(outside)
        with self.assertRaises(RuntimeError):
            tool.brave_caches(self.root)
        self.assertFalse((outside / 'GPUCache').is_symlink())

    def test_unexpected_cache_symlink_refused(self):
        for broken in [False, True]:
            with self.subTest(broken=broken):
                path = self.home / ('broken' if broken else 'wrong')
                target = self.base / ('missing' if broken else 'outside')
                if not broken:
                    target.mkdir()
                path.symlink_to(target)
                with self.assertRaises(RuntimeError):
                    tool.cache_link(path, self.root / 'cache/test')
                self.assertEqual(os.readlink(path), str(target))

    def test_prepare_idempotent_and_recreates_lost_cache_targets(self):
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'install_self'), mock.patch.object(tool, 'install'), \
             mock.patch.object(tool, 'stopped'), mock.patch.object(tool, 'run'), \
             mock.patch.object(tool, 'desktop_integration'):
            tool.main(['prepare'])
            (tool.BRAVE / 'Default').mkdir()
            tool.main(['prepare'])
            link = tool.BRAVE / 'Default/DawnGraphiteCache'
            timestamp = link.lstat().st_mtime_ns
            tool.main(['prepare'])
            self.assertEqual(link.lstat().st_mtime_ns, timestamp)
            tool.shutil.rmtree(self.root / 'cache')
            tool.main(['prepare'])
            self.assertTrue(link.is_dir())
            self.assertTrue((tool.CODE / 'GPUCache').is_dir())

    def test_download_host_allowlist(self):
        self.assertEqual(tool.url_checked('https://github.com/brave/example'), 'https://github.com/brave/example')
        for url in ['http://github.com/a', 'https://github.com.evil.example/a',
                    'https://user:password@github.com/a', 'file:///tmp/a']:
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                tool.url_checked(url)

    def test_checksum_validation(self):
        path = self.base / 'payload'
        path.write_bytes(b'known')
        tool.verify_checksum(path, hashlib.sha256(b'known').hexdigest())
        with self.assertRaises(RuntimeError):
            tool.verify_checksum(path, '0' * 64)

    def test_failed_install_keeps_current_release(self):
        current = self.root / 'apps/brave/current'
        old = self.root / 'apps/brave/releases/old'
        old.mkdir(parents=True)
        current.symlink_to(old)
        work = self.root / 'work'
        work.mkdir()
        def fake_fetch(url, path):
            path.write_bytes(b'corrupt')
        with mock.patch.object(tool, 'fetch', side_effect=fake_fetch), \
             mock.patch.object(tool.shutil, 'disk_usage', return_value=mock.Mock(free=10 * 1024**3)):
            with self.assertRaises(RuntimeError):
                tool.install_app(self.root, 'brave', '1.2.3', 'https://github.com/brave/a', '0'*64, 'zip', work)
        self.assertEqual(current.resolve(), old)

    def test_arbitrary_flatpak_path_refused(self):
        for app in ['../firefox', 'org.mozilla.firefox', '/tmp', 'all']:
            with self.subTest(app=app), self.assertRaises(RuntimeError):
                tool.flatpak_path(app)

    def test_symlink_flatpak_path_refused(self):
        root = self.home / '.var/app'
        root.mkdir(parents=True)
        (root / 'com.brave.Browser').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            tool.flatpak_path('brave')

    def test_retirement_dry_run_is_read_only(self):
        path = tool.flatpak_path('brave')
        path.mkdir(parents=True)
        (path / 'important').write_text('keep')
        before = sorted(str(p.relative_to(self.base)) for p in self.base.rglob('*'))
        with mock.patch.object(tool, 'stopped') as stopped, \
             mock.patch.object(tool, 'backup_flatpak') as backup, \
             mock.patch.object(tool.shutil, 'rmtree') as remove:
            tool.retire_flatpak(self.root, 'brave', dry_run=True)
            stopped.assert_not_called()
            backup.assert_not_called()
            remove.assert_not_called()
        self.assertEqual(before, sorted(str(p.relative_to(self.base)) for p in self.base.rglob('*')))

    def test_retirement_requires_manual_verification(self):
        with self.assertRaises(RuntimeError), mock.patch.object(tool.shutil, 'rmtree') as remove:
            tool.retire_flatpak(self.root, 'brave', yes=True)
        remove.assert_not_called()

    def test_prepare_dry_run_creates_nothing(self):
        before = sorted(str(p.relative_to(self.base)) for p in self.base.rglob('*'))
        with mock.patch.object(tool, 'goinfre', return_value=self.root):
            tool.main(['prepare', '--dry-run'])
        self.assertEqual(before, sorted(str(p.relative_to(self.base)) for p in self.base.rglob('*')))

    def test_jsonc_comments_and_strings(self):
        value = tool.parse_jsonc('{/*comment*/"url":"https://example.test/a,}",\n"list":[1,],// note\n}')
        self.assertEqual(value, {'url':'https://example.test/a,}', 'list':[1]})

    def test_fresh_account_migration_idempotent(self):
        tool.STATE.mkdir(parents=True)
        with mock.patch.object(tool, 'stopped'):
            tool.migrate(self.root)
            tool.migrate(self.root)
        self.assertTrue((tool.BRAVE / '.thin-client-import').is_file())
        self.assertTrue((tool.CODE / '.thin-client-import').is_file())

    def test_small_code_jsonc_migration_preserves_source(self):
        tool.STATE.mkdir(parents=True)
        tool.OLD_CODE.mkdir(parents=True)
        original = '{// custom preference\n"terminal.integrated.profiles.linux": {"Old": {"path":"/usr/bin/flatpak-spawn"}}, "terminal.integrated.defaultProfile.linux":"Old", "editor.tabSize":4,}'
        source = tool.OLD_CODE / 'settings.json'
        source.write_text(original)
        tool.migrate_code()
        migrated = tool.parse_jsonc((tool.CODE / 'User/settings.json').read_text())
        self.assertEqual(migrated, {'editor.tabSize':4})
        self.assertEqual(source.read_text(), original)
        self.assertEqual((tool.STATE / 'flatpak-vscode-settings.original.jsonc').read_text(), original)

    def test_normal_and_url_desktop_entries_separate(self):
        (self.home / '.local/bin').mkdir(parents=True)
        with mock.patch.object(tool, 'run'):
            tool.desktop_integration()
        applications = self.home / '.local/share/applications'
        normal = (applications / 'code-native.desktop').read_text()
        handler = (applications / 'code-native-url-handler.desktop').read_text()
        self.assertIn(' %F', normal)
        self.assertNotIn('--open-url', normal)
        self.assertIn('--open-url %U', handler)
        self.assertIn('MimeType=x-scheme-handler/vscode;', handler)
        self.assertIn(str(self.home / '.local/bin/code-native'), handler)
        self.assertNotIn('x-scheme-handler/http', handler)

    def test_retirement_removes_only_selected_fixture(self):
        selected = tool.flatpak_path('vscode')
        selected.mkdir(parents=True)
        (selected / 'obsolete').write_text('fixture')
        other = self.home / '.var/app/org.mozilla.firefox'
        other.mkdir()
        (other / 'keep').write_text('important fixture')
        tool.CODE.mkdir(parents=True)
        (tool.CODE / '.thin-client-import').write_text('fixture')
        exe = self.root / 'apps/vscode/current/bin/code'
        exe.parent.mkdir(parents=True)
        exe.write_text('fixture')
        exe.chmod(0o755)
        with mock.patch.object(tool, 'stopped'), mock.patch.object(tool, 'backup_flatpak'), \
             mock.patch.object(tool, 'allocated', return_value=8192), \
             mock.patch.object(tool, 'run', return_value=mock.Mock(stdout='code-native-url-handler.desktop\n')):
            tool.retire_flatpak(self.root, 'vscode', verified=True, yes=True)
        self.assertFalse(selected.exists())
        self.assertEqual((other / 'keep').read_text(), 'important fixture')

    def test_prepare_never_implicitly_sets_browser_default(self):
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'install_self'), mock.patch.object(tool, 'install'), \
             mock.patch.object(tool, 'migrate'), mock.patch.object(tool, 'integrate'), \
             mock.patch.object(tool, 'make_default_browser') as default:
            tool.main(['prepare'])
            default.assert_not_called()

    def test_open_url_argument_is_preserved(self):
        parsed = tool.cli_parser().parse_args(['open','code','--open-url','vscode://settings/editor.fontSize'])
        self.assertEqual(parsed.args, ['--open-url','vscode://settings/editor.fontSize'])


if __name__ == '__main__':
    unittest.main()
