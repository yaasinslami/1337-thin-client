import errno
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
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
        self.bwrap = self.base / 'bwrap'
        self.bwrap.write_text('#!/bin/sh\n')
        self.bwrap.chmod(0o755)
        self.patch = mock.patch.multiple(tool, HOME=self.home,
            BRAVE=self.home / '.config/BraveSoftware/Brave-Browser',
            CODE=self.home / '.config/Code-Thin',
            SPOTIFY=self.home / '.config/Spotify-Thin',
            OLD_BRAVE=self.home / '.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser',
            OLD_CODE=self.home / '.var/app/com.visualstudio.code/config/Code/User',
            STATE=self.home / '.local/share/thin-client',
            SELF=self.home / '.local/bin/thin-client-setup', BWRAP=self.bwrap)
        self.patch.start()
        directories = mock.patch.object(tool, 'system_application_dirs',
                                        return_value=[self.base / 'system/applications'])
        directories.start()
        self.addCleanup(directories.stop)
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
            if path == root or root in path.parents:
                data = list(result)
                data[2] += 1
                if path == root and uid is not None:
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

    def test_readonly_goinfre_is_accepted_for_launch_only(self):
        with self.fake_mount(), mock.patch.object(tool.os, 'access', return_value=False):
            self.assertEqual(tool.validate_goinfre(
                self.root.parent, self.home, require_writable=False), self.root.parent)
            with self.assertRaisesRegex(RuntimeError, 'not writable'):
                tool.validate_goinfre(self.root.parent, self.home, require_writable=True)

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

    def test_managed_cache_link_switches_to_transient_target_only(self):
        path = self.home / 'cache'
        persistent = self.root / 'cache'
        transient = self.base / 'runtime/cache'
        persistent.mkdir(parents=True)
        path.symlink_to(persistent)
        tool.cache_link(path, transient, (persistent, transient))
        self.assertEqual(path.resolve(), transient)
        outside = self.base / 'outside'
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, 'Unexpected cache link'):
            tool.cache_link(path, persistent, (persistent, transient))

    def test_cache_existing_data_preserved(self):
        path = self.home / 'cache'
        path.mkdir()
        (path / 'important').write_text('keep')
        with self.assertRaises(RuntimeError):
            tool.cache_link(path, self.root / 'cache')
        self.assertEqual((path / 'important').read_text(), 'keep')

    def icon_fixture(self):
        import base64
        release = self.root / 'apps/vscode/releases/fixture'
        source = release / 'resources/app/resources/linux/code.png'
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='))
        (self.root / 'apps/vscode/current').symlink_to(release, target_is_directory=True)
        brave = self.root / 'apps/brave/releases/fixture'
        brave.mkdir(parents=True, exist_ok=True)
        (brave / 'product_logo_256.png').write_bytes(source.read_bytes())
        (self.root / 'apps/brave/current').symlink_to(brave, target_is_directory=True)
        return source

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

    def desktop_icon_fixtures(self):
        brave = self.root / 'apps/brave/current/product_logo_256.png'
        code = self.root / 'apps/vscode/current/resources/app/resources/linux/code.png'
        brave.parent.mkdir(parents=True, exist_ok=True)
        code.parent.mkdir(parents=True, exist_ok=True)
        brave.write_bytes(b'brave icon fixture')
        code.write_bytes(b'code icon fixture')

    def spotify_fixture(self):
        release = self.root / 'apps/spotify/releases/1.2.3-snap-r99'
        executable = release / 'usr/share/spotify/spotify'
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text('official spotify fixture')
        executable.chmod(0o755)
        chains = ['snap/command-chain/gpu-2404-wrapper',
                  'snap/command-chain/desktop-launch']
        for relative in chains:
            wrapper = release / relative
            wrapper.parent.mkdir(parents=True, exist_ok=True)
            wrapper.write_text('#!/bin/sh\nexec "$@"\n')
            wrapper.chmod(0o755)
        snap_yaml = release / 'meta/snap.yaml'
        snap_yaml.parent.mkdir(parents=True, exist_ok=True)
        # Snapcraft uses this valid YAML indentation-less sequence form.
        snap_yaml.write_text('name: spotify\nversion: 1.2.3\nbase: core24\n'
                             'apps:\n  spotify:\n'
                             '    command: usr/share/spotify/spotify\n'
                             '    command-chain:\n'
                             '    - snap/command-chain/gpu-2404-wrapper\n'
                             '    - snap/command-chain/desktop-launch\n'
                             '    plugs: [network]\n')
        metadata = {
            'backend': 'official-spotify-snap', 'version': '1.2.3',
            'revision': '99', 'snap_id': tool.SPOTIFY_SNAP_ID,
            'sha3_384': 'A' * 64, 'size': 1, 'base': 'core24',
            # Deliberately model an old 0.10.x receipt. spotify_release must
            # repair its chain in memory from the signed snap.yaml.
            'command': ['usr/share/spotify/spotify'], 'command_chain': [],
        }
        (release / '.thin-client-install.json').write_text(json.dumps(metadata))
        current = self.root / 'apps/spotify/current'
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(release)
        for name, snap_id in tool.SPOTIFY_RUNTIME_SNAP_IDS.items():
            runtime = self.root / f'apps/snap-runtimes/{name}/releases/fixture-snap-r1'
            runtime.mkdir(parents=True)
            (runtime / '.thin-client-install.json').write_text(json.dumps({
                'backend': 'official-snap-runtime', 'name': name, 'version': 'fixture',
                'revision': '1', 'snap_id': snap_id,
                'sha3_384': 'A' * 64, 'size': 1,
            }))
            runtime_current = runtime.parent.parent / 'current'
            runtime_current.symlink_to(runtime)
            if name == 'gnome-46-2404':
                for relative in ('usr/lib/x86_64-linux-gnu/webkit2gtk-4.1',
                                 'usr/share/xml/iso-codes'):
                    (runtime / relative).mkdir(parents=True)
            elif name == 'mesa-2404':
                for relative in ('libdrm', 'drirc.d'):
                    (runtime / relative).mkdir(parents=True)
                error_db = runtime / 'X11/XErrorDB'
                error_db.parent.mkdir(parents=True)
                error_db.write_text('X11 error database fixture')
        return executable

    def spotify_download(self, args, **kwargs):
        argv = [str(value) for value in args]
        work = next((Path(value.split('=', 1)[1]) for value in argv
                     if value.startswith('--target-directory=')), None)
        if argv[:2] == ['snap', 'download']:
            name = argv[2]
            snap_id = (tool.SPOTIFY_SNAP_ID if name == 'spotify' else
                       tool.SPOTIFY_RUNTIME_SNAP_IDS[name])
            payload = f'official {name} snap fixture'.encode()
            (work / f'{name}.snap').write_bytes(payload)
            checksum = tool.base64.urlsafe_b64encode(
                hashlib.sha3_384(payload).digest()).decode().rstrip('=')
            (work / f'{name}.assert').write_text(
                'type: snap-revision\n'
                f'snap-id: {snap_id}\n'
                'snap-revision: 99\n'
                f'snap-size: {len(payload)}\n'
                f'snap-sha3-384: {checksum}\n\nfixture-signature\n')
        elif argv[0] == 'unsquashfs':
            unpack = Path(argv[argv.index('-d') + 1])
            name = Path(argv[-1]).stem
            if name != 'spotify':
                self.assertIn('-no-xattrs', argv)
                self.assertIn('-no-exit-code', argv)
            meta = unpack / 'meta/snap.yaml'
            meta.parent.mkdir(parents=True)
            if name == 'spotify':
                executable = unpack / 'usr/share/spotify/spotify'
                executable.parent.mkdir(parents=True)
                executable.write_text('official spotify fixture')
                executable.chmod(0o755)
                for relative in ('snap/command-chain/gpu-2404-wrapper',
                                 'snap/command-chain/desktop-launch'):
                    wrapper = unpack / relative
                    wrapper.parent.mkdir(parents=True, exist_ok=True)
                    wrapper.write_text('#!/bin/sh\nexec "$@"\n')
                    wrapper.chmod(0o755)
                meta.write_text('name: spotify\nversion: 1.2.3\nbase: core24\n'
                                'apps:\n  spotify:\n'
                                '    command: usr/share/spotify/spotify\n'
                                '    command-chain:\n'
                                '    - snap/command-chain/gpu-2404-wrapper\n'
                                '    - snap/command-chain/desktop-launch\n')
            else:
                meta.write_text(f'name: {name}\nversion: 1.0\n')
                if name == 'gnome-46-2404':
                    for relative in ('usr/lib/x86_64-linux-gnu/webkit2gtk-4.1',
                                     'usr/share/xml/iso-codes'):
                        (unpack / relative).mkdir(parents=True)
                elif name == 'mesa-2404':
                    for relative in ('libdrm', 'drirc.d'):
                        (unpack / relative).mkdir(parents=True)
                    error_db = unpack / 'X11/XErrorDB'
                    error_db.parent.mkdir(parents=True)
                    error_db.write_text('X11 error database fixture')
        else:
            self.fail(f'Unexpected Spotify installer command: {argv}')
        return mock.Mock(stdout='')

    def fake_theme_install(self, root):
        """Keep unrelated prepare tests offline while preserving Code setup effects."""
        tool.vscode_caches(root)
        tool.environment(root, 'vscode')

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
        self.assertTrue(tool.vscode_extensions().is_dir())

    def test_open_brave_never_inspects_code(self):
        self.native_fixture('brave')
        tool.CODE.mkdir(parents=True)
        (tool.CODE / 'GPUCache').mkdir()
        # Even an invalid extension destination must not block Brave.
        tool.STATE.mkdir(parents=True)
        tool.vscode_extensions().write_text('unexpected')
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
        self.assertFalse(tool.vscode_extensions().exists())

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
             mock.patch.object(tool, 'install_vscode_theme', side_effect=self.fake_theme_install), \
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
        self.assertEqual(tool.url_checked('https://chatgpt.com/codex/install.sh'),
                         'https://chatgpt.com/codex/install.sh')
        self.assertEqual(tool.url_checked('https://downloads.claude.ai/example'),
                         'https://downloads.claude.ai/example')
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

    def test_spotify_official_snap_is_verified_and_activation_is_atomic(self):
        with mock.patch.object(tool.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'snap is required'):
                tool.install_spotify(self.root)
        with mock.patch.object(tool, 'run', side_effect=self.spotify_download), \
             mock.patch.object(tool.shutil, 'which', return_value='/usr/bin/tool'):
            tool.install_spotify(self.root)
        current = self.root / 'apps/spotify/current'
        first = current.resolve()
        with mock.patch.object(tool, 'run', side_effect=self.spotify_download), \
             mock.patch.object(tool, 'stopped'), \
             mock.patch.object(tool.shutil, 'which', return_value='/usr/bin/tool'):
            tool.install_spotify(self.root)
        self.assertEqual(current.resolve(), first)
        metadata = json.loads((current / '.thin-client-install.json').read_text())
        self.assertEqual(metadata['backend'], 'official-spotify-snap')
        self.assertEqual(metadata['snap_id'], tool.SPOTIFY_SNAP_ID)
        self.assertEqual(metadata['revision'], '99')
        for name, snap_id in tool.SPOTIFY_RUNTIME_SNAP_IDS.items():
            runtime, runtime_metadata = tool.snap_runtime_release(self.root, name)
            self.assertTrue(runtime.is_dir())
            self.assertEqual(runtime_metadata['snap_id'], snap_id)

    def test_spotify_runtime_retry_preserves_readonly_partial_extraction(self):
        work = self.root / 'tmp/spotify-download-retained'
        runtime_work = work / 'core24'
        runtime_work.mkdir(parents=True)
        self.spotify_download([
            'snap', 'download', 'core24', '--stable', '--basename=core24',
            f'--target-directory={runtime_work}'])
        partial = runtime_work / 'core24-unpacked'
        protected = partial / 'etc/sudoers'
        protected.parent.mkdir(parents=True)
        protected.write_text('partial')
        protected.chmod(0o400)
        with mock.patch.object(tool, 'run', side_effect=self.spotify_download):
            installed = tool.install_snap_runtime(self.root, 'core24', work)
        self.assertTrue(installed.is_dir())
        backups = list(runtime_work.glob('core24-unpacked.incomplete-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'etc/sudoers').read_text(), 'partial')
        self.assertTrue((installed / '.thin-client-extraction-complete').is_file())

    def test_spotify_store_identity_and_storage_overrides_are_enforced(self):
        assertion = self.base / 'spotify.assert'
        assertion.write_text('type: snap-revision\nsnap-id: attacker\n'
                             'snap-revision: 1\nsnap-size: 1\n'
                             f'snap-sha3-384: {"A" * 64}\n\ninvalid\n')
        with self.assertRaisesRegex(RuntimeError, 'pinned Store identity'):
            tool.spotify_assertion(assertion)
        self.spotify_fixture()
        tool.migrate_spotify()
        with self.assertRaisesRegex(RuntimeError, 'Storage overrides'):
            tool.open_app(self.root, 'spotify', ['--user-data-dir=/tmp/escape'])

    def test_spotify_launch_uses_managed_core24_container(self):
        executable = self.spotify_fixture()
        tool.migrate_spotify()
        xauthority = self.home / '.Xauthority'
        xauthority.write_text('test cookie')
        with mock.patch.object(tool, 'gpu_args', return_value=[]), \
             mock.patch.object(tool, 'spotify_audio_api', return_value='pulseaudio'), \
             mock.patch.object(tool.os, 'execve') as execute, \
             mock.patch.object(tool, 'start_spotify_browser_bridge',
                               return_value=Path('/run/user/fixture/open.fifo')), \
             mock.patch.dict(tool.os.environ, {'XAUTHORITY': str(xauthority)}):
            tool.open_app(self.root, 'spotify', ['spotify:track:fixture'])
        launched, argv, env = execute.call_args.args
        self.assertEqual(launched, self.bwrap)
        self.assertEqual(argv[0], str(self.bwrap))
        core = tool.snap_runtime_release(self.root, 'core24')[0]
        self.assertIn(str(core), argv)
        self.assertIn('/snap/spotify/current', argv)
        self.assertIn('/snap/spotify/current/snap/command-chain/gpu-2404-wrapper', argv)
        self.assertIn('/snap/spotify/current/snap/command-chain/desktop-launch', argv)
        self.assertIn('/snap/spotify/current/usr/share/spotify/spotify', argv)
        self.assertIn('--audio-api=pulseaudio', argv)
        self.assertNotIn('--debug-level=8', argv)
        self.assertFalse(any(value.startswith('--log-file=') for value in argv))
        self.assertIn('spotify:track:fixture', argv)
        for target in ('/usr/lib/x86_64-linux-gnu/webkit2gtk-4.1',
                       '/usr/share/xml/iso-codes', '/usr/share/libdrm',
                       '/usr/share/drirc.d', '/usr/share/X11/XErrorDB'):
            self.assertIn(target, argv)
        self.assertEqual(env['SNAP'], '/snap/spotify/current')
        self.assertEqual(env['XDG_RUNTIME_DIR'], f'/run/user/{os.getuid()}/snap.spotify')
        self.assertEqual(env['PIPEWIRE_RUNTIME_DIR'], f'/run/user/{os.getuid()}')
        self.assertEqual(env['PULSE_SERVER'],
                         f'unix:/run/user/{os.getuid()}/pulse/native')
        self.assertEqual(env['DBUS_SYSTEM_BUS_ADDRESS'],
                         f'unix:path=/run/user/{os.getuid()}/snap.spotify/no-system-bus')
        self.assertIn('/snap/spotify/current/gnome-platform/usr/lib/x86_64-linux-gnu',
                      env['LD_LIBRARY_PATH'])
        self.assertIn('/snap/spotify/current/gnome-platform/usr/lib/x86_64-linux-gnu/libproxy',
                      env['LD_LIBRARY_PATH'])
        self.assertNotIn('/usr/local/lib', env['LD_LIBRARY_PATH'])
        self.assertEqual(env['FONTCONFIG_PATH'],
                         '/snap/spotify/current/gnome-platform/etc/fonts')
        self.assertEqual(env['XAUTHORITY'], str(self.home / '.thin-client-Xauthority'))
        self.assertEqual(env['THIN_CLIENT_OPEN_FIFO'], '/run/user/fixture/open.fifo')
        self.assertIn(str(xauthority), argv)
        self.assertIn(str(self.home / '.thin-client-Xauthority'), argv)
        helper = tool.spotify_open_helper()
        self.assertTrue(os.access(helper, os.X_OK))
        self.assertIn(str(helper), argv)
        self.assertIn('/usr/bin/xdg-open', argv)
        snapctl = tool.spotify_snapctl_helper()
        self.assertTrue(os.access(snapctl, os.X_OK))
        self.assertIn(str(snapctl.parent), argv)
        self.assertIn('/usr/lib/snapd', argv)
        self.assertEqual(subprocess.run([snapctl, 'is-connected', 'gsettings'],
                                       check=False).returncode, 0)
        refused_snapctl = subprocess.run([snapctl, 'get', 'secret'], capture_output=True,
                                         text=True, check=False)
        self.assertEqual(refused_snapctl.returncode, 1)
        refused = subprocess.run([helper, 'file:///etc/passwd'], capture_output=True,
                                 text=True, check=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn('refused non-web URL', refused.stderr)
        self.assertTrue(executable.is_file())

    def test_spotify_audio_backend_adapts_to_host_session(self):
        runtime = self.base / 'audio-runtime'
        socket_stat = mock.Mock(st_mode=stat.S_IFSOCK)
        with mock.patch.object(Path, 'stat',
                               side_effect=[FileNotFoundError(), socket_stat]):
            self.assertEqual(tool.spotify_audio_api(runtime), 'pipewire')
        with mock.patch.object(Path, 'stat', return_value=socket_stat):
            self.assertEqual(tool.spotify_audio_api(runtime), 'pulseaudio')

    def test_spotify_login_repair_archives_credentials_and_cache(self):
        self.spotify_fixture()
        tool.migrate_spotify()
        config = tool.SPOTIFY / '.config/spotify'
        keyring = tool.SPOTIFY / '.local/share/keyrings'
        cache = self.root / 'cache/spotify'
        for directory in (config, keyring, cache):
            directory.mkdir(parents=True, exist_ok=True)
        (config / 'prefs').write_text('saved credentials')
        (keyring / 'default.keyring').write_text('isolated secret')
        (cache / 'generated').write_text('wrong account cache')
        with mock.patch.object(tool, 'stopped') as stopped:
            tool.repair_spotify_login(self.root)
        stopped.assert_called_once_with(('spotify',))
        backups = list((self.root / 'legacy-backups').glob('spotify-login-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'spotify-config/prefs').read_text(),
                         'saved credentials')
        self.assertEqual((backups[0] / 'isolated-keyrings/default.keyring').read_text(),
                         'isolated secret')
        self.assertEqual((backups[0] / 'spotify-cache/generated').read_text(),
                         'wrong account cache')
        self.assertFalse(config.exists())
        self.assertFalse(keyring.exists())
        self.assertTrue(cache.is_dir())

    def test_spotify_browser_bridge_accepts_only_http_urls(self):
        self.assertEqual(tool.spotify_browser_url('https://accounts.spotify.com/login?a=1'),
                         'https://accounts.spotify.com/login?a=1')
        self.assertEqual(tool.spotify_browser_url('http://127.0.0.1/callback'),
                         'http://127.0.0.1/callback')
        for value in ('spotify://callback', 'file:///etc/passwd', 'https:///missing-host',
                      'https://example.com/\nsecond'):
            self.assertIsNone(tool.spotify_browser_url(value))

    def test_codex_installer_confines_packages_to_goinfre(self):
        work = self.root / 'work'
        work.mkdir()
        calls = []
        def fake_fetch(url, path):
            path.write_text('# installer fixture\n')
        def fake_run(args, **kwargs):
            argv = [str(a) for a in args]
            calls.append(argv)
            env = kwargs['env']
            self.assertEqual(env['CODEX_HOME'], str(self.root / 'apps/codex/installer-home'))
            self.assertEqual(env['CODEX_INSTALL_DIR'], str(self.root / 'apps/codex/bin'))
            if argv[0] == '/bin/sh':
                self.assertEqual(argv[:2], ['/bin/sh', str(work / 'codex-install.sh')])
                executable = self.root / 'apps/codex/bin/codex'
                executable.write_text('fixture')
                executable.chmod(0o755)
            elif argv[-3:] == ['app-server', 'daemon', 'start']:
                daemon = (self.root / 'apps/codex/installer-home/packages/'
                          'app-server-daemon/current/bin/codex')
                daemon.parent.mkdir(parents=True)
                daemon.write_text('daemon fixture')
                daemon.chmod(0o755)
            return mock.Mock()
        with mock.patch.object(tool, 'fetch', side_effect=fake_fetch), \
             mock.patch.object(tool, 'run', side_effect=fake_run), \
             mock.patch.object(tool, 'cli_stopped') as stopped:
            tool.install_codex(self.root, work)
        stopped.assert_called_once_with('codex')
        self.assertEqual(calls[-1][-3:], ['app-server', 'daemon', 'stop'])
        self.assertTrue((self.root / 'apps/codex/bin/codex').is_file())
        self.assertTrue((self.root / 'apps/codex/installer-home/packages/'
                         'app-server-daemon/current/bin/codex').is_file())
        self.assertFalse((self.home / '.codex').exists())

    def test_codex_packages_are_backed_up_then_redirected_without_touching_state(self):
        state = self.home / '.codex'
        packages = state / 'packages'
        (packages / 'standalone').mkdir(parents=True)
        (packages / 'standalone/binary').write_bytes(b'reproducible standalone')
        (packages / 'app-server-daemon').mkdir()
        (packages / 'app-server-daemon/binary').write_bytes(b'reproducible daemon')
        (state / 'auth.json').write_text('persistent secret fixture')
        (state / 'sessions').mkdir()
        (state / 'sessions/one.jsonl').write_text('persistent session fixture')
        target = self.root / 'apps/codex/installer-home/packages'
        target.mkdir(parents=True)
        with mock.patch.object(tool, 'cli_stopped') as stopped:
            tool.relocate_codex_packages(self.root)
        stopped.assert_called_once_with('codex')
        self.assertTrue(packages.is_symlink())
        self.assertEqual(packages.resolve(), target)
        self.assertEqual((target / 'app-server-daemon/binary').read_bytes(),
                         b'reproducible daemon')
        self.assertEqual((state / 'auth.json').read_text(), 'persistent secret fixture')
        self.assertEqual((state / 'sessions/one.jsonl').read_text(),
                         'persistent session fixture')
        backups = list((self.root / 'legacy-backups').glob('codex-packages-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'standalone/binary').read_bytes(),
                         b'reproducible standalone')
        self.assertEqual((backups[0] / 'app-server-daemon/binary').read_bytes(),
                         b'reproducible daemon')

    def test_codex_missing_daemon_is_recovered_from_package_backup(self):
        state = self.home / '.codex'
        state.mkdir()
        target = self.root / 'apps/codex/installer-home/packages'
        target.mkdir(parents=True)
        (state / 'packages').symlink_to(target)
        backup = self.root / 'legacy-backups/codex-packages-20261013-000000-fixture'
        daemon = backup / 'app-server-daemon/current/bin/codex'
        daemon.parent.mkdir(parents=True)
        daemon.write_text('daemon fixture')
        daemon.chmod(0o755)
        with mock.patch.object(tool, 'cli_stopped') as stopped:
            tool.relocate_codex_packages(self.root)
        stopped.assert_called_once_with('codex')
        restored = target / 'app-server-daemon/current/bin/codex'
        self.assertTrue(os.access(restored, os.X_OK))
        self.assertEqual(restored.read_text(), 'daemon fixture')

    def test_codex_package_relocation_refuses_unexpected_link(self):
        state = self.home / '.codex'
        state.mkdir()
        outside = self.base / 'outside-packages'
        outside.mkdir()
        (state / 'packages').symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, 'Unexpected Codex package link'):
            tool.relocate_codex_packages(self.root)

    def test_codex_process_detection_blocks_relocation(self):
        result = mock.Mock(stdout=f'{os.getuid()} 123 codex\n')
        with mock.patch.object(tool, 'run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'Quit every codex process'):
                tool.cli_stopped('codex')

    def test_claude_binary_uses_signed_manifest_checksum_and_versioned_release(self):
        work = self.root / 'work'
        work.mkdir()
        payload = b'claude fixture'
        checksum = hashlib.sha256(payload).hexdigest()
        manifest = {'platforms': {'linux-x64': {'checksum': checksum, 'size': len(payload)}}}
        def fake_fetch(url, path):
            if url.endswith('/latest'):
                path.write_text('2.1.999\n')
            elif url.endswith('/manifest.json'):
                path.write_text(json.dumps(manifest))
            elif url.endswith('/manifest.json.sig'):
                path.write_bytes(b'signature fixture')
            elif url.endswith('/linux-x64/claude'):
                path.write_bytes(payload)
            else:
                self.fail(f'Unexpected URL: {url}')
        with mock.patch.object(tool, 'fetch', side_effect=fake_fetch), \
             mock.patch.object(tool, 'verify_claude_manifest') as verify:
            tool.install_claude(self.root, work)
        release = self.root / 'apps/claude/releases/2.1.999'
        verify.assert_called_once()
        self.assertEqual((release / 'claude').read_bytes(), payload)
        self.assertEqual((self.root / 'apps/claude/current').resolve(), release)

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

    def test_islands_dark_install_preserves_unrelated_settings_and_is_idempotent(self):
        source = self.base / 'islands-source'
        (source / 'themes').mkdir(parents=True)
        (source / 'fonts').mkdir()
        (source / 'package.json').write_text(json.dumps({
            'name': 'islands-dark', 'publisher': 'bwya77', 'version': tool.ISLANDS_DARK_VERSION,
            'contributes': {'themes': [{'label': 'Islands Dark',
                                        'path': './themes/islands-dark.json'}]},
        }))
        (source / 'themes/islands-dark.json').write_text('{}')
        (source / 'settings.json').write_text(json.dumps({
            'workbench.colorTheme': 'Islands Dark',
            'editor.fontFamily': 'IBM Plex Mono',
            'terminal.integrated.fontFamily': 'FiraCode Nerd Font Mono',
            'custom-ui-style.stylesheet': {'.part.editor': {'border-radius': '24px'}},
        }))
        (source / 'fonts/BearSansUI.otf').write_bytes(b'font fixture')
        self.native_fixture('code')
        settings = tool.CODE / 'User/settings.json'
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text('{// keep me\n'
                            '"terminal.integrated.defaultProfile.linux":"zsh-local",'
                            '"editor.fontFamily":"user-old",}')
        tool.STATE.mkdir(parents=True, exist_ok=True)
        extension_root = tool.vscode_extensions()
        extension_root.mkdir()
        (extension_root / 'extensions.json').write_text('[{"fixture":true}]')
        with mock.patch.object(tool.shutil, 'which', return_value=None):
            tool.install_islands_dark_files(self.root, source)
            first = settings.read_bytes()
            tool.install_islands_dark_files(self.root, source)
        merged = tool.parse_jsonc(settings.read_text())
        self.assertEqual(merged['terminal.integrated.defaultProfile.linux'], 'zsh-local')
        self.assertEqual(merged['workbench.colorTheme'], 'Islands Dark')
        self.assertEqual(merged['workbench.iconTheme'], 'vs-seti-folder')
        self.assertEqual(merged['editor.fontFamily'],
                         "'Noto Sans Mono', 'DejaVu Sans Mono', monospace")
        self.assertEqual(merged['terminal.integrated.fontFamily'],
                         "'Noto Sans Mono', 'DejaVu Sans Mono', monospace")
        self.assertEqual(merged['terminal.integrated.minimumContrastRatio'], 4.5)
        self.assertEqual(merged['terminal.integrated.fontSize'], 14)
        self.assertEqual(merged['terminal.integrated.fontWeight'], '500')
        self.assertEqual(merged['editor.cursorSmoothCaretAnimation'], 'off')
        self.assertFalse(merged['editor.smoothScrolling'])
        self.assertFalse(merged['workbench.list.smoothScrolling'])
        self.assertFalse(merged['terminal.integrated.smoothScrolling'])
        self.assertEqual(merged['terminal.integrated.localEchoEnabled'], 'on')
        self.assertEqual(merged['terminal.integrated.localEchoLatencyThreshold'], 0)
        self.assertEqual(merged['terminal.integrated.localEchoStyle'], '#EEEEEC')
        self.assertFalse(merged['editor.minimap.renderCharacters'])
        self.assertTrue(merged['files.watcherExclude']['**/node_modules/**'])
        self.assertEqual(merged['custom-ui-style.stylesheet']['.monaco-workbench *']
                         ['transition-duration'], '0s !important')
        self.assertTrue(merged['remote.SSH.useLocalServer'])
        self.assertFalse(merged['remote.SSH.useExecServer'])
        self.assertEqual(merged['remote.SSH.connectTimeout'], 30)
        self.assertEqual(settings.read_text().count('"editor.fontFamily"'), 1)
        self.assertEqual(settings.read_text().count('"terminal.integrated.fontFamily"'), 1)
        self.assertEqual(merged['workbench.colorCustomizations']['[Islands Dark]']
                         ['editor.background'], '#15171A')
        self.assertEqual(merged['workbench.colorCustomizations']['[Islands Dark]']
                         ['terminal.foreground'], '#EEEEEC')
        self.assertEqual(merged['workbench.colorCustomizations']['[Islands Dark]']
                         ['terminal.background'], '#181A1D')
        self.assertEqual(settings.read_bytes(), first)
        self.assertTrue((extension_root /
                         f'bwya77.islands-dark-{tool.ISLANDS_DARK_VERSION}/themes/islands-dark.json').is_file())
        self.assertEqual((tool.STATE / 'settings.pre-islands-dark.jsonc').read_text(),
                         '{// keep me\n'
                         '"terminal.integrated.defaultProfile.linux":"zsh-local",'
                         '"editor.fontFamily":"user-old",}')
        self.assertTrue((tool.STATE / 'extensions.pre-islands-dark.json').is_file())
        self.assertFalse((extension_root / 'extensions.json').exists())
        self.assertEqual((tool.STATE / 'fonts/islands-dark/BearSansUI.otf').read_bytes(),
                         b'font fixture')
        self.assertIn(str(tool.STATE / 'fonts/islands-dark'),
                      (self.home / '.config/fontconfig/conf.d/50-thin-client-islands-dark.conf').read_text())

    def test_islands_dark_source_uses_pinned_revision_and_validates_archive(self):
        payload = self.base / 'payload'
        (payload / 'themes').mkdir(parents=True)
        (payload / 'package.json').write_text(json.dumps({
            'name': 'islands-dark', 'publisher': 'bwya77', 'version': tool.ISLANDS_DARK_VERSION,
            'contributes': {'themes': [{'label': 'Islands Dark',
                                        'path': './themes/islands-dark.json'}]},
        }))
        (payload / 'settings.json').write_text('{"workbench.colorTheme":"Islands Dark"}')
        (payload / 'themes/islands-dark.json').write_text('{}')
        archive = self.base / 'islands.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            bundle.add(payload, arcname=f'vscode-dark-islands-{tool.ISLANDS_DARK_COMMIT}')
        urls = []
        def fake_fetch(url, path):
            urls.append(url)
            shutil.copy2(archive, path)
        with mock.patch.object(tool, 'fetch', side_effect=fake_fetch):
            first = tool.islands_dark_source(self.root)
            second = tool.islands_dark_source(self.root)
        self.assertEqual(first, second)
        self.assertEqual(urls, [('https://github.com/bwya77/vscode-dark-islands/archive/'
                                 f'{tool.ISLANDS_DARK_COMMIT}.tar.gz')])
        self.assertEqual(json.loads((first / 'package.json').read_text())['publisher'], 'bwya77')

    def test_islands_dark_source_reuses_retained_cross_filesystem_staging(self):
        payload = (self.root / 'tmp/islands-dark-retained/unpacked' /
                   f'vscode-dark-islands-{tool.ISLANDS_DARK_COMMIT}')
        (payload / 'themes').mkdir(parents=True)
        (payload / 'package.json').write_text(json.dumps({
            'name': 'islands-dark', 'publisher': 'bwya77',
            'version': tool.ISLANDS_DARK_VERSION,
            'contributes': {'themes': [{'label': 'Islands Dark',
                                        'path': './themes/islands-dark.json'}]},
        }))
        (payload / 'settings.json').write_text('{"workbench.colorTheme":"Islands Dark"}')
        (payload / 'themes/islands-dark.json').write_text('{}')
        transient = self.base / 'transient'
        transient.mkdir()
        with mock.patch.object(tool, 'fetch') as fetch, \
             mock.patch.object(tool, 'runtime_storage', return_value=transient):
            installed = tool.islands_dark_source(self.root)
        fetch.assert_not_called()
        self.assertTrue(installed.is_relative_to(tool.STATE))
        self.assertNotEqual(installed.stat().st_ino, payload.stat().st_ino)
        self.assertEqual((installed / 'themes/islands-dark.json').read_text(), '{}')

    def test_islands_dark_pin_has_fixed_activity_bar_geometry(self):
        # Upstream did not bump package.json for this post-release revision.
        self.assertEqual(tool.ISLANDS_DARK_VERSION, '0.0.2')
        self.assertEqual(tool.ISLANDS_DARK_COMMIT,
                         '6a1621a980053af19161594bf82322af7c911578')

    def test_islands_dark_managed_revision_is_upgraded(self):
        source = self.base / 'islands-upgrade-source'
        (source / 'themes').mkdir(parents=True)
        (source / 'fonts').mkdir()
        (source / 'package.json').write_text(json.dumps({
            'name': 'islands-dark', 'publisher': 'bwya77',
            'version': tool.ISLANDS_DARK_VERSION,
            'contributes': {'themes': [{'label': 'Islands Dark',
                                        'path': './themes/islands-dark.json'}]},
        }))
        (source / 'themes/islands-dark.json').write_text('{"new":true}')
        (source / 'settings.json').write_text('{"workbench.colorTheme":"Islands Dark"}')
        destination = (tool.vscode_extensions() /
                       f'bwya77.islands-dark-{tool.ISLANDS_DARK_VERSION}')
        destination.mkdir(parents=True)
        (destination / 'stale').write_text('old revision')
        (destination / '.thin-client-islands-dark.json').write_text(json.dumps({
            'repository': 'https://github.com/bwya77/vscode-dark-islands',
            'commit': tool.ISLANDS_DARK_MANAGED_COMMITS[0],
            'version': tool.ISLANDS_DARK_VERSION,
        }))
        tool.STATE.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(tool.shutil, 'which', return_value=None):
            tool.install_islands_dark_files(self.root, source)
        self.assertFalse((destination / 'stale').exists())
        self.assertEqual(json.loads((destination /
                         '.thin-client-islands-dark.json').read_text())['commit'],
                         tool.ISLANDS_DARK_COMMIT)
        self.assertEqual((destination / 'themes/islands-dark.json').read_text(),
                         '{"new":true}')

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
        self.desktop_icon_fixtures()
        with mock.patch.object(tool, 'run'):
            self.icon_fixture()
            tool.desktop_integration(self.root)
        applications = self.home / '.local/share/applications'
        normal = (applications / 'code-native.desktop').read_text()
        handler = (applications / 'code-native-url-handler.desktop').read_text()
        self.assertIn(' %F', normal)
        self.assertNotIn('--open-url', normal)
        self.assertIn('--open-url %U', handler)
        self.assertIn('MimeType=x-scheme-handler/vscode;', handler)
        self.assertIn(str(self.home / '.local/bin/code-native'), handler)
        self.assertIn(f'Icon={tool.STATE / "icons/code-native.png"}', normal)
        self.assertIn(f'Icon={tool.STATE / "icons/code-native.png"}', handler)
        self.assertNotIn('x-scheme-handler/http', handler)
        self.assertIn('Hidden=true', (applications / 'code.desktop').read_text())
        self.assertIn('Hidden=true', (applications / 'code-url-handler.desktop').read_text())
        self.assertIn('open code', (self.home / '.local/bin/code').read_text())
        self.assertIn('# >>> thin-client native-code >>>', (self.home / '.zshrc').read_text())

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

    def test_pyenv_shell_block_repairs_path_and_is_idempotent(self):
        zshrc = self.home / '.zshrc'
        bashrc = self.home / '.bashrc'
        zshrc.write_text('export PATH=/usr/bin\n')
        bashrc.write_text('# keep me\n')
        tool.install_pyenv_shell(self.root)
        before = {path: path.read_bytes() for path in (zshrc, bashrc)}
        tool.install_pyenv_shell(self.root)
        self.assertEqual(before, {path: path.read_bytes() for path in (zshrc, bashrc)})
        for path in (zshrc, bashrc):
            contents = path.read_text()
            self.assertEqual(contents.count('# >>> thin-client pyenv >>>'), 1)
            self.assertIn('$PYENV_ROOT/bin:$PYENV_ROOT/shims:$PATH', contents)
            self.assertIn('/opt/pyenv/bin/pyenv', contents)
            self.assertIn('unfunction _pyenv_virtualenv_hook', contents)
            self.assertIn('unset -f _pyenv_virtualenv_hook', contents)
            self.assertIn('/opt/pyenv|/opt/pyenv/*', contents)
        self.assertTrue(zshrc.read_text().startswith('export PATH=/usr/bin\n'))
        self.assertTrue(bashrc.read_text().startswith('# keep me\n'))

    def test_pyenv_shell_block_refuses_malformed_existing_marker(self):
        path = self.home / '.zshrc'
        path.write_text('# >>> thin-client pyenv >>>\nbroken\n')
        with self.assertRaisesRegex(RuntimeError, 'Malformed managed shell block'):
            tool.install_pyenv_shell(self.root)
        self.assertEqual(path.read_text(), '# >>> thin-client pyenv >>>\nbroken\n')

    def test_pinned_pyenv_install_keeps_versions_outside_release(self):
        archive = self.base / 'pyenv.tar.gz'
        root_name = f'pyenv-{tool.PYENV_COMMIT}'
        with tarfile.open(archive, 'w:gz') as bundle:
            files = {
                'bin/pyenv': '#!/bin/sh\n',
                'libexec/pyenv': '#!/bin/sh\n',
                'libexec/pyenv---version': f'#!/bin/sh\nversion="{tool.PYENV_VERSION}"\n',
                'plugins/python-build/bin/python-build': '#!/bin/sh\n',
            }
            for relative, contents in files.items():
                data = contents.encode()
                info = tarfile.TarInfo(f'{root_name}/{relative}')
                info.size = len(data)
                info.mode = 0o755
                bundle.addfile(info, io.BytesIO(data))
            for directory in ('pyenv.d', 'completions'):
                info = tarfile.TarInfo(f'{root_name}/{directory}')
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                bundle.addfile(info)
        work = self.base / 'work'
        work.mkdir()
        def fetch_fixture(url, destination):
            self.assertIn(tool.PYENV_COMMIT, url)
            shutil.copy2(archive, destination)
        with mock.patch.object(tool, 'fetch', side_effect=fetch_fixture):
            tool.install_pyenv(self.root, work)
        app = self.root / 'apps/pyenv'
        self.assertEqual((app / 'current').resolve(), app / 'releases' / tool.PYENV_VERSION)
        self.assertTrue((app / 'versions').is_dir())
        self.assertFalse((app / 'versions').is_symlink())
        self.assertEqual(os.readlink(app / 'bin'), 'current/bin')
        self.assertEqual(json.loads((app / 'current/.thin-client-pyenv.json').read_text()), {
            'version': tool.PYENV_VERSION, 'commit': tool.PYENV_COMMIT})
        self.assertIn('# >>> thin-client pyenv >>>', (self.home / '.zshrc').read_text())


if __name__ == '__main__':
    unittest.main()
