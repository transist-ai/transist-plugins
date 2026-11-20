"""Check teardown of isolated smoke fixtures on deadlines and protocol errors."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
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


class BuildTests(unittest.TestCase):
    """Ensure release payloads are immutable and executable after copying."""

    def test_existing_output_is_preserved(self) -> None:
        """A preexisting output fails before compilation and remains unchanged."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            output = Path(directory) / "ollama.cli"
            output.write_bytes(b"existing")
            result = subprocess.run(
                ["sh", str(PACKAGE / "build.sh"), str(output)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing to overwrite", result.stderr)
            self.assertEqual(output.read_bytes(), b"existing")

    def test_published_binary_survives_package_copy(self) -> None:
        """The real build must handshake before and after byte-preserving install."""
        with tempfile.TemporaryDirectory(prefix="ollama-build-test-", dir="/tmp") as d:
            root = Path(d)
            binary = Path(SMOKE.build_binary(d))
            installed = root / "installed" / "bin" / "ollama.cli"
            installed.parent.mkdir(parents=True)
            shutil.copyfile(binary, installed)
            installed.chmod(binary.stat().st_mode)
            self.assertEqual(binary.read_bytes(), installed.read_bytes())
            manifest = next(
                line.removeprefix("version: ")
                for line in (PACKAGE / "plugin.yaml").read_text().splitlines()
                if line.startswith("version: ")
            )
            for payload in (binary, installed):
                with self.subTest(payload=payload.name, installed=payload == installed):
                    if sys.platform == "darwin":
                        signature = subprocess.run(
                            ["/usr/bin/codesign", "--verify", "--strict", str(payload)],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        self.assertEqual(signature.returncode, 0, signature.stderr)
                    result = subprocess.run(
                        [str(payload)],
                        input=json.dumps(
                            {
                                "type": "app.handshake.request.v1",
                                "protocol_version": "app.v1",
                            }
                        )
                        + "\n",
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    # EOF after discovery is intentional: no model is invoked.
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(result.stderr, "")
                    response = json.loads(result.stdout)
                    self.assertEqual(response["type"], "app.handshake.response.v1")
                    self.assertEqual(response["protocol_version"], "app.v1")
                    self.assertEqual(
                        response["plugin"], {"id": "ollama.cli", "version": manifest}
                    )


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
