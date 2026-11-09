"""Bounded, offline APP HTTP text service; start separately from installation."""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

PORT = 18765
MAX_BODY = 65536
DEADLINE = 3.0
IDENTITY = {"id": "text.http", "version": "0.1.0"}


def analyze(value: Any) -> dict[str, Any]:
    """Validate inline text and return deterministic UTF-8 statistics, without I/O."""
    if not isinstance(value, dict) or set(value) - {"text", "maxBytes"}:
        raise ValueError("invalid input fields")
    text = value.get("text")
    bound = value.get("maxBytes", 4096)
    if not isinstance(text, str) or len(text) > 4096:
        raise ValueError("text must be a string of at most 4096 characters")
    if type(bound) is not int or not 0 <= bound <= 4096:
        raise ValueError("maxBytes must be an integer from 0 to 4096")
    data = text.encode("utf-8", errors="strict")
    if len(data) > bound:
        raise ValueError("UTF-8 text exceeds maxBytes")
    return {
        "bytes": len(data),
        "chars": len(text),
        "lines": text.count("\n") + 1 if text else 0,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def object_fields(value: Any, fields: set[str]) -> bool:
    """Check an exact object shape; used for fixed APP envelope objects."""
    return isinstance(value, dict) and set(value) == fields


def nonempty(value: Any) -> bool:
    """Return whether a protocol identifier is a nonempty string."""
    return isinstance(value, str) and bool(value)


def validate_invoke(message: dict[str, Any]) -> None:
    """Reject malformed APP envelopes; observational paths are never dereferenced."""
    required = {
        "type",
        "protocol_version",
        "invoke_id",
        "run_id",
        "workflow_id",
        "step",
        "plugin",
        "action",
        "input",
        "paths",
        "requested_capabilities",
        "policy",
        "metadata",
    }
    if not required <= message.keys() or message.keys() - required - {
        "timeout_ms",
        "max_timeout_ms",
    }:
        raise ValueError("invalid invoke envelope fields")
    if (
        message["type"] != "app.invoke.request.v1"
        or message["protocol_version"] != "app.v1"
    ):
        raise ValueError("unsupported APP message")
    if not all(
        nonempty(message[key]) for key in ("invoke_id", "run_id", "workflow_id")
    ):
        raise ValueError("invalid identifiers")
    if message["plugin"] != IDENTITY or message["action"] != "analyze":
        raise ValueError("unsupported plugin or action")
    step = message["step"]
    if not object_fields(step, {"id", "execution"}) or not nonempty(step["id"]):
        raise ValueError("invalid step")
    if type(step["execution"]) is not int or step["execution"] < 1:
        raise ValueError("invalid execution")
    paths = message["paths"]
    if not object_fields(
        paths, {"run_root", "artifacts_dir", "evidence_dir", "workspace_dir"}
    ):
        raise ValueError("invalid paths envelope")
    if not all(nonempty(path) for path in paths.values()):
        raise ValueError("invalid paths envelope")
    caps = message["requested_capabilities"]
    if not isinstance(caps, list):
        raise ValueError("invalid capabilities")
    for cap in caps:
        if not object_fields(cap, {"capability", "resource", "operations"}):
            raise ValueError("invalid capability")
        if not isinstance(cap["capability"], str) or cap["capability"] not in {
            "adapter",
            "plugin",
            "action",
            "provider",
            "secret",
            "filesystem",
            "network",
            "approval",
        }:
            raise ValueError("invalid capability kind")
        ops = cap["operations"]
        if not nonempty(cap["resource"]) or not isinstance(ops, list) or not ops:
            raise ValueError("invalid capability resource or operations")
        if not all(nonempty(op) for op in ops) or len(set(ops)) != len(ops):
            raise ValueError("invalid capability operations")
    for key in ("timeout_ms", "max_timeout_ms"):
        value = message.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("invalid timeout")


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Decode JSON objects while rejecting ambiguous duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def reject_constant(value: str) -> Any:
    """Reject non-JSON numeric constants rather than accepting NaN/Infinity."""
    raise ValueError("non-JSON constant")


class Handler(BaseHTTPRequestHandler):
    """Serve only APP handshake/invoke with bounded bodies and connection lifetime."""

    def setup(self) -> None:
        """Start a hard connection deadline covering headers, body and response."""
        super().setup()
        self.connection.settimeout(DEADLINE)
        self.timer = threading.Timer(DEADLINE, self.expire)
        self.timer.daemon = True
        self.timer.start()

    def expire(self) -> None:
        """Interrupt slow peers at the absolute deadline; no text is logged."""
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self) -> None:
        """Release the connection timer and standard HTTP buffers."""
        self.timer.cancel()
        super().finish()

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress request logs so supplied text and identifiers are not recorded."""

    def reply(self, status: int, body: dict[str, Any]) -> None:
        """Write a length-delimited JSON response and close the connection."""
        data = json.dumps(body, ensure_ascii=True, allow_nan=False).encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def do_POST(self) -> None:
        """Parse a bounded JSON APP request and dispatch the sole analyze action."""
        try:
            try:
                self.dispatch()
            except (ValueError, UnicodeError, RecursionError):
                self.reply(400, {"error": "invalid APP request or input"})
        except (TimeoutError, OSError):
            self.close_connection = True

    def do_GET(self) -> None:
        """Answer bodyless GET handshake/health at the exact APP endpoints."""
        try:
            if self.path == "/app/health":
                self.reply(200, {"status": "ok"})
                return
            if self.path != "/app/handshake":
                self.reply(404, {"error": "unknown endpoint"})
                return
            self.handshake()
        except OSError:
            self.close_connection = True

    def handshake(self) -> None:
        """Return the APP identity and only implemented capabilities."""
        self.reply(
            200,
            {
                "type": "app.handshake.response.v1",
                "protocol_version": "app.v1",
                "plugin": IDENTITY,
                "adapter_kind": "http",
                "capabilities": {
                    "actions": ["analyze"],
                    "supports_cancel": False,
                    "supports_health": True,
                    "supports_artifact_hints": False,
                    "supports_evidence_hints": False,
                },
            },
        )

    def dispatch(self) -> None:
        """Reject unsupported routes/framing before reading or processing JSON."""
        if self.path not in {"/app/handshake", "/app/invoke"}:
            self.reply(404, {"error": "unknown endpoint"})
            return
        if self.headers.get("Transfer-Encoding") or self.headers.get(
            "Content-Encoding"
        ):
            self.reply(415, {"error": "encoded or chunked bodies unsupported"})
            return
        if self.headers.get_content_type() != "application/json":
            self.reply(415, {"error": "application/json required"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            raise ValueError("invalid content length")
        length = int(lengths[0])
        if length > MAX_BODY:
            self.reply(413, {"error": "request too large"})
            return
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("truncated body")
        message = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
        if not isinstance(message, dict):
            raise ValueError("object required")
        if self.path == "/app/handshake":
            if message != {
                "type": "app.handshake.request.v1",
                "protocol_version": "app.v1",
            }:
                raise ValueError("invalid handshake")
            self.handshake()
            return
        validate_invoke(message)
        result = {
            "type": "app.invoke.result.v1",
            "protocol_version": "app.v1",
            "invoke_id": message["invoke_id"],
            "status": "success",
            "output": {},
            "artifact_hints": [],
            "evidence_hints": [],
            "metrics": {},
        }
        try:
            result["output"] = analyze(message["input"])
        except (ValueError, UnicodeError):
            result["status"] = "failed"
            result["error"] = {
                "code": "validation_error",
                "class": "validation",
                "retryability": "none",
                "message": "Invalid analyze input or UTF-8 byte bound exceeded",
            }
        self.reply(200, result)


def make_server(port: int = PORT, handler: type[Handler] = Handler) -> HTTPServer:
    """Bind an IPv4 loopback-only serial server; port zero supports isolated tests."""
    return HTTPServer(("127.0.0.1", port), handler)


def main() -> None:
    """Start the separately managed service; --port is an optional test override."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    with make_server(args.port) as server:
        print(
            f"text.http listening on http://127.0.0.1:{server.server_port}", flush=True
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
