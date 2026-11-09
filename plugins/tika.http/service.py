"""Remote, memory-only Tika APP boundary; run behind a non-logging HTTPS ingress."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import math
import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

IDENTITY = {"id": "tika.http", "version": "0.1.0"}
ACTIONS = {"extract_text": "/tika", "extract_metadata": "/meta"}
TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
}
KEYS = {
    "Content-Type",
    "Content-Length",
    "Content-Encoding",
    "dc:title",
    "dc:creator",
    "dcterms:created",
    "dcterms:modified",
    "xmpTPg:NPages",
}
MAX_DOCUMENT = 5 * 1024 * 1024
MAX_OUTPUT = 128 * 1024
MAX_BODY = 4 * ((MAX_DOCUMENT + 2) // 3) + 65536
MAX_METADATA_RESPONSE = 1024 * 1024
DEADLINE = 30.0
ERRORS = {
    "validation_error": ("validation", "Invalid invocation or document"),
    "timeout_error": ("timeout", "Operation deadline exceeded"),
    "transport_error": ("transport", "Backend unavailable"),
    "protocol_error": ("protocol", "Invalid backend response"),
}


class Failure(Exception):
    """Carry only a fixed public error code, never backend diagnostics."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object, rejecting duplicate keys at every nesting level."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def reject_constant(value: str) -> Any:
    """Reject JavaScript numeric extensions to strict JSON."""
    raise ValueError("non-JSON number")


def finite_number(value: str) -> float:
    """Decode JSON fractional numbers without overflowing to nonfinite values."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite number")
    return number


def parse_json(raw: bytes) -> Any:
    """Decode strict UTF-8, duplicate-free JSON without NaN or Infinity."""
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
        parse_float=finite_number,
    )


def nonempty(value: Any) -> bool:
    """Test protocol identifiers without coercing other JSON types."""
    return isinstance(value, str) and bool(value)


def exact(value: Any, keys: set[str]) -> bool:
    """Test closed APP object shapes."""
    return isinstance(value, dict) and set(value) == keys


def validate_invoke(message: Any) -> None:
    """Validate the APP envelope; paths and policy remain observational only."""
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
    if not isinstance(message, dict) or not required <= message.keys():
        raise ValueError("envelope")
    if message.keys() - required - {"timeout_ms", "max_timeout_ms"}:
        raise ValueError("envelope")
    if (
        message["type"] != "app.invoke.request.v1"
        or message["protocol_version"] != "app.v1"
        or message["plugin"] != IDENTITY
        or message["action"] not in ACTIONS
    ):
        raise ValueError("identity")
    if not all(nonempty(message[k]) for k in ("invoke_id", "run_id", "workflow_id")):
        raise ValueError("identifier")
    step = message["step"]
    if (
        not exact(step, {"id", "execution"})
        or not nonempty(step["id"])
        or type(step["execution"]) is not int
        or step["execution"] < 1
    ):
        raise ValueError("step")
    paths = message["paths"]
    if not exact(
        paths, {"run_root", "artifacts_dir", "evidence_dir", "workspace_dir"}
    ) or not all(nonempty(v) for v in paths.values()):
        raise ValueError("paths")
    caps = message["requested_capabilities"]
    if not isinstance(caps, list):
        raise ValueError("capabilities")
    for cap in caps:
        if not exact(cap, {"capability", "resource", "operations"}):
            raise ValueError("capability")
        if cap["capability"] not in (
            "adapter",
            "plugin",
            "action",
            "provider",
            "secret",
            "filesystem",
            "network",
            "approval",
        ):
            raise ValueError("capability")
        ops = cap["operations"]
        if (
            not nonempty(cap["resource"])
            or not isinstance(ops, list)
            or not ops
            or not all(nonempty(op) for op in ops)
            or len(set(ops)) != len(ops)
        ):
            raise ValueError("operations")
    for key in ("timeout_ms", "max_timeout_ms"):
        value = message.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("timeout")


def decode_document(value: Any) -> tuple[bytes, str]:
    """Validate the closed action input and decode 1–5 MiB canonical Base64."""
    if not exact(value, {"document_base64", "content_type"}):
        raise ValueError("input")
    encoded, content_type = value["document_base64"], value["content_type"]
    if not isinstance(content_type, str) or content_type not in TYPES:
        raise ValueError("content type")
    if (
        not isinstance(encoded, str)
        or not encoded
        or len(encoded) > 4 * ((MAX_DOCUMENT + 2) // 3)
    ):
        raise ValueError("size")
    try:
        document = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("base64") from None
    if (
        not 1 <= len(document) <= MAX_DOCUMENT
        or base64.b64encode(document).decode("ascii") != encoded
    ):
        raise ValueError("canonical base64")
    return document, content_type


def serialize(value: Any) -> bytes:
    """Serialize strict compact UTF-8 JSON for byte-bound enforcement."""
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def normalize_metadata(value: Any, digest: str) -> dict[str, Any]:
    """Whitelist metadata; normalize scalar strings without dropping excess values."""
    if not isinstance(value, dict):
        raise Failure("protocol_error")
    metadata: dict[str, list[str]] = {}
    for key in sorted(KEYS & value.keys()):
        values = value[key] if isinstance(value[key], list) else [value[key]]
        if len(values) > 16 or any(
            not isinstance(v, str) or len(v) > 4096 for v in values
        ):
            raise Failure("protocol_error")
        metadata[key] = values
    output = {"document_sha256": digest, "metadata": metadata}
    if len(serialize(output)) > MAX_OUTPUT:
        raise Failure("protocol_error")
    return output


def result(
    invoke_id: str, output: Any = None, code: str | None = None
) -> dict[str, Any]:
    """Create a complete APP result with fixed errors and no invented hints/usage."""
    value = {
        "type": "app.invoke.result.v1",
        "protocol_version": "app.v1",
        "invoke_id": invoke_id,
        "status": "failed" if code else "success",
        "output": {} if code else output,
        "artifact_hints": [],
        "evidence_hints": [],
        "metrics": {},
    }
    if code:
        category, message = ERRORS[code]
        value["error"] = {
            "code": code,
            "class": category,
            "retryability": "none",
            "message": message,
        }
    return value


class TikaApp:
    """ASGI APP service: authenticate first; never open client paths or log data."""

    def __init__(
        self,
        authorization: str,
        backend: str,
        transport: httpx.AsyncBaseTransport | None = None,
        deadline: float = DEADLINE,
    ) -> None:
        """Configure a fixed origin and Bearer secret; transport is test-only."""
        if not re.fullmatch(r"Bearer [A-Za-z0-9._~+/-]{32,}={0,2}", authorization):
            raise ValueError("Strong Bearer authorization required")
        url = urlsplit(backend)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("Fixed backend origin required")
        if not 0 < deadline <= DEADLINE:
            raise ValueError("Invalid deadline")
        self.authorization = authorization.encode("ascii")
        self.backend = backend.rstrip("/")
        self.transport = transport
        self.deadline = deadline

    async def extract(
        self, action: str, document: bytes, content_type: str
    ) -> dict[str, Any]:
        """PUT bytes to Tika without retries, proxies, redirects or credentials."""
        accept = "text/plain" if action == "extract_text" else "application/json"
        limit = MAX_OUTPUT if action == "extract_text" else MAX_METADATA_RESPONSE
        async with httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=self.deadline,
            transport=self.transport,
        ) as client:
            async with client.stream(
                "PUT",
                self.backend + ACTIONS[action],
                content=document,
                headers={
                    "Content-Type": content_type,
                    "Accept": accept,
                    "Accept-Encoding": "identity",
                    "X-Tika-Skip-Embedded": "true",
                },
            ) as response:
                if 300 <= response.status_code < 400:
                    raise Failure("protocol_error")
                if response.status_code != 200:
                    raise Failure("transport_error")
                if (
                    response.headers.get("content-type", "")
                    .split(";")[0]
                    .strip()
                    .lower()
                    != accept
                    or response.headers.get("content-encoding", "identity")
                    != "identity"
                ):
                    raise Failure("protocol_error")
                raw = bytearray()
                async for chunk in response.aiter_raw():
                    if len(raw) + len(chunk) > limit:
                        raise Failure("protocol_error")
                    raw.extend(chunk)
        digest = hashlib.sha256(document).hexdigest()
        try:
            if action == "extract_text":
                return {"document_sha256": digest, "text": raw.decode("utf-8")}
            return normalize_metadata(parse_json(bytes(raw)), digest)
        except (ValueError, UnicodeError, RecursionError):
            raise Failure("protocol_error") from None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle discovery or an authenticated invoke with an absolute deadline."""
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        path, method = scope["path"], scope["method"]
        if method == "GET" and path in {"/app/handshake", "/app/health"}:
            body = (
                {"status": "ok"}
                if path.endswith("health")
                else {
                    "type": "app.handshake.response.v1",
                    "protocol_version": "app.v1",
                    "plugin": IDENTITY,
                    "adapter_kind": "http",
                    "capabilities": {
                        "actions": list(ACTIONS),
                        "supports_health": True,
                        "supports_cancel": False,
                        "supports_artifact_hints": False,
                        "supports_evidence_hints": False,
                    },
                }
            )
            await JSONResponse(body)(scope, receive, send)
            return
        if method != "POST" or path != "/app/invoke":
            await JSONResponse({"error": "Not found"}, 404)(scope, receive, send)
            return
        auth = [v for k, v in scope["headers"] if k.lower() == b"authorization"]
        if len(auth) != 1 or not hmac.compare_digest(auth[0], self.authorization):
            await JSONResponse({"error": "Unauthorized"}, 401)(scope, receive, send)
            return
        invoke_id = "invalid"
        try:
            async with asyncio.timeout(self.deadline):
                if (
                    request.headers.get("content-type", "").lower()
                    not in {"application/json", "application/json; charset=utf-8"}
                    or request.headers.get("content-encoding", "identity") != "identity"
                ):
                    raise ValueError("framing")
                raw = bytearray()
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > MAX_BODY:
                        raise ValueError("body size")
                    raw.extend(chunk)
                message = parse_json(bytes(raw))
                if isinstance(message, dict) and nonempty(message.get("invoke_id")):
                    message["invoke_id"].encode("utf-8", errors="strict")
                    invoke_id = message["invoke_id"]
                validate_invoke(message)
                document, content_type = decode_document(message["input"])
                output = await self.extract(message["action"], document, content_type)
                body = result(invoke_id, output)
        except (ValueError, UnicodeError, RecursionError, TypeError):
            body = result(invoke_id, code="validation_error")
        except (TimeoutError, httpx.TimeoutException):
            body = result(invoke_id, code="timeout_error")
        except (httpx.RemoteProtocolError, httpx.DecodingError):
            body = result(invoke_id, code="protocol_error")
        except httpx.HTTPError:
            body = result(invoke_id, code="transport_error")
        except Failure as error:
            body = result(invoke_id, code=error.code)
        await JSONResponse(body)(scope, receive, send)


def create_app() -> TikaApp:
    """Read deployment-only configuration; suppress HTTP client diagnostic logging."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).disabled = True
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
        logging.getLogger(name).propagate = False
    return TikaApp(
        os.environ["TIKA_AUTHORIZATION"],
        os.environ.get("TIKA_BACKEND_URL", "http://tika:9998"),
    )
