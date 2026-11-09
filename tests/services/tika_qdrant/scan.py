"""Check externally stored logs without exposing fixture credentials to the host."""

import base64
import json
import sys
from pathlib import Path
from urllib.parse import quote


def scan() -> None:
    """Read logs on stdin and fail on raw, URL-encoded or Base64 credentials."""
    logs = sys.stdin.buffer.read()
    credentials = json.loads(Path("/assets/credentials.json").read_text())
    for value in credentials.values():
        for secret in (value, "Bearer " + value):
            variants = (
                secret.encode(),
                base64.b64encode(secret.encode()),
                quote(secret, safe="").encode(),
            )
            if any(variant in logs for variant in variants):
                raise RuntimeError("Credential leak detected in fixture logs")
    print("PASS: fixture logs contain no credential values or encodings")


if __name__ == "__main__":
    scan()
