#!/usr/bin/env python3
"""Exercise real Copilot startup with an isolated runtime temporary directory."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile

EXPECTED_REPLY = "Copilot TMPDIR startup OK."


def main() -> None:
    """Invoke the plugin and its real Copilot child with private runtime paths."""
    with tempfile.TemporaryDirectory(prefix="github-copilot-live-") as root_text:
        root = pathlib.Path(root_text)
        workspace = root / "workspace"
        tmpdir = root / "private" / "copilot" / "tmp"
        config_dir = root / "private" / "copilot" / "config"
        cache_dir = root / "private" / "copilot" / "cache"
        session_dir = root / "private" / "copilot" / "session-state"
        for directory in (workspace, tmpdir, config_dir, cache_dir, session_dir):
            directory.mkdir(parents=True, exist_ok=True)

        environment = os.environ.copy()
        environment.update(
            {
                "COPILOT_HOME": str(config_dir),
                "COPILOT_CONFIG_DIR": str(config_dir),
                "COPILOT_SESSION_STATE_DIR": str(session_dir),
                "TMPDIR": str(tmpdir),
                "XDG_CACHE_HOME": str(cache_dir),
            }
        )
        run_root = root / "run"
        artifacts_dir = run_root / "artifacts"
        evidence_dir = run_root / "evidence"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        handshake = {"type": "app.handshake.request.v1", "protocol_version": "app.v1"}
        invoke = {
            "type": "app.invoke.request.v1",
            "protocol_version": "app.v1",
            "invoke_id": "live-copilot-private-tmpdir",
            "action": "exec_task",
            "input": {
                "prompt": (
                    "Reply with exactly this text, no extra commentary:"
                    f" {EXPECTED_REPLY}"
                ),
                "response_file": "run://copilot-response.md",
                "timeout_ms": 180000,
            },
            "paths": {
                "workspace_dir": str(workspace),
                "run_root": str(run_root),
                "artifacts_dir": str(artifacts_dir),
                "evidence_dir": str(evidence_dir),
            },
        }
        process = subprocess.Popen(
            [
                sys.executable,
                str(pathlib.Path(__file__).resolve().parents[1] / "plugin.py"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=workspace,
            env=environment,
        )
        payload = f"{json.dumps(handshake)}\n{json.dumps(invoke)}\n"
        stdout, stderr = process.communicate(payload, timeout=210)
        combined_output = f"{stdout}\n{stderr}"
        if "Directory does not exist or cannot be accessed" in combined_output:
            raise AssertionError("gh copilot rejected the private TMPDIR")
        if process.returncode != 0:
            raise AssertionError(
                f"github.copilot.cli failed with exit code {process.returncode}:"
                f" {combined_output}"
            )
        frames = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        result = frames[-1] if frames else {}
        if result.get("status") != "success":
            raise AssertionError(f"Copilot startup did not succeed: {result}")
        if EXPECTED_REPLY not in result.get("output", {}).get(
            "canonical_response_text", ""
        ):
            raise AssertionError(
                "gh copilot did not produce the expected startup response"
            )


if __name__ == "__main__":
    main()
