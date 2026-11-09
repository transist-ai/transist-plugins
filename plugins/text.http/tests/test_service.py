"""Offline HTTP wire/schema tests using vendored contracts and no providers."""

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT.parents[1] / "tests" / "contracts"
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
from contract_registry import app_registry  # noqa: E402

REGISTRY = app_registry(CONTRACTS)
SPEC = importlib.util.spec_from_file_location("text_http_service", ROOT / "service.py")
assert SPEC is not None and SPEC.loader is not None
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)


def invoke(text: Any = "Hello, 世界!\n", **extra: Any) -> dict[str, Any]:
    """Build a real APP envelope with deliberately unusable observational paths."""
    return {
        "type": "app.invoke.request.v1",
        "protocol_version": "app.v1",
        "invoke_id": "wire-1",
        "run_id": "run-1",
        "workflow_id": "demo",
        "step": {"id": "analyze", "execution": 1},
        "plugin": service.IDENTITY,
        "action": "analyze",
        "input": {"text": text, **extra},
        "paths": {
            key: "/does/not/exist"
            for key in ("run_root", "artifacts_dir", "evidence_dir", "workspace_dir")
        },
        "requested_capabilities": [],
        "policy": {},
        "metadata": {},
    }


def schema(path: Path) -> Draft202012Validator:
    """Compile a local Draft 2020-12 schema without network resolution."""
    value = json.loads(path.read_text())
    Draft202012Validator.check_schema(value)
    return Draft202012Validator(value, registry=REGISTRY)


class WireTests(unittest.TestCase):
    """Exercise the supplied service over actual loopback HTTP connections."""

    @classmethod
    def setUpClass(cls) -> None:
        """Start the supplied service on an ephemeral, loopback-only port."""
        cls.server = service.make_server(0)
        cls.thread = threading.Thread(target=cls.server.serve_forever)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        """Stop the listener and join its owner thread."""
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(
        self,
        body: Any,
        path: str = "/app/invoke",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        """POST bytes or JSON and decode the HTTP response."""
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        try:
            connection.request(
                "POST",
                path,
                body=data,
                headers=(
                    headers
                    if headers is not None
                    else {"Content-Type": "application/json"}
                ),
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_handshake_and_result_schemas(self) -> None:
        """Validate wire messages against authoritative committed APP schemas."""
        message = {"type": "app.handshake.request.v1", "protocol_version": "app.v1"}
        status, handshake = self.request(message, "/app/handshake")
        self.assertEqual(status, 200)
        self.assertEqual(handshake["plugin"], {"id": "text.http", "version": "0.1.0"})
        schema(CONTRACTS / "app/handshake.response.v1.schema.json").validate(handshake)
        self.assertFalse(handshake["capabilities"]["supports_cancel"])
        self.assertTrue(handshake["capabilities"]["supports_health"])
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        try:
            connection.request("GET", "/app/handshake")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), handshake)
            connection.request("GET", "/app/health")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"status": "ok"})
        finally:
            connection.close()
        request = invoke()
        schema(CONTRACTS / "app/invoke.request.v1.schema.json").validate(request)
        status, result = self.request(request)
        self.assertEqual(status, 200)
        schema(CONTRACTS / "app/invoke.result.v1.schema.json").validate(result)
        schema(ROOT / "schemas/analyze.output.schema.json").validate(result["output"])
        self.assertEqual(
            result["output"],
            {
                "bytes": 15,
                "chars": 11,
                "lines": 2,
                "sha256": hashlib.sha256("Hello, 世界!\n".encode()).hexdigest(),
            },
        )
        self.assertNotIn("usage", result)
        self.assertNotIn("error", result)
        self.assertEqual(result, self.request(request)[1])

    def test_bounds_and_line_semantics(self) -> None:
        """Check byte/character differences, empty text, LF and boundary sizes."""
        for text, bound, lines in [
            ("", 0, 0),
            ("é", 2, 1),
            ("\r\n", 2, 2),
            ("a" * 4096, 4096, 1),
        ]:
            with self.subTest(text_length=len(text)):
                status, result = self.request(invoke(text, maxBytes=bound))
                self.assertEqual(status, 200)
                self.assertEqual(result["output"]["bytes"], len(text.encode()))
                self.assertEqual(result["output"]["chars"], len(text))
                self.assertEqual(result["output"]["lines"], lines)
        for message in [invoke("é", maxBytes=1), invoke("é" * 4096)]:
            schema(ROOT / "schemas/analyze.input.schema.json").validate(
                message["input"]
            )
            status, result = self.request(message)
            self.assertEqual(status, 200)
            schema(CONTRACTS / "app/invoke.result.v1.schema.json").validate(result)
            self.assertEqual(result["invoke_id"], message["invoke_id"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["output"], {})
            self.assertEqual(result["error"]["code"], "validation_error")
            self.assertEqual(result["error"]["class"], "validation")
            self.assertEqual(result["error"]["retryability"], "none")

    def test_invalid_inputs_and_path_fields(self) -> None:
        """Reject path authority, extra fields, wrong types and invalid Unicode."""
        for message in [
            invoke(1),
            invoke("a" * 4097),
            invoke("\ud800"),
            invoke("x", maxBytes=True),
            invoke("x", maxBytes=-1),
            invoke("x", maxBytes=4097),
            invoke("x", path="/etc/passwd"),
            invoke("x", secret_refs={}),
            invoke(None),
        ]:
            with self.subTest(message=repr(message["input"])[:80]):
                status, result = self.request(message)
                self.assertEqual(status, 200)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"]["code"], "validation_error")
        # A URI-shaped string is literal text, never a resolver or file read.
        self.assertEqual(
            self.request(invoke("file:///etc/passwd"))[1]["output"]["bytes"], 18
        )

    def test_malformed_messages(self) -> None:
        """Reject malformed JSON and APP envelopes without crashing the service."""
        for body in [
            b"{",
            b"[]",
            b"null",
            b"\xff",
            b'{"x":NaN}',
            b'{"type":1,"type":2}',
            b"[" * 1200,
        ]:
            self.assertEqual(self.request(body)[0], 400)
        for key, value in [
            ("type", "other"),
            ("protocol_version", "app.v2"),
            ("invoke_id", ""),
            ("step", {}),
            ("plugin", {}),
            ("action", "read"),
            ("paths", {}),
            ("timeout_ms", -1),
            (
                "requested_capabilities",
                [{"capability": [], "resource": "x", "operations": ["read"]}],
            ),
        ]:
            message = invoke()
            message[key] = value
            self.assertEqual(self.request(message)[0], 400)
        self.assertEqual(
            self.request({"type": "app.handshake.request.v1"}, "/app/handshake")[0], 400
        )

    def test_http_limits_routes_and_deadline(self) -> None:
        """Check HTTP framing, content types, route exactness and slow body cutoff."""
        self.assertEqual(self.request(b"x" * (service.MAX_BODY + 1))[0], 413)
        self.assertEqual(
            self.request(invoke(), headers={"Content-Type": "text/plain"})[0], 415
        )
        self.assertEqual(
            self.request(
                invoke(),
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
            )[0],
            415,
        )
        for path in [
            "/app/cancel",
            "/app/health",
            "/app/invoke?path=x",
            "/../etc/passwd",
        ]:
            self.assertEqual(self.request(invoke(), path)[0], 404)
        for headers in [
            b"Content-Length: -1\r\n",
            b"Content-Length: 2\r\nContent-Length: 2\r\n",
        ]:
            with socket.create_connection(
                self.server.server_address, timeout=5
            ) as peer:
                peer.sendall(
                    b"POST /app/invoke HTTP/1.0\r\nContent-Type: application/json\r\n"
                    + headers
                    + b"\r\n{}"
                )
                self.assertIn(b"400", peer.recv(1024))
        started = time.monotonic()
        with socket.create_connection(self.server.server_address, timeout=5) as peer:
            peer.sendall(
                b"POST /app/invoke HTTP/1.0\r\nContent-Type: application/json\r\n"
                b"Content-Length: 100\r\n\r\n{"
            )
            self.assertEqual(peer.recv(1024), b"")
        self.assertLess(time.monotonic() - started, service.DEADLINE + 1)
        self.assertEqual(self.request(invoke())[0], 200)

    def test_action_schemas(self) -> None:
        """Assert schema rejection independently of service validation."""
        validator = schema(ROOT / "schemas/analyze.input.schema.json")
        validator.validate({"text": "é", "maxBytes": 2})
        for value in [
            {},
            {"text": 1},
            {"text": "x", "path": "x"},
            {"text": "x" * 4097},
            {"text": "x", "maxBytes": True},
        ]:
            self.assertFalse(validator.is_valid(value))
        self.assertFalse(
            schema(ROOT / "schemas/analyze.output.schema.json").is_valid({"bytes": 1})
        )


if __name__ == "__main__":
    unittest.main()
