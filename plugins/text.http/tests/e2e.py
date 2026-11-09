"""Install/run offline in a disposable standalone home using the supplied service."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from test_service import ROOT, service

sys.path.insert(0, str(ROOT.parents[1] / "tests"))
from installed_fixture import (  # noqa: E402
    TERMINAL,
    settle_runs,
    runtime_binary,
    fixture_base,
    json_args,
)  # noqa: E402


class RecordingHandler(service.Handler):
    """Record calls and optionally return invalid output to test host rejection."""

    calls: list[str] = []
    invalid = False

    def do_GET(self) -> None:
        """Record the adapter's bodyless GET handshake for network denial checks."""
        self.calls.append(self.path)
        super().do_GET()

    def dispatch(self) -> None:
        """Record service-bound requests while retaining the actual implementation."""
        self.calls.append(self.path)
        super().dispatch()

    def reply(self, status: int, body: dict[str, Any]) -> None:
        """Inject a schema-invalid successful output only in the negative test."""
        if self.invalid and body.get("type") == "app.invoke.result.v1":
            body["output"] = {"bytes": "invalid"}
        super().reply(status, body)


def main() -> None:
    """Exercise public lifecycle, installed workflow, network denial and validation."""
    binary = runtime_binary()
    if binary is None:
        raise RuntimeError("transist must be installed for E2E")
    with tempfile.TemporaryDirectory(
        prefix="text-http-e2e-", dir=fixture_base()
    ) as scratch, service.make_server(0, RecordingHandler) as server:
        # Runtime homes reject symlink traversal; /tmp is a symlink on macOS.
        base = Path(scratch).resolve()
        home = base / "home"
        workspace = base / "workspace"
        workspace.mkdir()
        package = base / "package"
        shutil.copytree(
            ROOT, package, ignore=shutil.ignore_patterns("tests", "__pycache__")
        )
        port = server.server_port
        manifest = package / "plugin.yaml"
        manifest.write_text(manifest.read_text().replace("18765", str(port)))
        workflow = package / "workflow.yaml"
        workflow.write_text(workflow.read_text().replace("18765", str(port)))
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        # Do not inherit provider credentials, proxies or existing runtime state.
        env = {"PATH": os.defpath, "HOME": str(base), "TRANSIST_HOME": str(home)}

        def cli(*args: str, success: bool = True) -> str:
            """Run a bounded local CLI command with isolated state and print results."""
            result = subprocess.run(
                [binary, "--home", str(home), *json_args(args)],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=workspace,
            )
            print("transist", " ".join(args), "exit", result.returncode)
            print(result.stdout, result.stderr)
            if success and result.returncode != 0:
                raise AssertionError("CLI command failed")
            return result.stdout + result.stderr

        def wait_result(run_id: str, status: str) -> dict[str, Any]:
            """Poll detached execution until its persisted step result is terminal."""
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = json.loads(cli("runs", "show", run_id))
                result = (
                    state.get("steps", {}).get("analyze_demo", {}).get("last_result")
                )
                if result and result["status"] == status:
                    if state["status"] in TERMINAL:
                        assert state["status"] == status, state
                        return result
                time.sleep(0.1)
            raise AssertionError(f"run {run_id} did not reach {status}")

        try:
            cli("home", "init", "--mode", "standalone")
            cli("plugins", "install", "--path", str(manifest))
            cli("plugins", "list")
            cli("plugins", "inspect", "text.http")
            cli("workflows", "check", "--path", str(workflow))
            cli("workflows", "install", "--path", str(workflow), "--approve")
            cli("workflows", "check", "text_http_demo@0.1.0")
            cli(
                "workflows",
                "run",
                "text_http_demo@0.1.0",
                "--run-ref",
                "text-http-success",
                "--local-only",
            )
            result = wait_result("text-http-success", "success")
            assert result["output"] == {
                "bytes": 15,
                "chars": 11,
                "lines": 2,
                "sha256": hashlib.sha256("Hello, 世界!\n".encode()).hexdigest(),
            }, result
            assert "/app/handshake" in RecordingHandler.calls
            assert "/app/health" in RecordingHandler.calls
            assert "/app/invoke" in RecordingHandler.calls
            bad_input = package / "bad-input.yaml"
            bad_input.write_text(
                workflow.read_text()
                .replace("text_http_demo", "text_http_bad_input")
                .replace("      maxBytes: 4096", '      path: "/etc/passwd"')
            )
            cli("workflows", "install", "--path", str(bad_input), "--approve")
            RecordingHandler.calls.clear()
            invalid_input = cli(
                "workflows",
                "check",
                "text_http_bad_input@0.1.0",
                success=False,
            )
            assert "input_schema" in invalid_input, invalid_input
            assert '"status": "failed"' in invalid_input, invalid_input
            assert not RecordingHandler.calls, RecordingHandler.calls
            byte_bound = package / "byte-bound.yaml"
            byte_bound.write_text(
                workflow.read_text()
                .replace("text_http_demo", "text_http_byte_bound")
                .replace('text: "Hello, 世界!\\n"', 'text: "é"')
                .replace("maxBytes: 4096", "maxBytes: 1")
            )
            cli("workflows", "install", "--path", str(byte_bound), "--approve")
            cli("workflows", "check", "text_http_byte_bound@0.1.0")
            RecordingHandler.calls.clear()
            cli(
                "workflows",
                "run",
                "text_http_byte_bound@0.1.0",
                "--run-ref",
                "text-http-byte-bound",
                success=False,
            )
            result = wait_result("text-http-byte-bound", "failed")
            assert "/app/invoke" in RecordingHandler.calls, RecordingHandler.calls
            assert result["error"]["code"] == "validation_error", result
            assert result["error"]["class"] == "validation", result
            assert result["error"]["retryability"] == "none", result
            assert result["output"] == {}, result
            denied = package / "denied.yaml"
            denied.write_text(
                workflow.read_text()
                .replace("text_http_demo", "text_http_denied")
                .replace("mode: loopback", "mode: none")
            )
            cli("workflows", "install", "--path", str(denied), "--approve")
            RecordingHandler.calls.clear()
            denial = cli(
                "workflows",
                "run",
                "text_http_denied@0.1.0",
                "--run-ref",
                "text-http-denied",
                success=False,
            )
            assert not RecordingHandler.calls, RecordingHandler.calls
            assert "none" in denial.lower(), denial
            RecordingHandler.invalid = True
            rejection = cli(
                "workflows",
                "run",
                "text_http_demo@0.1.0",
                "--run-ref",
                "text-http-invalid",
                success=False,
            )
            result = wait_result("text-http-invalid", "failed")
            assert result["error"]["code"] == "validation_error", (rejection, result)
            assert result["output"] == {}, result
            print(
                "PASS: installed workflow, local-only, "
                "canonical byte-bound failure, zero-call network denial, "
                "invalid output rejection"
            )
        finally:
            settle_runs(home, cli)
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    main()
