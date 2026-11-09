#!/usr/bin/env python3
"""Bounded OpenAI-compatible Chat Completions APP process plugin."""

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from file_io import data_path, read_bytes, write_bytes  # noqa: E402

PLUGIN_ID = "openai.compatible"
PLUGIN_VERSION = "0.1.0"
SECRET_NAME = "OPENAI_COMPATIBLE_API_KEY"
MAX_REQUEST = 256 * 1024
MAX_RESPONSE = 1024 * 1024
ACTIVE_SOCKET = None
CANCELLED = False
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


class PluginError(Exception):
    """Safe diagnostic that never contains provider or credential data."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate_input(value, request=None):
    """Validate an action input and return defaults without coercing types."""
    if not isinstance(value, dict) or set(value) - {
        "base_url",
        "model",
        "messages",
        "max_tokens",
        "timeout_ms",
        "secret_refs",
        "user_text_file",
        "system_prompt",
        "response_file",
        "max_prompt_bytes",
    }:
        raise PluginError("validation_error", "invalid action input fields")
    if "secret_refs" in value and value["secret_refs"] != [SECRET_NAME]:
        raise PluginError("validation_error", "invalid secret reference")
    model = value.get("model")
    if (
        not isinstance(model, str)
        or not model
        or len(model) > 256
        or any(ord(c) < 32 for c in model)
    ):
        raise PluginError("validation_error", "invalid model")
    if ("messages" in value) == ("user_text_file" in value):
        raise PluginError(
            "validation_error", "exactly one of messages or user_text_file is required"
        )
    limit = value.get("max_prompt_bytes", MAX_REQUEST)
    if type(limit) is not int or not 1 <= limit <= MAX_REQUEST:
        raise PluginError("validation_error", "invalid max_prompt_bytes")
    if "system_prompt" in value and (
        "user_text_file" not in value
        or not isinstance(value["system_prompt"], str)
        or not value["system_prompt"]
    ):
        raise PluginError("validation_error", "system_prompt requires user_text_file")
    if "response_file" in value:
        try:
            data_path(request, value["response_file"])
        except (ValueError, TypeError, KeyError):
            raise PluginError("validation_error", "invalid response_file") from None
    messages = value.get("messages")
    if "user_text_file" in value:
        try:
            text = read_bytes(
                data_path(request, value["user_text_file"]), min(limit, 65536)
            ).decode("utf-8")
        except (OSError, ValueError, TypeError, KeyError):
            raise PluginError(
                "validation_error", "invalid bounded UTF-8 user_text_file"
            ) from None
        messages = (
            [{"role": "system", "content": value["system_prompt"]}]
            if "system_prompt" in value
            else []
        )
        messages.append({"role": "user", "content": text})
    if not isinstance(messages, list) or not 1 <= len(messages) <= 64:
        raise PluginError("validation_error", "messages must contain 1 to 64 entries")
    for item in messages:
        if (
            not isinstance(item, dict)
            or set(item) != {"role", "content"}
            or item["role"] not in ("system", "user", "assistant")
            or not isinstance(item["content"], str)
            or not item["content"]
            or len(item["content"].encode("utf-8")) > min(limit, 65536)
        ):
            raise PluginError("validation_error", "invalid message")
    if sum(len(item["content"].encode("utf-8")) for item in messages) > limit:
        raise PluginError("validation_error", "prompt exceeds max_prompt_bytes")
    for name, default, maximum in (
        ("max_tokens", 256, 4096),
        ("timeout_ms", 30000, 120000),
    ):
        number = value.get(name, default)
        if type(number) is not int or not 1 <= number <= maximum:
            raise PluginError("validation_error", f"invalid {name}")
    endpoint = parse_endpoint(value.get("base_url"))
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "max_tokens": value.get("max_tokens", 256),
            "stream": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    if len(body) > MAX_REQUEST:
        raise PluginError("validation_error", "request exceeds size limit")
    return endpoint, body, value.get("timeout_ms", 30000)


def parse_endpoint(raw):
    """Validate a base URL and return parsed endpoint and normalized target."""
    import ipaddress
    import urllib.parse

    if (
        not isinstance(raw, str)
        or not raw
        or any(ord(c) < 33 or ord(c) == 127 for c in raw)
    ):
        raise PluginError("configuration_error", "invalid base_url")
    try:
        parsed = urllib.parse.urlsplit(raw)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise PluginError("configuration_error", "invalid base_url") from None
    try:
        ip = ipaddress.ip_address(host) if host else None
    except ValueError:
        ip = None
    if (
        parsed.scheme not in ("https", "http")
        or not host
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (
            parsed.netloc.endswith(":")
            or (
                ":" in parsed.netloc
                and port is None
                and not parsed.netloc.startswith("[")
            )
        )
        or (port is not None and not 1 <= port <= 65535)
        or (
            parsed.scheme == "http"
            and host.lower() != "localhost"
            and not (ip and ip.is_loopback)
        )
    ):
        raise PluginError("configuration_error", "invalid base_url")
    if not all(c.isascii() and (c.isalnum() or c in ".-") for c in host) and ip is None:
        raise PluginError("configuration_error", "invalid base_url")
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        target = path
    else:
        target = path + "/chat/completions"
    if not target.startswith("/") or ".." in target.split("/") or "%" in target:
        raise PluginError("configuration_error", "invalid base_url path")
    return (
        parsed,
        host.lower().rstrip("."),
        port or (443 if parsed.scheme == "https" else 80),
        target,
    )


def check_policy(endpoint, policy):
    """Enforce the forwarded effective network policy for destination and port."""
    parsed, host, port, _ = endpoint
    network = policy.get("network") if isinstance(policy, dict) else None
    if not isinstance(network, dict):
        raise PluginError("configuration_error", "network policy is required")
    mode = network.get("mode")
    loopback = host == "localhost" or _is_loopback_ip(host)
    if mode == "allowlist":
        if host not in [
            str(h).lower().rstrip(".") for h in network.get("allow_hosts", [])
        ] or (network.get("allow_ports") and port not in network["allow_ports"]):
            raise PluginError("configuration_error", "destination is not allowlisted")
    elif mode != "loopback" or not loopback:
        raise PluginError("configuration_error", "network policy denies destination")
    if network.get("tls_required") and parsed.scheme != "https":
        raise PluginError("configuration_error", "TLS required by network policy")
    return loopback


def _is_loopback_ip(host):
    """Return whether a literal IP is loopback."""
    import ipaddress

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_address(endpoint, loopback):
    """Check every DNS result and pin the first safe socket address."""
    import ipaddress

    _, host, port, _ = endpoint
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses:
            raise PluginError("transport_error", "DNS returned no addresses")
        for entry in addresses:
            ip = ipaddress.ip_address(entry[4][0])
            if not ip.is_loopback if loopback else not ip.is_global:
                raise PluginError(
                    "configuration_error", "DNS returned an unsafe address"
                )
        return addresses[0]
    except (socket.gaierror, OSError) as exc:
        raise PluginError("transport_error", "DNS resolution failed") from exc


def classify_status(status):
    """Map non-200 HTTP status to a fixed safe APP error."""
    if status == 200:
        return None
    code = (
        "timeout_error"
        if status == 408
        else (
            "auth_error"
            if status in (401, 403)
            else "rate_limit_error" if status == 429 else "tool_execution_error"
        )
    )
    return PluginError(code, f"provider returned HTTP {status}")


def parse_response(raw):
    """Map one provider response to action output, optional APP usage and hint."""
    try:
        data = json.loads(raw)
        choice = data["choices"][0]
        message = choice["message"]
        text = message["content"]
        if message["role"] != "assistant" or not isinstance(text, str) or not text:
            raise ValueError()
        output = {"response_text": text}
        for source, target in (
            ("finish_reason", "finish_reason"),
            ("id", "response_id"),
            ("model", "response_model"),
        ):
            item = choice.get(source) if source == "finish_reason" else data.get(source)
            if isinstance(item, str):
                output[target] = item
        usage = data.get("usage")
        if (
            not isinstance(usage, dict)
            or type(usage.get("prompt_tokens")) is not int
            or type(usage.get("completion_tokens")) is not int
            or min(usage["prompt_tokens"], usage["completion_tokens"]) < 0
        ):
            return output, None
        total = usage.get("total_tokens")
        usage_output = {
            "input_tokens": usage["prompt_tokens"],
            "output_tokens": usage["completion_tokens"],
            "total_tokens": (
                total
                if type(total) is int and total >= 0
                else usage["prompt_tokens"] + usage["completion_tokens"]
            ),
            "model": output.get("response_model", ""),
            "provider": "openai-compatible",
        }
        return output, usage_output
    except (ValueError, TypeError, KeyError, IndexError):
        raise PluginError("protocol_error", "invalid provider response") from None


def perform_request(endpoint, body, timeout_ms, secret, policy):
    """POST with a pinned socket, verified TLS, deadline and bounded response."""
    import http.client
    import ssl

    global ACTIVE_SOCKET
    parsed, host, port, target = endpoint
    address = resolve_address(endpoint, check_policy(endpoint, policy))
    deadline = time.monotonic() + timeout_ms / 1000
    raw = socket.socket(address[0], address[1], address[2])
    ACTIVE_SOCKET = raw
    conn = None
    try:
        raw.settimeout(max(0.001, deadline - time.monotonic()))
        raw.connect(address[4])
        if parsed.scheme == "https":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            ACTIVE_SOCKET = raw
        conn = http.client.HTTPResponse(raw)
        host_header = f"[{host}]" if ":" in host else host
        if port != (443 if parsed.scheme == "https" else 80):
            host_header += f":{port}"
        request = (
            f"POST {target} HTTP/1.1\r\nHost: {host_header}\r\nAuthorization: Bearer"
            f" {secret}\r\nContent-Type: application/json\r\nAccept:"
            " application/json\r\nAccept-Encoding: identity\r\nContent-Length:"
            f" {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode("utf-8")
        raw.sendall(request + body)
        raw.settimeout(max(0.001, deadline - time.monotonic()))
        conn.begin()
        error = classify_status(conn.status)
        if error:
            raise error
        chunks = bytearray()
        while True:
            raw.settimeout(max(0.001, deadline - time.monotonic()))
            chunk = conn.read(min(65536, MAX_RESPONSE + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > MAX_RESPONSE:
                raise PluginError(
                    "protocol_error", "provider response exceeds size limit"
                )
        if conn.length is not None and conn.length > 0:
            raise PluginError("protocol_error", "truncated provider response")
        return bytes(chunks)
    except http.client.IncompleteRead as exc:
        raise PluginError("protocol_error", "truncated provider response") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise PluginError("timeout_error", "provider request timed out") from exc
    except (ssl.SSLError, OSError, http.client.HTTPException) as exc:
        if CANCELLED:
            raise PluginError("cancelled", "request cancelled") from exc
        raise PluginError("transport_error", "provider connection failed") from exc
    finally:
        ACTIVE_SOCKET = None
        raw.close()


def _cancel(_signal, _frame):
    """Interrupt an active connection on SIGTERM."""
    global CANCELLED
    CANCELLED = True
    if ACTIVE_SOCKET:
        ACTIVE_SOCKET.close()


def result(request, output=None, usage=None, error=None):
    """Build a canonical APP invoke result for success or failure."""
    hint = [{"name": "usage.json", "contents": json.dumps(usage)}] if usage else []
    return {
        "type": "app.invoke.result.v1",
        "protocol_version": "app.v1",
        "invoke_id": request["invoke_id"],
        "status": "failed" if error else "success",
        "output": output or {},
        **({"usage": usage} if usage else {}),
        "artifact_hints": hint,
        "evidence_hints": [],
        "metrics": {},
        **({"error": error} if error is not None else {}),
    }


def invoke(request):
    """Validate and execute one APP invocation; return only safe diagnostics."""
    try:
        if request.get("action") != "chat_complete":
            raise PluginError("validation_error", "unsupported action")
        endpoint, body, timeout = validate_input(request.get("input"), request)
        check_policy(endpoint, request.get("policy"))
        secret = os.environ.get(SECRET_NAME)
        if not secret or any(c in secret for c in "\r\n"):
            raise PluginError("secret_resolution_error", "API key unavailable")
        output, usage = parse_response(
            perform_request(endpoint, body, timeout, secret, request.get("policy"))
        )
        if "response_file" in request["input"]:
            try:
                write_bytes(
                    data_path(request, request["input"]["response_file"]),
                    output["response_text"].encode("utf-8"),
                )
            except (OSError, ValueError, UnicodeError):
                raise PluginError(
                    "validation_error", "invalid response_file destination"
                ) from None
        return result(request, output, usage)
    except PluginError as exc:
        kind, retryability = ERROR_KINDS[exc.code]
        return result(
            request,
            error={
                "code": exc.code,
                "class": kind,
                "retryability": retryability,
                "message": str(exc),
            },
        )


def main():
    """Serve one APP handshake and invocation on stdio."""
    signal.signal(signal.SIGTERM, _cancel)
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
    print(json.dumps(invoke(json.loads(line))), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
