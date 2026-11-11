#!/usr/bin/env python3
"""Installed, offline subprocess lifecycle regressions; no provider accounts."""

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

import yaml

from installed_fixture import (
    TERMINAL,
    settle_runs,
    runtime_binary,
    fixture_base,
    json_args,
)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "installed_lifecycle_native", ROOT / "plugins/cursor.cli/process_lifecycle.py"
)
NATIVE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = NATIVE
SPEC.loader.exec_module(NATIVE)
PACKAGES = (
    "openspec",
    "github.copilot.cli",
    "opencode.cli",
    "codex.cli",
    "shell",
    "git.cli",
    "github.cli",
)
SELECTED = set(filter(None, os.environ.get("TRANSIST_TEST_PACKAGES", "").split(",")))
PACKAGES = tuple(name for name in PACKAGES if not SELECTED or name in SELECTED)
PLAN = {
    "summary_markdown": "# Offline plan",
    "proposal_markdown": "# Proposal",
    "tasks_markdown": "- [ ] Test lifecycle",
    "design_markdown": "# Design",
    "specs": [
        {
            "capability": "offline",
            "spec_markdown": (
                "## ADDED Requirements\n### Requirement: Lifecycle\n"
                "The fixture SHALL finish.\n#### Scenario: Execute\n"
                "- WHEN invoked\n- THEN finish without live descendants"
            ),
        }
    ],
}

# Executables inherit the runtime-owned group. Only this offline provider forks;
# it records fixture PID metadata, never credentials, arguments, or prompts.
PROVIDER = r"""
import json, os, signal, sys, time
from pathlib import Path
root = Path(os.environ['FIXTURE_ROOT'])
tool = Path(sys.argv[0]).name
mode = (root / 'mode').read_text().strip()
label = tool + ('-export' if tool == 'opencode' and 'export' in sys.argv else '')
if os.fork() == 0:
    if mode == 'resistant':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / (label + '.pid')).write_text(json.dumps([os.getpid(), os.getpgrp()]))
    while True:
        time.sleep(.05)
while not (root / (label + '.pid')).exists():
    time.sleep(.005)
if mode == 'cancel':
    time.sleep(120)
if tool == 'cursor-agent':
    print(json.dumps(PLAN), flush=True)
elif tool == 'openspec':
    print('validated', flush=True)
elif tool == 'gh':
    print('fixture response' if 'copilot' in sys.argv else '[]', flush=True)
elif tool == 'git':
    print('fixture-branch', flush=True)
elif tool == 'opencode':
    if 'export' in sys.argv:
        print(json.dumps({'info': {'id': 'fixture'}, 'messages': []}), flush=True)
    else:
        print(json.dumps({'type': 'text', 'sessionID': 'fixture',
                          'part': {'text': 'fixture response'}}), flush=True)
elif tool == 'codex':
    sys.stdin.read()
    if '--output-last-message' in sys.argv:
        Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(
            'fixture response')
    print('fixture response', flush=True)
else:
    print('fixture response', flush=True)
time.sleep(.1)
"""


class InstalledLifecycleTests(unittest.TestCase):
    """Run self-contained packages through the actual adapter and native sandbox."""

    def test_installed_wrappers(self) -> None:
        """Lingering inherited pipes and TERM-resistant children finish cleanly."""
        binary = runtime_binary()
        self.assertIsNotNone(binary, "Install Transist or set TRANSIST_BIN")
        with tempfile.TemporaryDirectory(
            prefix="plugin-installed-lifecycle-", dir=fixture_base()
        ) as directory:
            root = Path(directory).resolve()
            home, workspace, source = root / "home", root / "workspace", root / "source"
            workspace.mkdir()
            source.mkdir()
            tools = workspace / "tools"
            tools.mkdir()
            (workspace / "openspec/changes").mkdir(parents=True)
            environment = {
                "PATH": str(tools) + os.pathsep + os.environ.get("PATH", os.defpath),
                "HOME": str(root / "operator"),
                "TMPDIR": str(root),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            (root / "operator").mkdir()
            for tool in ("cursor-agent", "gh", "codex", "opencode", "git", "openspec"):
                path = tools / tool
                path.write_text(
                    f"#!/usr/bin/env python3\nPLAN = {PLAN!r}\n" + PROVIDER,
                    encoding="utf-8",
                )
                path.chmod(0o755)
            shell_provider = tools / "fixture-shell"
            shell_provider.write_text(
                "#!/usr/bin/env python3\n" + PROVIDER, encoding="utf-8"
            )
            shell_provider.chmod(0o755)

            def cli(*args: str) -> str:
                """Execute against only the disposable home with a fixed deadline."""
                result = subprocess.run(
                    [binary, "--home", str(home), *json_args(args)],
                    cwd=workspace,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            cli("home", "init", "--mode", "standalone")
            plugin_versions: dict[str, str] = {}
            try:
                for package in PACKAGES:
                    fixture_package = source / "packages" / package
                    shutil.copytree(ROOT / "plugins" / package, fixture_package)
                    manifest_path = fixture_package / "plugin.yaml"
                    manifest = yaml.safe_load(manifest_path.read_text())
                    plugin_versions[package] = manifest["version"]
                    # Native sandbox dispatch restores the detached runner's
                    # PATH. Set fixture-only environment in the SAME entrypoint
                    # process, without introducing a subprocess/session or
                    # changing the production plugin/helper bytes.
                    fixture_path = str(tools) + os.pathsep + os.defpath
                    (fixture_package / "fixture_entrypoint.py").write_text(
                        "#!/usr/bin/env python3\nimport os, runpy\n"
                        "from pathlib import Path\n"
                        f"os.environ['PATH'] = {fixture_path!r}\n"
                        f"os.environ['FIXTURE_ROOT'] = {str(workspace)!r}\n"
                        "runpy.run_path(str(Path(__file__).with_name('plugin.py')), "
                        "run_name='__main__')\n"
                    )
                    (fixture_package / "fixture_entrypoint.py").chmod(0o755)
                    manifest["entrypoint"]["command"] = "fixture_entrypoint.py"
                    manifest["package"]["files"].append("fixture_entrypoint.py")
                    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
                    cli(
                        "plugins",
                        "install",
                        "--path",
                        str(manifest_path),
                    )
                    self.assertIn(
                        plugin_versions[package], cli("plugins", "inspect", package)
                    )
                for package in PACKAGES:
                    for mode in ("pipes", "resistant"):
                        with self.subTest(package=package, mode=mode):
                            for marker in workspace.glob("*.pid"):
                                marker.unlink()
                            (workspace / "mode").write_text(mode)
                            action, inputs = self.action(package, shell_provider)
                            identity = package.replace(".", "-") + "-" + mode
                            flow = {
                                "schema": "transist.flow.v1",
                                "id": identity,
                                "version": "0.1.0",
                                "plugins": {package: plugin_versions[package]},
                                "workspace": {"path": str(workspace), "access": "full"},
                                "network": {"mode": "none"},
                                "steps": [
                                    {
                                        "id": "invoke",
                                        "uses": f"process:{package}/{action}",
                                        "with": inputs,
                                    }
                                ],
                            }
                            path = source / (identity + ".yaml")
                            path.write_text(json.dumps(flow))
                            cli("workflows", "check", "--path", str(path))
                            cli(
                                "workflows", "install", "--path", str(path), "--approve"
                            )
                            reference = identity + "@0.1.0"
                            ready = json.loads(
                                cli("workflows", "check", reference, "--live")
                            )
                            if (
                                ready.get("diagnostics", {})
                                .get("authorization", {})
                                .get("missing_capabilities", 0)
                            ):
                                cli("workflows", "authorize", reference, "--approve")
                                ready = json.loads(
                                    cli("workflows", "check", reference, "--live")
                                )
                            self.assertEqual(ready["status"], "ready", ready)
                            cli("workflows", "run", reference, "--run-ref", identity)
                            self.wait_success(cli, identity)
                            self.assert_children_stopped(workspace)
                for package in PACKAGES:
                    cli(
                        "plugins",
                        "uninstall",
                        package,
                        "--version",
                        plugin_versions[package],
                    )
            finally:
                settle_runs(home, cli)

    def wait_success(self, cli, run_id: str) -> None:
        """Poll persisted state, keeping failure diagnostics local to the fixture."""
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            report = json.loads(cli("runs", "show", run_id))
            if report.get("status") in TERMINAL:
                failures = [
                    {
                        "error": state.get("last_result", {}).get("error"),
                        "stderr": state.get("last_result", {}).get("stderr"),
                    }
                    for state in report.get("steps", {}).values()
                    if state.get("status") == "failed"
                ]
                self.assertEqual(report["status"], "success", json.dumps(failures))
                return
            time.sleep(0.05)
        self.fail("installed fixture did not become terminal")

    def assert_children_stopped(self, workspace: Path) -> None:
        """Check recorded fixture children, including the separate validator/export."""
        markers = list(workspace.glob("*.pid"))
        self.assertTrue(markers, "fake provider was not invoked")
        observer = NATIVE.NativeProcesses()
        for marker in markers:
            pid, _ = json.loads(marker.read_text())
            process = observer.read(pid)
            self.assertTrue(process is None or not process.live, "live fixture child")

    @staticmethod
    def action(package: str, shell_provider: Path) -> tuple[str, dict]:
        """Return the smallest schema-valid invocation for each command wrapper."""
        if package == "openspec":
            return "generate_plan", {
                "backend": "cursor",
                "change_name": "offline",
                "prompt": "fixture",
                "timeout_ms": 30000,
            }
        if package == "git.cli":
            return "current_branch", {}
        if package == "github.cli":
            return "pr_list", {"repo": "fixture/repository"}
        if package == "shell":
            return "exec", {"command": str(shell_provider)}
        return "exec_task", {"prompt": "fixture", "timeout_ms": 30000}


if __name__ == "__main__":
    unittest.main()
