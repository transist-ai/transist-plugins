#!/usr/bin/env python3
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))

from support import (  # noqa: E402
    app_invoke_request,
    assert_app_frames,
    read_json,
    run_process_plugin_subprocess,
    validate_json_schema,
)


def skip(message):
    print(f"codex.cli live test skipped: {message}")
    raise SystemExit(0)


def main():
    if shutil.which("codex") is None:
        skip("codex CLI not found in PATH")
    login_status = subprocess.run(
        ["codex", "login", "status"],
        capture_output=True,
        text=True,
        cwd=ROOT.parents[1],
    )
    if login_status.returncode != 0:
        skip("codex login status is not ready")
    output_schema = read_json(ROOT / "schemas/exec_task.output.schema.json")
    invoke_request = app_invoke_request(
        "exec_task",
        {"prompt": "Say hello in one short sentence.", "sandbox": "read-only"},
    )
    exit_code, frames, stderr = run_process_plugin_subprocess(
        ROOT, invoke_request, timeout=90
    )
    if exit_code != 0:
        raise AssertionError(f"plugin process exited with {exit_code}: {stderr}")
    _, result = assert_app_frames(frames, "codex.cli")
    if result["status"] != "success":
        combined = " ".join(
            [
                result.get("output", {}).get("stdout", ""),
                result.get("output", {}).get("stderr", ""),
                (result.get("error") or {}).get("message", ""),
            ]
        ).lower()
        if (
            "stream disconnected" in combined
            or "operation not permitted" in combined
            or "unable to open database" in combined
            or "failed to update path" in combined
        ):
            skip("codex runtime is present but this environment blocks a full live run")
        raise AssertionError(f"expected successful codex integration, got: {result}")
    validate_json_schema(output_schema, result["output"])

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write("Say hello from a live file-based Codex run.\n")
        prompt_path = handle.name
    invoke_request = app_invoke_request(
        "exec_task",
        {"prompt_file": prompt_path, "sandbox": "read-only"},
    )
    exit_code, frames, stderr = run_process_plugin_subprocess(
        ROOT, invoke_request, timeout=90
    )
    if exit_code != 0:
        raise AssertionError(f"plugin process exited with {exit_code}: {stderr}")
    _, file_result = assert_app_frames(frames, "codex.cli")
    if file_result["status"] != "success":
        raise AssertionError(
            f"expected successful codex prompt_file integration, got: {file_result}"
        )
    validate_json_schema(output_schema, file_result["output"])
    print("codex.cli live test passed")


if __name__ == "__main__":
    main()
