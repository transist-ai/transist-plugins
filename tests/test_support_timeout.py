"""Regress owned subprocess cleanup before runtime temporary directories disappear."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from typing import Any
import unittest
from unittest.mock import patch

import support


class TimeoutCleanupTests(unittest.TestCase):
    """Exercise a real plugin wrapper with a pipe-owning grandchild."""

    def test_sigterm_resistant_plugin_is_killed_and_reaped(self) -> None:
        """Bound timeout cleanup even when the direct plugin ignores SIGTERM."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "plugin.yaml").write_text("entrypoint: {}\n")
            (root / "plugin.py").write_text(
                "import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "print('ready', flush=True)\n"
                "time.sleep(60)\n"
            )
            processes: list[subprocess.Popen[str]] = []
            real_popen = subprocess.Popen

            def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
                """Capture the real plugin child to verify escalation and reaping."""
                process = real_popen(*args, **kwargs)
                processes.append(process)
                return process

            started = time.monotonic()
            with patch.object(support.subprocess, "Popen", side_effect=spawn):
                with self.assertRaises(subprocess.TimeoutExpired):
                    support.run_process_plugin_subprocess(root, {}, timeout=0.5)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(processes[0].returncode, -signal.SIGKILL)
            with self.assertRaises(ChildProcessError):
                os.waitpid(processes[0].pid, os.WNOHANG)

    def test_timeout_exception_and_cancellation_reap_before_temp_cleanup(self) -> None:
        """Preserve original failures while cleaning the group within a fixed bound."""
        for mode in ("timeout", "error", "cancel"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as scratch:
                root = Path(scratch)
                pid_file = root / "child.pid"
                reaped = root / "reaped"
                (root / "plugin.yaml").write_text(
                    "entrypoint:\n  env:\n    TMPDIR: runtime://temp\n"
                )
                (root / "plugin.py").write_text(
                    "import os, signal, subprocess, sys, time\n"
                    "child = subprocess.Popen([sys.executable, '-c', "
                    "'import time; time.sleep(60)'])\n"
                    "def stop(signum, frame):\n"
                    "    child.wait()\n"
                    f"    open({str(reaped)!r}, 'w').write("
                    "str(os.path.isdir(os.environ['TMPDIR'])))\n"
                    "    sys.exit(0)\n"
                    "signal.signal(signal.SIGTERM, stop)\n"
                    f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
                    "print('ready', flush=True)\n"
                    "time.sleep(60)\n"
                )
                processes: list[subprocess.Popen[str]] = []
                real_popen = subprocess.Popen
                failure = (
                    RuntimeError("communicate failed")
                    if mode == "error"
                    else KeyboardInterrupt()
                )

                def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
                    """Capture the child and inject failures after startup."""
                    process = real_popen(*args, **kwargs)
                    processes.append(process)
                    if mode != "timeout":
                        communicate = process.communicate

                        def interrupted(*args: Any, **kwargs: Any) -> tuple[str, str]:
                            """Fail once the child and signal handler are ready."""
                            deadline = time.monotonic() + 2
                            while not pid_file.exists() and time.monotonic() < deadline:
                                time.sleep(0.01)
                            process.communicate = communicate
                            raise failure

                        process.communicate = interrupted
                    return process

                started = time.monotonic()
                try:
                    with patch.object(support.subprocess, "Popen", side_effect=spawn):
                        expected = (
                            subprocess.TimeoutExpired
                            if mode == "timeout"
                            else type(failure)
                        )
                        with self.assertRaises(expected):
                            support.run_process_plugin_subprocess(root, {}, timeout=0.5)
                    self.assertLess(time.monotonic() - started, 3)
                    self.assertEqual(reaped.read_text(), "True")
                    self.assertIsNotNone(processes[0].returncode)
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(processes[0].pid, os.WNOHANG)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(pid_file.read_text()), 0)
                finally:
                    for process in processes:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()


if __name__ == "__main__":
    unittest.main()
