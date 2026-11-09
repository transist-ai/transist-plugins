"""Small subprocess/inspection driver regressions, independent of installed E2E."""

import base64
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import e2e_lifecycle as runner


class DriverTests(unittest.TestCase):
    """Verify explicit executable selection and safe bounded subprocess handling."""

    def test_fixture_shebang_resolves_virtualenv_alias(self) -> None:
        """System-only sandbox views must not need the test virtualenv's alias."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "python"
            target.write_text("fixture interpreter")
            alias = root / "virtualenv-python"
            alias.symlink_to(target)
            with mock.patch.object(sys, "executable", str(alias)):
                self.assertEqual(runner.fixture_interpreter(), target)

    def test_recipe_source_is_isolated_from_home_and_workspace(self) -> None:
        """A read-only recipe mount must not cover writable home staging/workspace."""
        with tempfile.TemporaryDirectory() as directory:
            driver = runner.Driver(Path(sys.executable), Path(directory))
            try:
                paths = (driver.home, driver.workspace, driver.source)
                for path in paths:
                    self.assertEqual(path.parent, driver.root)
                    for other in paths:
                        if path != other:
                            self.assertFalse(path.is_relative_to(other))
                staging = driver.home / ".transist-staging"
                self.assertFalse(staging.is_relative_to(driver.source))
                ready = subprocess.CompletedProcess([], 0, '{"status":"ready"}', "")
                with mock.patch.object(driver, "cli", return_value=ready) as cli:
                    name = driver.install_flow("success")
                flow = driver.source / f"{name}.yaml"
                self.assertTrue(flow.is_file())
                self.assertFalse((driver.root / flow.name).exists())
                cli.assert_any_call("workflows", "check", "--path", str(flow))
                self.assertEqual(
                    json.loads(flow.read_text())["workspace"]["path"],
                    str(driver.workspace),
                )
            finally:
                driver.close()

    def test_system_python_imports_selected_payload(self) -> None:
        """Import real helper/plugin on macOS preflight Python (3.9 on this host)."""
        interpreter = "/usr/bin/python3" if sys.platform == "darwin" else sys.executable
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    interpreter,
                    "-I",
                    "-B",
                    "-c",
                    "import json, runpy, sys; "
                    "assert sys.version_info >= (3, 9); "
                    "runpy.run_path(sys.argv[1]); runpy.run_path(sys.argv[2]); "
                    "print(json.dumps(list(sys.version_info[:2])))",
                    str(runner.PACKAGE / "process_lifecycle.py"),
                    str(runner.PACKAGE / "plugin.py"),
                ],
                cwd=directory,
                env={"PATH": "/usr/bin:/bin"},
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, "selected-payload import failed")
            self.assertEqual(result.stderr, "")
            self.assertGreaterEqual(tuple(json.loads(result.stdout)), (3, 9))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_missing_runtime_is_nonzero_without_fallback(self) -> None:
        """A missing explicit binary must fail before any home is created."""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(Path(runner.__file__)),
                    "--transist",
                    str(Path(directory) / "missing"),
                    "--fixture-base",
                    directory,
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("runtime executable missing", result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_subprocess_uses_explicit_home_and_isolated_environment(self) -> None:
        """The CLI child sees disposable authority and no host agent environment."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "fake-cli"
            binary.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "print(json.dumps({'argv': sys.argv[1:], "
                "'home': os.environ['HOME'], "
                "'inherited': 'HOST_AGENT_SECRET' in os.environ}))\n"
            )
            binary.chmod(0o755)
            driver = runner.Driver(binary, root)
            try:
                with mock.patch.dict("os.environ", {"HOST_AGENT_SECRET": "private"}):
                    report = json.loads(driver.cli("runs", "show", "fixture").stdout)
                self.assertEqual(
                    report["argv"][:4], ["--home", str(driver.home), "runs", "show"]
                )
                self.assertEqual(report["home"], str(root / "operator"))
                self.assertFalse(report["inherited"])
                self.assertFalse(driver.home.is_relative_to(driver.workspace))
            finally:
                driver.close()

    def test_secret_output_failure_never_prints_content(self) -> None:
        """Raw/base64 subprocess leakage fails without rendering either value."""
        with tempfile.TemporaryDirectory() as directory:
            driver = runner.Driver(Path(sys.executable), Path(directory))
            try:
                for text in (
                    runner.SENTINEL,
                    base64.b64encode(runner.SENTINEL.encode()).decode(),
                ):
                    result = subprocess.CompletedProcess([], 1, text, "")
                    captured = io.StringIO()
                    with mock.patch.object(subprocess, "run", return_value=result):
                        with contextlib.redirect_stderr(captured):
                            with self.assertRaisesRegex(AssertionError, "suppressed"):
                                driver.cli("runs", "show", "fixture")
                    self.assertEqual(captured.getvalue(), "")
            finally:
                driver.close()

    def test_cli_timeout_is_bounded_and_does_not_fallback(self) -> None:
        """A CLI timeout propagates after exactly one explicitly bounded dispatch."""
        with tempfile.TemporaryDirectory() as directory:
            driver = runner.Driver(Path(sys.executable), Path(directory))
            try:
                with mock.patch.object(
                    subprocess, "run", side_effect=subprocess.TimeoutExpired([], 20)
                ) as dispatch:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        driver.cli("runs", "show", "fixture")
                self.assertEqual(dispatch.call_count, 1)
                self.assertEqual(dispatch.call_args.kwargs["timeout"], 20)
            finally:
                driver.close()


if __name__ == "__main__":
    unittest.main()
