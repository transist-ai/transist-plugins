#!/usr/bin/env python3
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
    print(f"github.cli live test skipped: {message}")
    raise SystemExit(0)


def command_ok(command):
    completed = subprocess.run(
        command, capture_output=True, text=True, cwd=ROOT.parents[1]
    )
    return completed.returncode == 0


def main():
    if shutil.which("gh") is None:
        skip("gh CLI not found in PATH")
    if not command_ok(["gh", "auth", "status"]):
        skip("gh auth status is not ready")
    if not command_ok(["gh", "repo", "view", "--json", "name"]):
        skip("current workspace is not ready for gh repo view")
    output_schema = read_json(ROOT / "schemas/pr_list.output.schema.json")
    invoke_request = app_invoke_request("pr_list", {"limit": 1})
    exit_code, frames, stderr = run_process_plugin_subprocess(
        ROOT, invoke_request, timeout=30
    )
    if exit_code != 0:
        raise AssertionError(f"plugin process exited with {exit_code}: {stderr}")
    _, result = assert_app_frames(frames, "github.cli")
    if result["status"] != "success":
        raise AssertionError(f"expected successful gh integration, got: {result}")
    validate_json_schema(output_schema, result["output"])
    print("github.cli live test passed")


if __name__ == "__main__":
    main()
