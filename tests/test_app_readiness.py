"""Apps become usable independently; downloads never hold an app critical lock."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import test_safety as fixtures
import test_launch_guard as launch_fixtures

tool = fixtures.tool


class AppReadinessTests(unittest.TestCase):
    setUp = fixtures.SafetyTests.setUp
    native_fixture = fixtures.SafetyTests.native_fixture
    icon_fixture = fixtures.SafetyTests.icon_fixture
    ready = launch_fixtures.LaunchGuardTests.ready
    wrapper_fixture = launch_fixtures.LaunchGuardTests.wrapper_fixture
    executable_fixture = launch_fixtures.LaunchGuardTests.executable_fixture

    def assert_immediate_launch(self, app, args=None):
        args = args or ['--version']
        with mock.patch.object(tool, 'launch_notice', side_effect=AssertionError('no notification')), \
             mock.patch.object(tool.time, 'sleep', side_effect=AssertionError('no wait')), \
             mock.patch.object(tool.subprocess, 'run', side_effect=AssertionError('no network or second prepare')), \
             mock.patch.object(tool.os, 'execve') as launch:
            tool.open_app(self.root, app, args)
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[1][-len(args):], args)

    def test_global_job_and_other_app_critical_phase_do_not_block_ready_app(self):
        for app, other in (('brave', 'code'), ('code', 'brave')):
            with self.subTest(app=app):
                self.ready(app)
                tool.app_progress(self.root, other, phase='downloading', result='running', critical_incomplete=True)
                with tool.session_lock_path(self.root).open('a') as global_job:
                    fcntl.flock(global_job, fcntl.LOCK_EX)
                    self.assert_immediate_launch(app)
                    with tool.app_lock_path(self.root, other).open('a+') as critical:
                        fcntl.flock(critical, fcntl.LOCK_EX)
                        self.assert_immediate_launch(app)

    def test_only_requested_app_waits_during_its_critical_phase(self):
        for app, other in (('brave', 'code'), ('code', 'brave')):
            self.ready(app)
            self.ready(other)
            with tool.app_lock_path(self.root, app).open('a+') as critical:
                fcntl.flock(critical, fcntl.LOCK_EX)
                self.assert_immediate_launch(other)
                def finish(_):
                    fcntl.flock(critical, fcntl.LOCK_UN)
                with mock.patch.object(tool.time, 'sleep', side_effect=finish) as sleep, \
                     mock.patch.object(tool, 'launch_notice') as notice, \
                     mock.patch.object(tool.os, 'execve') as launch:
                    tool.open_app(self.root, app, [])
                sleep.assert_called_once()
                notice.assert_called_once()
                launch.assert_called_once()
        self.assertEqual(tool.LAUNCH_WAIT_SECONDS, 60)

    def test_one_app_failure_never_disables_other_healthy_app(self):
        for failed, healthy in (('brave', 'code'), ('code', 'brave')):
            self.ready(healthy)
            tool.app_progress(self.root, failed, phase='critical', result='failed', critical_incomplete=True)
            tool.write_session_log(self.root / 'runtime', {'result': 'failed'})
            self.assert_immediate_launch(healthy)
            with mock.patch.object(tool, 'launch_notice'), mock.patch.object(tool.os, 'execve') as launch:
                with self.assertRaisesRegex(RuntimeError, 'not ready'):
                    tool.open_app(self.root, failed, [])
                launch.assert_not_called()

    def test_real_wrappers_launch_while_global_job_and_other_app_lock_are_held(self):
        self.wrapper_fixture()
        for app, other, arguments in (
                ('brave', 'code', ['--incognito', 'https://example.invalid/a b']),
                ('code', 'brave', ['.', '--new-window', 'a b', '--open-url', 'vscode://fixture'])):
            self.ready(app)
            self.executable_fixture(app)
            output = self.base / ('launched-' + app)
            env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'FIXTURE_ROOT': str(self.root),
                   'FIXTURE_LAUNCHED': str(output)}
            with tool.session_lock_path(self.root).open('a') as global_job, \
                 tool.app_lock_path(self.root, other).open('a+') as other_job:
                fcntl.flock(global_job, fcntl.LOCK_EX)
                fcntl.flock(other_job, fcntl.LOCK_EX)
                result = subprocess.run([str(self.home / '.local/bin' / app), *arguments],
                                        env=env, capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, '')
                self.assertEqual(json.loads(output.read_text())[-len(arguments):], arguments)

    def test_partial_session_status_is_read_only_and_app_specific(self):
        self.ready('brave')
        tool.app_progress(self.root, 'code', phase='downloading', result='running')
        with tool.session_lock_path(self.root).open('a') as global_job:
            fcntl.flock(global_job, fcntl.LOCK_EX)
            before = {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
            with mock.patch.object(tool, 'goinfre', return_value=self.root), \
                 mock.patch.object(tool.subprocess, 'run', side_effect=AssertionError('status cannot install')), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                tool.main(['session', 'status'])
            self.assertIn('Core     preparing', out.getvalue())
            self.assertIn('Brave    ready', out.getvalue())
            self.assertIn('Code     preparing', out.getvalue())
            self.assertEqual(before, {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()})
        self.assertEqual(tool.app_status(self.root, 'code'), 'not prepared')

    def test_ready_record_never_substitutes_for_real_artifacts(self):
        self.ready('code')
        self.assertTrue(tool.app_ready(self.root, 'code'))
        (self.root / 'vscode-extensions').rmdir()
        self.assertFalse(tool.app_ready(self.root, 'code'))
        (self.root / 'vscode-extensions').mkdir()
        wrapper = self.home / '.local/bin/code-native'
        wrapper.write_text('unrelated wrapper')
        self.assertFalse(tool.app_ready(self.root, 'code'))
        wrapper.write_text(tool.native_wrapper_contents('code'))
        tool.app_progress(self.root, 'code', integrated_release='/unrelated/stale-release')
        self.assertFalse(tool.app_ready(self.root, 'code'))
        (self.root / 'runtime/code-prepare.json').write_text('interrupted record')
        self.assertFalse(tool.app_ready(self.root, 'code'))

    def test_real_prepare_finishes_brave_before_code_downloads(self):
        self.icon_fixture()
        sequence = []
        actual_migrate, actual_integrate = tool.migrate, tool.integrate
        def install(root, app, *, activate):
            sequence.append(('download', app))
            if app == 'code':
                self.assertTrue(tool.core_job_active(root))
                self.assertTrue(tool.app_ready(root, 'brave'))
                self.assertFalse(tool.critical_app_active(root, 'brave'))
                self.assert_immediate_launch('brave', ['--incognito'])
            return fixtures.staged_install(root, app, activate=activate)
        def migrate(root, app):
            sequence.append(('migrate', app))
            actual_migrate(root, app)
        def integrate(root, app):
            sequence.append(('integrate', app))
            actual_integrate(root, app)
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'install_self'), mock.patch.object(tool, 'activate_release'), \
             mock.patch.object(tool, 'install', side_effect=install), \
             mock.patch.object(tool, 'migrate', side_effect=migrate), \
             mock.patch.object(tool, 'integrate', side_effect=integrate), \
             mock.patch.object(tool, 'stopped'), mock.patch.object(tool, 'run'), \
             mock.patch.object(tool, 'install_codex', side_effect=AssertionError('optional forbidden')):
            tool.main(['prepare'])
        self.assertEqual(sequence, [('download', 'brave'), ('migrate', 'brave'), ('integrate', 'brave'),
                                    ('download', 'code'), ('migrate', 'code'), ('integrate', 'code')])
        self.assertTrue(tool.core_ready(self.root))

    def test_existing_code_can_launch_during_brave_download(self):
        self.icon_fixture()
        self.ready('code')
        def install(root, app, *, activate):
            if app == 'brave':
                self.assertTrue(tool.core_job_active(root))
                self.assert_immediate_launch('code', ['.', '--new-window', 'a b'])
            return fixtures.staged_install(root, app, activate=activate)
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'install_self'), mock.patch.object(tool, 'activate_release'), \
             mock.patch.object(tool, 'install', side_effect=install), \
             mock.patch.object(tool, 'stopped'), mock.patch.object(tool, 'run'):
            tool.main(['prepare'])
        self.assertTrue(tool.core_ready(self.root))

    def failure_case(self, failing):
        self.icon_fixture()
        healthy = 'code' if failing == 'brave' else 'brave'
        def install(root, app, *, activate):
            if app == failing:
                raise RuntimeError('fixture download failed')
            return fixtures.staged_install(root, app, activate=activate)
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'install_self'), mock.patch.object(tool, 'activate_release'), \
             mock.patch.object(tool, 'install', side_effect=install), \
             mock.patch.object(tool, 'stopped'), mock.patch.object(tool, 'run'), \
             self.assertRaisesRegex(RuntimeError, 'Core preparation failed'):
            tool.main(['prepare'])
        self.assertTrue(tool.app_ready(self.root, healthy))
        self.assertFalse(tool.app_ready(self.root, failing))
        self.assert_immediate_launch(healthy)

    def test_brave_failure_does_not_prevent_code_preparation(self):
        self.failure_case('brave')

    def test_code_failure_keeps_completed_brave_usable(self):
        self.failure_case('code')

    def stage_fixture(self, work):
        def download(url, archive):
            self.assertFalse(tool.critical_app_active(self.root, 'brave'))
            self.assert_immediate_launch('brave')
            archive.write_bytes(b'fixture archive')
        def extract(archive, unpack, kind):
            self.assertFalse(tool.critical_app_active(self.root, 'brave'))
            self.assert_immediate_launch('brave')
            exe = unpack / 'brave'
            exe.write_text('new executable fixture')
            exe.chmod(0o755)
        return mock.patch.object(tool, 'fetch', side_effect=download), mock.patch.object(tool, 'extract', side_effect=extract)

    def test_download_extract_unlocked_but_activation_exclusive_and_versions_retained(self):
        self.icon_fixture()
        self.ready('brave')
        old = (self.root / 'apps/brave/current').resolve()
        work = self.root / 'tmp/download-fixture'
        work.mkdir()
        download, extract = self.stage_fixture(work)
        actual_switch = tool.switch
        def switch(link, target):
            self.assertTrue(tool.critical_app_active(self.root, 'brave'))
            actual_switch(link, target)
        with download, extract, mock.patch.object(tool, 'verify_checksum'), \
             mock.patch.object(tool.shutil, 'disk_usage', return_value=mock.Mock(free=4 * 1024**3)), \
             mock.patch.object(tool, 'stopped') as stopped, mock.patch.object(tool, 'switch', side_effect=switch):
            release = tool.install_app(self.root, 'brave', 'new', 'https://github.com/brave/fixture',
                                       '0'*64, 'zip', work)
        self.assertEqual((self.root / 'apps/brave/current').resolve(), release)
        self.assertTrue(old.exists())
        self.assertTrue((work / 'brave.zip').exists())
        self.assertEqual(stopped.call_args_list, [mock.call(('brave',))])
        self.assertTrue(tool.app_ready(self.root, 'brave'))

    def test_running_app_does_not_block_download_but_activation_keeps_current_intact(self):
        self.icon_fixture()
        self.ready('brave')
        old = (self.root / 'apps/brave/current').resolve()
        work = self.root / 'tmp/download-fixture'
        work.mkdir()
        download, extract = self.stage_fixture(work)
        with tool.app_lock_path(self.root, 'brave').open('a+') as running:
            fcntl.flock(running, fcntl.LOCK_SH)
            with download, extract, mock.patch.object(tool, 'verify_checksum'), \
                 mock.patch.object(tool.shutil, 'disk_usage', return_value=mock.Mock(free=4 * 1024**3)), \
                 self.assertRaisesRegex(RuntimeError, 'current release left intact'):
                tool.install_app(self.root, 'brave', 'new', 'https://github.com/brave/fixture', '0'*64, 'zip', work)
        self.assertEqual((self.root / 'apps/brave/current').resolve(), old)
        self.assertTrue((self.root / 'apps/brave/releases/new').exists())
        self.assertTrue(tool.app_ready(self.root, 'brave'))
