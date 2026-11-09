#!/usr/bin/env python3
"""Installed 0.1.0 demo regressions with a mocked provider command boundary."""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack

import yaml

PLUGINS = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGINS.parent / "tests"))
from installed_fixture import (  # noqa: E402
    TERMINAL,
    settle_runs,
    runtime_binary,
    fixture_base,
    json_args,
)  # noqa: E402

WRAPPER = '''#!/usr/bin/env python3
"""Exercise the packaged action while replacing only the external CLI call."""
import json
import subprocess
import plugin
from pathlib import Path

commands = []

def fixture_command(
    request: dict, command: list, *args: object, **kwargs: object
) -> object:
    """Capture the provider command and return a deterministic local response."""
    commands.append(command)
    if plugin.PLUGIN_ID == "codex.cli":
        Path(request["paths"]["artifacts_dir"], "last_message.txt").write_text(
            "fixture response")
    if plugin.PLUGIN_ID == "github.copilot.cli":
        event = {"type": "assistant.message", "data": {"content": "fixture response"}}
        return subprocess.CompletedProcess(command, 0, json.dumps(event), "")
    completed = subprocess.CompletedProcess(command, 0, "fixture response", "")
    if plugin.PLUGIN_ID == "opencode.cli":
        return {"completed": completed, "response_text": "fixture response"}
    return completed

plugin.run_command = fixture_command
if plugin.PLUGIN_ID == "codex.cli":
    plugin.extract_codex_usage = lambda _started: None
if plugin.PLUGIN_ID == "github.copilot.cli":
    plugin.require_json_transport = lambda *args: None
original = plugin.handle_request

def fixture_request(request: dict) -> dict:
    """Return the real action result plus observable inputs and CLI arguments."""
    result = original(request)
    result["artifact_hints"].append({
        "name": "fixture.json",
        "contents": json.dumps({"input": request["input"], "commands": commands}),
    })
    return result

plugin.handle_request = fixture_request
raise SystemExit(plugin.main())
'''


class OptionalDemoTests(unittest.TestCase):
    """Validate real workflow binding, adapter schema validation and flag selection."""

    def cli(self, home: pathlib.Path, *args: str) -> str:
        """Run an installed CLI against an explicit disposable home, without egress."""
        env = os.environ.copy()
        env.pop("TRANSIST_HOME", None)
        result = subprocess.run(
            [runtime_binary(), "--home", str(home), *json_args(args)],
            cwd=home.parent / "workspace",
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def check_demo(self, name: str) -> None:
        """Install a provider-mocked payload and dispatch required/explicit inputs."""
        version = subprocess.run(
            [runtime_binary(), "--version"], capture_output=True, text=True, check=True
        )
        self.assertEqual(version.stdout.strip(), "transist 0.1.0")
        with tempfile.TemporaryDirectory(
            prefix="plugin-optional-e2e-", dir=fixture_base()
        ) as directory, ExitStack() as cleanup:
            root = pathlib.Path(directory).resolve()
            home = root / "home"
            workspace = root / "workspace"
            workspace.mkdir()
            source_dir = root / "source"
            source_dir.mkdir()
            package = root / "package"
            package.mkdir()
            source = PLUGINS / name
            manifest = yaml.safe_load((source / "plugin.yaml").read_text())
            for relative in manifest["package"]["files"]:
                target = package / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / relative, target)
            # Only the test wrapper substitutes provider transport. The public
            # action schemas, bindings, implementation and workflow are unchanged.
            manifest["entrypoint"]["command"] = "fixture.py"
            manifest["package"]["files"].append("fixture.py")
            manifest["prerequisites"] = {"binaries": [{"name": "python3"}]}
            (package / "plugin.yaml").write_text(yaml.safe_dump(manifest))
            wrapper = package / "fixture.py"
            wrapper.write_text(WRAPPER)
            wrapper.chmod(0o755)
            self.cli(home, "home", "init", "--mode", "standalone")
            cleanup.callback(settle_runs, home, lambda *args: self.cli(home, *args))
            self.cli(home, "plugins", "install", "--path", str(package / "plugin.yaml"))
            flow = yaml.safe_load((source / "workflow.yaml").read_text())
            flow["network"] = {"mode": "none"}
            path = source_dir / "workflow.yaml"
            path.write_text(yaml.safe_dump(flow))
            self.cli(home, "workflows", "check", "--path", str(path))
            self.cli(home, "workflows", "install", "--path", str(path), "--approve")
            for explicit in (False, True):
                run_id = "explicit" if explicit else "required_only"
                args = ["--input", "prompt=required prompt"]
                if explicit:
                    args.extend(
                        [
                            "--input",
                            "model=chosen-model",
                            "--input",
                            "reasoning_effort=high",
                        ]
                    )
                    if name == "opencode.cli":
                        args.extend(
                            ["--input", "variant=minimal", "--input", "agent=build"]
                        )
                self.cli(
                    home,
                    "workflows",
                    "run",
                    f"{flow['id']}@{flow['version']}",
                    "--run-ref",
                    run_id,
                    *args,
                )
                for _ in range(100):
                    show = self.cli(home, "runs", "show", run_id)
                    if json.loads(show).get("status") in TERMINAL:
                        break
                    time.sleep(0.1)
                self.assertEqual(json.loads(show)["status"], "success", show)
                files = list((home / "runs").rglob("*fixture.json"))
                fixture = next(
                    json.loads(file.read_text())
                    for file in files
                    if f"run_{run_id}" in str(file)
                )
                command = fixture["commands"][0]
                if explicit:
                    self.assertEqual(
                        command[command.index("--model") + 1], "chosen-model"
                    )
                    if name == "codex.cli":
                        self.assertIn("model_reasoning_effort=high", command)
                    elif name == "github.copilot.cli":
                        self.assertEqual(
                            command[command.index("--reasoning-effort") + 1], "high"
                        )
                    else:
                        self.assertEqual(
                            command[command.index("--variant") + 1], "minimal"
                        )
                        self.assertEqual(command[command.index("--agent") + 1], "build")
                else:
                    for key in ("model", "reasoning_effort", "variant", "agent"):
                        if key not in flow["steps"][0]["with"]:
                            continue
                        self.assertIsNone(fixture["input"][key])
                    self.assertFalse(
                        any(
                            flag in command
                            for flag in (
                                "--model",
                                "--reasoning-effort",
                                "--variant",
                                "--agent",
                                "-c",
                            )
                        )
                    )
            settle_runs(home, lambda *args: self.cli(home, *args))
            self.cli(
                home, "plugins", "uninstall", name, "--version", manifest["version"]
            )

    def test_codex(self) -> None:
        """Required-only Codex inputs omit model/effort flags; explicit options work."""
        self.check_demo("codex.cli")

    def test_copilot(self) -> None:
        """Copilot defaults omit model/effort flags; explicit options work."""
        self.check_demo("github.copilot.cli")

    def test_opencode(self) -> None:
        """OpenCode defaults omit flags and preserve variant precedence."""
        self.check_demo("opencode.cli")


if __name__ == "__main__":
    selected = set(
        filter(None, os.environ.get("TRANSIST_TEST_PACKAGES", "").split(","))
    )
    names = {
        "codex.cli": "test_codex",
        "github.copilot.cli": "test_copilot",
        "opencode.cli": "test_opencode",
    }
    methods = [
        "OptionalDemoTests." + method
        for name, method in names.items()
        if not selected or name in selected
    ]
    unittest.main(argv=[sys.argv[0], *methods])
