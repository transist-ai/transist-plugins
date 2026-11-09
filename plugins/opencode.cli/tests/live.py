#!/usr/bin/env python3
import pathlib
import shutil
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
    print(f"opencode.cli live test skipped: {message}")
    raise SystemExit(0)


def combined_failure_text(result):
    return " ".join(
        [
            result.get("output", {}).get("stdout", ""),
            result.get("output", {}).get("stderr", ""),
            (result.get("error") or {}).get("message", ""),
        ]
    ).lower()


def should_skip_live_failure(result):
    combined = combined_failure_text(result)
    return any(
        needle in combined
        for needle in [
            "wal_checkpoint(passive)",
            "failed to run the query",
            "unable to open database",
            "api key",
            "provider",
            "configure",
            "authentication",
            "login",
            "unauthorized",
            "forbidden",
        ]
    )


def main():
    if shutil.which("opencode") is None:
        skip("opencode CLI not found in PATH")
    output_schema = read_json(ROOT / "schemas/exec_task.output.schema.json")
    invoke_request = app_invoke_request(
        "exec_task",
        {"prompt": "Say hello in one short sentence."},
    )
    exit_code, frames, stderr = run_process_plugin_subprocess(
        ROOT, invoke_request, timeout=90
    )
    if exit_code != 0:
        raise AssertionError(f"plugin process exited with {exit_code}: {stderr}")
    _, result = assert_app_frames(frames, "opencode.cli")
    if result["status"] != "success":
        if should_skip_live_failure(result):
            skip(
                "opencode runtime is present but this environment blocks a full"
                " live run"
            )
        raise AssertionError(f"expected successful opencode integration, got: {result}")
    validate_json_schema(output_schema, result["output"])

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write("Say hello from a live file-based OpenCode run.\n")
        prompt_path = handle.name
    invoke_request = app_invoke_request(
        "exec_task",
        {"prompt_file": prompt_path},
    )
    exit_code, frames, stderr = run_process_plugin_subprocess(
        ROOT, invoke_request, timeout=90
    )
    if exit_code != 0:
        raise AssertionError(f"plugin process exited with {exit_code}: {stderr}")
    _, file_result = assert_app_frames(frames, "opencode.cli")
    if file_result["status"] != "success":
        if should_skip_live_failure(file_result):
            skip("opencode file-based runtime is blocked in this environment")
        raise AssertionError(
            f"expected successful opencode prompt_file integration, got: {file_result}"
        )
    validate_json_schema(output_schema, file_result["output"])
    print("opencode.cli live test passed")


if __name__ == "__main__":
    main()
