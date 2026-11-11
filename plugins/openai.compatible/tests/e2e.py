#!/usr/bin/env python3
"""Offline installed-workflow tests against a loopback Chat Completions server."""

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

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
from installed_fixture import (  # noqa: E402
    TERMINAL,
    settle_runs,
    runtime_binary,
    fixture_base,
    json_args,
)  # noqa: E402

CANARY = "openai-compatible-test-secret-canary"


class Handler(http.server.BaseHTTPRequestHandler):
    """Capture a request and return a selected deterministic provider response."""

    def do_POST(self):
        self.server.requests.append(
            (
                self.path,
                json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                self.headers.get("Authorization"),
            )
        )
        status, body, delay, truncate = self.server.reply
        if delay:
            import time

            time.sleep(delay)
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header(
                "Content-Length", str(len(body) + (100 if truncate else 0))
            )
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, *_args):
        """Silence server access logs, which could expose request metadata."""


def cli(home, *args):
    """Run the public CLI with a transient environment-bound dummy credential."""
    env = os.environ.copy()
    env["OPENAI_COMPATIBLE_API_KEY"] = CANARY
    result = subprocess.run(
        [runtime_binary(), "--home", str(home), *json_args(args)],
        cwd=home.parent / "workspace",
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert CANARY not in result.stdout + result.stderr
    return result


class InstalledWorkflowTests(unittest.TestCase):
    """Test actual APP dispatch and persistence through a fresh Transist home."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.requests = []
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.temp = tempfile.TemporaryDirectory(prefix="openai-e2e-", dir=fixture_base())
        cls.root = pathlib.Path(cls.temp.name).resolve()
        cls.home = cls.root / "home"
        cls.workspace = cls.root / "workspace"
        cls.workspace.mkdir()
        cls.run_command("home", "init", "--mode", "standalone")
        config = cls.home / "config.yaml"
        config.write_text(config.read_text().replace("process: false", "process: true"))
        cls.run_command("plugins", "install", "--path", str(ROOT / "plugin.yaml"))
        cls.source = cls.root / "source"
        cls.source.mkdir()
        cls.flow = cls.source / "workflow.yaml"
        cls.flow.write_text(
            f"""schema: transist.flow.v1
id: openai_compatible_offline
version: 0.1.0
plugins:
  openai.compatible: 0.1.0
network:
  mode: none
steps:
  - id: ask
    uses: process:openai.compatible/chat_complete
    network:
      mode: allowlist
      allow_hosts: [127.0.0.1]
      allow_ports: [{cls.server.server_port}]
    with:
      base_url: "http://127.0.0.1:{cls.server.server_port}/v1"
      model: test-model
      messages:
        - role: user
          content: sample request
      max_tokens: 16
      timeout_ms: 3000
      secret_refs: [OPENAI_COMPATIBLE_API_KEY]
"""
        )
        cls.run_command("workflows", "install", "--path", str(cls.flow), "--approve")

    @classmethod
    def tearDownClass(cls):
        settle_runs(cls.home, cls.run_command)
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    @classmethod
    def run_command(cls, *args):
        """Assert success without leaking a credential in test failures."""
        result = cli(cls.home, *args)
        if result.returncode:
            raise AssertionError(f"CLI failed: {args}: {result.stdout} {result.stderr}")
        return result.stdout

    def test_provider_cases(self):
        success = {
            "id": "r1",
            "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "check sensor"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        }
        scenarios = [
            ("success", 200, success, "success", None, True),
            ("no_usage", 200, success | {"usage": {}}, "success", None, False),
            (
                "unauthorized",
                401,
                {"error": "sensitive"},
                "failed",
                "auth_error",
                False,
            ),
            ("rate_limit", 429, {}, "failed", "rate_limit_error", False),
            ("upstream_error", 503, {}, "failed", "tool_execution_error", False),
            ("redirect", 302, {}, "failed", "tool_execution_error", False),
            ("malformed", 200, None, "failed", "protocol_error", False),
            (
                "invalid",
                200,
                {"choices": [{"message": {"role": "assistant", "content": None}}]},
                "failed",
                "protocol_error",
                False,
            ),
            ("truncated", 200, success, "failed", "protocol_error", False),
            ("timeout", 200, success, "failed", "timeout_error", False),
        ]
        for name, status, payload, expected_status, error, has_usage in scenarios:
            with self.subTest(name=name):
                body = b"{" if payload is None else json.dumps(payload).encode()
                self.server.reply = (
                    status,
                    body,
                    4 if name == "timeout" else 0,
                    name == "truncated",
                )
                run_id = f"e2e_{name}"
                result = cli(
                    self.home,
                    "workflows",
                    "run",
                    "openai_compatible_offline@0.1.0",
                    "--run-ref",
                    run_id,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                # Invocation failure is persisted by the detached runner;
                # inspect the run rather than the launch command's status.
                self.assertNotIn(CANARY, result.stdout + result.stderr)
                for _ in range(60):
                    inspected = cli(self.home, "runs", "show", run_id)
                    self.assertEqual(inspected.returncode, 0, inspected.stderr)
                    show = inspected.stdout
                    if json.loads(show).get("status") in TERMINAL:
                        break
                    time.sleep(0.2)
                self.assertNotIn(CANARY, show)
                self.assertEqual(json.loads(show)["status"], expected_status, show)
                if error:
                    self.assertIn(error, show, show)
                else:
                    self.assertIn("check sensor", show, show)
                if has_usage:
                    self.assertIn("input_tokens", show, show)
                self.assertEqual(self.server.requests[-1][0], "/v1/chat/completions")
                self.assertEqual(self.server.requests[-1][1]["model"], "test-model")
                self.assertEqual(self.server.requests[-1][2], f"Bearer {CANARY}")
                persisted = self.home / "runs" / f"run_{run_id}"
                if persisted.exists():
                    for file in persisted.rglob("*.json"):
                        self.assertNotIn(
                            CANARY, file.read_text(errors="replace"), str(file)
                        )

    def test_supplied_demo_required_inputs(self):
        """Dispatch the supplied demo with only model/prompt to a loopback fixture."""
        # Adapt only the provider boundary; keep the shipped bindings/defaults/pin.
        source = (ROOT / "workflow.yaml").read_text()
        source = source.replace("openai_compatible_demo", "openai_demo_fixture")
        source = source.replace(
            "https://openrouter.ai/api/v1",
            f"http://127.0.0.1:{self.server.server_port}/v1",
        )
        source = source.replace(
            "allow_hosts: [openrouter.ai]", "allow_hosts: [127.0.0.1]"
        )
        source = source.replace(
            "allow_ports: [443]", f"allow_ports: [{self.server.server_port}]"
        )
        flow = self.source / "supplied-demo.yaml"
        flow.write_text(source)
        self.run_command("workflows", "check", "--path", str(flow))
        self.run_command("workflows", "install", "--path", str(flow), "--approve")
        self.server.reply = (
            200,
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "supplied demo response",
                            }
                        }
                    ]
                }
            ).encode(),
            0,
            False,
        )
        run_id = "supplied_demo"
        self.run_command(
            "workflows",
            "run",
            "openai_demo_fixture@0.1.0",
            "--run-ref",
            run_id,
            "--input",
            "model=test-model",
            "--input",
            "prompt=required-only prompt",
        )
        for _ in range(60):
            show = self.run_command("runs", "show", run_id)
            if json.loads(show).get("status") in TERMINAL:
                break
            time.sleep(0.2)
        self.assertEqual(json.loads(show)["status"], "success", show)
        self.assertIn("supplied demo response", show, show)
        self.assertEqual(self.server.requests[-1][1]["model"], "test-model")
        self.assertEqual(self.server.requests[-1][1]["max_completion_tokens"], 256)
        self.assertNotIn("max_tokens", self.server.requests[-1][1])
        self.assertEqual(
            self.server.requests[-1][1]["messages"],
            [{"role": "user", "content": "required-only prompt"}],
        )


if __name__ == "__main__":
    unittest.main()
