"""Check teardown of isolated smoke fixtures on deadlines and protocol errors."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ollama_smoke", PACKAGE / "tests/smoke.py"
)
assert SPEC is not None and SPEC.loader is not None
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


class SmokeCleanupTests(unittest.TestCase):
    """Exercise real disposable groups, never provider or runtime groups."""

    def test_deadline_reclaims_pipe_holding_descendant(self) -> None:
        """An exited leader's resistant child cannot survive a communicate timeout."""
        self._fixture("deadline")

    def test_invalid_protocol_reclaims_descendant(self) -> None:
        """Invalid JSON still finalizes the group before run_plugin raises."""
        self._fixture("invalid")

    def test_cancellation_reclaims_descendant(self) -> None:
        """BaseException interruption still reclaims the isolated fixture group."""
        self._fixture("cancel")

    def _fixture(self, mode: str) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ollama-smoke-cleanup-", dir="/tmp"
        ) as d:
            root = Path(d)
            pid_file = root / "child.pid"
            fixture = root / "plugin"
            child = (
                "import signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
            )
            fixture.write_text(
                f"#!{sys.executable}\n"
                "import subprocess, sys, time\n"
                f"child = subprocess.Popen([sys.executable, '-c', {child!r}]"
                + (
                    ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
                    if mode != "deadline"
                    else ""
                )
                + ")\n"
                + f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
                + "print('not-json', flush=True)\n"
            )
            fixture.chmod(0o700)
            started = time.monotonic()
            if mode == "cancel":
                communicate = subprocess.Popen.communicate

                def interrupted(process: subprocess.Popen, *args, **kwargs) -> None:
                    """Interrupt after the fixture starts its descendant."""
                    communicate(process, *args, **kwargs)
                    raise KeyboardInterrupt()

                with patch.object(subprocess.Popen, "communicate", interrupted):
                    with self.assertRaises(KeyboardInterrupt):
                        SMOKE.run_plugin(str(fixture), {}, timeout=1)
            else:
                error = subprocess.TimeoutExpired if mode == "deadline" else ValueError
                with self.assertRaises(error):
                    SMOKE.run_plugin(str(fixture), {}, timeout=1)
            self.assertLess(time.monotonic() - started, 3)
            pid = int(pid_file.read_text())
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                observed = subprocess.run(
                    ["/bin/ps", "-o", "stat=", "-p", str(pid)],
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
                if observed.returncode or observed.stdout.strip().startswith("Z"):
                    break
                time.sleep(0.01)
            else:
                os.kill(pid, 9)  # Test-owned last-resort teardown before failing.
                self.fail("isolated fixture descendant remained live")


if __name__ == "__main__":
    unittest.main()
