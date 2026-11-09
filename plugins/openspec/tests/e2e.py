#!/usr/bin/env python3
"""Execute the installed demo with omitted overrides and offline backend fixtures."""

import json
import errno
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import yaml
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
from installed_fixture import (  # noqa: E402
    TERMINAL,
    settle_runs,
    runtime_binary,
    fixture_base,
    json_args,
)  # noqa: E402

PLAN = {
    "summary_markdown": "# Offline plan",
    "proposal_markdown": "# Proposal",
    "tasks_markdown": "- [ ] Test",
    "design_markdown": "# Design",
    "specs": [
        {
            "capability": "offline",
            "spec_markdown": (
                "## ADDED Requirements\n### Requirement: Offline test\nThe fixture"
                " SHALL pass.\n#### Scenario: Run\n- WHEN invoked\n- THEN succeed"
            ),
        }
    ],
}


@contextmanager
def disposable_directory() -> Iterator[str]:
    """Remove test state, allowing a bounded terminal-supervisor flush race."""
    directory = tempfile.mkdtemp(prefix="openspec-e2e-", dir=fixture_base())
    try:
        yield directory
    finally:
        # Terminal state can be visible before the detached supervisor exits.
        # Retry only concurrent directory writes, never hide permission/I/O errors.
        deadline = time.monotonic() + 5
        while True:
            try:
                shutil.rmtree(directory)
                break
            except FileNotFoundError:
                break
            except OSError as error:
                if error.errno != errno.ENOTEMPTY or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)


class InstalledWorkflowTests(unittest.TestCase):
    """Verify demo mapping, null handling and nested hints via real dispatch."""

    def test_demo_without_overrides(self) -> None:
        """Run demo actions without overrides, using fake gh/openspec CLIs."""
        binary = runtime_binary()
        self.assertIsNotNone(
            binary,
            "Install Transist 0.1.0 on PATH or set TRANSIST_BIN to its executable",
        )
        version = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertEqual(version.stdout.strip(), "transist 0.1.0")
        with disposable_directory() as temp, ExitStack() as cleanup:
            root = pathlib.Path(temp).resolve()
            home, workspace = root / "home", root / "workspace"
            source = root / "source"
            source.mkdir()
            demo = source / "demo.yaml"
            shutil.copy2(ROOT / "workflow.yaml", demo)
            shutil.copytree(ROOT / "skills", source / "skills")
            tools = workspace / "tools"
            (workspace / "openspec/changes").mkdir(parents=True)
            tools.mkdir()
            gh = tools / "gh"
            gh.write_text(
                "#!/usr/bin/env python3\nimport json, sys\nassert '--model' not in"
                " sys.argv and '--reasoning-effort' not in sys.argv\nplan ="
                f" {PLAN!r}\nverify = {{'passed': True, 'report_markdown': '# PASS',"
                " 'findings': []}\nprint(json.dumps(verify if 'VERIFY_FIXTURE' in '"
                " '.join(sys.argv) else plan))\n"
            )
            gh.chmod(0o755)
            validator = tools / "openspec"
            validator.write_text("#!/bin/sh\nexit 0\n")
            validator.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = str(tools) + os.pathsep + env["PATH"]
            env.pop("TRANSIST_HOME", None)
            env.pop("TRANSIST_TRUSTED_EXECUTION", None)

            def cli(*args: str) -> str:
                """Execute CLI commands in an isolated standalone home."""
                command = [binary, "--home", str(home), *json_args(args)]
                result = subprocess.run(
                    command,
                    cwd=workspace,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(
                    result.returncode, 0, f"{command}\n{result.stdout}\n{result.stderr}"
                )
                return result.stdout

            cli("home", "init", "--mode", "standalone")
            cleanup.callback(settle_runs, home, cli)
            package = source / "package"
            package.mkdir()
            manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
            for relative in manifest["package"]["files"]:
                target = package / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            # Keep the fake tools selected inside the sandboxed plugin, whose
            # environment is otherwise reconstructed by the detached runner.
            wrapper = package / "fixture_entrypoint.py"
            wrapper.write_text(
                "#!/usr/bin/env python3\nimport os, runpy\nfrom pathlib import Path\n"
                + f"os.environ['PATH'] = {str(tools) + os.pathsep + os.defpath!r}\n"
                + "runpy.run_path(str(Path(__file__).with_name('plugin.py')), "
                "run_name='__main__')\n"
            )
            wrapper.chmod(0o755)
            manifest["entrypoint"]["command"] = wrapper.name
            manifest["package"]["files"].append(wrapper.name)
            (package / "plugin.yaml").write_text(yaml.safe_dump(manifest))
            cli("plugins", "install", "--path", str(package / "plugin.yaml"))
            self.assertIn("0.1.0", cli("plugins", "inspect", "openspec"))
            cli(
                "workflows",
                "install",
                "--path",
                str(demo),
                "--approve",
            )
            inputs = [
                "--input",
                f"repo_path={workspace}",
                "--input",
                "change_name=offline",
                "--input",
                "plan_prompt=PLAN_FIXTURE",
                "--input",
                "verify_prompt=VERIFY_FIXTURE",
            ]
            ready = json.loads(
                cli("workflows", "check", "openspec_demo@0.1.0", *inputs, "--live")
            )
            self.assertEqual(ready["status"], "ready", ready)
            cli(
                "workflows",
                "run",
                "openspec_demo@0.1.0",
                "--run-ref",
                "openspec-offline",
                *inputs,
            )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                report = json.loads(cli("runs", "show", "openspec-offline"))
                if report["status"] in TERMINAL:
                    break
                time.sleep(0.05)
            self.assertEqual(report["status"], "success", json.dumps(report, indent=2))
            specs = list((home / "runs").rglob("*spec.md--*"))
            self.assertTrue(specs, "nested spec hints were not promoted")
            associations = list((home / "runs").rglob("*artifact-paths.json"))
            self.assertTrue(associations)
            mappings = [json.loads(path.read_text()) for path in associations]
            self.assertTrue(
                any("specs/offline/spec.md" in mapping.values() for mapping in mappings)
            )
            canonical_flow = {
                "schema": "transist.flow.v1",
                "id": "openspec_canonical_regression",
                "version": "0.1.0",
                "plugins": {"openspec": "0.1.0"},
                "workspace": {"path": str(workspace), "access": "full"},
                "network": {"mode": "none"},
                "steps": [
                    {
                        "id": "plan",
                        "uses": "process:openspec/generate_plan",
                        "prompt": "PLAN_FIXTURE",
                        "with": {
                            "change_name": "canonical",
                            "canonical_plan_dir": "plans/canonical",
                            "model": None,
                            "reasoning_effort": None,
                        },
                    }
                ],
            }
            flow_path = source / "canonical.yaml"
            flow_path.write_text(json.dumps(canonical_flow))
            cli("workflows", "install", "--path", str(flow_path), "--approve")
            cli(
                "workflows",
                "run",
                "openspec_canonical_regression@0.1.0",
                "--run-ref",
                "openspec-canonical",
            )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                report = json.loads(cli("runs", "show", "openspec-canonical"))
                if report["status"] in TERMINAL:
                    break
                time.sleep(0.05)
            self.assertEqual(report["status"], "success", json.dumps(report, indent=2))
            canonical_spec = workspace / "plans/canonical/specs/offline/spec.md"
            self.assertEqual(
                canonical_spec.read_text().strip(), PLAN["specs"][0]["spec_markdown"]
            )
            mappings = [
                json.loads(path.read_text())
                for path in (home / "runs").rglob("*artifact-paths.json")
            ]
            self.assertTrue(
                any(
                    "canonical/specs/offline/spec.md" in mapping.values()
                    for mapping in mappings
                )
            )


if __name__ == "__main__":
    unittest.main()
