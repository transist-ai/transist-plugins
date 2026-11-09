#!/usr/bin/env python3
import os
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
    print(f"cursor.cli live test skipped: {message}")
    raise SystemExit(0)


def main():
    if not os.environ.get("CURSOR_API_KEY"):
        skip("CURSOR_API_KEY is not set")
    binary = shutil.which("agent") or shutil.which("cursor-agent") or None
    if binary is None:
        env_path = pathlib.Path(
            str(pathlib.Path.home()) + "/.local/share/cursor-agent/versions"
        )
        if env_path.exists():
            versions = sorted(env_path.iterdir())
            if versions:
                candidate = versions[-1] / "cursor-agent"
                if candidate.exists():
                    binary = str(candidate)
        if binary is None:
            skip("Cursor Agent CLI not found in PATH")
    output_schema = read_json(ROOT / "schemas/exec_task.output.schema.json")
    invoke_request = app_invoke_request(
        "exec_task",
        {
            "prompt": "Say hello in one short sentence.",
            "mode": "ask",
            "force": False,
        },
    )
    exit_code, frames, stderr = run_process_plugin_subprocess(
        ROOT, invoke_request, timeout=120
    )
    if exit_code != 0:
        raise AssertionError(f"plugin process exited with {exit_code}: {stderr}")
    _, result = assert_app_frames(frames, "cursor.cli")
    if result["status"] != "success":
        combined = " ".join(
            [
                result.get("output", {}).get("stdout", ""),
                result.get("output", {}).get("stderr", ""),
                (result.get("error") or {}).get("message", ""),
            ]
        ).lower()
        if any(
            needle in combined
            for needle in [
                "api key",
                "unauthorized",
                "forbidden",
                "authentication",
                "login",
            ]
        ):
            skip(
                "Cursor Agent runtime is present but this environment is not"
                " authenticated"
            )
        raise AssertionError(f"expected successful Cursor integration, got: {result}")
    validate_json_schema(output_schema, result["output"])

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write("Say hello from a live file-based Cursor run.\n")
        prompt_path = handle.name
    invoke_request = app_invoke_request(
        "exec_task",
        {"prompt_file": prompt_path, "mode": "ask", "force": False},
    )
    exit_code, frames, stderr = run_process_plugin_subprocess(
        ROOT, invoke_request, timeout=120
    )
    if exit_code != 0:
        raise AssertionError(f"plugin process exited with {exit_code}: {stderr}")
    _, file_result = assert_app_frames(frames, "cursor.cli")
    if file_result["status"] != "success":
        raise AssertionError(
            f"expected successful Cursor prompt_file integration, got: {file_result}"
        )
    validate_json_schema(output_schema, file_result["output"])
    print("cursor.cli live test passed")


if __name__ == "__main__":
    main()
