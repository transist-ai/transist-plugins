#!/usr/bin/env python3
"""
Offline smoke test for the ollama.cli plugin.

Builds the Go binary into a temp directory, creates a fake ollama executable
there, then exercises the full APP stdio protocol without any real Ollama
installation required.

Skipped (gracefully) when `go` is not found in PATH.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))

from support import (  # noqa: E402
    app_invoke_request,
    assert_app_frames,
    assert_canonical_error,
    read_json,
    validate_json_schema,
)

BINARY_NAME = "ollama.cli"


def skip(message):
    print(f"ollama.cli smoke test skipped: {message}")
    raise SystemExit(0)


def build_binary(target_dir: str) -> str:
    """Build the Go binary into target_dir and return its path."""
    binary_path = os.path.join(target_dir, BINARY_NAME)
    result = subprocess.run(
        ["sh", str(ROOT / "build.sh"), binary_path],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"plugin build failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return binary_path


def make_fake_ollama(target_dir, response="Hello from fake Ollama.", exit_code=0):
    """Write a shell script at target_dir/ollama with the given behaviour."""
    script = os.path.join(target_dir, "ollama")
    lines = ["#!/bin/sh\n", "cat > /dev/null\n"]  # consume stdin
    if exit_code != 0:
        lines.append('printf "%s\\n" "model not found" >&2\n')
        lines.append(f"exit {exit_code}\n")
    else:
        lines.append(f'printf "%s\\n" "{response}"\n')
    with open(script, "w") as fh:
        fh.writelines(lines)
    os.chmod(
        script,
        stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
    )
    return script


def run_plugin(
    binary_path: str,
    invoke_request: dict,
    env: dict[str, str] | None = None,
    timeout: float = 30,
) -> tuple[int, list[dict], str]:
    """Exchange APP frames and always reclaim the isolated outer fixture group."""
    handshake = {"type": "app.handshake.request.v1", "protocol_version": "app.v1"}
    payload = json.dumps(handshake) + "\n" + json.dumps(invoke_request) + "\n"
    process = subprocess.Popen(
        [binary_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,  # Isolated outer fixture, never plugin-owned escape.
    )
    try:
        stdout, stderr = process.communicate(payload, timeout=timeout)
        frames = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        return process.returncode, frames, stderr
    finally:
        # This group was created solely by the test harness. Kill it even when
        # the leader has exited: fixture descendants may still own the pipes.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        # An interrupted stdin flush may see EPIPE after kill;
                        # it must not skip closing other pipes or reaping.
                        pass
            process.wait(timeout=1)


def main():
    # Skip when Go is not available — this is still classified as a smoke test
    # because the Go binary is required infrastructure for the plugin.
    if shutil.which("go") is None:
        skip("go not found in PATH; install Go 1.22+ to run this test")

    # Required package files.
    for required in ["plugin.yaml", "go.mod", "workflow.yaml", "README.md"]:
        if not (ROOT / required).exists():
            raise AssertionError(f"missing required file: {required}")
    for schema in [
        "schemas/generate.input.schema.json",
        "schemas/generate.output.schema.json",
    ]:
        if not (ROOT / schema).exists():
            raise AssertionError(f"missing schema file: {schema}")

    output_schema = read_json(ROOT / "schemas/generate.output.schema.json")

    with tempfile.TemporaryDirectory(prefix="ollama-cli-smoke-", dir="/tmp") as tmpdir:
        binary_path = build_binary(tmpdir)
        make_fake_ollama(tmpdir)
        env = os.environ.copy()
        env["PATH"] = tmpdir + os.pathsep + env.get("PATH", "")

        # -----------------------------------------------------------------
        # 1. Happy path — inline prompt
        # -----------------------------------------------------------------
        invoke_request = app_invoke_request(
            "generate",
            {"prompt": "Say hello.", "model": "test-model"},
        )
        exit_code, frames, stderr = run_plugin(binary_path, invoke_request, env=env)
        if exit_code != 0 or stderr:
            raise AssertionError(
                f"happy path: unexpected exit={exit_code} stderr={stderr!r}"
            )
        _, result = assert_app_frames(frames, "ollama.cli")
        if result["status"] != "success":
            raise AssertionError(
                f"happy path: expected success, got {result['status']!r}"
            )
        validate_json_schema(output_schema, result["output"])
        if not result["output"]["ok"]:
            raise AssertionError("happy path: expected ok=true")
        if "Hello from fake Ollama" not in result["output"]["response_text"]:
            raise AssertionError(
                "happy path: unexpected response_text:"
                f" {result['output']['response_text']!r}"
            )
        if result["output"]["response_file"] != "":
            raise AssertionError("happy path: response_file must be empty string")
        if result["output"]["exit_code"] != 0:
            raise AssertionError("happy path: expected exit_code=0")
        progress_frames = [f for f in frames if f["type"] == "app.invoke.progress.v1"]
        if not progress_frames:
            raise AssertionError("happy path: expected at least one progress frame")
        if progress_frames[0].get("line_preview") != "Launching ollama...":
            raise AssertionError(
                "happy path: expected initial 'Launching ollama...' status frame"
            )

        # Compact has a distinct closed schema and complete authoritative text.
        make_fake_ollama(tmpdir, response="Complete Unicode é snowman ☃.")
        compact_request = app_invoke_request(
            "generate",
            {
                "prompt": "éé",
                "model": "test-model",
                "output_mode": "compact",
                "max_prompt_bytes": 4,
            },
            artifacts_dir=tmpdir,
        )
        _, compact_frames, compact_stderr = run_plugin(
            binary_path, compact_request, env=env
        )
        _, compact = assert_app_frames(compact_frames, "ollama.cli")
        validate_json_schema(output_schema, compact["output"])
        if (
            compact_stderr
            or compact["status"] != "success"
            or compact["output"]["response_text"] != "Complete Unicode é snowman ☃."
        ):
            raise AssertionError("compact must retain complete response_text")
        if "stdout" in compact["output"] or "stderr" in compact["output"]:
            raise AssertionError("compact must omit inline logs")
        names = [hint["name"] for hint in compact["artifact_hints"]]
        if len(names) != len(set(names)) or "response.txt" in names:
            raise AssertionError("compact must not duplicate raw logs or response")
        invalid = {**compact["output"], "stdout": ""}
        try:
            validate_json_schema(output_schema, invalid)
        except AssertionError:
            pass
        else:
            raise AssertionError("compact schema accepted empty-string log lie")

        # UTF-8 byte budget validation must precede lookup/launch.
        marker = pathlib.Path(tmpdir) / "launched"
        pathlib.Path(tmpdir, "ollama").write_text(
            f"#!/bin/sh\ntouch '{marker}'\nexit 0\n"
        )
        compact_request["input"]["max_prompt_bytes"] = 3
        _, rejected_frames, _ = run_plugin(binary_path, compact_request, env=env)
        _, rejected = assert_app_frames(rejected_frames, "ollama.cli")
        if rejected["status"] != "failed" or marker.exists():
            raise AssertionError("excessive Unicode prompt launched a provider")
        input_schema = read_json(ROOT / "schemas/generate.input.schema.json")
        for bad in (True, 0, -1, 1.5, None):
            try:
                validate_json_schema(
                    input_schema,
                    {"prompt": "task", "model": "m", "max_prompt_bytes": bad},
                )
            except AssertionError:
                pass
            else:
                raise AssertionError("schema accepted invalid byte budget")
        make_fake_ollama(tmpdir)

        # -----------------------------------------------------------------
        # 2. Happy path — prompt_file
        # -----------------------------------------------------------------
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, dir=tmpdir
        ) as fh:
            fh.write("Hello from prompt file.\n")
            prompt_file_path = fh.name
        try:
            file_request = app_invoke_request(
                "generate",
                {"prompt_file": prompt_file_path, "model": "test-model"},
            )
            exit_code, frames, stderr = run_plugin(binary_path, file_request, env=env)
            if exit_code != 0 or stderr:
                raise AssertionError(
                    f"prompt_file: unexpected exit={exit_code} stderr={stderr!r}"
                )
            _, file_result = assert_app_frames(frames, "ollama.cli")
            if file_result["status"] != "success":
                raise AssertionError(
                    f"prompt_file: expected success, got {file_result['status']!r}"
                )
            validate_json_schema(output_schema, file_result["output"])
        finally:
            os.unlink(prompt_file_path)

        # -----------------------------------------------------------------
        # 3. Validation error — both prompt and prompt_file provided
        # -----------------------------------------------------------------
        both_request = app_invoke_request(
            "generate",
            {"prompt": "inline", "prompt_file": "/tmp/whatever.txt", "model": "m"},
        )
        exit_code, frames, stderr = run_plugin(binary_path, both_request, env=env)
        if exit_code != 0 or stderr:
            raise AssertionError("validation (both): unexpected exit or stderr")
        _, val_result = assert_app_frames(frames, "ollama.cli")
        if val_result["status"] != "failed":
            raise AssertionError("validation (both): expected failed status")
        assert_canonical_error(
            val_result["error"],
            "validation_error",
            ["exactly one of prompt or prompt_file is required"],
        )

        # -----------------------------------------------------------------
        # 4. Validation error — missing model
        # -----------------------------------------------------------------
        no_model_request = app_invoke_request("generate", {"prompt": "hi"})
        exit_code, frames, stderr = run_plugin(binary_path, no_model_request, env=env)
        if exit_code != 0 or stderr:
            raise AssertionError("validation (no model): unexpected exit or stderr")
        _, no_model_result = assert_app_frames(frames, "ollama.cli")
        if no_model_result["status"] != "failed":
            raise AssertionError("validation (no model): expected failed status")
        assert_canonical_error(
            no_model_result["error"],
            "validation_error",
            ["model is required"],
        )

        # -----------------------------------------------------------------
        # 5. Configuration error — ollama not in PATH
        # -----------------------------------------------------------------
        no_ollama_env = os.environ.copy()
        no_ollama_env["PATH"] = "/nonexistent_dir_has_no_ollama"
        missing_request = app_invoke_request(
            "generate",
            {"prompt": "hello", "model": "test-model"},
        )
        exit_code, frames, stderr = run_plugin(
            binary_path, missing_request, env=no_ollama_env
        )
        if exit_code != 0 or stderr:
            raise AssertionError("config error: unexpected exit or stderr")
        _, missing_result = assert_app_frames(frames, "ollama.cli")
        assert_canonical_error(
            missing_result["error"],
            "configuration_error",
            ["ollama CLI not found in PATH"],
        )

        # -----------------------------------------------------------------
        # 6. Tool execution error — ollama exits non-zero
        # -----------------------------------------------------------------
        make_fake_ollama(tmpdir, exit_code=1)
        fail_request = app_invoke_request(
            "generate",
            {"prompt": "fail please", "model": "test-model"},
        )
        exit_code, frames, stderr = run_plugin(binary_path, fail_request, env=env)
        if exit_code != 0 or stderr:
            raise AssertionError("tool error: unexpected exit or stderr")
        _, fail_result = assert_app_frames(frames, "ollama.cli")
        if fail_result["status"] != "failed":
            raise AssertionError("tool error: expected failed status")
        assert_canonical_error(
            fail_result["error"],
            "tool_execution_error",
        )
        if fail_result["output"].get("ok"):
            raise AssertionError("tool error: expected ok=false")
        if fail_result["output"].get("exit_code") == 0:
            raise AssertionError("tool error: expected non-zero exit_code")
        validate_json_schema(output_schema, fail_result["output"])

    print("ollama.cli offline smoke test passed")


if __name__ == "__main__":
    main()
