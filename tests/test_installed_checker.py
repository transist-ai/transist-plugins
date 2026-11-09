"""Explicit installed inventory, prerequisite and fail-closed runner regressions."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

from installed_fixture import fixture_base, runtime_binary
import e2e_gate_decision_contract as gate_fixture
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_installed as checker  # noqa: E402
from docker_endpoint import local_docker_host  # noqa: E402


class InstalledCheckerTests(unittest.TestCase):
    """Reject implicit binaries, missing fixtures, duplicates and suite omissions."""

    def test_inventory_is_complete_and_unique(self) -> None:
        """Every deterministic E2E and document/lifecycle runner is explicit."""
        actual = {
            path.relative_to(ROOT).as_posix()
            for path in ROOT.glob("plugins/*/tests/e2e*.py")
        }
        actual.add("tests/e2e_process_lifecycle.py")
        actual.add("tests/e2e_document_actions.py")
        actual.add("tests/e2e_review_actions.py")
        actual.add("tests/e2e_gate_decision_contract.py")
        inventory = {runner.path for runner in checker.RUNNERS}
        self.assertLessEqual(actual, inventory)
        self.assertEqual(len(inventory), len(checker.RUNNERS))
        self.assertEqual(len({r.name for r in checker.RUNNERS}), len(checker.RUNNERS))
        self.assertIn("document-actions", checker.PACKAGES)
        self.assertEqual(
            [r.name for r in checker.select(["review-actions"])], ["review-actions"]
        )
        for package in ("git.cli", "github.cli"):
            self.assertIn("review-actions", [r.name for r in checker.select([package])])
        self.assertEqual(
            [r.name for r in checker.select(["reply-publication"])],
            ["reply-publication"],
        )
        self.assertIn("reply-publication", [r.name for r in checker.select([])])
        self.assertIn(
            "reply-publication", [r.name for r in checker.select(["github.cli"])]
        )
        self.assertEqual(
            [r.name for r in checker.select(["gate-contract"])], ["gate-contract"]
        )
        self.assertIn("gate-contract", [r.name for r in checker.select([])])
        for package in (
            "shell",
            "codex.cli",
            "cursor.cli",
            "github.copilot.cli",
            "opencode.cli",
            "openspec",
        ):
            self.assertIn("gate-contract", [r.name for r in checker.select([package])])

    def test_repeat_selection_deduplicates_and_unknown_fails(self) -> None:
        """Selection is stable, checked and deduplicated across shared runners."""
        once = checker.select(["codex.cli", "github.copilot.cli"])
        repeated = checker.select(["codex.cli", "codex.cli", "github.copilot.cli"])
        self.assertEqual(once, repeated)
        self.assertEqual(sum(r.name == "optional-cli" for r in once), 1)
        with self.assertRaisesRegex(ValueError, "unknown package"):
            checker.select(["does-not-exist"])

    def test_gate_fixture_covers_every_selected_binding(self) -> None:
        """The finite gate runner cannot omit either Cursor action or a package."""
        packages = (
            "codex.cli",
            "cursor.cli",
            "github.copilot.cli",
            "opencode.cli",
            "openspec",
            "shell",
        )
        expected = set()
        for package in packages:
            manifest = yaml.safe_load(
                (ROOT / "plugins" / package / "plugin.yaml").read_text()
            )
            expected.update(
                (package, action["name"])
                for action in manifest["actions"]
                if "transist_gate_decision_v1" in action["bindings"]
            )
        self.assertEqual(set(gate_fixture.PACKAGES), expected)
        self.assertEqual(len(gate_fixture.PACKAGES), len(expected))

    def test_missing_environment_never_uses_path(self) -> None:
        """Runner helpers fail with missing environment, even if PATH has binaries."""
        with mock.patch.dict(os.environ, {}, clear=True):
            for function in (runtime_binary, fixture_base):
                with self.assertRaises(RuntimeError):
                    function()
            with self.assertRaises(RuntimeError):
                runtime_binary(admin=True)

    def test_binary_identity_and_absolute_requirement(self) -> None:
        """Absent, nonexecutable, relative, wrong-version and failed binaries fail."""
        for raw in ("transist", "/missing-transist"):
            with self.assertRaises(ValueError):
                checker.executable(raw, "transist")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tool"
            path.write_text("#!/bin/sh\nexit 0\n")
            with self.assertRaises(ValueError):
                checker.executable(str(path), "transist")
            path.chmod(0o755)
            for code, version in (
                (0, "transist 0.12.0"),
                (0, "transist 0.11.0"),
                (0, "transist v0.1.0"),
                (0, "transist 0.1.0 extra"),
                (0, ""),
                (1, "transist 0.1.0"),
            ):
                with mock.patch.object(
                    checker.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], code, version, ""),
                ):
                    with self.assertRaises(ValueError):
                        checker.executable(str(path), "transist")

    def test_public_release_binary_identities(self) -> None:
        """Accept exact successful runtime/admin release output, including newline."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tool"
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
            for name in ("transist", "transist-admin"):
                with self.subTest(name=name), mock.patch.object(
                    checker.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, f"{name} 0.1.0\n", ""
                    ),
                ):
                    self.assertEqual(
                        checker.executable(str(path), name), path.resolve()
                    )

    def test_entrypoint_errors_and_skips_are_failures(self) -> None:
        """The top-level checker preserves failed and skipped children as failures."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            args = [
                "check_installed.py",
                "--transist-bin",
                "/fixture/transist",
                "--admin-bin",
                "/fixture/admin",
                "--fixture-base",
                str(base),
                "--package",
                "document-actions",
            ]
            for child in (
                subprocess.CompletedProcess([], 1, "", "error"),
                subprocess.CompletedProcess([], 0, "", "OK (skipped=1)"),
                subprocess.CompletedProcess([], 0, "", "Ran 0 tests"),
            ):
                with mock.patch.object(sys, "argv", args), mock.patch.object(
                    checker, "executable", return_value=Path(sys.executable)
                ), mock.patch.object(checker.subprocess, "run", return_value=child):
                    self.assertEqual(checker.main(), 1)
            with mock.patch.object(
                sys, "argv", [*args[:-1], "review-actions"]
            ), mock.patch.object(
                checker, "executable", return_value=Path(sys.executable)
            ), mock.patch.object(
                checker.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "", "SKIP:"),
            ):
                self.assertEqual(checker.main(), 1)
            with mock.patch.object(
                sys, "argv", [*args[:6], "/missing-fixture", *args[7:]]
            ), mock.patch.object(
                checker, "executable", return_value=Path(sys.executable)
            ):
                self.assertEqual(checker.main(), 1)

    def test_every_command_uses_explicit_binary_where_required(self) -> None:
        """Special runner CLIs receive current binaries, storage and lifecycle mode."""
        for runner in checker.RUNNERS:
            command = checker.command(
                runner, Path("/fixture/transist"), Path("/fixture/base")
            )
            self.assertEqual(command[:2], [sys.executable, "-B"])
            if runner.name in ("cursor-lifecycle", "lean-storage", "git-mcp"):
                self.assertIn("/fixture/transist", command)
            if runner.name == "cursor-lifecycle":
                self.assertIn("--candidate-lifecycle", command)

    def test_docker_endpoint_only_exports_local_socket(self) -> None:
        """Resolve the public parent context, retaining no credentials in children."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "docker.sock"
            with mock.patch.object(
                Path, "stat", return_value=SimpleNamespace(st_mode=stat.S_IFSOCK)
            ):
                endpoint = "unix://" + str(path)
                with mock.patch.dict(
                    os.environ, {"DOCKER_HOST": endpoint}
                ), mock.patch.object(checker.subprocess, "run") as command:
                    self.assertEqual(local_docker_host(), endpoint)
                    command.assert_not_called()
                with mock.patch.dict(
                    os.environ,
                    {"HOME": temporary, "PRIVATE_TOKEN": "not-forwarded"},
                    clear=True,
                ), mock.patch.object(
                    checker.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, endpoint + "\n", ""
                    ),
                ) as command:
                    self.assertEqual(local_docker_host(), endpoint)
                    self.assertEqual(
                        command.call_args.kwargs["env"], {"HOME": temporary}
                    )
                for invalid in (
                    "",
                    "tcp://example.invalid:2375",
                    endpoint + "\n",
                    endpoint + "?x=1",
                ):
                    with self.subTest(endpoint=invalid), mock.patch.dict(
                        os.environ, {"DOCKER_HOST": invalid}
                    ):
                        with self.assertRaises((ValueError, OSError)):
                            local_docker_host()
            path.write_text("not a socket")
            with mock.patch.dict(os.environ, {"DOCKER_HOST": endpoint}):
                with self.assertRaisesRegex(ValueError, "not a UNIX socket"):
                    local_docker_host()
            path.unlink()
            with mock.patch.dict(os.environ, {"DOCKER_HOST": endpoint}):
                with self.assertRaises(FileNotFoundError):
                    local_docker_host()

    def test_tika_endpoint_resolves_before_home_isolation(self) -> None:
        """The checker exports only the endpoint, not parent Docker/login state."""
        with tempfile.TemporaryDirectory() as temporary:
            args = [
                "check_installed.py",
                "--transist-bin",
                "/fixture/transist",
                "--admin-bin",
                "/fixture/admin",
                "--fixture-base",
                temporary,
                "--package",
                "tika.http",
            ]
            parent_home = "/fixture/parent-context"

            def resolve() -> str:
                """Observe the parent context at the exact pre-isolation boundary."""
                self.assertEqual(os.environ["HOME"], parent_home)
                return "unix:///fixture/docker.sock"

            with mock.patch.dict(
                os.environ,
                {
                    "HOME": parent_home,
                    "DOCKER_CONFIG": "/fixture/docker-config",
                    "PRIVATE_TOKEN": "never-forward",
                    "PYTHONPATH": "/fixture/modules",
                },
            ), mock.patch.object(sys, "argv", args), mock.patch.object(
                checker, "executable", return_value=Path(sys.executable)
            ), mock.patch.object(
                checker, "local_docker_host", side_effect=resolve
            ) as endpoint, mock.patch.object(
                checker.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "", "Ran 1 test\nOK\n"),
            ) as child:
                self.assertEqual(checker.main(), 0)
                endpoint.assert_called_once_with()
                env = child.call_args.kwargs["env"]
                self.assertEqual(env["DOCKER_HOST"], "unix:///fixture/docker.sock")
                self.assertNotEqual(env["HOME"], parent_home)
                for key in ("DOCKER_CONFIG", "PRIVATE_TOKEN", "PYTHONPATH"):
                    self.assertNotIn(key, env)


if __name__ == "__main__":
    unittest.main()
