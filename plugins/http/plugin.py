#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import signal
import socket
import ssl
import sys
import tempfile
import urllib.parse

APP_PROTOCOL = "app.v1"
ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
SECRET_SLOT_NAMES = tuple(f"HTTP_SECRET_{index}" for index in range(1, 9))
DEFAULT_TIMEOUT_MS = 30000
MAX_TIMEOUT_MS = 120000
DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_RESPONSE_BYTES = 25 * 1024 * 1024
MAX_HEADER_COUNT = 64
MAX_HEADER_VALUE_BYTES = 4096
DEFAULT_MAX_REDIRECTS = 5
READ_CHUNK_SIZE = 65536
METADATA_IPS = {"169.254.169.254"}
DEFAULT_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
CANONICAL_ERRORS = {
    "configuration_error": {"class": "configuration", "retryability": "none"},
    "auth_error": {"class": "auth", "retryability": "operator"},
    "secret_resolution_error": {"class": "secret", "retryability": "operator"},
    "validation_error": {"class": "validation", "retryability": "none"},
    "timeout_error": {"class": "timeout", "retryability": "safe"},
    "cancelled": {"class": "cancellation", "retryability": "none"},
    "rate_limit_error": {"class": "rate_limit", "retryability": "safe"},
    "transport_error": {"class": "transport", "retryability": "safe"},
    "protocol_error": {"class": "protocol", "retryability": "none"},
    "tool_execution_error": {"class": "tool", "retryability": "safe"},
    "adapter_internal_error": {"class": "internal", "retryability": "operator"},
}

ACTIVE_CONNECTION = None
CANCEL_REQUESTED = False
CURRENT_TEMP_PATH = None


def load_plugin_version():
    manifest_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "plugin.yaml"
    )
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("version: "):
                    version = line.split(": ", 1)[1].strip().strip("'\"")
                    if version:
                        return version
    except OSError:
        pass
    return "0.0.0"


PLUGIN_VERSION = load_plugin_version()


class ValidationError(Exception):
    pass


class HttpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def read_frame():
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


def canonical_error(code, message):
    payload = dict(CANONICAL_ERRORS[code])
    payload.update({"code": code, "message": message})
    return payload


def artifact_hint(name, contents):
    return {"name": name, "contents": contents}


def result_frame(
    request: dict,
    status: str,
    output: dict | None = None,
    error: dict | None = None,
    artifact_hints: list | None = None,
) -> dict:
    """Build a strict APP result, omitting absent optional fields."""
    frame = {
        "type": "app.invoke.result.v1",
        "protocol_version": APP_PROTOCOL,
        "invoke_id": request["invoke_id"],
        "status": status,
        "output": output or {},
        "artifact_hints": artifact_hints or [],
        "evidence_hints": [],
        "metrics": {},
    }
    if error is not None:
        frame["error"] = error
    return frame


def progress_frame(request: dict, bytes_downloaded: int) -> dict:
    """Report downloaded bytes using the supported APP status preview fields."""
    return {
        "type": "app.invoke.progress.v1",
        "protocol_version": APP_PROTOCOL,
        "invoke_id": request["invoke_id"],
        "stream": "status",
        "delta_lines": 0,
        "line_preview": f"Downloaded {bytes_downloaded} bytes",
    }


def is_loopback_host(hostname):
    host = (hostname or "").strip().lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def canonical_host(hostname):
    return str(hostname or "").strip().lower().rstrip(".")


def port_for_scheme(parsed_url):
    return parsed_url.port or (443 if parsed_url.scheme == "https" else 80)


def parse_network_policy(request):
    return (request.get("policy") or {}).get("network") or {}


def root_run_dir(request):
    run_root = os.path.normpath(request["paths"]["run_root"])
    while os.path.basename(os.path.dirname(run_root)) == "contained_runs":
        run_root = os.path.dirname(os.path.dirname(run_root))
    run_name = os.path.basename(run_root)
    parent = os.path.dirname(run_root)
    if run_name.startswith("run_"):
        root_run_id = run_name[len("run_") :].split("__", 1)[0]
        return os.path.join(parent, f"run_{root_run_id}")
    return run_root


def handle_sigterm(_signum, _frame):
    global CANCEL_REQUESTED
    CANCEL_REQUESTED = True
    connection = ACTIVE_CONNECTION
    if connection is not None:
        try:
            connection.close()
        except OSError:
            pass


def install_signal_handler():
    try:
        signal.signal(signal.SIGTERM, handle_sigterm)
    except ValueError:
        pass


def reset_cancel_state():
    global ACTIVE_CONNECTION, CANCEL_REQUESTED, CURRENT_TEMP_PATH
    ACTIVE_CONNECTION = None
    CANCEL_REQUESTED = False
    CURRENT_TEMP_PATH = None


def validate_url(parsed):
    if parsed.scheme not in {"http", "https"}:
        raise ValidationError("url must use http or https")
    if not parsed.hostname:
        raise ValidationError("url must include a host")
    if parsed.username or parsed.password:
        raise ValidationError("url must not embed credentials")
    if any(ord(char) < 32 for char in parsed.geturl()):
        raise ValidationError("url contains control characters")


def is_blocked_ip(ip_text):
    ip = ipaddress.ip_address(ip_text)
    if ip_text in METADATA_IPS:
        return True
    return any(
        [
            ip.is_loopback,
            ip.is_private,
            ip.is_link_local,
            ip.is_reserved,
            ip.is_multicast,
            getattr(ip, "is_unspecified", False),
        ]
    )


def validate_request_input(input_payload):
    if not isinstance(input_payload, dict):
        raise ValidationError("input must be an object")
    method = str(input_payload.get("method") or "GET").upper()
    if method not in ALLOWED_METHODS:
        raise ValidationError(f"unsupported method {method}")
    url = str(input_payload.get("url") or "").strip()
    if not url:
        raise ValidationError("url is required")
    parsed = urllib.parse.urlsplit(url)
    validate_url(parsed)
    if (
        input_payload.get("json") is not None
        and input_payload.get("body_text") is not None
    ):
        raise ValidationError("json and body_text are mutually exclusive")
    timeout_ms = int(input_payload.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
    if timeout_ms < 1 or timeout_ms > MAX_TIMEOUT_MS:
        raise ValidationError(f"timeout_ms must be between 1 and {MAX_TIMEOUT_MS}")
    max_response_bytes = int(
        input_payload.get("max_response_bytes") or DEFAULT_MAX_RESPONSE_BYTES
    )
    if max_response_bytes < 1 or max_response_bytes > MAX_RESPONSE_BYTES:
        raise ValidationError(
            f"max_response_bytes must be between 1 and {MAX_RESPONSE_BYTES}"
        )
    headers = input_payload.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValidationError("headers must be an object")
    if len(headers) > MAX_HEADER_COUNT:
        raise ValidationError(
            f"headers must contain at most {MAX_HEADER_COUNT} entries"
        )
    for name, value in headers.items():
        if not str(name).strip():
            raise ValidationError("header names must be non-empty")
        if len(str(value).encode("utf-8")) > MAX_HEADER_VALUE_BYTES:
            raise ValidationError("header values must stay within the size limit")
    query = input_payload.get("query") or {}
    if not isinstance(query, dict):
        raise ValidationError("query must be an object")
    secret_headers = input_payload.get("secret_headers") or []
    if not isinstance(secret_headers, list):
        raise ValidationError("secret_headers must be an array")
    for item in secret_headers:
        if not isinstance(item, dict):
            raise ValidationError("secret_headers entries must be objects")
        secret_name = str(item.get("secret") or "")
        header_name = str(item.get("header") or "")
        format_string = str(item.get("format") or "{value}")
        if secret_name not in SECRET_SLOT_NAMES:
            raise ValidationError(f"unsupported secret slot {secret_name}")
        if not header_name.strip():
            raise ValidationError("secret_headers.header is required")
        if "{value}" not in format_string:
            raise ValidationError("secret_headers.format must contain {value}")
    return {
        "method": method,
        "url": url,
        "parsed_url": parsed,
        "timeout_ms": timeout_ms,
        "max_response_bytes": max_response_bytes,
        "headers": headers,
        "query": query,
        "secret_headers": secret_headers,
        "json": input_payload.get("json"),
        "body_text": input_payload.get("body_text"),
        "expected_status": input_payload.get("expected_status"),
    }


def resolve_download_dest(request: dict, dest: str | None) -> str:
    """Resolve scratch run:// or durable workspace-relative destinations.

    Keep each destination contained in its root.
    """
    raw_dest = str(dest or "").strip()
    scratch = raw_dest.startswith("run://")
    relative_path = raw_dest[len("run://") :] if scratch else raw_dest
    if os.path.isabs(relative_path) or ":" in relative_path or "\\" in relative_path:
        raise ValidationError("dest must be a workspace-relative path or run:// path")
    if not relative_path:
        raise ValidationError("dest must include a non-empty path")
    run_root = os.path.realpath(
        root_run_dir(request) if scratch else request["paths"]["workspace_dir"]
    )
    candidate = os.path.realpath(os.path.join(run_root, relative_path))
    if candidate == run_root or not candidate.startswith(run_root + os.sep):
        raise ValidationError("dest must stay within the run root or workspace root")
    return candidate


def validate_download_input(request):
    input_payload = request.get("input") or {}
    if not isinstance(input_payload, dict):
        raise ValidationError("input must be an object")
    method = str(input_payload.get("method") or "GET").upper()
    if method not in {"GET", "POST"}:
        raise ValidationError(f"unsupported download method {method}")
    url = str(input_payload.get("url") or "").strip()
    if not url:
        raise ValidationError("url is required")
    parsed = urllib.parse.urlsplit(url)
    validate_url(parsed)
    headers = input_payload.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValidationError("headers must be an object")
    query = input_payload.get("query") or {}
    if not isinstance(query, dict):
        raise ValidationError("query must be an object")
    timeout_ms = int(input_payload.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
    if timeout_ms < 1 or timeout_ms > MAX_TIMEOUT_MS:
        raise ValidationError(f"timeout_ms must be between 1 and {MAX_TIMEOUT_MS}")
    max_bytes = int(input_payload.get("max_bytes") or DEFAULT_MAX_DOWNLOAD_BYTES)
    if max_bytes < 1 or max_bytes > MAX_DOWNLOAD_BYTES:
        raise ValidationError(f"max_bytes must be between 1 and {MAX_DOWNLOAD_BYTES}")
    secret_headers = input_payload.get("secret_headers") or []
    if not isinstance(secret_headers, list):
        raise ValidationError("secret_headers must be an array")
    for item in secret_headers:
        if not isinstance(item, dict):
            raise ValidationError("secret_headers entries must be objects")
        secret_name = str(item.get("secret") or "")
        header_name = str(item.get("header") or "")
        format_string = str(item.get("format") or "{value}")
        if secret_name not in SECRET_SLOT_NAMES:
            raise ValidationError(f"unsupported secret slot {secret_name}")
        if not header_name.strip():
            raise ValidationError("secret_headers.header is required")
        if "{value}" not in format_string:
            raise ValidationError("secret_headers.format must contain {value}")
    expected_sha = str(input_payload.get("sha256") or "").strip().lower()
    if expected_sha and (
        len(expected_sha) != 64
        or any(char not in "0123456789abcdef" for char in expected_sha)
    ):
        raise ValidationError("sha256 must be a lowercase 64-character hex digest")
    return {
        "method": method,
        "url": url,
        "parsed_url": parsed,
        "headers": headers,
        "query": query,
        "secret_headers": secret_headers,
        "timeout_ms": timeout_ms,
        "max_bytes": max_bytes,
        "sha256": expected_sha,
        "dest_path": resolve_download_dest(request, input_payload.get("dest")),
    }


def enforce_network_policy(request, parsed_url):
    policy = parse_network_policy(request)
    mode = str(policy.get("mode") or "").strip().lower()
    host = canonical_host(parsed_url.hostname)
    port = port_for_scheme(parsed_url)
    if not mode or mode == "any":
        if os.environ.get("HTTP_ALLOW_ANY") == "1":
            return policy
        raise ValidationError(
            "external egress requires policy.network.mode=allowlist or HTTP_ALLOW_ANY=1"
        )
    if mode in {"none", "loopback"}:
        raise HttpError(
            "transport_error", f"network policy mode {mode} blocks external egress"
        )
    if mode != "allowlist":
        raise ValidationError(f"unsupported network policy mode {mode}")
    allowed_hosts = {
        canonical_host(item)
        for item in policy.get("allow_hosts") or []
        if canonical_host(item)
    }
    allowed_ports = {int(item) for item in policy.get("allow_ports") or []}
    if host.lower() not in allowed_hosts:
        raise ValidationError(
            f"host {host} is not present in policy.network.allow_hosts"
        )
    if allowed_ports and port not in allowed_ports:
        raise ValidationError(
            f"port {port} is not present in policy.network.allow_ports"
        )
    if parsed_url.scheme == "http" and not is_loopback_host(host):
        raise ValidationError("http URLs are only allowed for loopback hosts")
    if policy.get("tls_required") and parsed_url.scheme != "https":
        raise ValidationError("policy.network.tls_required requires an https URL")
    return policy


def validate_resolved_addresses(parsed_url, policy):
    host = canonical_host(parsed_url.hostname)
    port = port_for_scheme(parsed_url)
    allowed_hosts = {
        canonical_host(item)
        for item in policy.get("allow_hosts") or []
        if canonical_host(item)
    }
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as err:
        raise HttpError("transport_error", str(err)) from err

    validated = []
    seen = set()
    for family, socktype, proto, _, sockaddr in records:
        ip_text = sockaddr[0]
        if ip_text in seen:
            continue
        seen.add(ip_text)
        if ip_text in METADATA_IPS:
            raise ValidationError(f"resolved IP {ip_text} is blocked")
        if is_blocked_ip(ip_text) and ip_text not in allowed_hosts:
            raise ValidationError(f"resolved IP {ip_text} is blocked for host {host}")
        validated.append(
            {
                "family": family,
                "socktype": socktype,
                "proto": proto,
                "ip": ip_text,
                "port": port,
                "sockaddr": sockaddr,
            }
        )
    if not validated:
        raise HttpError(
            "transport_error", f"no usable addresses resolved for host {host}"
        )
    return validated


def build_request_url(parsed_url, query):
    pairs = urllib.parse.parse_qsl(parsed_url.query, keep_blank_values=True)
    for key, value in query.items():
        if isinstance(value, list):
            for item in value:
                pairs.append((str(key), str(item)))
        else:
            pairs.append((str(key), str(value)))
    encoded_query = urllib.parse.urlencode(pairs, doseq=True)
    return urllib.parse.urlunsplit(
        (
            parsed_url.scheme,
            parsed_url.netloc,
            parsed_url.path or "/",
            encoded_query,
            parsed_url.fragment,
        )
    )


def build_headers(headers, secret_headers, input_payload):
    merged = {str(name): str(value) for name, value in headers.items()}
    merged.setdefault("Accept-Encoding", "identity")
    for item in secret_headers:
        secret_name = str(item["secret"])
        secret_value = os.environ.get(secret_name) or ""
        if not secret_value:
            raise HttpError(
                "secret_resolution_error", f"secret {secret_name} is not available"
            )
        merged[str(item["header"])] = str(item.get("format") or "{value}").replace(
            "{value}", secret_value
        )
    if input_payload.get("json") is not None:
        merged.setdefault("Content-Type", "application/json")
    return merged


def build_request_target(parsed_url, query):
    encoded_url = build_request_url(parsed_url, query)
    rebuilt = urllib.parse.urlsplit(encoded_url)
    target = urllib.parse.urlunsplit(
        ("", "", rebuilt.path or "/", rebuilt.query, rebuilt.fragment)
    )
    return encoded_url, target


def host_header_value(parsed_url):
    host = parsed_url.hostname or ""
    port = port_for_scheme(parsed_url)
    if (parsed_url.scheme == "https" and port == 443) or (
        parsed_url.scheme == "http" and port == 80
    ):
        return host
    return f"{host}:{port}"


class PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, timeout, resolved_ip, family, sockaddr):
        super().__init__(host=host, port=port, timeout=timeout)
        self.resolved_ip = resolved_ip
        self.family = family
        self.sockaddr = sockaddr

    def connect(self):
        self.sock = socket.socket(self.family, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.sockaddr)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, timeout, resolved_ip, family, sockaddr, context):
        super().__init__(host=host, port=port, timeout=timeout, context=context)
        self.resolved_ip = resolved_ip
        self.family = family
        self.sockaddr = sockaddr

    def connect(self):
        raw_sock = socket.socket(self.family, socket.SOCK_STREAM)
        raw_sock.settimeout(self.timeout)
        raw_sock.connect(self.sockaddr)
        self.sock = self._context.wrap_socket(raw_sock, server_hostname=self.host)


def open_http_exchange(parsed_url, input_payload, policy, body, request_headers):
    global ACTIVE_CONNECTION
    validated_addresses = validate_resolved_addresses(parsed_url, policy)
    encoded_url, target = build_request_target(parsed_url, input_payload["query"])
    errors = []
    for address in validated_addresses:
        connection = None
        opened = False
        try:
            timeout_seconds = input_payload["timeout_ms"] / 1000.0
            if parsed_url.scheme == "https":
                connection = PinnedHTTPSConnection(
                    host=parsed_url.hostname,
                    port=port_for_scheme(parsed_url),
                    timeout=timeout_seconds,
                    resolved_ip=address["ip"],
                    family=address["family"],
                    sockaddr=address["sockaddr"],
                    context=ssl.create_default_context(),
                )
            else:
                connection = PinnedHTTPConnection(
                    host=parsed_url.hostname,
                    port=port_for_scheme(parsed_url),
                    timeout=timeout_seconds,
                    resolved_ip=address["ip"],
                    family=address["family"],
                    sockaddr=address["sockaddr"],
                )
            ACTIVE_CONNECTION = connection
            connection.putrequest(
                input_payload["method"],
                target,
                skip_host=True,
                skip_accept_encoding=True,
            )
            connection.putheader("Host", host_header_value(parsed_url))
            for name, value in request_headers.items():
                connection.putheader(name, value)
            if body is not None:
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            opened = True
            return connection, response, encoded_url
        except TimeoutError as err:
            raise err
        except socket.timeout as err:
            raise err
        except ssl.SSLError as err:
            errors.append(str(err))
        except OSError as err:
            if CANCEL_REQUESTED:
                raise HttpError("cancelled", "request cancelled by SIGTERM") from err
            errors.append(str(err))
        finally:
            if connection is not None and not opened:
                if ACTIVE_CONNECTION is connection:
                    ACTIVE_CONNECTION = None
                try:
                    connection.close()
                except OSError:
                    pass
    ACTIVE_CONNECTION = None
    if errors:
        raise HttpError("transport_error", errors[0])
    raise HttpError("transport_error", "request could not establish a connection")


def normalize_response_headers(headers):
    normalized = {}
    for name, value in list(headers.items())[:MAX_HEADER_COUNT]:
        normalized[str(name).lower()] = str(value)
    return normalized


def decode_response_body(body, headers):
    content_type = str(headers.get("content-type") or "")
    if "json" in content_type:
        try:
            return {"json": json.loads(body.decode("utf-8"))}
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    try:
        return {"text": body.decode("utf-8")}
    except UnicodeDecodeError:
        return {}


def matches_expected_status(expected_status, status_code):
    if expected_status is None:
        return True
    if isinstance(expected_status, list):
        return status_code in [int(item) for item in expected_status]
    return status_code == int(expected_status)


def read_response_body(response, max_response_bytes):
    body_parts = []
    body_bytes = 0
    truncated = False
    while True:
        chunk = response.read(READ_CHUNK_SIZE)
        if not chunk:
            break
        if body_bytes < max_response_bytes:
            remaining = max_response_bytes - body_bytes
            body_parts.append(chunk[:remaining])
        body_bytes += len(chunk)
        if body_bytes > max_response_bytes:
            truncated = True
            break
    return b"".join(body_parts), truncated


def perform_single_http_request(
    parsed_url, input_payload, policy, body, request_headers
):
    connection = None
    try:
        connection, response, encoded_url = open_http_exchange(
            parsed_url,
            input_payload,
            policy,
            body,
            request_headers,
        )
        response_body, truncated = read_response_body(
            response, input_payload["max_response_bytes"]
        )
        normalized_headers = normalize_response_headers(response.headers)
        return {
            "status_code": int(response.status),
            "headers": normalized_headers,
            "body": response_body,
            "truncated": truncated,
            "url_final": encoded_url,
            "location": normalized_headers.get("location"),
        }
    finally:
        global ACTIVE_CONNECTION
        ACTIVE_CONNECTION = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass


def perform_http_request(input_payload, request):
    parsed_url = input_payload["parsed_url"]
    policy = enforce_network_policy(request, parsed_url)
    body = None
    if input_payload["json"] is not None:
        body = json.dumps(input_payload["json"]).encode("utf-8")
    elif input_payload["body_text"] is not None:
        body = str(input_payload["body_text"]).encode("utf-8")
    request_headers = build_headers(
        input_payload["headers"], input_payload["secret_headers"], input_payload
    )
    redirect_limit = int(policy.get("max_redirects") or DEFAULT_MAX_REDIRECTS)
    redirect_count = 0
    while True:
        response = perform_single_http_request(
            parsed_url, input_payload, policy, body, request_headers
        )
        status_code = response["status_code"]
        location = response.get("location")
        if status_code not in {301, 302, 303, 307, 308} or not location:
            return response
        if redirect_count >= redirect_limit:
            raise ValidationError(f"redirect limit exceeded ({redirect_limit})")
        redirect_count += 1
        next_url = urllib.parse.urljoin(response["url_final"], location)
        parsed_url = urllib.parse.urlsplit(next_url)
        validate_url(parsed_url)
        enforce_network_policy(request, parsed_url)
        if status_code in {301, 302, 303} and input_payload["method"] not in {
            "GET",
            "HEAD",
        }:
            input_payload = dict(input_payload)
            input_payload["method"] = "GET"
            body = None


def handle_request_action(request):
    validated = validate_request_input(request.get("input") or {})
    response = perform_http_request(validated, request)
    status_code = response["status_code"]
    if status_code in {401, 403}:
        raise HttpError("auth_error", f"request failed with status {status_code}")
    if status_code == 429:
        raise HttpError("rate_limit_error", "request was rate limited")
    if not matches_expected_status(validated["expected_status"], status_code):
        raise HttpError("tool_execution_error", f"unexpected HTTP status {status_code}")
    output = {
        "status_code": status_code,
        "headers": response["headers"],
        "body_bytes": len(response["body"]),
        "truncated": response["truncated"],
        "url_final": response["url_final"],
    }
    output.update(decode_response_body(response["body"], response["headers"]))
    return output


def stream_download_response(request, response, output_path, max_bytes, expected_sha):
    global CURRENT_TEMP_PATH
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    checksum = hashlib.sha256()
    total_bytes = 0
    temp_fd, temp_path = tempfile.mkstemp(
        prefix=".http-download-", dir=os.path.dirname(output_path)
    )
    CURRENT_TEMP_PATH = temp_path
    try:
        with os.fdopen(temp_fd, "wb") as handle:
            while True:
                if CANCEL_REQUESTED:
                    raise HttpError("cancelled", "request cancelled by SIGTERM")
                chunk = response.read(READ_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise ValidationError(
                        f"download exceeded max_bytes limit of {max_bytes}"
                    )
                checksum.update(chunk)
                handle.write(chunk)
                emit(progress_frame(request, total_bytes))
        digest = checksum.hexdigest()
        if expected_sha and digest != expected_sha:
            raise ValidationError("sha256 checksum mismatch")
        os.replace(temp_path, output_path)
        CURRENT_TEMP_PATH = None
        return total_bytes, digest
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        CURRENT_TEMP_PATH = None
        raise


def perform_download(input_payload, request):
    parsed_url = input_payload["parsed_url"]
    policy = enforce_network_policy(request, parsed_url)
    request_headers = build_headers(
        input_payload["headers"], input_payload["secret_headers"], input_payload
    )
    redirect_limit = int(policy.get("max_redirects") or DEFAULT_MAX_REDIRECTS)
    redirect_count = 0
    while True:
        connection = None
        try:
            connection, response, encoded_url = open_http_exchange(
                parsed_url,
                input_payload,
                policy,
                None,
                request_headers,
            )
            status_code = int(response.status)
            headers = normalize_response_headers(response.headers)
            location = headers.get("location")
            if status_code in {301, 302, 303, 307, 308} and location:
                if redirect_count >= redirect_limit:
                    raise ValidationError(f"redirect limit exceeded ({redirect_limit})")
                redirect_count += 1
                next_url = urllib.parse.urljoin(encoded_url, location)
                parsed_url = urllib.parse.urlsplit(next_url)
                validate_url(parsed_url)
                enforce_network_policy(request, parsed_url)
                continue
            if status_code in {401, 403}:
                raise HttpError(
                    "auth_error", f"request failed with status {status_code}"
                )
            if status_code == 429:
                raise HttpError("rate_limit_error", "request was rate limited")
            if status_code < 200 or status_code >= 300:
                raise HttpError(
                    "tool_execution_error", f"unexpected HTTP status {status_code}"
                )
            bytes_written, digest = stream_download_response(
                request,
                response,
                input_payload["dest_path"],
                input_payload["max_bytes"],
                input_payload["sha256"],
            )
            return {
                "path": input_payload["dest_path"],
                "bytes": bytes_written,
                "content_type": headers.get("content-type", "application/octet-stream"),
                "sha256": digest,
                "truncated": False,
                "url_final": encoded_url,
            }
        finally:
            global ACTIVE_CONNECTION
            ACTIVE_CONNECTION = None
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass


def handle_download_action(request: dict) -> tuple[dict, list]:
    """Download raw bytes to the selected filesystem root and hint only JSON metadata.

    APP inline hints cannot promote binary files; workspace outputs survive invocation
    cleanup, whereas run:// outputs are deliberately scratch-only.
    """
    validated = validate_download_input(request)
    output = perform_download(validated, request)
    hints = [artifact_hint("download.json", json.dumps(output, sort_keys=True))]
    return output, hints


def main():
    install_signal_handler()
    handshake = read_frame()
    if handshake is None:
        return 1
    emit(
        {
            "type": "app.handshake.response.v1",
            "protocol_version": APP_PROTOCOL,
            "plugin": {"id": "http", "version": PLUGIN_VERSION},
            "adapter_kind": "process",
            "capabilities": {
                "actions": ["request", "download"],
                "supports_cancel": True,
                "supports_health": False,
                "supports_artifact_hints": True,
                "supports_evidence_hints": True,
            },
        }
    )

    request = read_frame()
    if request is None:
        return 1

    action = request.get("action")
    reset_cancel_state()
    try:
        if action == "request":
            output = handle_request_action(request)
            emit(result_frame(request, "success", output=output, error=None))
            return 0
        if action == "download":
            output, artifact_hints = handle_download_action(request)
            emit(
                result_frame(
                    request,
                    "success",
                    output=output,
                    error=None,
                    artifact_hints=artifact_hints,
                )
            )
            return 0
        emit(
            result_frame(
                request,
                "failed",
                error=canonical_error(
                    "validation_error", f"unsupported action {action}"
                ),
            )
        )
        return 0
    except ValidationError as err:
        emit(
            result_frame(
                request, "failed", error=canonical_error("validation_error", str(err))
            )
        )
        return 0
    except HttpError as err:
        emit(
            result_frame(
                request, "failed", error=canonical_error(err.code, err.message)
            )
        )
        return 0
    except socket.timeout:
        emit(
            result_frame(
                request,
                "failed",
                error=canonical_error("timeout_error", "request timed out"),
            )
        )
        return 0
    except ssl.SSLError as err:
        emit(
            result_frame(
                request, "failed", error=canonical_error("transport_error", str(err))
            )
        )
        return 0
    except OSError as err:
        emit(
            result_frame(
                request, "failed", error=canonical_error("transport_error", str(err))
            )
        )
        return 0
    finally:
        temp_path = CURRENT_TEMP_PATH
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        reset_cancel_state()


if __name__ == "__main__":
    raise SystemExit(main())
