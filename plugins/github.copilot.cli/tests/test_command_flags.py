#!/usr/bin/env python3
"""Regression tests for the Copilot child-process permission flags."""

import importlib.util
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_plugin_module():
    """Load the package entrypoint without depending on repository test helpers."""
    specification = importlib.util.spec_from_file_location(
        "github_copilot_plugin", ROOT / "plugin.py"
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("could not load github.copilot.cli plugin module")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class GithubCopilotCliCommandFlagsTests(unittest.TestCase):
    """Verify explicit Copilot permissions remain narrower than --allow-all."""

    def test_exec_task_enables_tools_and_paths_without_allow_all(self):
        """Build the child command with permissions and no URL-expanding flag."""
        module = load_plugin_module()
        with tempfile.TemporaryDirectory(prefix="copilot-command-flags-") as temp_root:
            root = pathlib.Path(temp_root)
            runtime_env = {
                "COPILOT_HOME": str(root / "runtime" / "copilot"),
                "COPILOT_CONFIG_DIR": str(root / "runtime" / "copilot"),
                "COPILOT_SESSION_STATE_DIR": str(
                    root / "runtime" / "copilot" / "session-state"
                ),
                "XDG_CACHE_HOME": str(root / "runtime" / "copilot" / "cache"),
                "TMPDIR": str(root / "runtime" / "copilot" / "tmp"),
            }
            for directory in runtime_env.values():
                pathlib.Path(directory).mkdir(parents=True, exist_ok=True)
            workspace = root / "workspace"
            artifacts = root / "artifacts"
            workspace.mkdir()
            artifacts.mkdir()
            request = {
                "invoke_id": "command-flags",
                "action": "exec_task",
                "input": {"prompt": "Say hello.", "timeout_ms": 6000},
                "paths": {
                    "workspace_dir": str(workspace),
                    "artifacts_dir": str(artifacts),
                    "run_root": str(root / "run"),
                    "evidence_dir": str(root / "evidence"),
                },
            }
            captured = {}

            def capture_command(_request, command, *_args, **_kwargs):
                captured["command"] = command
                return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

            with mock.patch.dict(os.environ, runtime_env, clear=False):
                with mock.patch.object(
                    module, "run_command", side_effect=capture_command
                ):
                    with mock.patch.object(module, "emit_status_preview"):
                        result = module.handle_request(request)

        self.assertEqual(result["status"], "success")
        self.assertIn("--allow-all-tools", captured["command"])
        self.assertIn("--allow-all-paths", captured["command"])
        self.assertNotIn("--allow-all", captured["command"])


if __name__ == "__main__":
    unittest.main()
