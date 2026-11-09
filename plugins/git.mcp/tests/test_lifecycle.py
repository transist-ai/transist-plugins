"""Native inherited-group tests in isolated OUTER sessions, without providers."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
from process_lifecycle import Identity, NativeProcesses, ProcessLifecycle  # noqa: E402


class LifecycleTests(unittest.TestCase):
    """Verify native cleanup, protected identities and fail-closed observation."""

    def test_outer_native_cases(self) -> None:
        """Normal/nonzero orphan cleanup preserves siblings and group inheritance."""
        for mode in ("normal", "nonzero", "orphan", "pipes", "cancel"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                code = r"""
import os, pathlib, signal, subprocess, sys, time
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import server
from process_lifecycle import NativeProcesses
root = pathlib.Path(sys.argv[2])
mode = sys.argv[3]
sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
native = NativeProcesses()
before = native.read(sentinel.pid)
fixture = root / "git"
fixture.write_text(
    "#!" + sys.executable + "\n"
    "import os, subprocess, sys, time\n"
    "assert os.getpgrp() == " + str(os.getpgrp()) + "\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']"
    + (", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
       if mode not in ("pipes", "cancel") else "")
    + ")\n"
    "open(" + repr(str(root / "pid")) + ", 'w').write(str(child.pid))\n"
    "print('refs/heads/main', flush=True)\n"
    "sys.exit(" + ("7" if mode == "nonzero" else "0") + ")\n"
)
fixture.chmod(0o700)
started = time.monotonic()
real_select = server.selectors.DefaultSelector.select
def select(selector, timeout=None):
    ready = real_select(selector, timeout)
    if mode == "cancel" and ready:
        raise KeyboardInterrupt()
    return ready
try:
    with patch.object(server.shutil, "which", return_value=str(fixture)), \
         patch.object(server, "GIT_TIMEOUT", 2), \
         patch.object(server.selectors.DefaultSelector, "select", select):
        try:
            result = server._git(["symbolic-ref", "HEAD"])
            assert mode in ("normal", "orphan"), (mode, result)
        except server.ToolFailure:
            assert mode in ("nonzero", "pipes")
        except KeyboardInterrupt:
            assert mode == "cancel"
    assert time.monotonic() - started < 5.5
    after = native.read(sentinel.pid)
    assert after is not None and after.live and after.birth == before.birth
    child = native.read(int((root / "pid").read_text()))
    assert child is None or not child.live
finally:
    sentinel.kill()
    sentinel.wait(timeout=1)
"""
                result = subprocess.run(
                    [sys.executable, "-I", "-c", code, str(PACKAGE), directory, mode],
                    start_new_session=True,  # Test isolation only, outside the server.
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_reuse_and_protected_pids_never_signalled(self) -> None:
        """Changed birth cannot grant authority, even after preexisting PID reuse."""
        lifecycle = ProcessLifecycle.__new__(ProcessLifecycle)
        lifecycle.native = NativeProcesses()
        lifecycle.pgid = os.getpgrp()
        lifecycle.protected = {10: (1,)}
        lifecycle.owned = {20: (1,)}
        lifecycle.confirmed = True
        members = [
            Identity(10, (2,), 1, lifecycle.pgid, "R"),
            Identity(20, (2,), 1, lifecycle.pgid, "R"),
        ]
        with patch.object(
            lifecycle.native, "members", return_value=members
        ), patch.object(lifecycle.native, "send") as send:
            self.assertFalse(lifecycle.cleanup(lambda: None))
            send.assert_not_called()

    def test_signal_rechecks_birth_and_group(self) -> None:
        """Incorrect self identity must never authorize SIGKILL on the test runner."""
        native = NativeProcesses()
        current = native.read(os.getpid())
        assert current is not None
        self.assertFalse(
            native.send(
                Identity(current.pid, (0,), current.ppid, current.pgid, current.state),
                signal.SIGKILL,
            )
        )
        self.assertFalse(
            native.send(
                Identity(
                    current.pid,
                    current.birth,
                    current.ppid,
                    current.pgid + 1,
                    current.state,
                ),
                signal.SIGKILL,
            )
        )

    def test_observation_failure_is_closed(self) -> None:
        """Denied enumeration prevents launch; denied finalization prevents success."""
        with patch.object(NativeProcesses, "members", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                ProcessLifecycle()
        lifecycle = ProcessLifecycle()
        with patch.object(lifecycle.native, "members", side_effect=PermissionError):
            self.assertFalse(lifecycle.cleanup(lambda: None))


if __name__ == "__main__":
    unittest.main()
