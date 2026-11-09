"""Offline-runner capture and complete-discovery checks without package execution."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_plugins  # noqa: E402


class CheckPluginsTests(unittest.TestCase):
    """Failures retain captured diagnostics and do not omit later units or smokes."""

    def test_unit_failure_still_runs_both_smokes_and_next_package(self) -> None:
        """A unit failure is reported without skipping compact smoke or later suites."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                Path("plugins/git.cli/plugin.yaml"),
                Path("plugins/text.http/plugin.yaml"),
            ]
            for path in paths:
                tests = root / path.parent / "tests"
                tests.mkdir(parents=True)
                (tests / "test_fixture.py").touch()
                if path.parent.name == "git.cli":
                    (tests / "smoke.py").touch()
            results = [
                subprocess.CompletedProcess(
                    [], code, f"output-{index}\n", f"error-{index}\n"
                )
                for index, code in enumerate((1, 0, 0, 0))
            ]
            output, errors = io.StringIO(), io.StringIO()
            with mock.patch.object(check_plugins, "ROOT", root), mock.patch.object(
                check_plugins, "candidate_files", return_value=paths
            ), mock.patch.object(
                check_plugins.shutil, "which", return_value="go"
            ), mock.patch.object(
                check_plugins.subprocess, "run", side_effect=results
            ) as run, redirect_stdout(
                output
            ), redirect_stderr(
                errors
            ):
                self.assertEqual(check_plugins.main(), 1)
            self.assertEqual(run.call_count, 4)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertIn("discover", commands[0])
            self.assertTrue(commands[1][-1].endswith("smoke.py"))
            self.assertEqual(commands[2][-1], "--compact")
            self.assertIn("text.http", commands[3][commands[3].index("-s") + 1])
            for index, call in enumerate(run.call_args_list):
                self.assertIn(f"output-{index}", output.getvalue())
                self.assertIn(f"error-{index}", errors.getvalue())
                self.assertTrue(call.kwargs["capture_output"])
                self.assertTrue(call.kwargs["text"])
                self.assertEqual(call.kwargs["timeout"], 300)
            self.assertIn("1 command(s)", errors.getvalue())

    def test_timeout_preserves_partial_streams_and_failure(self) -> None:
        """Retain bounded-command timeout diagnostics in either subprocess encoding."""
        output, errors = io.StringIO(), io.StringIO()
        timeout = subprocess.TimeoutExpired(
            ["fixture"], 300, output=b"partial output\n", stderr=b"partial error\n"
        )
        with mock.patch.object(
            check_plugins.subprocess, "run", side_effect=timeout
        ), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(check_plugins.run_offline_command(["fixture"], {}), 1)
        self.assertIn("partial output", output.getvalue())
        self.assertIn("partial error", errors.getvalue())
        self.assertIn("300-second deadline", errors.getvalue())

    def test_empty_public_inventory_fails(self) -> None:
        """An empty source inventory cannot be reported as successful acceptance."""
        with mock.patch.object(
            check_plugins, "candidate_files", return_value=[]
        ), mock.patch.object(
            check_plugins.shutil, "which", return_value="go"
        ), redirect_stderr(
            io.StringIO()
        ):
            self.assertEqual(check_plugins.main(), 1)

    def test_http_test_dependencies_match_selected_package_pins(self) -> None:
        """Root offline HTTP dependencies match the public Tika runtime versions."""
        root = Path(__file__).resolve().parents[1]
        root_requirements = (root / "tests/requirements.txt").read_text().splitlines()
        package_requirements = (
            (root / "plugins/tika.http/requirements.lock").read_text().splitlines()
        )
        for name in ("httpx", "starlette", "uvicorn"):
            expected = next(
                line.split()[0]
                for line in package_requirements
                if line.startswith(name + "==")
            )
            self.assertIn(expected, root_requirements)


if __name__ == "__main__":
    unittest.main()
