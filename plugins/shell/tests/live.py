#!/usr/bin/env python3
import pathlib
import shutil
import sys

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
    print(f"shell live test skipped: {message}")
    raise SystemExit(0)


def main():
    python_bin = shutil.which("python3") or shutil.which("python")
    if python_bin is None:
        skip("python3 or python not found in PATH")

    output_schema = read_json(ROOT / "schemas/exec.output.schema.json")
    invoke_request = app_invoke_request(
        "exec",
        {
            "command": python_bin,
            "args": ["-c", "print('hello from shell live test')"],
        },
    )
    exit_code, frames, stderr = run_process_plugin_subprocess(
        ROOT, invoke_request, timeout=20
    )
    if exit_code != 0:
        raise AssertionError(f"plugin process exited with {exit_code}: {stderr}")
    _, result = assert_app_frames(frames, "shell")
    if result["status"] != "success":
        raise AssertionError(f"expected success result, got: {result}")
    validate_json_schema(output_schema, result["output"])
    if result["output"]["stdout"].strip() != "hello from shell live test":
        raise AssertionError(f"unexpected live stdout: {result['output']['stdout']!r}")
    print("shell live test passed")


if __name__ == "__main__":
    main()
