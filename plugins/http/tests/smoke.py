#!/usr/bin/env python3
import hashlib
import json
import pathlib
import sys
import tempfile
import urllib.parse
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))

from support import (  # noqa: E402
    app_invoke_request,
    assert_app_frames,
    assert_canonical_error,
    load_plugin_module,
    read_json,
    run_process_plugin_main,
    validate_json_schema,
)


def with_perform_single(module, *responses):
    return mock.patch.object(
        module, "perform_single_http_request", side_effect=list(responses)
    )


class DummyConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeStreamingResponse:
    def __init__(self, status, chunks, headers=None, on_read=None):
        self.status = status
        self._chunks = list(chunks)
        self.headers = headers or {}
        self._on_read = on_read

    def read(self, _size=None):
        if self._on_read is not None:
            callback = self._on_read
            self._on_read = None
            callback()
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def with_open_exchange(module, *responses):
    return mock.patch.object(module, "open_http_exchange", side_effect=list(responses))


def manifest_version():
    for line in (ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version: "):
            version = line.split(": ", 1)[1].strip().strip("'\"")
            if version:
                return version
    raise AssertionError("plugin.yaml is missing a non-empty version field")


def allowlist_request(input_payload, action="request"):
    request = app_invoke_request(action, input_payload)
    request["policy"] = {
        "network": {
            "mode": "allowlist",
            "allow_hosts": ["api.example.test"],
            "allow_ports": [443],
            "tls_required": True,
            "max_redirects": 3,
        }
    }
    return request


def main():
    for required in ["plugin.yaml", "plugin.py", "workflow.yaml", "README.md"]:
        if not (ROOT / required).exists():
            raise AssertionError(f"missing required file: {required}")

    output_schema = read_json(ROOT / "schemas/request.output.schema.json")
    module = load_plugin_module(ROOT)
    expected_plugin_version = manifest_version()

    success_request = allowlist_request(
        {
            "method": "GET",
            "url": "https://api.example.test/v1",
            "headers": {"Accept": "application/json"},
            "query": {"page": 1},
            "expected_status": [200],
        }
    )
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        success_request,
        [
            with_perform_single(
                module,
                {
                    "status_code": 200,
                    "headers": {"content-type": "application/json"},
                    "body": json.dumps({"ok": True}).encode("utf-8"),
                    "truncated": False,
                    "url_final": "https://api.example.test/v1?page=1",
                    "location": None,
                },
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "request success path should not fail, got"
            f" exit={exit_code} stderr={stderr}"
        )
    _, result = assert_app_frames(frames, "http")
    if frames[0]["plugin"]["version"] != expected_plugin_version:
        raise AssertionError("handshake plugin version does not match plugin.yaml")
    if sorted(frames[0]["capabilities"]["actions"]) != ["download", "request"]:
        raise AssertionError(
            f"unexpected actions in handshake: {frames[0]['capabilities']['actions']}"
        )
    if result["status"] != "success":
        raise AssertionError(f"expected request success result, got: {result}")
    validate_json_schema(output_schema, result["output"])
    if result["output"]["json"] != {"ok": True}:
        raise AssertionError(f"unexpected JSON output: {result['output']}")

    missing_url_request = app_invoke_request("request", {"method": "GET"})
    exit_code, frames, stderr = run_process_plugin_main(module, missing_url_request)
    if exit_code != 0 or stderr:
        raise AssertionError("missing-url path should still return a normal APP result")
    _, missing_url_result = assert_app_frames(frames, "http")
    assert_canonical_error(
        missing_url_result["error"],
        expected_code="validation_error",
        message_contains=["url is required"],
    )

    mixed_body_request = app_invoke_request(
        "request",
        {"url": "https://api.example.test/v1", "json": {"a": 1}, "body_text": "oops"},
    )
    exit_code, frames, stderr = run_process_plugin_main(module, mixed_body_request)
    if exit_code != 0 or stderr:
        raise AssertionError(
            "mutually-exclusive-body path should still return a normal APP result"
        )
    _, mixed_body_result = assert_app_frames(frames, "http")
    assert_canonical_error(
        mixed_body_result["error"],
        expected_code="validation_error",
        message_contains=["mutually exclusive"],
    )

    no_policy_request = app_invoke_request(
        "request", {"url": "https://api.example.test/v1"}
    )
    exit_code, frames, stderr = run_process_plugin_main(module, no_policy_request)
    if exit_code != 0 or stderr:
        raise AssertionError(
            "missing-policy path should still return a normal APP result"
        )
    _, no_policy_result = assert_app_frames(frames, "http")
    assert_canonical_error(
        no_policy_result["error"],
        expected_code="validation_error",
        message_contains=["policy.network.mode=allowlist"],
    )

    disallowed_host_request = allowlist_request(
        {"url": "https://other.example.test/v1"}
    )
    exit_code, frames, stderr = run_process_plugin_main(module, disallowed_host_request)
    if exit_code != 0 or stderr:
        raise AssertionError(
            "disallowed-host path should still return a normal APP result"
        )
    _, disallowed_host_result = assert_app_frames(frames, "http")
    assert_canonical_error(
        disallowed_host_result["error"],
        expected_code="validation_error",
        message_contains=["allow_hosts"],
    )

    loopback_blocked_request = app_invoke_request(
        "request", {"url": "https://api.example.test/v1"}
    )
    loopback_blocked_request["policy"] = {"network": {"mode": "loopback"}}
    exit_code, frames, stderr = run_process_plugin_main(
        module, loopback_blocked_request
    )
    if exit_code != 0 or stderr:
        raise AssertionError("loopback mode should still return a normal APP result")
    _, loopback_result = assert_app_frames(frames, "http")
    assert_canonical_error(
        loopback_result["error"],
        expected_code="transport_error",
        message_contains=["loopback"],
    )

    auth_request = allowlist_request({"url": "https://api.example.test/v1"})
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        auth_request,
        [
            with_perform_single(
                module,
                {
                    "status_code": 401,
                    "headers": {},
                    "body": b"",
                    "truncated": False,
                    "url_final": "https://api.example.test/v1",
                    "location": None,
                },
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("401 path should still return a normal APP result")
    _, auth_result = assert_app_frames(frames, "http")
    assert_canonical_error(auth_result["error"], expected_code="auth_error")

    rate_limit_request = allowlist_request({"url": "https://api.example.test/v1"})
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        rate_limit_request,
        [
            with_perform_single(
                module,
                {
                    "status_code": 429,
                    "headers": {},
                    "body": b"",
                    "truncated": False,
                    "url_final": "https://api.example.test/v1",
                    "location": None,
                },
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("429 path should still return a normal APP result")
    _, rate_limit_result = assert_app_frames(frames, "http")
    assert_canonical_error(rate_limit_result["error"], expected_code="rate_limit_error")

    redirect_request = allowlist_request({"url": "https://api.example.test/v1"})
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        redirect_request,
        [
            with_perform_single(
                module,
                {
                    "status_code": 302,
                    "headers": {"location": "https://evil.example.test/redirect"},
                    "body": b"",
                    "truncated": False,
                    "url_final": "https://api.example.test/v1",
                    "location": "https://evil.example.test/redirect",
                },
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "redirect validation path should still return a normal APP result"
        )
    _, redirect_result = assert_app_frames(frames, "http")
    assert_canonical_error(
        redirect_result["error"],
        expected_code="validation_error",
        message_contains=["allow_hosts"],
    )

    redirect_limit_request = allowlist_request({"url": "https://api.example.test/v1"})
    redirect_limit_request["policy"]["network"]["max_redirects"] = 1
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        redirect_limit_request,
        [
            with_perform_single(
                module,
                {
                    "status_code": 302,
                    "headers": {"location": "https://api.example.test/v2"},
                    "body": b"",
                    "truncated": False,
                    "url_final": "https://api.example.test/v1",
                    "location": "https://api.example.test/v2",
                },
                {
                    "status_code": 302,
                    "headers": {"location": "https://api.example.test/v3"},
                    "body": b"",
                    "truncated": False,
                    "url_final": "https://api.example.test/v2",
                    "location": "https://api.example.test/v3",
                },
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "redirect-limit path should still return a normal APP result"
        )
    _, redirect_limit_result = assert_app_frames(frames, "http")
    assert_canonical_error(
        redirect_limit_result["error"],
        expected_code="validation_error",
        message_contains=["redirect limit exceeded"],
    )

    parsed_url = urllib.parse.urlsplit("https://api.example.test/v1")
    network_policy = allowlist_request({"url": "https://api.example.test/v1"})[
        "policy"
    ]["network"]
    with mock.patch.object(
        module.socket,
        "getaddrinfo",
        return_value=[
            (module.socket.AF_INET, module.socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))
        ],
    ):
        try:
            module.validate_resolved_addresses(parsed_url, network_policy)
        except module.ValidationError as err:
            if "blocked" not in str(err):
                raise AssertionError(f"unexpected private-IP message: {err}")
        else:
            raise AssertionError("expected private resolved IP to be blocked")

    with mock.patch.object(
        module.socket,
        "getaddrinfo",
        return_value=[
            (
                module.socket.AF_INET,
                module.socket.SOCK_STREAM,
                6,
                "",
                ("169.254.169.254", 443),
            )
        ],
    ):
        try:
            module.validate_resolved_addresses(parsed_url, network_policy)
        except module.ValidationError as err:
            if "169.254.169.254" not in str(err):
                raise AssertionError(f"unexpected metadata-IP message: {err}")
        else:
            raise AssertionError("expected metadata IP to be blocked")

    tls_request = allowlist_request({"url": "http://localhost:8080/health"})
    tls_request["policy"]["network"]["tls_required"] = True
    tls_request["policy"]["network"]["allow_hosts"] = ["localhost"]
    tls_request["policy"]["network"]["allow_ports"] = [8080]
    exit_code, frames, stderr = run_process_plugin_main(module, tls_request)
    if exit_code != 0 or stderr:
        raise AssertionError(
            "tls-required path should still return a normal APP result"
        )
    _, tls_result = assert_app_frames(frames, "http")
    assert_canonical_error(
        tls_result["error"],
        expected_code="validation_error",
        message_contains=["tls_required"],
    )

    port_request = allowlist_request({"url": "https://api.example.test:8443/v1"})
    exit_code, frames, stderr = run_process_plugin_main(module, port_request)
    if exit_code != 0 or stderr:
        raise AssertionError(
            "port-validation path should still return a normal APP result"
        )
    _, port_result = assert_app_frames(frames, "http")
    assert_canonical_error(
        port_result["error"],
        expected_code="validation_error",
        message_contains=["allow_ports"],
    )

    download_output_schema = read_json(ROOT / "schemas/download.output.schema.json")

    with tempfile.TemporaryDirectory(prefix="transist-http-download-") as tempdir:
        digest = hashlib.sha256(b"hello world").hexdigest()
        download_request = allowlist_request(
            {
                "url": "https://api.example.test/file.txt",
                "dest": "run://downloads/example.txt",
                "sha256": digest,
            },
            action="download",
        )
        download_request["paths"]["run_root"] = tempdir
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            download_request,
            [
                with_open_exchange(
                    module,
                    (
                        DummyConnection(),
                        FakeStreamingResponse(
                            200,
                            [b"hello ", b"world"],
                            headers={"content-type": "text/plain"},
                        ),
                        "https://api.example.test/file.txt",
                    ),
                )
            ],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "download success path should not fail, got"
                f" exit={exit_code} stderr={stderr}"
            )
        _, download_result = assert_app_frames(frames, "http")
        if len(frames) < 3:
            raise AssertionError("download should emit at least one progress frame")
        if download_result["status"] != "success":
            raise AssertionError(
                f"expected download success result, got: {download_result}"
            )
        validate_json_schema(download_output_schema, download_result["output"])
        output_path = pathlib.Path(download_result["output"]["path"])
        if output_path.read_bytes() != b"hello world":
            raise AssertionError(
                "downloaded file contents did not match expected bytes"
            )
        if not download_result["artifact_hints"]:
            raise AssertionError("download success should emit an artifact hint")

    traversal_request = allowlist_request(
        {"url": "https://api.example.test/file.txt", "dest": "run://../escape.txt"},
        action="download",
    )
    exit_code, frames, stderr = run_process_plugin_main(module, traversal_request)
    if exit_code != 0 or stderr:
        raise AssertionError(
            "dest-traversal path should still return a normal APP result"
        )
    _, traversal_result = assert_app_frames(frames, "http")
    assert_canonical_error(
        traversal_result["error"],
        expected_code="validation_error",
        message_contains=["run root"],
    )

    with tempfile.TemporaryDirectory(prefix="transist-http-download-") as tempdir:
        checksum_request = allowlist_request(
            {
                "url": "https://api.example.test/file.txt",
                "dest": "run://downloads/example.txt",
                "sha256": "0" * 64,
            },
            action="download",
        )
        checksum_request["paths"]["run_root"] = tempdir
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            checksum_request,
            [
                with_open_exchange(
                    module,
                    (
                        DummyConnection(),
                        FakeStreamingResponse(
                            200,
                            [b"hello world"],
                            headers={"content-type": "text/plain"},
                        ),
                        "https://api.example.test/file.txt",
                    ),
                )
            ],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "checksum-mismatch path should still return a normal APP result"
            )
        _, checksum_result = assert_app_frames(frames, "http")
        assert_canonical_error(
            checksum_result["error"],
            expected_code="validation_error",
            message_contains=["checksum mismatch"],
        )

    with tempfile.TemporaryDirectory(prefix="transist-http-download-") as tempdir:
        oversize_request = allowlist_request(
            {
                "url": "https://api.example.test/file.txt",
                "dest": "run://downloads/example.txt",
                "max_bytes": 5,
            },
            action="download",
        )
        oversize_request["paths"]["run_root"] = tempdir
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            oversize_request,
            [
                with_open_exchange(
                    module,
                    (
                        DummyConnection(),
                        FakeStreamingResponse(
                            200,
                            [b"hello", b"!"],
                            headers={"content-type": "text/plain"},
                        ),
                        "https://api.example.test/file.txt",
                    ),
                )
            ],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "oversize-download path should still return a normal APP result"
            )
        _, oversize_result = assert_app_frames(frames, "http")
        assert_canonical_error(
            oversize_result["error"],
            expected_code="validation_error",
            message_contains=["max_bytes"],
        )

    with tempfile.TemporaryDirectory(prefix="transist-http-download-") as tempdir:
        cancelled_request = allowlist_request(
            {
                "url": "https://api.example.test/file.txt",
                "dest": "run://downloads/example.txt",
            },
            action="download",
        )
        cancelled_request["paths"]["run_root"] = tempdir
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            cancelled_request,
            [
                with_open_exchange(
                    module,
                    (
                        DummyConnection(),
                        FakeStreamingResponse(
                            200,
                            [b"hello", b" world"],
                            headers={"content-type": "text/plain"},
                            on_read=lambda: module.handle_sigterm(None, None),
                        ),
                        "https://api.example.test/file.txt",
                    ),
                )
            ],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "cancelled-download path should still return a normal APP result"
            )
        _, cancelled_result = assert_app_frames(frames, "http")
        assert_canonical_error(cancelled_result["error"], expected_code="cancelled")

    dummy_connection = DummyConnection()
    module.ACTIVE_CONNECTION = dummy_connection
    module.handle_sigterm(None, None)
    if not module.CANCEL_REQUESTED or not dummy_connection.closed:
        raise AssertionError(
            "SIGTERM handler should mark the request cancelled and close the active"
            " connection"
        )
    module.reset_cancel_state()

    unsupported_request = app_invoke_request("not_real", {})
    exit_code, frames, stderr = run_process_plugin_main(module, unsupported_request)
    if exit_code != 0 or stderr:
        raise AssertionError(
            "unsupported-action path should still return a normal APP result"
        )
    _, unsupported_result = assert_app_frames(frames, "http")
    assert_canonical_error(
        unsupported_result["error"],
        expected_code="validation_error",
        message_contains=["unsupported action not_real"],
    )

    print("http smoke tests passed")


if __name__ == "__main__":
    main()
