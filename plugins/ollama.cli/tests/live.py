#!/usr/bin/env python3
"""
Live integration test for the ollama.cli plugin.

Requires:
  - ollama installed and in PATH
  - at least one model pulled (ollama list)
  - the plugin binary built: go build -o ./bin/ollama.cli ./cmd/plugin

Skipped gracefully when any prerequisite is absent.
"""
import json
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
    validate_json_schema,
)


def skip(message):
    print(f"ollama.cli live test skipped: {message}")
    raise SystemExit(0)


def run_plugin(binary_path, invoke_request, timeout=120):
    """Run the compiled plugin binary with the APP stdio protocol."""
    handshake = {"type": "app.handshake.request.v1", "protocol_version": "app.v1"}
    payload = json.dumps(handshake) + "\n" + json.dumps(invoke_request) + "\n"
    process = subprocess.Popen(
        [binary_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate(payload, timeout=timeout)
    frames = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    return process.returncode, frames, stderr


def find_or_build_binary():
    """Return path to the plugin binary, building it if necessary."""
    candidate = ROOT / "bin" / BINARY_NAME
    if candidate.exists():
        return str(candidate)
    go_bin = shutil.which("go")
    if go_bin is None:
        skip(
            "ollama.cli binary not built and go not found in PATH; "
            "run: go build -o ./bin/ollama.cli ./cmd/plugin"
        )
    result = subprocess.run(
        ["go", "build", "-o", str(candidate), "./cmd/plugin"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(f"go build failed:\n{result.stderr}")
    return str(candidate)


BINARY_NAME = "ollama.cli"


def main():
    if shutil.which("ollama") is None:
        skip("ollama not found in PATH")

    binary_path = find_or_build_binary()

    # Discover available models.
    list_result = subprocess.run(
        ["ollama", "list"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if list_result.returncode != 0:
        skip("ollama list failed — is the Ollama server running?")
    lines = list_result.stdout.strip().splitlines()
    if len(lines) < 2:
        skip("no ollama models available; run: ollama pull qwen3:8b")

    # Pick the first available model (first column of the first data row).
    model = lines[1].split()[0]

    output_schema = read_json(ROOT / "schemas/generate.output.schema.json")
    invoke_request = app_invoke_request(
        "generate",
        {
            "prompt": "Reply with exactly one word: ready",
            "model": model,
            "timeout_seconds": 300,
        },
    )

    exit_code, frames, stderr = run_plugin(binary_path, invoke_request, timeout=360)
    if exit_code != 0:
        raise AssertionError(f"plugin process exited with {exit_code}: {stderr}")

    _, result = assert_app_frames(frames, "ollama.cli")
    if result["status"] != "success":
        raise AssertionError(f"expected success, got: {result['status']!r}")
    validate_json_schema(output_schema, result["output"])
    if not result["output"]["ok"]:
        raise AssertionError("expected ok=true for real ollama invocation")
    if not result["output"]["response_text"]:
        raise AssertionError("expected non-empty response_text")

    print(
        "ollama.cli live test passed "
        f"(model={model!r}, response={result['output']['response_text']!r})"
    )


if __name__ == "__main__":
    main()
