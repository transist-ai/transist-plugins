"""Offline lifecycle regressions; native signals only run in isolated fixtures."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_NATIVE = sys.platform in ("linux", "darwin")
PACKAGES = (
    "openspec",
    "github.copilot.cli",
    "opencode.cli",
    "codex.cli",
    "shell",
    "git.cli",
    "github.cli",
)
NATIVE_PATH = ROOT / "plugins/cursor.cli/process_lifecycle.py"
SPEC = importlib.util.spec_from_file_location("runner_test_native", NATIVE_PATH)
NATIVE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = NATIVE
SPEC.loader.exec_module(NATIVE)

# Only this outer harness creates a session. Providers and the runner inherit it.
# Results contain fixed fixture output and identity/liveness metadata, never an
# argv, environment, native exception detail, or user/provider prompt.
FIXTURE = r"""
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, sys.argv[1])
import process_runner as runner
import process_lifecycle as native

case = sys.argv[2]
caller = threading.get_ident()
threads = set(threading.enumerate())
callbacks = []
ticks = []
callback_error = None
command = "import sys; print('ok'); print('err', file=sys.stderr)"
timeout = 3
input_text = None
options = {}
preexisting = None
finalization_started = None
backlog_output = None

if case == 'preexisting':
    preexisting = subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(60)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

def line(stream, text):
    callbacks.append((stream, text, threading.get_ident()))
    if case == 'final_backlog' and text == 'backlog\n':
        time.sleep(0.01)
    if case == 'line_exception':
        raise ValueError('callback fixture')

def tick():
    ticks.append(threading.get_ident())
    if case == 'tick_exception':
        raise ValueError('callback fixture')

if case == 'failure':
    command += '; sys.exit(7)'
elif case in ('orphan', 'grandchild', 'resistant', 'inherited', 'timeout'):
    sleeper = "import time; print('descendant', flush=True); time.sleep(60)"
    if case == 'resistant':
        sleeper = (
            "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            + sleeper
        )
    if case == 'grandchild':
        sleeper = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c'," + repr(sleeper) + "]); "
            "time.sleep(0.2)"
        )
    command = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c'," + repr(sleeper) + "]); "
        "print('parent', flush=True); "
        "print('parent-error',file=sys.stderr,flush=True); "
        "time.sleep(0.3)"
    )
    if case == 'timeout':
        command += '; time.sleep(60)'
        timeout = 0.7
elif case in ('line_exception', 'tick_exception'):
    command += "; sys.stdout.flush(); import time; time.sleep(60)"
elif case in ('stdin', 'stdin_timeout'):
    input_text = 'z' * (2 * 1024 * 1024)
    command = (
        "import sys; sys.stdout.write('x' * (1024 * 1024) + '\\n'); "
        "sys.stdout.flush(); "
    )
    if case == 'stdin':
        command += (
            "data=sys.stdin.read(); print(len(data)); "
            "print('done',file=sys.stderr)"
        )
        timeout = 8
    else:
        command += "import time; time.sleep(60)"
        timeout = 0.5
elif case == 'unterminated':
    command = "import sys; sys.stdout.write('tail'); sys.stderr.write('other')"
elif case == 'unicode':
    command = "import sys; print(sys.stdin.read(), end='')"
    input_text = 'héllo 🌍\n' * 10000
elif case == 'noise':
    command = (
        "import sys,time; [print('noise') for _ in range(1000000)]; "
        "time.sleep(60)"
    )
    timeout = 0.2
elif case == 'no_timeout':
    timeout = None
elif case == 'empty_stdin':
    input_text = ''
    command = "import sys; print(len(sys.stdin.read()))"
elif case == 'closed_stdin':
    input_text = 'z' * 1048576
    command = "import os,time; os.close(0); print('closed',flush=True); time.sleep(0.1)"
elif case == 'setup_exception':
    original = native.PipeReader.start
    starts = []
    def broken_start(self):
        starts.append(self)
        if len(starts) == 2:
            raise OSError('private setup detail')
        original(self)
    native.PipeReader.start = broken_start
    command = "import time; time.sleep(60)"
elif case == 'stdin_setup_exception':
    original = os.set_blocking
    def broken_blocking(fd, blocking):
        if threading.get_ident() == caller:
            raise OSError('private stdin detail')
        return original(fd, blocking)
    os.set_blocking = broken_blocking
    input_text = 'input'
    command = "import time; time.sleep(60)"
elif case == 'reader_exception':
    def broken_read(self):
        self.output.put(('reader_error', ''))
        self.stream.close()
    native.PipeReader._read = broken_read
    command = "import time; time.sleep(60)"
elif case == 'observer_denied':
    def denied(self, pgid):
        raise PermissionError('private observation detail')
    native.NativeProcesses.members = denied
elif case == 'observer_cleanup_denied':
    original = native.NativeProcesses.members
    observations = []
    def denied(self, pgid):
        observations.append(pgid)
        if len(observations) > 1:
            raise PermissionError('private observation detail')
        return original(self, pgid)
    native.NativeProcesses.members = denied
elif case == 'signal_denied':
    native.NativeProcesses.send = lambda self, expected, sig: False
    command = "import time; print('started',flush=True); time.sleep(60)"
    timeout = 0.2
    original_finish = runner.finish_readers
    def observe_stop(readers, deadline):
        confirmed = original_finish(readers, deadline)
        result['reader_stop_requested'] = all(
            reader.stop.is_set() for reader in readers)
        return confirmed
    runner.finish_readers = observe_stop
elif case == 'missing':
    options['missing'] = True
elif case in ('final_backlog', 'finite_backlog'):
    original_finish = runner.finish_readers
    def finish_with_backlog(readers, deadline):
        global finalization_started, backlog_output
        finalization_started = time.monotonic()
        confirmed = original_finish(readers, deadline)
        # All real readers are stopped. Inject queued output at precisely the
        # final-drain boundary, without bypassing native lifecycle checks.
        backlog_output = readers[0].output
        for _ in range(300):
            backlog_output.put(('stdout', 'backlog\n'))
        return confirmed
    runner.finish_readers = finish_with_backlog

started = time.monotonic()
result = {}
try:
    args = (
        ['/nonexistent/transist-fixture-provider'] if options
        else [sys.executable, '-c', command]
    )
    completed = runner.run_process(
        args, timeout=timeout, input=input_text, on_line=line, on_tick=tick,
    )
    result.update(kind='completed', code=completed.returncode,
                  stdout=completed.stdout, stderr=completed.stderr)
    result['command_preserved'] = completed.args == args
except BaseException as error:
    result.update(kind=type(error).__name__,
                  stdout=getattr(error, 'stdout', '') or '',
                  stderr=getattr(error, 'stderr', '') or '')
    if isinstance(error, runner.CleanupUnconfirmedError):
        result['message'] = str(error)
        result['context_hidden'] = error.__suppress_context__
    if isinstance(error, subprocess.TimeoutExpired):
        result['timeout'] = error.timeout
        result['command_preserved'] = error.cmd == args
result['elapsed'] = time.monotonic() - started
if finalization_started is not None:
    result['finalization_elapsed'] = time.monotonic() - finalization_started
    result['backlog_remaining'] = backlog_output.qsize()
result['calling_thread'] = (
    all(item[2] == caller for item in callbacks)
    and all(t == caller for t in ticks)
)
result['thread_shutdown_on_return'] = set(threading.enumerate()) == threads
count = len(callbacks) + len(ticks)
time.sleep(0.1)
result['thread_shutdown'] = set(threading.enumerate()) == threads
result['no_late_callbacks'] = count == len(callbacks) + len(ticks)
result['lines'] = len(callbacks)
result['ticks'] = len(ticks)
if preexisting is not None:
    result['preexisting_survived'] = preexisting.poll() is None
    preexisting.terminate()
    preexisting.wait(timeout=3)
if case not in ('observer_denied', 'observer_cleanup_denied', 'signal_denied'):
    result['live_members'] = [
        member.pid for member in native.NativeProcesses().members(os.getpgrp())
        if member.pid != os.getpid() and member.live
    ]
print(json.dumps(result))
"""


@unittest.skipUnless(SUPPORTED_NATIVE, "native lifecycle supports Linux and macOS only")
class ProcessRunnerTests(unittest.TestCase):
    """Exercise real inherited groups without exposing the test process group."""

    def fixture(self, case: str, package: str = "shell") -> dict:
        """Run a disposable isolated fixture, always reclaiming its own group."""
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                FIXTURE,
                str(ROOT / "plugins" / package),
                case,
            ],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertNotEqual(process.pid, os.getpgrp())
        try:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, "isolated fixture failed")
            self.assertEqual(stderr, "", "unexpected fixture diagnostics")
            result = json.loads(stdout)
        finally:
            # Only the group created above is ever signalled by the harness.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
        self.assertTrue(result["calling_thread"])
        if case != "signal_denied":
            self.assertTrue(result["thread_shutdown_on_return"])
        self.assertTrue(result["thread_shutdown"])
        self.assertTrue(result["no_late_callbacks"])
        self.assertEqual(result.get("live_members", []), [])
        return result

    def test_success_in_every_self_contained_package(self) -> None:
        for package in PACKAGES:
            with self.subTest(package=package):
                result = self.fixture("success", package)
                self.assertEqual(result["kind"], "completed")
                self.assertEqual(result["code"], 0)
                self.assertTrue(result["command_preserved"])
                self.assertEqual(result["stdout"], "ok\n")
                self.assertEqual(result["stderr"], "err\n")
                self.assertEqual(result["lines"], 2)
                self.assertGreater(result["ticks"], 0)

    def test_nonzero_exit_is_a_completed_process(self) -> None:
        result = self.fixture("failure")
        self.assertEqual(result["kind"], "completed")
        self.assertEqual(result["code"], 7)
        self.assertEqual(result["stderr"], "err\n")

    def test_real_preexisting_member_is_protected(self) -> None:
        result = self.fixture("preexisting")
        self.assertEqual(result["kind"], "completed")
        self.assertTrue(result["preexisting_survived"])

    def test_orphan_grandchild_and_inherited_pipes_are_cleaned(self) -> None:
        for case in ("orphan", "grandchild", "inherited", "resistant"):
            with self.subTest(case=case):
                result = self.fixture(case)
                self.assertEqual(result["kind"], "completed")
                self.assertIn("parent\n", result["stdout"])
                self.assertIn("descendant\n", result["stdout"])
                self.assertLess(result["elapsed"], 5.5)
                if case == "resistant":
                    self.assertGreaterEqual(result["elapsed"], 2)

    def test_timeout_retains_command_deadline_and_output(self) -> None:
        result = self.fixture("timeout")
        self.assertEqual(result["kind"], "TimeoutExpired")
        self.assertEqual(result["timeout"], 0.7)
        self.assertTrue(result["command_preserved"])
        self.assertIn("parent\n", result["stdout"])
        self.assertEqual(result["stderr"], "parent-error\n")
        self.assertLess(result["elapsed"], 5.8)

    def test_callback_exceptions_clean_up_and_propagate(self) -> None:
        for case in ("line_exception", "tick_exception"):
            with self.subTest(case=case):
                result = self.fixture(case)
                self.assertEqual(result["kind"], "ValueError")
                self.assertLess(result["elapsed"], 5.5)

    def test_setup_reader_and_observation_failures_are_sanitized(self) -> None:
        for case in (
            "setup_exception",
            "stdin_setup_exception",
            "reader_exception",
            "observer_denied",
            "observer_cleanup_denied",
            "signal_denied",
        ):
            with self.subTest(case=case):
                result = self.fixture(case)
                self.assertEqual(result["kind"], "CleanupUnconfirmedError")
                self.assertEqual(
                    result["message"], "Process cleanup could not be confirmed"
                )
                self.assertTrue(result["context_hidden"])
                self.assertIsInstance(result["stdout"], str)
                self.assertIsInstance(result["stderr"], str)
                self.assertNotIn(
                    "private", result["message"] + result["stdout"] + result["stderr"]
                )
                self.assertLess(result["elapsed"], 5.5)
                if case == "signal_denied":
                    # A denied signal leaves inherited writers alive. Cleanup
                    # explicitly reports uncertainty; Linux select readers can
                    # observe their stop request on the next 25-ms poll. Check
                    # shutdown after the fixture's existing 100-ms quiescence,
                    # while retaining immediate shutdown for every other case.
                    self.assertTrue(result["reader_stop_requested"])
                    self.assertIn("started\n", result["stdout"])

    def test_missing_executable_remains_file_not_found(self) -> None:
        self.assertEqual(self.fixture("missing")["kind"], "FileNotFoundError")

    def test_large_stdin_and_stdout_do_not_deadlock(self) -> None:
        result = self.fixture("stdin")
        self.assertEqual(result["kind"], "completed")
        self.assertEqual(result["stdout"], "x" * 1048576 + "\n2097152\n")
        self.assertEqual(result["stderr"], "done\n")

    def test_stdin_backpressure_and_noisy_output_respect_deadline(self) -> None:
        for case in ("stdin_timeout", "noise"):
            with self.subTest(case=case):
                result = self.fixture(case)
                self.assertEqual(result["kind"], "TimeoutExpired")
                self.assertTrue(result["stdout"])
                self.assertLess(result["elapsed"], 3)

    def test_unicode_empty_closed_stdin_and_unterminated_output(self) -> None:
        result = self.fixture("unicode")
        self.assertEqual(result["stdout"], "héllo 🌍\n" * 10000)
        self.assertEqual(self.fixture("empty_stdin")["stdout"], "0\n")
        self.assertEqual(self.fixture("closed_stdin")["stdout"], "closed\n")
        result = self.fixture("unterminated")
        self.assertEqual(result["stdout"], "tail")
        self.assertEqual(result["stderr"], "other")

    def test_none_timeout(self) -> None:
        self.assertEqual(self.fixture("no_timeout")["kind"], "completed")

    def test_final_backlog_is_bounded_and_fails_closed_in_every_package(self) -> None:
        for package in PACKAGES:
            with self.subTest(package=package):
                result = self.fixture("final_backlog", package)
                self.assertEqual(result["kind"], "CleanupUnconfirmedError")
                self.assertEqual(
                    result["message"], "Process cleanup could not be confirmed"
                )
                self.assertTrue(result["context_hidden"])
                self.assertLess(result["finalization_elapsed"], 1.3)
                self.assertGreater(result["backlog_remaining"], 0)
                # Check the deadline within a batch, not just between batches.
                drained = result["stdout"].count("backlog\n")
                self.assertGreater(drained, 0)
                self.assertLess(drained, 120)
                self.assertEqual(result["stderr"], "err\n")

    def test_ordinary_finite_final_backlog_flushes_completely(self) -> None:
        result = self.fixture("finite_backlog")
        self.assertEqual(result["kind"], "completed")
        self.assertEqual(result["stdout"], "ok\n" + "backlog\n" * 300)
        self.assertEqual(result["stderr"], "err\n")
        self.assertEqual(result["backlog_remaining"], 0)
        self.assertLess(result["finalization_elapsed"], 1)


@unittest.skipUnless(SUPPORTED_NATIVE, "native lifecycle supports Linux and macOS only")
class NativeSafetyTests(unittest.TestCase):
    """Adversarial identity checks use metadata doubles, never native signals."""

    def lifecycle(self) -> tuple:
        """Construct a lifecycle snapshot with protected/preexisting metadata."""

        class Observer:
            def __init__(self) -> None:
                self.snapshots = []
                self.sent = []

            def ancestor(self, pid: int) -> tuple:
                return 1, (10,)

            def members(self, pgid: int) -> list:
                return self.snapshots

            def send(self, expected: object, sig: int) -> bool:
                self.sent.append((expected.pid, sig))
                return True

        observer = Observer()
        observer.snapshots = [NATIVE.Identity(900002, (20,), 1, 900000, "S")]
        with (
            patch.object(NATIVE, "NativeProcesses", return_value=observer),
            patch.object(NATIVE.os, "getpid", return_value=900001),
            patch.object(NATIVE.os, "getpgrp", return_value=900000),
        ):
            lifecycle = NATIVE.ProcessLifecycle()
        return lifecycle, observer

    def test_ancestors_and_preexisting_members_never_become_owned(self) -> None:
        lifecycle, observer = self.lifecycle()
        observer.snapshots += [
            NATIVE.Identity(900001, (10,), 1, 900000, "S"),
            NATIVE.Identity(1, (1,), 0, 900000, "S"),
            NATIVE.Identity(900003, (30,), 900001, 900000, "S"),
        ]
        self.assertEqual([item.pid for item in lifecycle.remaining()], [900003])
        self.assertEqual(observer.sent, [])

    def test_reused_protected_or_owned_pids_fail_closed(self) -> None:
        for pid in (900002, 900003):
            with self.subTest(pid=pid):
                lifecycle, observer = self.lifecycle()
                lifecycle.owned[900003] = (30,)
                observer.snapshots = [NATIVE.Identity(pid, (99,), 1, 900000, "S")]
                self.assertEqual(lifecycle.remaining(), [])
                self.assertFalse(lifecycle.cleanup(lambda: None))
                self.assertEqual(observer.sent, [])

    def test_native_send_checks_birth_and_group_before_signalling(self) -> None:
        observer = NATIVE.NativeProcesses()
        expected = NATIVE.Identity(900003, (30,), 1, 900000, "S")
        for current in (
            NATIVE.Identity(900003, (99,), 1, 900000, "S"),
            NATIVE.Identity(900003, (30,), 1, 900099, "S"),
        ):
            with (
                patch.object(observer, "read", return_value=current),
                patch.object(NATIVE.os, "pidfd_open", return_value=42, create=True),
                patch.object(NATIVE.os, "close"),
                patch.object(NATIVE.os, "kill") as kill,
                patch.object(
                    NATIVE.signal, "pidfd_send_signal", create=True
                ) as pidfd_signal,
            ):
                self.assertFalse(observer.send(expected, signal.SIGTERM))
                kill.assert_not_called()
                pidfd_signal.assert_not_called()


class HelperParityTests(unittest.TestCase):
    """Packages ship independent, byte-identical helper implementations."""

    def test_companion_helper_parity(self) -> None:
        native_bytes = NATIVE_PATH.read_bytes()
        runner_bytes = (ROOT / "plugins/shell/process_runner.py").read_bytes()
        for package in PACKAGES:
            with self.subTest(package=package):
                directory = ROOT / "plugins" / package
                self.assertEqual(
                    (directory / "process_lifecycle.py").read_bytes(), native_bytes
                )
                self.assertEqual(
                    (directory / "process_runner.py").read_bytes(), runner_bytes
                )

    def test_helpers_are_selected_package_payloads_without_version_changes(
        self,
    ) -> None:
        for package in PACKAGES:
            with self.subTest(package=package):
                manifest = yaml.safe_load(
                    (ROOT / "plugins" / package / "plugin.yaml").read_text()
                )
                self.assertEqual(manifest["version"], "0.1.0")
                self.assertIn("process_lifecycle.py", manifest["package"]["files"])
                self.assertIn("process_runner.py", manifest["package"]["files"])

    def test_public_release_shell_helper_parity(self) -> None:
        """Compare exact public-release helper hashes without a sibling checkout."""
        record = json.loads(
            (ROOT / "tests/contracts/provenance-public-v0.1.0.json").read_text()
        )
        expected = record["runtime_shell_helpers_sha256"]
        self.assertEqual(
            set(expected),
            {"plugins/shell/process_lifecycle.py", "plugins/shell/process_runner.py"},
        )
        for path, digest in expected.items():
            self.assertEqual(
                hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), digest
            )
        manifest = yaml.safe_load((ROOT / "plugins/shell/plugin.yaml").read_text())
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertIn("process_lifecycle.py", manifest["package"]["files"])
        self.assertIn("process_runner.py", manifest["package"]["files"])


if __name__ == "__main__":
    unittest.main()
