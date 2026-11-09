"""Local regressions for the static CI runner; no plugin or provider execution."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_static  # noqa: E402


class StaticChecksTests(unittest.TestCase):
    """Check runner boundaries with fake tools and small disposable source fixtures."""

    def test_main_uses_only_static_tools(self) -> None:
        """The runner calls static tools, never plugin implementations or tests."""
        files = {
            "scripts/example.py",
            "plugins/example/main.go",
            ".github/workflows/plugin-ci.yml",
        }
        with mock.patch.object(
            check_static, "source_files", return_value=files
        ), mock.patch.object(check_static, "regular_file"), mock.patch.object(
            check_static, "check_repository", return_value=[]
        ), mock.patch.object(
            check_static, "run_check"
        ) as run, mock.patch.object(
            check_static, "check_go_format"
        ) as go, mock.patch.object(
            check_static, "scan_secrets"
        ) as secrets:
            self.assertEqual(check_static.main(), 0)
        self.assertEqual(
            [call.args[1][0] for call in run.call_args_list],
            [sys.executable, sys.executable, "actionlint"],
        )
        self.assertIn("black", run.call_args_list[0].args[1])
        self.assertIn("flake8", run.call_args_list[1].args[1])
        go.assert_called_once_with(check_static.ROOT, ["plugins/example/main.go"])
        secrets.assert_called_once_with(check_static.ROOT, sorted(files))

    def test_structure_failure_stops_tools(self) -> None:
        """An invalid source tree cannot pass or proceed to tool execution."""
        with mock.patch.object(
            check_static, "check_repository", return_value=["bad layout"]
        ), mock.patch.object(check_static, "run_check") as run:
            self.assertEqual(check_static.main(), 1)
        run.assert_not_called()

    def test_missing_tool_fails(self) -> None:
        """Missing static tool prerequisites are errors rather than silent skips."""
        with mock.patch.object(
            check_static, "check_repository", return_value=[]
        ), mock.patch.object(
            check_static, "source_files", return_value={"file.py"}
        ), mock.patch.object(
            check_static, "regular_file"
        ), mock.patch.object(
            check_static, "run_check", side_effect=FileNotFoundError("missing tool")
        ):
            self.assertEqual(check_static.main(), 1)

    def test_go_format_is_read_only_and_fails_on_output(self) -> None:
        """gofmt uses list-only mode, and formatting differences fail the gate."""
        with mock.patch.object(
            check_static.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "main.go\n"),
        ) as run:
            with self.assertRaisesRegex(ValueError, "need gofmt"):
                check_static.check_go_format(check_static.ROOT, ["main.go"])
        self.assertEqual(run.call_args.args[0], ["gofmt", "-l", "main.go"])

    def test_secret_scan_only_copies_source_and_redacts(self) -> None:
        """Ignored/private files are not copied; the transient tree is cleaned up."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            (root / "source.py").write_text("# source\n")
            (root / "private.key").write_text("not part of the file inventory")
            destinations = []

            def inspect(
                label: str, command: list[str], env: dict[str, str] | None = None
            ) -> None:
                """Observe the scanner input without starting a real scanner."""
                self.assertIn("--redact", command)
                self.assertIn("--ignore-gitleaks-allow", command)
                self.assertIn("--config", command)
                self.assertNotIn("GITLEAKS_CONFIG", env)
                self.assertNotIn("GITLEAKS_CONFIG_TOML", env)
                self.assertEqual(command[:2], ["gitleaks", "dir"])
                target = Path(command[-1])
                destinations.append(target)
                self.assertEqual(
                    [path.name for path in target.iterdir()], ["source.py"]
                )
                self.assertEqual((target / "source.py").read_text(), "# source\n")

            with mock.patch.dict(
                check_static.os.environ,
                {
                    "GITLEAKS_CONFIG": "local-config",
                    "GITLEAKS_CONFIG_TOML": "local-rules",
                },
            ), mock.patch.object(check_static, "run_check", side_effect=inspect):
                check_static.scan_secrets(root, ["source.py"])
            self.assertFalse(destinations[0].exists())
            self.assertTrue((root / "private.key").exists())

    def test_ci_contract_is_static_only(self) -> None:
        """Keep one short-lived, read-only job without test or runtime dependencies."""
        path = check_static.ROOT / ".github/workflows/plugin-ci.yml"
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["jobs"]), {"static"})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "true")
        self.assertEqual(workflow["jobs"]["static"]["timeout-minutes"], "5")
        self.assertNotIn("BLACK_CACHE_DIR", workflow["env"])
        self.assertIn("BLACK_CACHE_DIR", workflow["jobs"]["static"]["steps"][-1]["env"])
        commands = "\n".join(
            step.get("run", "") for step in workflow["jobs"]["static"]["steps"]
        )
        self.assertIn("scripts/check_static.py", commands)
        for forbidden in (
            "cargo",
            "unittest",
            "check_plugins.py",
            "e2e.py",
            "live.py",
            "go install",
            "bubblewrap",
            "export_public_snapshot.py",
        ):
            self.assertNotIn(forbidden, commands)
        self.assertFalse((path.parent / "plugin-live.yml").exists())


if __name__ == "__main__":
    unittest.main()
