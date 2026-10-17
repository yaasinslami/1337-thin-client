"""Preparation/launch races with real kernel locks and generated native wrappers."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

import test_safety as fixtures

tool = fixtures.tool


class LaunchGuardTests(unittest.TestCase):
    setUp = fixtures.SafetyTests.setUp
    native_fixture = fixtures.SafetyTests.native_fixture
    icon_fixture = fixtures.SafetyTests.icon_fixture

    def ready(self, app):
        self.native_fixture(app)
        tool.environment(self.root, 'brave' if app == 'brave' else 'vscode')
        (self.root / ('cache/brave-profile' if app == 'brave' else 'cache/vscode')).mkdir(parents=True, exist_ok=True)

    def test_ready_app_launches_without_wait_notification_or_network(self):
        for app, args in [('code', ['.', '--new-window', 'a b']), ('brave', ['--incognito'])]:
            with self.subTest(app=app):
                self.ready(app)
                with mock.patch.object(tool, 'launch_notice', side_effect=AssertionError('ready: no notice')), \
                     mock.patch.object(tool.time, 'sleep', side_effect=AssertionError('ready: no waiting')), \
                     mock.patch.object(tool.subprocess, 'run', side_effect=AssertionError('ready: no network')), \
                     mock.patch.object(tool.os, 'execve') as launch:
                    tool.open_app(self.root, app, args)
                launch.assert_called_once()
                self.assertEqual(launch.call_args.args[1][-len(args):], args)
        with tool.session_lock_path(self.root).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # Mocked exec did not leak the FD.

    def test_ready_brave_does_not_depend_on_code_and_vice_versa(self):
        for app in ('brave', 'code'):
            self.ready(app)
            with mock.patch.object(tool.os, 'execve') as launch:
                tool.open_app(self.root, app, [])
            launch.assert_called_once()

    def test_notifications_optional_and_failing_notifier_does_not_block(self):
        for notifier in (None, '/fixture/notify-send'):
            for error in (None, OSError('missing bus'), subprocess.TimeoutExpired('notify-send', 1)):
                with self.subTest(notifier=notifier, error=error), \
                     mock.patch.object(tool.shutil, 'which', return_value=notifier), \
                     mock.patch.object(tool.subprocess, 'run', side_effect=error) as run, \
                     contextlib.redirect_stderr(io.StringIO()) as stderr:
                    tool.launch_notice('Workstation preparation failed.')
                self.assertIn('Workstation preparation failed.', stderr.getvalue())
                self.assertEqual(run.call_count, 1 if notifier else 0)
                if notifier:
                    self.assertEqual(run.call_args.kwargs['timeout'], 1)

    def test_wait_success_checks_existing_lock_result_and_app_artifacts(self):
        for app in ('brave', 'code'):
            self.ready(app)
            with tool.session_lock_path(self.root).open('a') as prepare:
                fcntl.flock(prepare, fcntl.LOCK_EX)
                def finish(_):
                    tool.write_session_log(self.root / 'runtime', {'result': 'success'})
                    fcntl.flock(prepare, fcntl.LOCK_UN)
                with mock.patch.object(tool.time, 'sleep', side_effect=finish), \
                     mock.patch.object(tool, 'launch_notice') as notify, \
                     mock.patch.object(tool.os, 'execve') as launch:
                    tool.open_app(self.root, app, [])
                launch.assert_called_once()
                self.assertIn('still being prepared', notify.call_args.args[0])
                self.assertIn('Brave' if app == 'brave' else 'VS Code', notify.call_args.args[0])

    def test_failed_missing_or_running_receipt_refuses_even_if_binary_exists(self):
        self.ready('brave')
        for result in ('failed', 'running', None):
            with self.subTest(result=result), tool.session_lock_path(self.root).open('a') as prepare:
                fcntl.flock(prepare, fcntl.LOCK_EX)
                def finish(_):
                    tool.write_session_log(self.root / 'runtime', {'result': result})
                    fcntl.flock(prepare, fcntl.LOCK_UN)
                with mock.patch.object(tool.time, 'sleep', side_effect=finish), \
                     mock.patch.object(tool, 'launch_notice') as notify, \
                     mock.patch.object(tool.os, 'execve') as launch, \
                     self.assertRaisesRegex(RuntimeError, 'preparation failed'):
                    tool.open_app(self.root, 'brave', [])
                launch.assert_not_called()
                self.assertIn('session status', notify.call_args.args[0])

    def test_success_receipt_without_requested_app_runtime_refuses(self):
        self.ready('code')
        (self.root / 'vscode-extensions').rmdir()
        with tool.session_lock_path(self.root).open('a') as prepare:
            fcntl.flock(prepare, fcntl.LOCK_EX)
            def finish(_):
                tool.write_session_log(self.root / 'runtime', {'result': 'success'})
                fcntl.flock(prepare, fcntl.LOCK_UN)
            with mock.patch.object(tool.time, 'sleep', side_effect=finish), \
                 mock.patch.object(tool, 'launch_notice'), \
                 mock.patch.object(tool.os, 'execve') as launch, \
                 self.assertRaisesRegex(RuntimeError, 'preparation failed'):
                tool.open_app(self.root, 'code', [])
            launch.assert_not_called()
            self.assertFalse((self.root / 'vscode-extensions').exists())

    def test_already_failed_partial_prepare_is_not_blindly_launched(self):
        self.native_fixture('brave')
        path = tool.session_lock_path(self.root)
        tool.write_session_log(path.parent, {'result': 'failed'})
        with mock.patch.object(tool, 'launch_notice'), \
             mock.patch.object(tool.os, 'execve') as launch, \
             self.assertRaisesRegex(RuntimeError, 'preparation failed'):
            tool.open_app(self.root, 'brave', [])
        launch.assert_not_called()
        self.assertFalse((self.root / 'runtime/brave').exists())

    def test_timeout_bounded_does_not_launch_or_start_second_prepare(self):
        self.ready('brave')
        with tool.session_lock_path(self.root).open('a') as prepare:
            fcntl.flock(prepare, fcntl.LOCK_EX)
            start = time.monotonic()
            with mock.patch.object(tool, 'LAUNCH_WAIT_SECONDS', 0.04), \
                 mock.patch.object(tool, 'LAUNCH_POLL_SECONDS', 0.005), \
                 mock.patch.object(tool, 'launch_notice') as notify, \
                 mock.patch.object(tool.os, 'execve') as launch, \
                 mock.patch.object(tool.subprocess, 'run', side_effect=AssertionError('no second prepare')), \
                 self.assertRaisesRegex(RuntimeError, 'Timed out'):
                tool.open_app(self.root, 'brave', [])
            self.assertLess(time.monotonic() - start, 0.5)
            launch.assert_not_called()
            self.assertIn('Timed out', notify.call_args.args[0])

    def test_direct_core_prepare_uses_same_lock_and_records_failure(self):
        path = tool.session_lock_path(self.root)
        with tool.core_prepare_guard(self.root):
            with path.open('a') as other, self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
        self.assertEqual(tool.read_session_log(self.root)['result'], 'success')
        with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
            with tool.core_prepare_guard(self.root):
                raise RuntimeError('fixture failure')
        self.assertEqual(tool.read_session_log(self.root)['result'], 'failed')
        with path.open('a') as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_autostart_child_adopts_inherited_lock_without_deadlock(self):
        with tool.session_lock_path(self.root).open('a') as parent:
            fcntl.flock(parent, fcntl.LOCK_EX)
            with mock.patch.dict(os.environ, {tool.PREPARE_LOCK_FD: str(parent.fileno())}):
                with tool.core_prepare_guard(self.root):
                    self.assertEqual(tool.read_session_log(self.root)['result'], 'running')
            with tool.session_lock_path(self.root).open('a') as other, self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(tool.read_session_log(self.root)['result'], 'success')

    def test_unrelated_inherited_fd_is_refused(self):
        other = self.base / 'user-file'
        other.write_text('unchanged')
        with other.open() as fd, mock.patch.dict(os.environ, {tool.PREPARE_LOCK_FD: str(fd.fileno())}):
            with self.assertRaisesRegex(RuntimeError, 'Invalid inherited'):
                with tool.core_prepare_guard(self.root):
                    self.fail('must refuse')
        self.assertEqual(other.read_text(), 'unchanged')

    def test_real_child_can_adopt_parent_preparation_lock(self):
        program = (
            'import importlib.machinery,importlib.util\nfrom pathlib import Path\n'
            f'loader=importlib.machinery.SourceFileLoader("child_fixture",{str(fixtures.SCRIPT)!r})\n'
            'spec=importlib.util.spec_from_loader(loader.name,loader)\n'
            'tool=importlib.util.module_from_spec(spec); loader.exec_module(tool)\n'
            f'with tool.core_prepare_guard(Path({str(self.root)!r})):\n'
            '    print("adopted")\n')
        with tool.session_lock_path(self.root).open('a') as parent:
            fcntl.flock(parent, fcntl.LOCK_EX)
            result = subprocess.run([sys.executable, '-B', '-c', program],
                                    env={'HOME': str(self.home), 'PATH': '/usr/bin:/bin',
                                         tool.PREPARE_LOCK_FD: str(parent.fileno())},
                                    pass_fds=(parent.fileno(),), check=True,
                                    capture_output=True, text=True, timeout=3)
            self.assertEqual(result.stdout, 'adopted\n')
            with tool.session_lock_path(self.root).open('a') as other, self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def wrapper_fixture(self):
        self.icon_fixture()
        tool.SELF.parent.mkdir(parents=True)
        # The real generated wrappers use this temporary installed-script driver;
        # it loads the real implementation but substitutes fixture goinfre only.
        tool.SELF.write_text('#!' + sys.executable + '\n# Managed by thin-client-setup\n' +
            'import importlib.machinery, importlib.util, os, sys\nfrom pathlib import Path\n' +
            f'loader=importlib.machinery.SourceFileLoader("fixture_launch",{str(fixtures.SCRIPT)!r})\n' +
            'spec=importlib.util.spec_from_loader(loader.name,loader)\n' +
            'tool=importlib.util.module_from_spec(spec); loader.exec_module(tool)\n' +
            'tool.goinfre=lambda: Path(os.environ["FIXTURE_ROOT"])\n' +
            'tool.shutil.which=lambda name: None\n' +
            'sys.exit(tool.main(sys.argv[1:]) or 0)\n')
        tool.SELF.chmod(0o755)
        with mock.patch.object(tool, 'run'), mock.patch.object(tool.shutil, 'which', return_value=None):
            tool.desktop_integration(self.root)

    def executable_fixture(self, app):
        exe = self.native_fixture(app)
        exe.write_text('#!' + sys.executable + '\nimport json,os,sys\nfrom pathlib import Path\n' +
                       'Path(os.environ["FIXTURE_LAUNCHED"]).write_text(json.dumps(sys.argv[1:]))\n')
        exe.chmod(0o755)

    @unittest.skipUnless(hasattr(os, 'fork'), 'Unix race regression')
    def test_real_session_init_race_blocks_native_wrappers_and_preserves_arguments(self):
        self.wrapper_fixture()
        for app, arguments in (('brave', ['--incognito', 'https://example.invalid/a b']),
                               ('code', ['.', '--new-window', 'a b', '--open-url', 'vscode://fixture'])):
            with self.subTest(app=app):
                # Deliberately make the core incomplete while an app binary and
                # persistent profile already exist: the exact first-login race.
                for runtime in ('config', 'cache'):
                    p = self.root / 'runtime/brave' / runtime
                    if p.exists():
                        p.rmdir()
                started, finish = self.base / 'started', self.base / 'finish'
                launched = self.base / 'launched.json'
                for p in (started, finish, launched):
                    p.unlink(missing_ok=True)
                self.executable_fixture(app)
                count = self.base / ('prepare-count-' + app)
                def prepare(argv, **kwargs):
                    with count.open('a') as output:
                        output.write('prepare\n')
                    started.write_text('locked')
                    deadline = time.monotonic() + 5
                    while not finish.exists():
                        if time.monotonic() >= deadline:
                            return mock.Mock(returncode=1)
                        time.sleep(0.005)
                    for name in ('brave', 'code'):
                        self.executable_fixture(name)
                    for name in ('vscode-extensions', 'runtime/brave/config', 'runtime/brave/cache',
                                 'cache/brave-profile', 'cache/vscode'):
                        (self.root / name).mkdir(parents=True, exist_ok=True)
                    return mock.Mock(returncode=0)
                sys.stdout.flush()
                pid = os.fork()
                if pid == 0:
                    with mock.patch.object(tool, 'goinfre', return_value=self.root), \
                         mock.patch.object(tool.subprocess, 'run', side_effect=prepare):
                        os._exit(tool.session_init())
                process = None
                try:
                    deadline = time.monotonic() + 5
                    while not started.exists():
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.005)
                    env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin',
                           'FIXTURE_ROOT': str(self.root), 'FIXTURE_LAUNCHED': str(launched)}
                    process = subprocess.Popen([str(self.home / '.local/bin' / app), *arguments],
                                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    # Receiving the wait notice proves the wrapper reached the
                    # held lock; it has not launched or configured app caches.
                    notice = process.stderr.readline()
                    self.assertIn('still being prepared', notice)
                    self.assertFalse(launched.exists())
                    self.assertIsNone(process.poll())
                    self.assertFalse((self.root / 'runtime/brave/config').exists())
                    finish.write_text('complete')
                    stdout, stderr = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, stdout + stderr)
                    self.assertEqual(json.loads(launched.read_text())[-len(arguments):], arguments)
                    self.assertEqual(count.read_text(), 'prepare\n')
                finally:
                    finish.touch()
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)
                    _, status = os.waitpid(pid, 0)
                    self.assertEqual(os.waitstatus_to_exitcode(status), 0)

    def test_guard_does_not_modify_shell_or_history(self):
        self.ready('code')
        paths = [self.home / name for name in ('.zshrc', '.bashrc', '.zsh_history', '.zprofile')]
        for p in paths:
            p.write_bytes(b'user content\x00\xff')
        with mock.patch.object(tool.os, 'execve'):
            tool.open_app(self.root, 'code', ['.'])
        self.assertTrue(all(p.read_bytes() == b'user content\x00\xff' for p in paths))
