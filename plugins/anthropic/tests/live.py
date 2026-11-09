#!/usr/bin/env python3
"""Manual-only, externally credentialed Anthropic smoke; never invoked by CI."""

import base64
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIPPED = "Anthropic live smoke skipped: set ANTHROPIC_API_KEY and ANTHROPIC_TEST_MODEL"
REQUIRED = "Anthropic live smoke requires ANTHROPIC_API_KEY and ANTHROPIC_TEST_MODEL"
FAILED = "Anthropic live smoke failed"
PASSED = "Anthropic live smoke passed (non-empty response)"


def main() -> int:
    """Send one bounded APP invocation; emit only fixed diagnostics and exit status."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    model = os.environ.get("ANTHROPIC_TEST_MODEL")
    if not key or not model:
        if os.environ.get("REQUIRE_LIVE_PROVIDER") == "1":
            print(REQUIRED, file=sys.stderr)
            return 1
        print(SKIPPED)
        return 0

    # No configuration files, endpoint overrides, model fallback or retries.
    request = {
        "type": "app.invoke.request.v1",
        "protocol_version": "app.v1",
        "invoke_id": "live-smoke",
        "action": "chat_complete",
        "input": {
            "base_url": "https://api.anthropic.com",
            "model": model,
            "messages": [{"role": "user", "content": "Reply with a brief greeting."}],
            "max_tokens": 16,
            "timeout_ms": 30000,
            "secret_refs": ["ANTHROPIC_API_KEY"],
        },
        "policy": {
            "network": {
                "mode": "allowlist",
                "allow_hosts": ["api.anthropic.com"],
                "allow_ports": [443],
                "tls_required": True,
            }
        },
    }
    try:
        frames = (
            json.dumps(
                {"type": "app.handshake.request.v1", "protocol_version": "app.v1"}
            )
            + "\n"
            + json.dumps(request)
            + "\n"
        )
        env = os.environ.copy()
        env.pop("ANTHROPIC_TEST_MODEL", None)
        env.pop("REQUIRE_LIVE_PROVIDER", None)
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "plugin.py")],
            input=frames,
            capture_output=True,
            text=True,
            env=env,
            timeout=45,
        )
        # Even reflected/encoded credentials are rejected without displaying bytes.
        variants = {key}
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            encoded = encode(key.encode("utf-8")).decode("ascii")
            variants.update((encoded, encoded.rstrip("=")))
        diagnostics = result.stdout + result.stderr
        if (
            result.returncode
            or result.stderr
            or any(value in diagnostics for value in variants)
        ):
            raise ValueError
        parsed = [json.loads(line) for line in result.stdout.splitlines()]
        decoded = json.dumps(parsed, ensure_ascii=False)
        if any(value in decoded for value in variants):
            raise ValueError
        if len(parsed) != 2:
            raise ValueError
        handshake, response = parsed
        if (
            handshake.get("type") != "app.handshake.response.v1"
            or handshake.get("protocol_version") != "app.v1"
            or handshake.get("plugin") != {"id": "anthropic", "version": "0.1.0"}
            or response.get("type") != "app.invoke.result.v1"
            or response.get("protocol_version") != "app.v1"
            or response.get("invoke_id") != "live-smoke"
            or response.get("status") != "success"
            or not isinstance(response.get("output"), dict)
        ):
            raise ValueError
        text = response["output"].get("response_text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError
    except Exception:
        # Includes timeout output, JSON errors, OS errors and malformed APP shapes.
        # Never include exceptions, APP error codes, model output or stderr.
        print(FAILED, file=sys.stderr)
        return 1
    print(PASSED)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
