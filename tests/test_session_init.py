"""Graphical initializer contracts, using temporary accounts and fake core installs."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

import test_safety as fixtures

tool = fixtures.tool


class SessionTests(unittest.TestCase):
    setUp = fixtures.SafetyTests.setUp

    def ready_fixture(self):
        for name in ('apps/brave/current/brave', 'apps/vscode/current/bin/code'):
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('#!/bin/sh\nexit 0\n')
            p.chmod(0o755)
        for profile in (tool.BRAVE, tool.CODE):
            profile.mkdir(parents=True, exist_ok=True)
            (profile / '.thin-client-import').write_text('fixture')
        for name in ('vscode-extensions', 'runtime/brave/config', 'runtime/brave/cache'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        directory = self.home / '.local/bin'
        directory.mkdir(parents=True, exist_ok=True)
        for name in ('code-native', 'brave-native'):
            wrapper = directory / name
            wrapper.write_text(tool.native_wrapper_contents('brave' if name == 'brave-native' else 'code'))
            wrapper.chmod(0o755)
        tool.brave_caches(self.root)
        tool.vscode_caches(self.root)
        tool.environment(self.root, 'brave')
        tool.environment(self.root, 'vscode')

    def session(self, command=None):
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()) as err:
            result = tool.main(command or ['session-init'])
        return result, out.getvalue(), err.getvalue()

    def test_ready_startup_silent_and_never_runs_prepare_network_or_codex(self):
        self.ready_fixture()
        with mock.patch.object(tool.subprocess, 'run', side_effect=AssertionError('no subprocess/network')), \
             mock.patch.object(tool, 'install_codex', side_effect=AssertionError('optional forbidden')), \
             mock.patch.object(tool, 'install', side_effect=AssertionError('already ready')):
            self.assertEqual(self.session(), (0, '', ''))
        record = tool.read_session_log(self.root)
        self.assertEqual(record['result'], 'success')
        self.assertEqual(record['core'], 'ready')
        self.assertFalse(record['prepare_started'])

    def test_new_post_runs_only_core_prepare_once(self):
        calls = []
        def prepare(argv, **kwargs):
            calls.append(argv)
            self.assertEqual(argv, [sys.executable, str(fixtures.SCRIPT), 'prepare'])
            self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
            self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
            self.assertEqual(len(kwargs['pass_fds']), 1)
            self.ready_fixture()
            return mock.Mock(returncode=0)
        with mock.patch.object(tool.subprocess, 'run', side_effect=prepare), \
             mock.patch.object(tool, 'install_codex', side_effect=AssertionError('optional forbidden')):
            self.assertEqual(self.session()[0], 0)
            self.assertTrue(tool.read_session_log(self.root)['prepare_started'])
            self.assertEqual(self.session()[0], 0)
        self.assertEqual(len(calls), 1)

    def test_stale_markers_never_hide_missing_binaries(self):
        runtime = self.root / 'runtime'
        runtime.mkdir()
        (runtime / 'session-init.result').write_text('failed\n')  # Removed zsh implementation.
        tool.write_session_log(runtime, {'result': 'success', 'core': 'ready'})
        def prepare(*args, **kwargs):
            self.ready_fixture()
            return mock.Mock(returncode=0)
        with mock.patch.object(tool.subprocess, 'run', side_effect=prepare) as call:
            self.assertEqual(self.session()[0], 0)
        call.assert_called_once()

    def test_concurrent_lock_returns_immediately_without_overwriting_log(self):
        runtime = self.root / 'runtime'
        runtime.mkdir()
        tool.write_session_log(runtime, {'result': 'running'})
        before = (runtime / 'session-init.log').read_bytes()
        with (runtime / 'session-init.lock').open('a') as lock, \
             mock.patch.object(tool.subprocess, 'run', side_effect=AssertionError('locked')):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.session(), (0, '', ''))
        self.assertEqual((runtime / 'session-init.log').read_bytes(), before)

    @unittest.skipUnless(hasattr(os, 'fork'), 'Unix process locking')
    def test_two_actual_processes_only_one_prepares(self):
        count = self.base / 'prepare-count'
        def prepare(*args, **kwargs):
            with count.open('a') as output:
                output.write('prepare\n')
            time.sleep(0.2)
            self.ready_fixture()
            return mock.Mock(returncode=0)
        children = []
        sys.stdout.flush()
        with mock.patch.object(tool.subprocess, 'run', side_effect=prepare):
            for _ in range(2):
                pid = os.fork()
                if pid == 0:
                    try:
                        result = self.session()[0]
                    except BaseException:
                        os._exit(1)
                    os._exit(result)
                children.append(pid)
            for pid in children:
                _, status = os.waitpid(pid, 0)
                self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertEqual(count.read_text(), 'prepare\n')
        self.assertTrue(tool.core_ready(self.root))

    @unittest.skipUnless(hasattr(os, 'fork'), 'Unix process locking')
    def test_abnormal_process_termination_releases_kernel_lock(self):
        runtime = self.root / 'runtime'
        runtime.mkdir()
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            with (runtime / 'session-init.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                os.write(write_fd, b'locked')
                signal.pause()
            os._exit(0)
        os.close(write_fd)
        try:
            self.assertEqual(os.read(read_fd, 6), b'locked')
        finally:
            os.close(read_fd)
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        self.ready_fixture()
        self.assertEqual(self.session()[0], 0)

    def test_failure_returns_nonzero_cooldown_expires_and_lock_releases(self):
        with mock.patch.object(tool.subprocess, 'run', return_value=mock.Mock(returncode=7)) as run:
            result, stdout, stderr = self.session()
            self.assertEqual(result, 1)
            self.assertEqual(stdout, '')
            self.assertIn('setup failed', stderr)
            self.assertEqual(tool.read_session_log(self.root)['prepare_exit'], 7)
            self.assertEqual(self.session()[0], 1)
            run.assert_called_once()
        with (self.root / 'runtime/session-init.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = tool.read_session_log(self.root)
        record['attempted_at'] -= tool.SESSION_RETRY_SECONDS + 1
        tool.write_session_log(self.root / 'runtime', record)
        def prepare(*args, **kwargs):
            self.ready_fixture()
            return mock.Mock(returncode=0)
        with mock.patch.object(tool.subprocess, 'run', side_effect=prepare) as run:
            self.assertEqual(self.session()[0], 0)
            run.assert_called_once()

    def test_storage_errors_return_failure_without_exiting_parent(self):
        with mock.patch.object(tool, 'goinfre', side_effect=RuntimeError('PRIVATE profile path')), \
             contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(tool.main(['session-init']), 1)
        self.assertNotIn('PRIVATE', output.getvalue())
        self.assertIn('session status', output.getvalue())

    def test_actual_autostart_child_exits_nonzero_without_exiting_test_parent(self):
        result = subprocess.run([sys.executable, '-B', str(fixtures.SCRIPT), 'session-init'],
                                env={'HOME': str(self.home), 'GOINFRE': str(self.base / 'absent'),
                                     'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertIn('session setup unavailable', result.stderr)
        self.assertFalse((self.base / 'absent').exists())

    def test_run_alias_and_read_only_status(self):
        self.ready_fixture()
        self.assertEqual(self.session(['session', 'run'])[0], 0)
        before = {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool.subprocess, 'run', side_effect=AssertionError('status local')), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            tool.main(['session', 'status'])
        self.assertIn('Last init successful', out.getvalue())
        self.assertIn('Core     ready', out.getvalue())
        self.assertIn('Autostart disabled', out.getvalue())
        self.assertIn('Codex    not installed on this post', out.getvalue())
        self.assertEqual(before, {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()})

    def test_logs_remain_bounded_over_many_runs_and_exclude_child_output(self):
        runtime = self.root / 'runtime'
        runtime.mkdir()
        (runtime / 'session-init.log').write_bytes(b'x' * 100000)
        def prepare(*args, **kwargs):
            self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
            self.ready_fixture()
            return mock.Mock(returncode=0)
        with mock.patch.object(tool.subprocess, 'run', side_effect=prepare):
            self.assertEqual(self.session()[0], 0)
        for _ in range(12):
            self.assertEqual(self.session()[0], 0)
            self.assertLessEqual((runtime / 'session-init.log').stat().st_size, tool.SESSION_LOG_LIMIT)
        data = (runtime / 'session-init.log').read_text()
        self.assertNotIn(str(tool.BRAVE), data)
        self.assertNotIn(str(tool.CODE), data)
        self.assertNotIn('CODEX_HOME', data)

    def test_shell_and_history_files_untouched(self):
        self.ready_fixture()
        paths = [self.home / name for name in ('.zsh_history', '.bashrc', '.zshrc', '.zprofile', '.profile')]
        for p in paths:
            p.write_bytes(b'alias vssh=sentinel\x00\xff\n')
        expected = [p.read_bytes() for p in paths]
        self.session()
        self.assertEqual(expected, [p.read_bytes() for p in paths])

    def test_core_prepare_has_no_optional_installation_or_shell_write(self):
        paths = [self.home / name for name in ('.zsh_history', '.zshrc', '.bashrc', '.zprofile')]
        for path in paths:
            path.write_bytes(b'alias vssh=sentinel\xff\n')
        expected = [p.read_bytes() for p in paths]
        with mock.patch.object(tool, 'goinfre', return_value=self.root), \
             mock.patch.object(tool, 'install', side_effect=fixtures.staged_install) as install, \
             mock.patch.object(tool, 'activate_release'), mock.patch.object(tool, 'app_ready', return_value=True), \
             mock.patch.object(tool, 'migrate') as migrate, \
             mock.patch.object(tool, 'integrate') as integrate, \
             mock.patch.object(tool, 'install_codex', side_effect=AssertionError('optional forbidden')):
            tool.main(['prepare'])
        self.assertEqual(install.call_args_list, [mock.call(self.root, 'brave', activate=False),
                                                  mock.call(self.root, 'code', activate=False)])
        for stage in (migrate, integrate):
            self.assertEqual(stage.call_args_list, [mock.call(self.root, 'brave'), mock.call(self.root, 'code')])
        self.assertEqual(expected, [p.read_bytes() for p in paths])
        for forbidden in ('def install_spotify', 'def install_claude', 'def install_pyenv'):
            self.assertNotIn(forbidden, fixtures.SCRIPT.read_text())
