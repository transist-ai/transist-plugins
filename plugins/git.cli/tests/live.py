#!/usr/bin/env python3
import os
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))

from support import (  # noqa: E402
    app_invoke_request,
    assert_app_frames,
    read_json,
    run_process_plugin_subprocess,
)
from smoke import validate_json_schema  # noqa: E402


def skip(message):
    print(f"git.cli live test skipped: {message}")
    raise SystemExit(0)


def main():
    if shutil.which("git") is None:
        skip("git CLI not found in PATH")
    worktree_check = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        cwd=os.getcwd(),
    )
    if worktree_check.returncode != 0 or worktree_check.stdout.strip() != "true":
        skip("not inside a git worktree")
    output_schema = read_json(ROOT / "schemas/current_branch.output.schema.json")
    invoke_request = app_invoke_request("current_branch", {})
    invoke_request["paths"]["workspace_dir"] = os.getcwd()
    exit_code, frames, stderr = run_process_plugin_subprocess(
        ROOT, invoke_request, timeout=20
    )
    if exit_code != 0:
        raise AssertionError(f"plugin process exited with {exit_code}: {stderr}")
    _, result = assert_app_frames(frames, "git.cli")
    if result["status"] != "success":
        raise AssertionError(f"expected success result, got: {result}")
    validate_json_schema(output_schema, result["output"])
    print("git.cli live test passed")


if __name__ == "__main__":
    main()
