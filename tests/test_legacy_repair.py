"""Explicit legacy repair, exercised only in temporary profiles and storage."""
import contextlib
import io
import os
from pathlib import Path
import unittest
from unittest import mock

import test_safety as fixtures


tool = fixtures.tool


class LegacyRepairTests(unittest.TestCase):
    setUp = fixtures.SafetyTests.setUp
    fake_mount = fixtures.SafetyTests.fake_mount

    def legacy_link(self, profile='Default', name='ScriptCache', *, existing=True):
        path = tool.BRAVE / profile / 'Service Worker' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        target = self.root / 'cache/brave-profile' / path.relative_to(tool.BRAVE)
        if existing:
            target.mkdir(parents=True)
            (target / 'nested').mkdir()
            (target / 'nested/site-state').write_bytes(b'persistent site data\x00\xff')
            (target / 'empty-file').touch()
            (target / 'empty-directory').mkdir()
        path.symlink_to(target)
        return path, target

    def run_repair(self):
        with self.fake_mount(), mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'stopped') as stopped:
            tool.main(['repair-legacy'])
        return stopped

    def test_script_cache_restored_byte_for_byte_and_old_target_retained(self):
        path, target = self.legacy_link()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            stopped = self.run_repair()
        self.assertFalse(path.is_symlink())
        self.assertTrue(path.is_dir())
        self.assertEqual((path / 'nested/site-state').read_bytes(), b'persistent site data\x00\xff')
        self.assertEqual((path / 'empty-file').read_bytes(), b'')
        self.assertTrue((path / 'empty-directory').is_dir())
        self.assertEqual((target / 'nested/site-state').read_bytes(), b'persistent site data\x00\xff')
        self.assertEqual(tool.legacy_tree_manifest(path), tool.legacy_tree_manifest(target))
        self.assertTrue(path.is_relative_to(self.home))
        self.assertEqual(list(path.parent.glob('.thin-client-legacy-*')), [])
        stopped.assert_called_with(('brave',))
        self.assertIn('persistent HOME directory', output.getvalue())

    def test_cache_storage_and_numbered_profiles_restored(self):
        links = [self.legacy_link(profile, name) for profile in ['Default', 'Profile 1', 'Profile 12']
                 for name in ['ScriptCache', 'CacheStorage']]
        self.run_repair()
        for path, target in links:
            self.assertFalse(path.is_symlink())
            self.assertEqual(tool.legacy_tree_manifest(path), tool.legacy_tree_manifest(target))

    def test_broken_known_legacy_link_refuses_without_inventing_data(self):
        path, target = self.legacy_link(existing=False)
        with self.assertRaisesRegex(RuntimeError, 'restore the exact target from a persistent backup'):
            self.run_repair()
        self.assertTrue(path.is_symlink())
        self.assertEqual(os.readlink(path), str(target))
        self.assertFalse(target.exists())
        self.assertEqual(list(path.parent.glob('.thin-client-legacy-*')), [])

    def test_unknown_target_refused_and_valid_link_not_partially_repaired(self):
        valid, _ = self.legacy_link(name='ScriptCache')
        path = tool.BRAVE / 'Default/Service Worker/CacheStorage'
        outside = self.base / 'external-state'
        outside.mkdir()
        (outside / 'important').write_bytes(b'keep')
        path.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, 'Unrecognized Service Worker symlink target'):
            self.run_repair()
        self.assertTrue(path.is_symlink())
        self.assertTrue(valid.is_symlink())
        self.assertEqual((outside / 'important').read_bytes(), b'keep')

    def test_unrelated_goinfre_target_and_other_profile_target_refused(self):
        path, expected = self.legacy_link(existing=False)
        for target in [self.root / 'unrelated/ScriptCache',
                       self.root / 'cache/brave-profile/Profile 1/Service Worker/ScriptCache']:
            with self.subTest(target=target):
                target.mkdir(parents=True)
                (target / 'important').write_bytes(b'keep')
                path.unlink()  # replace only this fixture's link
                path.symlink_to(target)
                with self.assertRaisesRegex(RuntimeError, 'Unrecognized Service Worker symlink target'):
                    self.run_repair()
                self.assertEqual(os.readlink(path), str(target))
                self.assertEqual((target / 'important').read_bytes(), b'keep')
        self.assertFalse(expected.exists())

    def test_external_indirection_back_to_known_target_is_refused(self):
        path, target = self.legacy_link()
        indirect = self.base / 'user-created-link'
        indirect.symlink_to(target)
        path.unlink()
        path.symlink_to(indirect)
        with self.assertRaisesRegex(RuntimeError, 'Unrecognized Service Worker symlink target'):
            self.run_repair()
        self.assertEqual(os.readlink(path), str(indirect))
        self.assertTrue(indirect.is_symlink())

    def test_relative_link_to_exact_known_layout_is_supported(self):
        path, target = self.legacy_link()
        path.unlink()
        path.symlink_to(os.path.relpath(target, path.parent))
        self.run_repair()
        self.assertFalse(path.is_symlink())
        self.assertEqual(tool.legacy_tree_manifest(path), tool.legacy_tree_manifest(target))

    def test_broken_second_link_refuses_before_any_profile_replacement(self):
        available, _ = self.legacy_link(name='ScriptCache')
        broken, _ = self.legacy_link(name='CacheStorage', existing=False)
        with self.assertRaisesRegex(RuntimeError, 'missing or invalid'):
            self.run_repair()
        self.assertTrue(available.is_symlink())
        self.assertTrue(broken.is_symlink())
        self.assertEqual(list(available.parent.glob('.thin-client-legacy-*')), [])

    def test_prepare_never_implicitly_repairs_legacy_state(self):
        path, target = self.legacy_link()
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'install_self'), mock.patch.object(tool, 'install'), \
             mock.patch.object(tool, 'migrate'), mock.patch.object(tool, 'integrate'), \
             mock.patch.object(tool, 'repair_legacy', side_effect=AssertionError('must be explicit')):
            tool.main(['prepare'])
        self.assertEqual(os.readlink(path), str(target))

    def test_target_ancestor_symlink_escape_refused(self):
        path, target = self.legacy_link(existing=False)
        outside = self.base / 'external-state'
        outside.mkdir()
        (self.root / 'cache').symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, 'Unexpected symlink'):
            self.run_repair()
        self.assertEqual(os.readlink(path), str(target))
        self.assertEqual(list(outside.iterdir()), [])

    def test_normal_service_worker_directories_untouched(self):
        worker = tool.BRAVE / 'Default/Service Worker'
        for name in ['Database', 'ScriptCache', 'CacheStorage']:
            path = worker / name
            path.mkdir(parents=True)
            (path / 'state').write_bytes(name.encode())
        before = {path: path.lstat() for path in worker.rglob('*')}
        stopped = self.run_repair()
        stopped.assert_not_called()
        for path, metadata in before.items():
            self.assertEqual(path.lstat().st_ino, metadata.st_ino)
            self.assertEqual(path.lstat().st_mtime_ns, metadata.st_mtime_ns)
        for name in ['Database', 'ScriptCache', 'CacheStorage']:
            self.assertEqual((worker / name / 'state').read_bytes(), name.encode())
        self.assertFalse((self.root / 'cache').exists())

    def test_brave_running_refuses_repair_before_staging(self):
        path, target = self.legacy_link()
        with self.fake_mount(), mock.patch.object(tool, 'stopped', side_effect=RuntimeError('Quit Brave')):
            with self.assertRaisesRegex(RuntimeError, 'Quit Brave'):
                tool.repair_legacy(self.root)
        self.assertTrue(path.is_symlink())
        self.assertEqual((target / 'nested/site-state').read_bytes(), b'persistent site data\x00\xff')
        self.assertEqual(list(path.parent.glob('.thin-client-legacy-*')), [])

    def test_second_repair_is_idempotent(self):
        path, target = self.legacy_link()
        self.run_repair()
        before = {p: p.lstat() for p in path.rglob('*')}
        inode = path.lstat().st_ino
        stopped = self.run_repair()
        stopped.assert_not_called()
        self.assertEqual(path.lstat().st_ino, inode)
        for p, metadata in before.items():
            self.assertEqual(p.lstat().st_mtime_ns, metadata.st_mtime_ns)
        self.assertTrue(target.is_dir())

    def test_corrupt_copy_refuses_publication_and_retains_original(self):
        path, target = self.legacy_link()
        copytree = tool.shutil.copytree
        def corrupt(source, destination, *args, **kwargs):
            result = copytree(source, destination, *args, **kwargs)
            state = Path(destination) / 'nested/site-state'
            if state.is_file():
                state.write_bytes(b'corrupt fixture')
            return result
        with self.fake_mount(), mock.patch.object(tool, 'stopped'), \
             mock.patch.object(tool.shutil, 'copytree', side_effect=corrupt):
            with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                tool.repair_legacy(self.root)
        self.assertTrue(path.is_symlink())
        self.assertEqual((target / 'nested/site-state').read_bytes(), b'persistent site data\x00\xff')
        self.assertEqual(len(list(path.parent.glob('.thin-client-legacy-*'))), 1)

    def test_failed_directory_publication_rolls_back_original_symlink(self):
        path, target = self.legacy_link()
        with self.fake_mount(), mock.patch.object(tool, 'stopped'), \
             mock.patch.object(tool.os, 'replace', side_effect=OSError('publication failed')):
            with self.assertRaisesRegex(OSError, 'publication failed'):
                tool.repair_legacy(self.root)
        self.assertTrue(path.is_symlink())
        self.assertEqual(os.readlink(path), str(target))
        self.assertEqual((target / 'nested/site-state').read_bytes(), b'persistent site data\x00\xff')

    def test_nested_target_symlink_refused_without_reading_external_data(self):
        path, target = self.legacy_link()
        (target / 'external').symlink_to(self.base / 'unrelated')
        with self.assertRaisesRegex(RuntimeError, 'Unexpected link, mount, or special file'):
            self.run_repair()
        self.assertTrue(path.is_symlink())
        self.assertEqual(list(path.parent.glob('.thin-client-legacy-*')), [])

    def test_target_mount_refused(self):
        path, target = self.legacy_link()
        with self.fake_mount(), mock.patch.object(tool, 'stopped'), \
             mock.patch.object(Path, 'is_mount', autospec=True, side_effect=lambda p: p == target):
            with self.assertRaisesRegex(RuntimeError, 'missing or invalid'):
                tool.repair_legacy(self.root)
        self.assertTrue(path.is_symlink())

    def test_unrelated_storage_symlinks_not_inspected_or_repaired(self):
        profile = tool.BRAVE / 'Default'
        paths = [profile / 'Service Worker/Database', profile / 'IndexedDB', profile / 'Local Storage',
                 profile / 'Session Storage', profile / 'WebStorage', profile / 'Foo/ScriptCache',
                 tool.BRAVE / 'Profile custom/Service Worker/ScriptCache']
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(self.base / 'unrelated')
        self.run_repair().assert_not_called()
        for path in paths:
            self.assertEqual(os.readlink(path), str(self.base / 'unrelated'))

    def test_status_reports_known_legacy_link_without_modifying_it(self):
        path, target = self.legacy_link()
        with self.fake_mount(), mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'script_status'), mock.patch.object(tool, 'desktop_diagnostics'), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            tool.main(['status'])
        self.assertIn('Legacy Service Worker symlink detected', output.getvalue())
        self.assertIn('thin-client-setup repair-legacy', output.getvalue())
        self.assertEqual(os.readlink(path), str(target))
        self.assertEqual(list(path.parent.glob('.thin-client-legacy-*')), [])

    def test_repair_dry_run_changes_nothing_and_does_not_require_stopped_apps(self):
        path, target = self.legacy_link()
        before = sorted(self.base.rglob('*'))
        with self.fake_mount(), mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'stopped') as stopped:
            tool.main(['repair-legacy', '--dry-run'])
            stopped.assert_not_called()
        self.assertEqual(before, sorted(self.base.rglob('*')))
        self.assertEqual(os.readlink(path), str(target))


if __name__ == '__main__':
    unittest.main()
