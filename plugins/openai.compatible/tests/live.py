#!/usr/bin/env python3
"""Manual-only credentialed OpenRouter interoperability smoke (never PR CI)."""

import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    """Send one bounded non-streaming model call and assert usable output."""
    key = os.environ.get("OPENROUTER_KEY_PERSONAL")
    model = os.environ.get("OPENROUTER_MODEL")
    if not key or not model:
        if os.environ.get("REQUIRE_LIVE_PROVIDER") == "1":
            raise SystemExit(
                "Live release check requires OPENROUTER_KEY_PERSONAL and"
                " OPENROUTER_MODEL"
            )
        print("Live check skipped: set OPENROUTER_KEY_PERSONAL and OPENROUTER_MODEL")
        return 0
    env = os.environ.copy()
    env.pop("OPENROUTER_KEY_PERSONAL", None)
    env["OPENAI_COMPATIBLE_API_KEY"] = key
    request = {
        "type": "app.invoke.request.v1",
        "protocol_version": "app.v1",
        "invoke_id": "live-smoke",
        "action": "chat_complete",
        "input": {
            "base_url": "https://openrouter.ai/api/v1",
            "model": model,
            "messages": [{"role": "user", "content": "Reply with a brief greeting."}],
            "max_tokens": 16,
            "timeout_ms": 30000,
            "secret_refs": ["OPENAI_COMPATIBLE_API_KEY"],
        },
        "policy": {
            "network": {
                "mode": "allowlist",
                "allow_hosts": ["openrouter.ai"],
                "allow_ports": [443],
                "tls_required": True,
            }
        },
    }
    frames = (
        json.dumps({"type": "app.handshake.request.v1", "protocol_version": "app.v1"})
        + "\n"
        + json.dumps(request)
        + "\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", str(ROOT / "plugin.py")],
        input=frames,
        capture_output=True,
        text=True,
        env=env,
        timeout=45,
    )
    if key in result.stdout + result.stderr:
        raise AssertionError("Credential appeared in subprocess diagnostics")
    if result.returncode:
        raise AssertionError("Plugin exited without an APP result")
    response = json.loads(result.stdout.splitlines()[-1])
    if (
        response.get("status") != "success"
        or not response.get("output", {}).get("response_text", "").strip()
    ):
        detail = (
            response.get("error", {}).get("code", "unusable output")
            if isinstance(response.get("error"), dict)
            else "unusable output"
        )
        raise AssertionError(f"Live provider returned {detail}")
    print("OpenRouter live smoke passed (non-empty response)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
