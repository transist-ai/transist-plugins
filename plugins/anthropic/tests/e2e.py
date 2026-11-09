#!/usr/bin/env python3
"""Real loopback HTTP, installed workflows, trust lifecycle and secret confinement.

Requires Transist/admin 0.1.0 and a working platform sandbox; never skips.
All disposable homes, workflow packages, signing keys and provider state use /tmp.
"""

import base64
import copy
import hashlib
import http.server
import json
import os
import pathlib
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass, field
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
from support import (  # noqa: E402
    app_invoke_request,
    assert_app_frames,
    assert_canonical_error,
)
from installed_fixture import (  # noqa: E402
    TERMINAL,
    settle_runs,
    runtime_binary,
    fixture_base,
)  # noqa: E402

CANARY = "???~~~anthropic-e2e-private-canary-xy"
VARIANTS = {CANARY.encode()}
for encoder in (base64.b64encode, base64.urlsafe_b64encode):
    encoded = encoder(CANARY.encode())
    VARIANTS.update((encoded, encoded.rstrip(b"=")))
SUCCESS = {
    "type": "message",
    "role": "assistant",
    "id": "msg_native_123",
    "model": "native-model",
    "stop_reason": "end_turn",
    "content": [{"type": "text", "text": "check "}, {"type": "text", "text": "sensor"}],
    "usage": {"input_tokens": 2, "output_tokens": 3},
}
HISTORY = [
    {"role": "user", "content": "  first café\n"},
    {"role": "assistant", "content": "previous answer"},
    {"role": "user", "content": "follow-up ☃"},
    {"role": "user", "content": " preserve consecutive turns "},
]


def assert_secret_free(data: bytes | str) -> None:
    """Fail with a fixed diagnostic if raw or encoded credential bytes escaped."""
    raw = data.encode() if isinstance(data, str) else data
    if any(variant in raw for variant in VARIANTS):
        raise AssertionError("credential canary escaped confinement")


def scan_home(home: pathlib.Path) -> None:
    """Scan every regular disposable-home file, including non-JSON binary files."""
    count = 0
    for path in home.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert_secret_free(path.read_bytes())
            count += 1
    assert count, "secret scan found no persisted files"


@dataclass
class Reply:
    """One immutable-per-request HTTP fixture plus synchronization events."""

    status: int = 200
    body: bytes = field(default_factory=lambda: json.dumps(SUCCESS).encode())
    headers: dict[str, str] = field(
        default_factory=lambda: {"request-id": "req_native_123"}
    )
    mode: str = "normal"
    received: threading.Event = field(default_factory=threading.Event)
    closed: threading.Event = field(default_factory=threading.Event)


class Server(http.server.ThreadingHTTPServer):
    """Silent concurrent fixture server retaining only safe request observations."""

    daemon_threads = True
    block_on_close = False

    def __init__(self) -> None:
        """Listen only on literal loopback and initialize synchronized fixture state."""
        super().__init__(("127.0.0.1", 0), Handler)
        self.reply = Reply()
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.stopping = threading.Event()

    def handle_error(self, _request: Any, _address: Any) -> None:
        """Keep server exceptions silent; request assertions detect broken fixtures."""
        self.stopping.set()


class Handler(http.server.BaseHTTPRequestHandler):
    """Validate authentication in memory and simulate bounded native HTTP replies."""

    def do_POST(self) -> None:
        """Record exact non-secret request bytes and deliver the selected fixture."""
        server = self.server
        body = self.rfile.read(int(self.headers["Content-Length"]))
        assert_secret_free(body)
        observation = {
            "method": self.command,
            "path": self.path,
            "body": body,
            "auth_ok": self.headers.get("Authorization") == f"Bearer {CANARY}",
            "version_ok": self.headers.get("anthropic-version") == "2023-06-01",
            "content_type_ok": self.headers.get("Content-Type") == "application/json",
            "accept_ok": self.headers.get("Accept") == "application/json",
            "encoding_ok": self.headers.get("Accept-Encoding") == "identity",
            "no_extra_auth": self.headers.get("x-api-key") is None,
            "no_beta": self.headers.get("anthropic-beta") is None,
            "received_at": time.monotonic(),
        }
        with server.lock:
            reply = server.reply
            server.requests.append(observation)
        reply.received.set()
        try:
            if reply.mode == "wait":
                # Observe peer closure rather than sleeping after the caller exits.
                until = time.monotonic() + 10
                while time.monotonic() < until and not server.stopping.wait(0.02):
                    if select.select([self.connection], [], [], 0)[0]:
                        if not self.connection.recv(1, socket.MSG_PEEK):
                            reply.closed.set()
                            return
                return
            length = len(reply.body) + (100 if reply.mode == "truncate" else 0)
            headers = (
                f"HTTP/1.1 {reply.status} Fixture\r\n"
                "Content-Type: application/json\r\nConnection: close\r\n"
            )
            if reply.mode != "overflow_eof":
                headers += f"Content-Length: {length}\r\n"
            headers += (
                "".join(f"{key}: {value}\r\n" for key, value in reply.headers.items())
                + "\r\n"
            )
            if reply.mode == "slow_headers":
                self.trickle(headers.encode(), reply)
            else:
                self.connection.sendall(headers.encode())
            if reply.mode == "slow_body":
                self.trickle(reply.body, reply)
            else:
                self.connection.sendall(reply.body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            reply.closed.set()

    def trickle(self, data: bytes, reply: Reply) -> None:
        """Keep individual reads active while exceeding the absolute deadline."""
        for byte in data:
            if self.server.stopping.wait(0.04):
                return
            self.connection.sendall(bytes([byte]))

    def do_GET(self) -> None:
        """Record any forbidden redirect follow-up so request-count checks fail."""
        with self.server.lock:
            self.server.requests.append({"method": "GET", "path": self.path})
        self.send_response(500)
        self.end_headers()

    def log_message(self, _format: str, *_args: Any) -> None:
        """Disable access logs, including request and authentication metadata."""


class InstalledWorkflowTests(unittest.TestCase):
    """Exercise real APP processes through standalone and signed runtime dispatch."""

    def setUp(self) -> None:
        """Allocate a fresh isolated fixture, home and verified CLI pair per test."""
        self.temp = tempfile.TemporaryDirectory(
            prefix="anthropic-e2e-", dir=fixture_base()
        )
        self.addCleanup(self.temp.cleanup)
        # Transist's directory capabilities need canonical paths (macOS /tmp aliases).
        self.root = pathlib.Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.source = self.root / "source"
        self.source.mkdir()
        self.home = self.root / "standalone"
        self.env = os.environ.copy()
        self.env.update(
            {
                "ANTHROPIC_API_KEY": CANARY,
                "TMPDIR": str(self.root),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        self.env.pop("TRANSIST_HOME", None)
        self.env.pop("TRANSIST_TRUSTED_EXECUTION", None)
        self.transist = self.binary("transist")
        self.admin = self.binary("transist-admin")
        for binary in (self.transist, self.admin):
            version = self.command(binary, "--version")
            self.assertEqual(
                version.stdout.strip(), f"{pathlib.Path(binary).name} 0.1.0"
            )
        self.server = Server()
        self.addCleanup(self.stop_server)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.network = {
            # loopback is namespace-local; the fake server is in the host namespace.
            "mode": "allowlist",
            "allow_hosts": ["127.0.0.1"],
            "allow_ports": [self.server.server_port],
            "tls_required": False,
            "max_redirects": 0,
        }
        self.action = {
            "base_url": self.origin,
            "model": "test-model",
            "system": " system café\n",
            "messages": copy.deepcopy(HISTORY),
            "max_tokens": 16,
            "timeout_ms": 2000,
            "secret_refs": ["ANTHROPIC_API_KEY"],
        }
        self.init_home(self.home, "standalone")
        self.cli(self.home, "plugins", "install", "--path", str(ROOT / "plugin.yaml"))
        self.inspect_plugin(self.home)
        self.addCleanup(self.settle_homes)

    def settle_homes(self) -> None:
        """Settle every fixture runtime before stopping transport or removing state."""
        for config in self.root.glob("*/config.yaml"):
            home = config.parent
            settle_runs(home, lambda *args: self.cli(home, *args).stdout)

    def binary(self, name: str) -> str:
        """Resolve installed tools; missing tools are failures, never skips."""
        supplied = runtime_binary(admin=name == "transist-admin")
        self.assertIsNotNone(supplied, f"required binary unavailable on PATH: {name}")
        return supplied

    def stop_server(self) -> None:
        """Wake fixture workers, close the listener and join the serving thread."""
        self.server.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def command(self, *args: str, ok: bool = True) -> subprocess.CompletedProcess[str]:
        """Run a bounded tool command and scan output before displaying diagnostics."""
        result = subprocess.run(
            args,
            cwd=self.workspace,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert_secret_free(result.stdout + result.stderr)
        if ok and result.returncode:
            self.fail(f"command failed: {args}: {result.stdout} {result.stderr}")
        return result

    def cli(
        self, home: pathlib.Path, *args: str, ok: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Select the disposable home and request JSON at the parsed subcommand."""
        return self.command(
            self.transist, "--home", str(home), *args, "--format", "json", ok=ok
        )

    def init_home(self, home: pathlib.Path, mode: str) -> None:
        """Initialize a runtime with only process delivery enabled (HTTP/MCP denied)."""
        self.cli(home, "home", "init", "--mode", mode)
        config = home / "config.yaml"
        text = config.read_text()
        self.assertIn("process: false", text)
        self.assertIn("http: false", text)
        self.assertIn("mcp: false", text)
        config.write_text(text.replace("process: false", "process: true"))

    def inspect_plugin(self, home: pathlib.Path) -> None:
        """Verify listing/inspection identity and the installed selected file set."""
        listing = self.cli(home, "plugins", "list").stdout
        self.assertIn("anthropic", listing)
        self.assertIn("0.1.0", listing)
        inspection = self.cli(home, "plugins", "inspect", "anthropic").stdout
        self.assertIn("chat_complete", inspection)
        manifests = list((home / "plugins").rglob("plugin.yaml"))
        self.assertEqual(len(manifests), 1)
        package = manifests[0].parent
        selected = {
            str(path.relative_to(package))
            for path in package.rglob("*")
            if path.is_file()
        }
        self.assertTrue(
            {
                "plugin.yaml",
                "plugin.py",
                "schemas/chat_complete.input.schema.json",
                "schemas/chat_complete.output.schema.json",
            }.issubset(selected)
        )
        self.assertFalse(
            any(
                name.startswith("tests/") or name in ("README.md", "workflow.yaml")
                for name in selected
            )
        )

    def flow(
        self, name: str, action: dict | None = None, network: dict | None = None
    ) -> pathlib.Path:
        """Write YAML with JSON step values, root denial and scoped network policy."""
        path = self.source / f"{name}.yaml"
        path.write_text(
            f"schema: transist.flow.v1\nid: {name}\nversion: 0.1.0\n"
            'plugins: {anthropic: "0.1.0"}\nnetwork: {mode: none}\n'
            + "steps: "
            + json.dumps(
                [
                    {
                        "id": "ask",
                        "uses": "process:anthropic/chat_complete",
                        "network": self.network if network is None else network,
                        "with": self.action if action is None else action,
                    }
                ]
            )
            + "\n"
        )
        return path

    def install_flow(
        self, home: pathlib.Path, name: str, path: pathlib.Path, trusted: bool = False
    ) -> None:
        """Install and require readiness, applying exact missing authority if needed."""
        self.cli(
            home,
            "workflows",
            "install",
            "--package" if trusted else "--path",
            str(path),
            "--approve",
        )
        readiness = self.cli(
            home, "workflows", "check", f"{name}@0.1.0", "--live", ok=False
        )
        report = json.loads(readiness.stdout)
        if report.get("status") != "ready":
            self.assertTrue(
                report.get("diagnostics", {}).get("authorization", {}).get("eligible"),
                report,
            )
            self.cli(
                home,
                "workflows",
                "authorize",
                f"{name}@0.1.0",
                "--approve",
            )
            readiness = self.cli(home, "workflows", "check", f"{name}@0.1.0", "--live")
            report = json.loads(readiness.stdout)
        self.assertEqual(report.get("status"), "ready", report)

    def run_flow(
        self,
        home: pathlib.Path,
        name: str,
        error: str | None = None,
        trusted: bool = False,
    ) -> dict:
        """Start a run and poll inspection to the exact expected terminal state."""
        flags = ["--trusted-execution"] if trusted else []
        self.cli(
            home,
            "workflows",
            "run",
            f"{name}@0.1.0",
            "--run-ref",
            name,
            *flags,
            ok=False,
        )
        until = time.monotonic() + 15
        while time.monotonic() < until:
            result = self.cli(home, "runs", "show", name)
            report = json.loads(result.stdout)
            if report.get("status") in TERMINAL:
                break
            time.sleep(0.05)
        else:
            self.fail("run did not reach a terminal state")
        self.assertEqual(report.get("status"), "failed" if error else "success", report)
        if error:
            errors = [
                item["error"]
                for item in objects(report)
                if isinstance(item.get("error"), dict)
            ]
            self.assertTrue(errors, "persisted canonical error absent")
            self.assertTrue(any(item.get("code") == error for item in errors), report)
            for item in errors:
                if item.get("code") == error:
                    assert_canonical_error(item, error)
        scan_home(home)
        return report

    def assert_request(self, start: int, action: dict) -> None:
        """Require one native, authenticated, byte-exact request without extras."""
        with self.server.lock:
            requests = self.server.requests[start:]
        self.assertEqual(len(requests), 1, "expected exactly one upstream request")
        request = requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/v1/messages")
        for key in (
            "auth_ok",
            "version_ok",
            "content_type_ok",
            "accept_ok",
            "encoding_ok",
            "no_extra_auth",
            "no_beta",
        ):
            self.assertTrue(request[key], f"request header contract failed: {key}")
        expected = {
            "model": action["model"],
            "messages": [
                {
                    "role": item["role"],
                    "content": [{"type": "text", "text": item["content"]}],
                }
                for item in action["messages"]
            ],
            "max_tokens": action.get("max_tokens", 256),
            "stream": False,
        }
        if "system" in action:
            expected["system"] = action["system"]
        self.assertEqual(
            request["body"],
            json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode(),
        )

    def test_installed_http_cases(self) -> None:
        """Cover native mapping, response/error bounds and secret reflections."""
        minimal = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "check sensor"}],
        }
        projection = SUCCESS | {
            "content": [
                {"type": "thinking", "thinking": "never-project-this"},
                *SUCCESS["content"],
                {"type": "future_block", "data": "never-project-this"},
            ],
            "stop_reason": "future_stop",
        }
        reflected = " ".join(variant.decode() for variant in sorted(VARIANTS))
        cases = [
            ("native", Reply(), None, "check sensor", True),
            (
                "minimal",
                Reply(body=json.dumps(minimal).encode(), headers={}),
                None,
                "check sensor",
                False,
            ),
            (
                "projection",
                Reply(body=json.dumps(projection).encode()),
                None,
                "check sensor",
                True,
            ),
            (
                "tool_use",
                Reply(body=json.dumps(SUCCESS | {"stop_reason": "tool_use"}).encode()),
                "protocol_error",
                None,
                False,
            ),
            (
                "pause_turn",
                Reply(
                    body=json.dumps(SUCCESS | {"stop_reason": "pause_turn"}).encode()
                ),
                "protocol_error",
                None,
                False,
            ),
            ("malformed", Reply(body=b"{"), "protocol_error", None, False),
            (
                "invalid_200",
                Reply(body=b'{"type":"message","role":"user","content":[]}'),
                "protocol_error",
                None,
                False,
            ),
            ("truncated", Reply(mode="truncate"), "protocol_error", None, False),
            (
                "overflow_length",
                Reply(body=b"x" * (1024 * 1024 + 1)),
                "protocol_error",
                None,
                False,
            ),
            (
                "overflow_eof",
                Reply(body=b"x" * (1024 * 1024 + 1), mode="overflow_eof"),
                "protocol_error",
                None,
                False,
            ),
            ("timeout", Reply(mode="wait"), "timeout_error", None, False),
            ("slow_headers", Reply(mode="slow_headers"), "timeout_error", None, False),
            ("slow_body", Reply(mode="slow_body"), "timeout_error", None, False),
            (
                "reflect",
                Reply(
                    body=json.dumps(
                        SUCCESS
                        | {
                            "content": [{"type": "text", "text": reflected}],
                            "model": reflected,
                        }
                    ).encode()
                ),
                None,
                None,
                True,
            ),
        ]
        for status, code in (
            (400, "validation_error"),
            (401, "auth_error"),
            (403, "auth_error"),
            (404, "validation_error"),
            (408, "timeout_error"),
            (413, "validation_error"),
            (422, "validation_error"),
            (429, "rate_limit_error"),
            (500, "tool_execution_error"),
            (503, "tool_execution_error"),
            (504, "timeout_error"),
            (529, "tool_execution_error"),
        ):
            cases.append(
                (
                    f"http_{status}",
                    Reply(
                        status=status,
                        body=json.dumps(
                            {"error": {"message": reflected}, "request_id": "req_body"}
                        ).encode(),
                        headers={},
                    ),
                    code,
                    None,
                    False,
                )
            )
        for status in (301, 302, 307, 308):
            cases.append(
                (
                    f"redirect_{status}",
                    Reply(
                        status=status,
                        body=b"{}",
                        headers={"Location": self.origin + "/follow-up"},
                    ),
                    "tool_execution_error",
                    None,
                    False,
                )
            )
        # Parsing failures must not overwrite a known HTTP error classification.
        cases.extend(
            [
                (
                    "error_malformed",
                    Reply(status=429, body=b"{", headers={}),
                    "rate_limit_error",
                    None,
                    False,
                ),
                (
                    "error_truncated",
                    Reply(status=503, mode="truncate", headers={}),
                    "tool_execution_error",
                    None,
                    False,
                ),
                (
                    "error_overflow",
                    Reply(status=500, body=b"x" * (1024 * 1024 + 1), headers={}),
                    "tool_execution_error",
                    None,
                    False,
                ),
            ]
        )
        passed = 0
        for name, reply, error, text, has_usage in cases:
            with self.subTest(case=name):
                action = copy.deepcopy(self.action)
                # Installed readiness shares this input's timeout with process
                # startup. Keep the normal 2s budget so these cases reach HTTP;
                # direct APP tests below independently enforce both 350ms minima.
                self.server.reply = reply
                start = len(self.server.requests)
                flow_name = f"anthropic_{name}"
                self.install_flow(self.home, flow_name, self.flow(flow_name, action))
                report = self.run_flow(self.home, flow_name, error)
                self.assert_request(start, action)
                persisted = self.home / "runs" / f"run_{flow_name}"
                files = [path for path in persisted.rglob("*") if path.is_file()]
                self.assertTrue(files, "run persistence missing")
                if not error:
                    outputs = [
                        item["output"]
                        for item in objects(report)
                        if isinstance(item.get("output"), dict)
                        and "response_text" in item["output"]
                    ]
                    self.assertTrue(outputs, report)
                    output = outputs[0]
                    if text:
                        self.assertEqual(output["response_text"], text)
                    if name == "minimal":
                        self.assertEqual(output, {"response_text": "check sensor"})
                    if name == "native":
                        self.assertEqual(
                            output,
                            {
                                "response_text": "check sensor",
                                "finish_reason": "end_turn",
                                "response_id": "msg_native_123",
                                "response_model": "native-model",
                                "request_id": "req_native_123",
                            },
                        )
                    if name == "projection":
                        self.assertEqual(output["finish_reason"], "future_stop")
                        self.assertNotIn("never-project-this", json.dumps(report))
                usages = [path for path in files if path.name.endswith("_usage.json")]
                self.assertEqual(
                    bool(usages), has_usage, "usage artifact persistence mismatch"
                )
                result = report["steps"]["ask"]["last_result"]
                self.assertEqual(result["stderr"], "", "APP invocation emitted stderr")
                self.assertEqual(result["metrics"], {})
                self.assertEqual("usage" in result, has_usage)
                self.assertEqual(bool(result["artifacts"]), has_usage)
                if name.startswith("http_"):
                    self.assertEqual(
                        result["error"]["message"],
                        f"provider returned HTTP {reply.status}; request_id=req_body",
                    )
                if has_usage:
                    usage = json.loads(usages[0].read_text())
                    self.assertEqual(
                        {
                            key: usage[key]
                            for key in (
                                "input_tokens",
                                "output_tokens",
                                "total_tokens",
                                "provider",
                            )
                        },
                        {
                            "input_tokens": 2,
                            "output_tokens": 3,
                            "total_tokens": 5,
                            "provider": "anthropic",
                        },
                    )
                    self.assertEqual(usage, result["usage"])
                    descriptor = result["artifacts"][0]
                    self.assertEqual(pathlib.Path(descriptor["path"]), usages[0])
                    self.assertEqual(descriptor["bytes"], usages[0].stat().st_size)
                    self.assertEqual(
                        descriptor["sha256"],
                        hashlib.sha256(usages[0].read_bytes()).hexdigest(),
                    )
                    if name != "reflect":
                        self.assertEqual(usage["model"], "native-model")
                passed += 1
        self.assertFalse(self.server.stopping.is_set(), "HTTP fixture worker failed")
        self.uninstall(self.home)
        print(f"validated {passed}/{len(cases)} installed HTTP cases", flush=True)

    def test_demo_readiness(self) -> None:
        """Check/install the demo and bind its required inputs without egress."""
        demo = ROOT / "workflow.yaml"
        inputs = [
            "--input",
            "model=test-model",
            "--input",
            "prompt=offline-readiness-only",
        ]
        self.cli(self.home, "workflows", "check", "--path", str(demo), *inputs)
        self.cli(
            self.home,
            "workflows",
            "install",
            "--path",
            str(demo),
            "--approve",
        )
        ready = self.cli(
            self.home,
            "workflows",
            "check",
            "anthropic_demo@0.1.0",
            "--live",
            *inputs,
        )
        self.assertEqual(json.loads(ready.stdout)["status"], "ready")
        unbound = self.cli(
            self.home,
            "workflows",
            "check",
            "anthropic_demo@0.1.0",
            ok=False,
        )
        self.assertNotEqual(json.loads(unbound.stdout)["status"], "ready")
        self.assertEqual(self.server.requests, [], "readiness dispatched provider HTTP")
        scan_home(self.home)
        self.uninstall(self.home)

    def test_policy_denial(self) -> None:
        """Root and step restrictions must fail before the server receives HTTP."""
        for index, network in enumerate(
            (
                {"mode": "none"},
                self.network | {"allow_hosts": ["localhost"]},
                self.network | {"allow_ports": [1]},
                self.network | {"tls_required": True},
            )
        ):
            with self.subTest(policy=index):
                name = f"anthropic_denied_{index}"
                path = self.flow(name, network=network)
                self.install_flow(self.home, name, path)
                start = len(self.server.requests)
                self.run_flow(self.home, name, "configuration_error")
                self.assertEqual(
                    len(self.server.requests), start, "denied policy dispatched HTTP"
                )
        self.uninstall(self.home)

    def direct(
        self, reply: Reply, limits: dict | None = None, cancel: bool = False
    ) -> dict:
        """Exercise APP timeout minima or SIGTERM during a real outstanding request."""
        self.server.reply = reply
        start = len(self.server.requests)
        action = self.action | {"timeout_ms": 5000}
        request = app_invoke_request("chat_complete", action, self.root)
        request.update({"policy": {"network": self.network}, **(limits or {})})
        process = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "plugin.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        try:
            payload = (
                json.dumps(
                    {"type": "app.handshake.request.v1", "protocol_version": "app.v1"}
                )
                + "\n"
                + json.dumps(request)
                + "\n"
            )
            process.stdin.write(payload)
            process.stdin.flush()
            self.assertTrue(
                reply.received.wait(3), "provider never received APP request"
            )
            began = time.monotonic()
            if cancel:
                process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=3)
            assert_secret_free(stdout + stderr)
            self.assertEqual(
                process.returncode, 0, "APP process did not terminate cleanly"
            )
            self.assertEqual(stderr, "", "normal APP path emitted stderr")
            frames = [json.loads(line) for line in stdout.splitlines()]
            self.assertEqual(len(frames), 2)
            _, result = assert_app_frames(frames, "anthropic")
            assert_canonical_error(
                result["error"], "cancelled" if cancel else "timeout_error"
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["artifact_hints"], [])
            self.assertEqual(result["metrics"], {})
            self.assertNotIn("usage", result)
            self.assertLess(
                time.monotonic() - began,
                1.5,
                "absolute deadline/cancellation did not interrupt I/O",
            )
            self.assertTrue(reply.closed.wait(2), "active connection was not closed")
            self.assert_request(start, action)
            return result
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_deadlines_and_sigterm(self) -> None:
        """Trickling reads cannot extend APP budgets; cancellation closes I/O."""
        for mode in ("wait", "slow_headers", "slow_body"):
            for limits in (
                {"timeout_ms": 350, "max_timeout_ms": 4500},
                {"timeout_ms": 4500, "max_timeout_ms": 350},
            ):
                with self.subTest(mode=mode, limits=limits):
                    self.direct(Reply(mode=mode), limits)
        self.direct(Reply(mode="wait"), cancel=True)
        scan_home(self.home)
        self.uninstall(self.home)

    def uninstall(self, home: pathlib.Path) -> None:
        """Uninstall the version and verify registry and inspection removal."""
        settle_runs(home, lambda *args: self.cli(home, *args).stdout)
        self.cli(home, "plugins", "uninstall", "anthropic", "--version", "0.1.0")
        self.assertNotIn("anthropic", self.cli(home, "plugins", "list").stdout)
        self.assertNotEqual(
            self.cli(home, "plugins", "inspect", "anthropic", ok=False).returncode, 0
        )
        self.assertFalse(list((home / "plugins").rglob("plugin.yaml")))
        scan_home(home)

    def test_trusted_lifecycle(self) -> None:
        """Sign 0.1.0 payload/workflow; reject unsigned and tampered plugin bytes."""
        key, trust = self.root / "signing.key", self.root / "trust.pub"
        self.command(
            self.admin,
            "key",
            "generate",
            "--name",
            "anthropic-e2e",
            "--private-key-file",
            str(key),
        )
        self.command(
            self.admin,
            "key",
            "export-public",
            "--private-key-file",
            str(key),
            "--trust-root-file",
            str(trust),
        )
        package = self.root / "anthropic"
        package.mkdir()
        for relative in (
            "plugin.yaml",
            "plugin.py",
            "schemas/chat_complete.input.schema.json",
            "schemas/chat_complete.output.schema.json",
        ):
            destination = package / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        unsigned_home = self.root / "unsigned-home"
        self.init_home(unsigned_home, "governed")
        self.cli(unsigned_home, "trust", "import", str(trust))
        unsigned = self.cli(
            unsigned_home,
            "plugins",
            "install",
            "--path",
            str(package / "plugin.yaml"),
            "--trusted-execution",
            ok=False,
        )
        self.assertNotEqual(unsigned.returncode, 0, "unsigned 0.1.0 plugin installed")
        self.assertNotIn("anthropic", self.cli(unsigned_home, "plugins", "list").stdout)
        signature = self.root / "anthropic.sig"
        self.command(
            self.admin, "plugin", "sign", str(package), "--private-key-file", str(key)
        )
        signed = json.loads(signature.read_text())
        self.assertEqual(signed["schema"], "transist.signature.v1")
        self.assertEqual(signed["subject"]["version"], "0.1.0")
        self.command(
            self.admin,
            "plugin",
            "verify",
            str(package),
            "--signature",
            str(signature),
            "--trust-root-file",
            str(trust),
        )
        home = self.root / "governed"
        self.init_home(home, "governed")
        self.cli(home, "trust", "import", str(trust))
        self.cli(
            home,
            "plugins",
            "install",
            "--path",
            str(package / "plugin.yaml"),
            "--trusted-execution",
        )
        self.inspect_plugin(home)
        name = "anthropic_trusted"
        workflow = self.flow(name)
        release = self.root / "workflow-package"
        self.command(
            self.admin,
            "workflow",
            "package",
            str(workflow),
            "--version",
            "0.1.0",
            "--private-key-file",
            str(key),
            "--output",
            str(release),
        )
        self.install_flow(home, name, release, trusted=True)
        start = len(self.server.requests)
        self.run_flow(home, name, trusted=True)
        self.assert_request(start, self.action)
        scan_home(home)
        self.uninstall(home)
        # Valid public trust plus a stale signature must not authorize modified payload.
        with (package / "plugin.py").open("a") as handle:
            handle.write("\n# tampered selected payload\n")
        verify = self.command(
            self.admin,
            "plugin",
            "verify",
            str(package),
            "--signature",
            str(signature),
            "--trust-root-file",
            str(trust),
            ok=False,
        )
        self.assertNotEqual(
            verify.returncode, 0, "tampered signature unexpectedly verified"
        )
        denied = self.root / "tamper-home"
        self.init_home(denied, "governed")
        self.cli(denied, "trust", "import", str(trust))
        result = self.cli(
            denied,
            "plugins",
            "install",
            "--path",
            str(package),
            "--trusted-execution",
            ok=False,
        )
        self.assertNotEqual(result.returncode, 0, "tampered package installed")
        self.assertNotIn("anthropic", self.cli(denied, "plugins", "list").stdout)
        scan_home(denied)
        self.uninstall(self.home)


def objects(value: Any) -> list[dict]:
    """Collect nested inspection objects without assuming flattened runtime JSON."""
    if isinstance(value, dict):
        return [value] + [item for child in value.values() for item in objects(child)]
    if isinstance(value, list):
        return [item for child in value for item in objects(child)]
    return []


if __name__ == "__main__":
    unittest.main(verbosity=2)
