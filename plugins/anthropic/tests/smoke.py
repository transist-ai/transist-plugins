#!/usr/bin/env python3
"""Credential-free selected-payload and real subprocess APP smoke checks."""

import json
import os
import pathlib
import re
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


def main() -> None:
    """Check manifest identity/payload, schemas, and APP errors without dispatch."""
    manifest = (ROOT / "plugin.yaml").read_text()
    for declaration in (
        "schema: transist.plugin.v1",
        "id: anthropic",
        "version: 0.1.0",
        "adapter: process",
        "default_provider_kind: external",
        "command: plugin.py",
        "- name: python3",
        "input_schema: schemas/chat_complete.input.schema.json",
        "output_schema: schemas/chat_complete.output.schema.json",
        "- name: ANTHROPIC_API_KEY",
        "required: true",
        "type: env",
        "bindings: {}",
    ):
        assert declaration in manifest, "manifest contract mismatch"
    selected = re.search(r"^  files: \[(.*)\]$", manifest, re.MULTILINE)
    assert selected is not None, "missing selected payload"
    assert [item.strip() for item in selected[1].split(",")] == [
        "plugin.py",
        "schemas/chat_complete.input.schema.json",
        "schemas/chat_complete.output.schema.json",
    ]
    assert os.access(ROOT / "plugin.py", os.X_OK), "entrypoint is not executable"
    schemas = {}
    for kind in ("input", "output"):
        schema = read_json(ROOT / f"schemas/chat_complete.{kind}.schema.json")
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        schemas[kind] = schema
    validate_json_schema(
        schemas["input"],
        {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]},
    )
    validate_json_schema(schemas["output"], {"response_text": "hello"})
    # Explicit /tmp storage: shared subprocess helpers use platform-default temps.
    with tempfile.TemporaryDirectory(prefix="anthropic-smoke-", dir="/tmp") as tmp:
        request = app_invoke_request("chat_complete", {"messages": []}, tmp)
        try:
            validate_json_schema(schemas["input"], request["input"])
        except AssertionError:
            pass
        else:
            raise AssertionError("invalid input unexpectedly matched schema")
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        env["TMPDIR"] = tmp
        payload = json.dumps(
            {"type": "app.handshake.request.v1", "protocol_version": "app.v1"}
        )
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "plugin.py")],
            input=payload + "\n" + json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0 and not result.stderr, "APP process failed"
        frames = [json.loads(line) for line in result.stdout.splitlines()]
        assert len(frames) == 2, "stdout must contain exactly two APP frames"
        handshake, response = assert_app_frames(frames, "anthropic")
        assert handshake["type"] == "app.handshake.response.v1"
        assert handshake["protocol_version"] == response["protocol_version"] == "app.v1"
        assert handshake["plugin"] == {"id": "anthropic", "version": "0.1.0"}
        assert handshake["adapter_kind"] == "process"
        capabilities = {
            "actions": ["chat_complete"],
            "supports_cancel": True,
            "supports_health": False,
            "supports_artifact_hints": True,
            "supports_evidence_hints": False,
        }
        assert handshake["capabilities"] == capabilities
        for name, enabled in capabilities.items():
            if name != "actions":
                assert f"{name}: {str(enabled).lower()}" in manifest
        assert response["status"] == "failed"
        assert response["invoke_id"] == request["invoke_id"]
        assert_canonical_error(response["error"], "validation_error")
        assert set(response["error"]) == {"code", "class", "retryability", "message"}
        assert response["output"] == response["metrics"] == {}
        assert response["artifact_hints"] == response["evidence_hints"] == []
        assert "usage" not in response
        # 0.12.0 readiness uses a system-only PATH (macOS may select Python 3.9).
        # Postponed annotations must let that probe import and handshake normally.
        probe = subprocess.run(
            [str(ROOT / "plugin.py")],
            input=payload + "\n" + json.dumps(request) + "\n",
            capture_output=True,
            text=True,
            env=env | {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            timeout=10,
        )
        assert (
            probe.returncode == 0 and not probe.stderr
        ), "system-PATH readiness regression"
        assert [json.loads(line) for line in probe.stdout.splitlines()] == frames
    print("anthropic smoke passed (payload, schemas, handshake, invalid-input APP)")


if __name__ == "__main__":
    main()
