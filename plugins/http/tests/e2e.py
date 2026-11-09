#!/usr/bin/env python3
"""Installed standalone workflow regression using only a local fake HTTP server."""

import hashlib
import http.server
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
from installed_fixture import (  # noqa: E402
    TERMINAL,
    settle_runs,
    runtime_binary,
    fixture_base,
    json_args,
)  # noqa: E402

DATA = bytes(range(256)) * 2048  # Binary and larger than the inline APP hint limit.


class Handler(http.server.BaseHTTPRequestHandler):
    """Serve byte-exact fixtures on loopback without provider calls."""

    def do_GET(self) -> None:
        """Return a bounded binary body or JSON response for request coverage."""
        data = DATA if self.path == "/binary" else b'{"ok":true}'
        self.send_response(200)
        self.send_header(
            "Content-Type",
            (
                "application/octet-stream"
                if self.path == "/binary"
                else "application/json"
            ),
        )
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        """Keep fixture access logs quiet."""


class InstalledWorkflowTests(unittest.TestCase):
    """Prove workspace bytes and promoted metadata survive staging cleanup."""

    def test_binary_download(self) -> None:
        """Install, check and execute both HTTP actions with a 0.1.0 CLI."""
        binary = runtime_binary()
        self.assertIsNotNone(
            binary,
            "Install Transist 0.1.0 on PATH or set TRANSIST_BIN to its executable",
        )
        version = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(version.returncode, 0, version.stderr)
        self.assertEqual(version.stdout.strip(), "transist 0.1.0")
        with tempfile.TemporaryDirectory(
            prefix="http-e2e-", dir=fixture_base()
        ) as temp:
            root = pathlib.Path(temp).resolve()
            home, workspace = root / "home", root / "workspace"
            workspace.mkdir()
            source = root / "source"
            source.mkdir()
            env = os.environ.copy()
            env.pop("TRANSIST_HOME", None)
            env.pop("TRANSIST_TRUSTED_EXECUTION", None)

            def cli(*args: str) -> str:
                """Execute a bounded CLI command against the disposable home."""
                command = [binary, "--home", str(home), *json_args(args)]
                result = subprocess.run(
                    command,
                    cwd=workspace,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(
                    result.returncode, 0, f"{command}\n{result.stdout}\n{result.stderr}"
                )
                return result.stdout

            cli("home", "init", "--mode", "standalone")
            cli("plugins", "install", "--path", str(ROOT / "plugin.yaml"))
            self.assertIn("0.1.0", cli("plugins", "inspect", "http"))
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                origin = f"http://127.0.0.1:{server.server_port}"
                digest = hashlib.sha256(DATA).hexdigest()
                flow = {
                    "schema": "transist.flow.v1",
                    "id": "http_binary_regression",
                    "version": "0.1.0",
                    "plugins": {"http": "0.1.0"},
                    "workspace": {"path": str(workspace), "access": "full"},
                    "network": {
                        "mode": "allowlist",
                        "allow_hosts": ["127.0.0.1"],
                        "allow_ports": [server.server_port],
                        "tls_required": False,
                    },
                    "steps": [
                        {
                            "id": "fetch",
                            "uses": "process:http/request",
                            "with": {"url": origin + "/json"},
                        },
                        {
                            "id": "download",
                            "uses": "process:http/download",
                            "with": {
                                "url": origin + "/binary",
                                "dest": "downloads/body.bin",
                                "sha256": digest,
                            },
                        },
                    ],
                }
                path = source / "workflow.yaml"
                path.write_text(json.dumps(flow))
                cli("workflows", "install", "--path", str(path), "--approve")
                readiness = json.loads(
                    cli(
                        "workflows",
                        "check",
                        "http_binary_regression@0.1.0",
                        "--live",
                    )
                )
                self.assertEqual(readiness["status"], "ready", readiness)
                cli(
                    "workflows",
                    "run",
                    "http_binary_regression@0.1.0",
                    "--run-ref",
                    "http-binary",
                )
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    report = json.loads(cli("runs", "show", "http-binary"))
                    if report["status"] in TERMINAL:
                        break
                    time.sleep(0.05)
                self.assertEqual(
                    report["status"], "success", json.dumps(report, indent=2)
                )
                saved = workspace / "downloads/body.bin"
                self.assertEqual(saved.read_bytes(), DATA)
                self.assertEqual(hashlib.sha256(saved.read_bytes()).hexdigest(), digest)
                metadata_files = list((home / "runs").rglob("000_download.json"))
                self.assertTrue(metadata_files, "download metadata was not promoted")
                metadata = json.loads(metadata_files[0].read_text())
                self.assertEqual(metadata["path"], str(saved))
                self.assertEqual(metadata["bytes"], len(DATA))
                self.assertEqual(metadata["sha256"], digest)
                staging = home / ".transist-staging"
                self.assertFalse(
                    any(staging.glob("invoke-*")), "invocation staging was not cleaned"
                )
            finally:
                settle_runs(home, cli)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
