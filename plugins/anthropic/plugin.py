#!/usr/bin/env python3
"""Self-contained, bounded Anthropic Messages APP process provider (Python 3.10+)."""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import queue
import re
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any

PLUGIN_ID = "anthropic"
PLUGIN_VERSION = "0.1.0"
SECRET_NAME = "ANTHROPIC_API_KEY"
MAX_REQUEST = 256 * 1024
MAX_RESPONSE = 1024 * 1024
UINT64_MAX = (1 << 64) - 1
ACTIVE_SOCKET: Any = None
ERROR_KINDS = {
    "validation_error": ("validation", "none"),
    "configuration_error": ("configuration", "none"),
    "secret_resolution_error": ("secret", "operator"),
    "transport_error": ("transport", "safe"),
    "timeout_error": ("timeout", "safe"),
    "auth_error": ("auth", "operator"),
    "rate_limit_error": ("rate_limit", "safe"),
    "tool_execution_error": ("tool", "safe"),
    "protocol_error": ("protocol", "none"),
    "cancelled": ("cancellation", "none"),
}
Endpoint = tuple[urllib.parse.SplitResult, str, int, str]


class PluginError(Exception):
    """Carry a canonical code and a fixed diagnostic, never raw provider errors."""

    def __init__(self, code: str, message: str) -> None:
        """Store a safe code/message pair for terminal APP error emission."""
        super().__init__(message)
        self.code = code


def has_controls(value: str) -> bool:
    """Identify Unicode C0/C1 control characters without changing the text."""
    return any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)


def valid_text(value: Any, limit: int) -> bool:
    """Check nonblank, encodable text against a UTF-8 byte limit."""
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return len(value.encode("utf-8")) <= limit
    except UnicodeError:
        return False


def normalize_host(host: str) -> str:
    """Normalize DNS case/trailing dot and canonicalize literal IP addresses."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return host.lower().rstrip(".")


def is_loopback(host: str) -> bool:
    """Recognize localhost or a literal loopback address, without DNS."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def parse_endpoint(raw: Any) -> Endpoint:
    """Validate an origin override and return the fixed Messages target."""
    if (
        not isinstance(raw, str)
        or not raw
        or has_controls(raw)
        or any(c.isspace() for c in raw)
    ):
        raise PluginError("configuration_error", "invalid base_url")
    try:
        parsed = urllib.parse.urlsplit(raw)
        host, port = parsed.hostname, parsed.port
        if not host:
            raise ValueError()
        host = normalize_host(host)
        if "%" in host:
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if parsed.netloc.startswith("["):
                raise ValueError()
            labels = host.split(".")
            if len(host) > 253 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in labels
            ):
                raise ValueError()
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or "?" in raw
            or "#" in raw
            or parsed.netloc.endswith(":")
            or (port is not None and not 1 <= port <= 65535)
            or (parsed.scheme == "http" and not is_loopback(host))
        ):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise PluginError("configuration_error", "invalid base_url") from None
    return (
        parsed,
        host,
        port or (443 if parsed.scheme == "https" else 80),
        "/v1/messages",
    )


def validate_input(value: Any) -> tuple[Endpoint, bytes, int]:
    """Validate all action fields and serialize only native API request fields."""
    fields = {
        "model",
        "system",
        "messages",
        "max_tokens",
        "timeout_ms",
        "base_url",
        "secret_refs",
    }
    if not isinstance(value, dict) or set(value) - fields:
        raise PluginError("validation_error", "invalid action input fields")
    if "secret_refs" in value and value["secret_refs"] != [SECRET_NAME]:
        raise PluginError("validation_error", "invalid secret reference")
    model = value.get("model")
    if not valid_text(model, 1024) or len(model) > 256 or has_controls(model):
        raise PluginError("validation_error", "invalid model")
    if "system" in value and not valid_text(value["system"], 65536):
        raise PluginError("validation_error", "invalid system")
    messages = value.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 64:
        raise PluginError("validation_error", "messages must contain 1 to 64 entries")
    for item in messages:
        if (
            not isinstance(item, dict)
            or set(item) != {"role", "content"}
            or item["role"] not in ("user", "assistant")
            or not valid_text(item["content"], 65536)
        ):
            raise PluginError("validation_error", "invalid message")
    if messages[0]["role"] != "user" or messages[-1]["role"] != "user":
        raise PluginError(
            "validation_error", "conversation must start and end with user"
        )
    for name, default, maximum in (
        ("max_tokens", 256, 4096),
        ("timeout_ms", 30000, 120000),
    ):
        number = value.get(name, default)
        if type(number) is not int or not 1 <= number <= maximum:
            raise PluginError("validation_error", f"invalid {name}")
    endpoint = parse_endpoint(value.get("base_url", "https://api.anthropic.com"))
    payload = {
        "model": model,
        "messages": [
            {
                "role": item["role"],
                "content": [{"type": "text", "text": item["content"]}],
            }
            for item in messages
        ],
        "max_tokens": value.get("max_tokens", 256),
        "stream": False,
    }
    if "system" in value:
        payload["system"] = value["system"]
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(body) > MAX_REQUEST:
        raise PluginError("validation_error", "request exceeds size limit")
    return endpoint, body, value.get("timeout_ms", 30000)


def check_policy(endpoint: Endpoint, policy: Any) -> bool:
    """Reject missing/denied policy and enforce host, port and TLS restrictions."""
    parsed, host, port, _ = endpoint
    network = policy.get("network") if isinstance(policy, dict) else None
    if not isinstance(network, dict) or network.get("mode") not in (
        "allowlist",
        "loopback",
    ):
        raise PluginError("configuration_error", "network policy denies destination")
    hosts, ports = network.get("allow_hosts", []), network.get("allow_ports", [])
    if (
        not isinstance(hosts, list)
        or any(not isinstance(h, str) for h in hosts)
        or not isinstance(ports, list)
        or any(type(p) is not int or not 1 <= p <= 65535 for p in ports)
        or type(network.get("tls_required", False)) is not bool
    ):
        raise PluginError("configuration_error", "invalid network policy")
    loopback = is_loopback(host)
    if (
        (network["mode"] == "loopback" and not loopback)
        or (network["mode"] == "allowlist" and not hosts)
        or (hosts and host not in [normalize_host(h) for h in hosts])
        or (ports and port not in ports)
        or (network.get("tls_required") and parsed.scheme != "https")
    ):
        raise PluginError("configuration_error", "network policy denies destination")
    return loopback


def remaining(deadline: float) -> float:
    """Return the remaining absolute budget or raise a fixed timeout error."""
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise PluginError("timeout_error", "provider request timed out")
    return budget


def resolve_address(endpoint: Endpoint, loopback: bool, deadline: float) -> tuple:
    """Bound DNS waiting, validate every answer, and pin one safe socket address.

    A daemon resolver owns no socket or secret. If libc DNS cannot be cancelled,
    it may finish after the invocation, but cannot dispatch any network request.
    """
    _, host, port, _ = endpoint
    replies: queue.Queue = queue.Queue(maxsize=1)

    def resolve() -> None:
        """Publish DNS answers or a failure sentinel without leaking resolver text."""
        try:
            replies.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except (OSError, UnicodeError):
            replies.put(None)

    remaining(deadline)
    threading.Thread(target=resolve, daemon=True).start()
    try:
        addresses = replies.get(timeout=remaining(deadline))
    except queue.Empty:
        raise PluginError("timeout_error", "provider request timed out") from None
    remaining(deadline)
    if not addresses:
        raise PluginError("transport_error", "DNS resolution failed")
    for entry in addresses:
        try:
            ip = ipaddress.ip_address(entry[4][0])
            safe = (
                ip.is_loopback
                if loopback
                else ip.is_global
                and not ip.is_multicast
                and not ip.is_reserved
                and not getattr(ip, "is_site_local", False)
            )
            if entry[0] not in (socket.AF_INET, socket.AF_INET6) or not safe:
                raise ValueError()
        except (ValueError, IndexError, TypeError):
            raise PluginError(
                "configuration_error", "DNS returned an unsafe address"
            ) from None
    return addresses[0]


def _cancel(_signum: int, _frame: Any) -> None:
    """Interrupt DNS or I/O on SIGTERM and close any active transport socket."""
    if ACTIVE_SOCKET is not None:
        try:
            ACTIVE_SOCKET.close()
        except OSError:
            pass
    raise PluginError("cancelled", "request cancelled")


def _expire(_signum: int, _frame: Any) -> None:
    """Interrupt even trickling headers/body reads at the absolute deadline."""
    raise PluginError("timeout_error", "provider request timed out")


@contextmanager
def deadline_scope(deadline: float) -> Iterator[None]:
    """Arm a process-main-thread deadline/cancellation guard and restore it on exit."""
    old_alarm = signal.getsignal(signal.SIGALRM)
    old_term = signal.getsignal(signal.SIGTERM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    try:
        signal.signal(signal.SIGALRM, _expire)
        signal.signal(signal.SIGTERM, _cancel)
        signal.setitimer(signal.ITIMER_REAL, remaining(deadline))
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_alarm)
        signal.signal(signal.SIGTERM, old_term)
        if old_timer[0]:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.000001, old_timer[0] - (time.monotonic() - started)),
                old_timer[1],
            )


def safe_id(value: Any) -> str | None:
    """Allow only bounded ASCII identifiers in output and HTTP diagnostics."""
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value)
        else None
    )


def reject_json_constant(_value: str) -> Any:
    """Reject non-JSON NaN/Infinity extensions during native response parsing."""
    raise ValueError()


def classify_status(status: int, request_id: str | None = None) -> PluginError | None:
    """Map HTTP status to a canonical error with no provider message or body."""
    if status == 200:
        return None
    code = (
        "validation_error"
        if status in (400, 404, 413, 422)
        else (
            "auth_error"
            if status in (401, 403)
            else (
                "rate_limit_error"
                if status == 429
                else "timeout_error" if status in (408, 504) else "tool_execution_error"
            )
        )
    )
    suffix = f"; request_id={request_id}" if safe_id(request_id) else ""
    return PluginError(code, f"provider returned HTTP {status}{suffix}")


def read_body(response: http.client.HTTPResponse, raw: Any, deadline: float) -> bytes:
    """Read at most 1 MiB under the absolute deadline and reject truncation."""
    chunks = bytearray()
    while True:
        raw.settimeout(remaining(deadline))
        chunk = response.read(min(65536, MAX_RESPONSE + 1 - len(chunks)))
        remaining(deadline)
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > MAX_RESPONSE:
            raise PluginError("protocol_error", "provider response exceeds size limit")
    if response.length is not None and response.length > 0:
        raise PluginError("protocol_error", "truncated provider response")
    return bytes(chunks)


def verified_tls_context() -> ssl.SSLContext:
    """Load default CA trust and verify hostnames without session-key logging."""
    # create_default_context also reads SSLKEYLOGFILE and can persist session keys.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.load_default_certs()
    return context


def perform_request(
    endpoint: Endpoint, body: bytes, timeout_ms: int, secret: str, policy: Any
) -> tuple[bytes, str | None]:
    """Send exactly one pinned, verified-TLS POST; never use proxies or redirects."""
    global ACTIVE_SOCKET
    parsed, host, port, target = endpoint
    loopback = check_policy(endpoint, policy)
    deadline = time.monotonic() + timeout_ms / 1000
    raw = response = None
    try:
        with deadline_scope(deadline):
            address = resolve_address(endpoint, loopback, deadline)
            raw = socket.socket(address[0], address[1], address[2])
            ACTIVE_SOCKET = raw
            raw.settimeout(remaining(deadline))
            raw.connect(address[4])
            if parsed.scheme == "https":
                raw.settimeout(remaining(deadline))
                raw = verified_tls_context().wrap_socket(raw, server_hostname=host)
                ACTIVE_SOCKET = raw
            host_header = f"[{host}]" if ":" in host else host
            if port != (443 if parsed.scheme == "https" else 80):
                host_header += f":{port}"
            headers = (
                f"POST {target} HTTP/1.1\r\nHost: {host_header}\r\n"
                f"Authorization: Bearer {secret}\r\nanthropic-version: 2023-06-01\r\n"
                "Content-Type: application/json\r\nAccept: application/json\r\n"
                "Accept-Encoding: identity\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            )
            raw.settimeout(remaining(deadline))
            raw.sendall(headers.encode("ascii") + body)
            response = http.client.HTTPResponse(raw)
            raw.settimeout(remaining(deadline))
            response.begin()
            remaining(deadline)
            request_id = safe_id(response.getheader("request-id"))
            if response.status != 200:
                # Body failures must preserve the already-known HTTP classification.
                if request_id is None:
                    try:
                        data = json.loads(
                            read_body(response, raw, deadline),
                            parse_constant=reject_json_constant,
                        )
                        if isinstance(data, dict):
                            request_id = safe_id(data.get("request_id"))
                    except PluginError as exc:
                        if exc.code == "cancelled":
                            raise
                    except (
                        ValueError,
                        UnicodeError,
                        RecursionError,
                        OSError,
                        http.client.HTTPException,
                    ):
                        pass
                raise classify_status(response.status, request_id)
            if (
                response.getheader("Content-Encoding", "identity").strip().lower()
                != "identity"
            ):
                raise PluginError(
                    "protocol_error", "invalid provider response encoding"
                )
            length = response.getheader("Content-Length")
            transfer = response.getheader("Transfer-Encoding")
            if length is not None:
                length = length.strip(" \t")
            if (
                length is not None
                and (
                    not re.fullmatch(r"[0-9]+", length)
                    or len(length) > 20
                    or int(length) > MAX_RESPONSE
                )
            ) or (
                transfer is not None
                and (transfer.strip().lower() != "chunked" or length is not None)
            ):
                raise PluginError("protocol_error", "invalid provider response framing")
            return read_body(response, raw, deadline), request_id
    except http.client.IncompleteRead:
        raise PluginError("protocol_error", "truncated provider response") from None
    except (TimeoutError, socket.timeout):
        raise PluginError("timeout_error", "provider request timed out") from None
    except OSError:
        raise PluginError("transport_error", "provider connection failed") from None
    except http.client.HTTPException:
        if response is not None and response.status == 200:
            raise PluginError(
                "protocol_error", "invalid provider response framing"
            ) from None
        raise PluginError("transport_error", "provider connection failed") from None
    finally:
        ACTIVE_SOCKET = None
        try:
            if response is not None:
                try:
                    response.close()
                except OSError:
                    pass
        finally:
            if raw is not None:
                try:
                    raw.close()
                except OSError:
                    pass


def parse_response(
    raw: bytes, model: str, request_id: str | None = None
) -> tuple[dict, dict | None]:
    """Project native text blocks and best-effort canonical unsigned token usage."""
    try:
        if len(raw) > MAX_RESPONSE:
            raise ValueError()
        data = json.loads(raw.decode("utf-8"), parse_constant=reject_json_constant)
        if (
            not isinstance(data, dict)
            or data.get("type") != "message"
            or data.get("role") != "assistant"
        ):
            raise ValueError()
        blocks = data.get("content")
        if not isinstance(blocks, list) or not blocks:
            raise ValueError()
        texts = []
        for block in blocks:
            if (
                not isinstance(block, dict)
                or not isinstance(block.get("type"), str)
                or not block["type"]
            ):
                raise ValueError()
            if block["type"] == "text":
                if not isinstance(block.get("text"), str):
                    raise ValueError()
                texts.append(block["text"])
        text = "".join(texts)
        if not valid_text(text, MAX_RESPONSE):
            raise ValueError()
        output = {"response_text": text}
        stop = data.get("stop_reason")
        if stop is not None:
            if (
                not valid_text(stop, MAX_RESPONSE)
                or has_controls(stop)
                or stop in ("tool_use", "pause_turn")
            ):
                raise ValueError()
            output["finish_reason"] = stop
        for source, target in (
            (data.get("id"), "response_id"),
            (request_id, "request_id"),
        ):
            if safe_id(source):
                output[target] = source
        response_model = data.get("model")
        if valid_text(response_model, MAX_RESPONSE) and not has_controls(
            response_model
        ):
            output["response_model"] = response_model
        counts = data.get("usage")
        if not isinstance(counts, dict):
            return output, None
        incoming, outgoing = counts.get("input_tokens"), counts.get("output_tokens")
        if (
            type(incoming) is not int
            or type(outgoing) is not int
            or not 0 <= incoming <= UINT64_MAX
            or not 0 <= outgoing <= UINT64_MAX
            or incoming + outgoing > UINT64_MAX
        ):
            return output, None
        return output, {
            "input_tokens": incoming,
            "output_tokens": outgoing,
            "total_tokens": incoming + outgoing,
            "provider": "anthropic",
            "model": output.get("response_model", model),
        }
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PluginError("protocol_error", "invalid provider response") from None


def redact(value: Any, secret: str | None) -> Any:
    """Recursively redact credentials and padded/unpadded Base64 from string values."""
    variants = set()
    if secret:
        variants.add(secret)
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            encoded = encode(secret.encode("utf-8", errors="surrogatepass")).decode(
                "ascii"
            )
            variants.update((encoded, encoded.rstrip("=")))
    marker = next(
        (
            item
            for item in ("REDACTED", "redacted", "MASKED", "X", "Z", "_", "█")
            if not any(variant in item for variant in variants)
        ),
        "",
    )

    def clean(item: Any) -> Any:
        """Copy nested APP data, replacing only sensitive string contents."""
        if isinstance(item, str):
            for variant in sorted(variants, key=len, reverse=True):
                item = item.replace(variant, marker)
            return item
        if isinstance(item, dict):
            return {key: clean(entry) for key, entry in item.items()}
        if isinstance(item, list):
            return [clean(entry) for entry in item]
        return item

    return clean(value)


def result(
    request: dict,
    output: dict | None = None,
    usage: dict | None = None,
    error: dict | None = None,
) -> dict:
    """Build an APP terminal frame, omitting absent usage/error and empty telemetry."""
    frame = {
        "type": "app.invoke.result.v1",
        "protocol_version": "app.v1",
        "invoke_id": request.get("invoke_id", ""),
        "status": "failed" if error else "success",
        "output": output or {},
        "artifact_hints": [],
        "evidence_hints": [],
        "metrics": {},
    }
    if usage is not None:
        frame["usage"] = usage
        frame["artifact_hints"] = [
            {"name": "usage.json", "contents": json.dumps(usage)}
        ]
    if error is not None:
        frame["error"] = error
    return frame


def invoke(request: dict) -> dict:
    """Validate before dispatch, consume the secret, and emit sanitized APP data."""
    secret = None
    try:
        secret = os.environ.get(SECRET_NAME)
        if request.get("action") != "chat_complete":
            raise PluginError("validation_error", "unsupported action")
        endpoint, body, timeout = validate_input(request.get("input"))
        check_policy(endpoint, request.get("policy"))
        for field in ("timeout_ms", "max_timeout_ms"):
            if field in request:
                limit = request[field]
                if type(limit) is not int or limit < 1:
                    raise PluginError("validation_error", "invalid APP timeout")
                timeout = min(timeout, limit)
        if (
            not secret
            or not secret.isascii()
            or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in secret)
        ):
            raise PluginError("secret_resolution_error", "API key unavailable")
        raw, request_id = perform_request(
            endpoint, body, timeout, secret, request.get("policy")
        )
        output, usage = parse_response(raw, request["input"]["model"], request_id)
        # Redact before serializing usage so artifact contents match canonical usage.
        output = redact(output, secret)
        for field in ("response_id", "request_id"):
            if field in output and not safe_id(output[field]):
                del output[field]
        return redact(result(request, output, redact(usage, secret)), secret)
    except PluginError as exc:
        kind, retryability = ERROR_KINDS[exc.code]
        return redact(
            result(
                request,
                error={
                    "code": exc.code,
                    "class": kind,
                    "retryability": retryability,
                    "message": str(exc),
                },
            ),
            secret,
        )


def main() -> int:
    """Serve one handshake/invocation on stdio; normal paths emit no stderr."""
    if not sys.stdin.readline():
        return 1
    print(
        json.dumps(
            {
                "type": "app.handshake.response.v1",
                "protocol_version": "app.v1",
                "plugin": {"id": PLUGIN_ID, "version": PLUGIN_VERSION},
                "adapter_kind": "process",
                "capabilities": {
                    "actions": ["chat_complete"],
                    "supports_cancel": True,
                    "supports_health": False,
                    "supports_artifact_hints": True,
                    "supports_evidence_hints": False,
                },
            }
        ),
        flush=True,
    )
    line = sys.stdin.readline()
    if not line:
        return 1
    try:
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ValueError()
    except (ValueError, UnicodeError, RecursionError):
        request = {}
    previous_term = signal.signal(signal.SIGTERM, _cancel)
    try:
        frame = invoke(request)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
    print(json.dumps(frame), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
