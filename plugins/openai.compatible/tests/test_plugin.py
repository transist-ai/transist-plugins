"""Offline contract tests for the OpenAI-compatible provider boundary."""

import importlib.util
import http.server
import json
import pathlib
import socket
import ssl
import threading
import unittest
from unittest import mock

from jsonschema import Draft202012Validator

spec = importlib.util.spec_from_file_location(
    "compatible_plugin", pathlib.Path(__file__).resolve().parents[1] / "plugin.py"
)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

INPUT = {
    "base_url": "https://example.org/v1/",
    "model": "sample",
    "messages": [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ],
}
POLICY = {
    "network": {
        "mode": "allowlist",
        "allow_hosts": ["example.org"],
        "allow_ports": [443],
        "tls_required": True,
    }
}
RESPONSE = {
    "id": "response-1",
    "model": "sample",
    "choices": [
        {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"},
        {"message": {"role": "assistant", "content": "ignored"}},
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 4},
}


class ProviderContractTests(unittest.TestCase):
    """Exercise pure mapping, policy, and safe error handling without a provider."""

    def test_request_order_defaults_and_endpoint(self):
        endpoint, body, timeout = plugin.validate_input(INPUT)
        self.assertEqual(endpoint[3], "/v1/chat/completions")
        self.assertEqual(timeout, 30000)
        payload = json.loads(body)
        self.assertEqual(
            [item["role"] for item in payload["messages"]],
            ["system", "user", "assistant"],
        )
        self.assertEqual(payload["max_tokens"], 256)
        self.assertIs(payload["stream"], False)
        self.assertEqual(
            plugin.parse_endpoint("https://example.org/v1/chat/completions")[3],
            endpoint[3],
        )

    def test_invalid_inputs(self):
        cases = [
            {"model": None},
            {"extra": 1},
            {"messages": []},
            {"messages": [{"role": "tool", "content": "hi"}]},
            {"messages": [{"role": "user", "content": None}]},
            {"messages": [{"role": "user", "content": "x" * 65537}]},
            {"messages": INPUT["messages"] * 22},
            {"model": "x" * 257},
            {"max_tokens": True},
            {"max_tokens": 4097},
            {"timeout_ms": 120001},
        ]
        for change in cases:
            with self.subTest(change=str(change)[:80]), self.assertRaises(
                plugin.PluginError
            ) as raised:
                plugin.validate_input(INPUT | change)
            self.assertEqual(raised.exception.code, "validation_error")
        large = INPUT | {"messages": [{"role": "user", "content": "x" * 65000}] * 5}
        with self.assertRaises(plugin.PluginError):
            plugin.validate_input(large)

    def test_token_limit_schema_and_request_mapping(self) -> None:
        """Forward only the selected token parameter; retain the legacy default."""
        schema = json.loads(
            (
                pathlib.Path(__file__).resolve().parents[1]
                / "schemas/chat_complete.input.schema.json"
            ).read_text()
        )
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        for field in ("max_tokens", "max_completion_tokens"):
            for limit in (1, 64, 4096):
                with self.subTest(field=field, limit=limit):
                    value = INPUT | {field: limit}
                    validator.validate(value)
                    _, body, _ = plugin.validate_input(value)
                    payload = json.loads(body)
                    self.assertEqual(payload[field], limit)
                    other = (
                        "max_tokens"
                        if field == "max_completion_tokens"
                        else "max_completion_tokens"
                    )
                    self.assertNotIn(other, payload)
        validator.validate(INPUT)
        self.assertNotIn(
            "max_completion_tokens", json.loads(plugin.validate_input(INPUT)[1])
        )

    def test_invalid_token_limits_fail_before_transport(self) -> None:
        """Reject ambiguous, null, Boolean, fractional, or out-of-range limits."""
        schema = json.loads(
            (
                pathlib.Path(__file__).resolve().parents[1]
                / "schemas/chat_complete.input.schema.json"
            ).read_text()
        )
        validator = Draft202012Validator(schema)
        changes = [{"max_tokens": 64, "max_completion_tokens": 64}]
        changes += [
            {field: limit}
            for field in ("max_tokens", "max_completion_tokens")
            for limit in (None, True, False, "64", 1.5, 0, -1, 4097)
        ]
        for change in changes:
            with self.subTest(change=change):
                value = INPUT | change
                self.assertFalse(validator.is_valid(value))
                with mock.patch.object(plugin, "perform_request") as transport:
                    result = plugin.invoke(
                        {
                            "invoke_id": "invalid-tokens",
                            "action": "chat_complete",
                            "input": value,
                            "policy": POLICY,
                        }
                    )
                self.assertEqual(result["error"]["code"], "validation_error")
                transport.assert_not_called()

    def test_completion_tokens_over_http(self) -> None:
        """A local model fixture rejects legacy tokens and accepts the new field."""
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            """Record only request data and emulate the observed provider contract."""

            def do_POST(self) -> None:
                """Return a deterministic response without credentials or egress."""
                payload = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                requests.append(payload)
                accepted = (
                    "max_completion_tokens" in payload and "max_tokens" not in payload
                )
                body = json.dumps(
                    RESPONSE if accepted else {"error": "unsupported_parameter"}
                ).encode()
                self.send_response(200 if accepted else 400)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                """Suppress fixture logging."""

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for field, status in (
                ("max_tokens", "failed"),
                ("max_completion_tokens", "success"),
            ):
                with self.subTest(field=field), mock.patch.dict(
                    plugin.os.environ, {plugin.SECRET_NAME: "offline-fixture-key"}
                ):
                    result = plugin.invoke(
                        {
                            "invoke_id": "completion-tokens-http",
                            "action": "chat_complete",
                            "input": INPUT
                            | {
                                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                                field: 64,
                            },
                            "policy": {
                                "network": {
                                    "mode": "loopback",
                                    "allow_hosts": ["127.0.0.1"],
                                    "allow_ports": [server.server_port],
                                }
                            },
                        }
                    )
                    self.assertEqual(result["status"], status)
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[1]["max_completion_tokens"], 64)
            self.assertNotIn("max_tokens", requests[1])
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        self.assertFalse(thread.is_alive())

    def test_url_restrictions(self):
        for url in (
            "http://example.org",
            "https://u:p@example.org",
            "https://example.org/?q=x",
            "https://example.org/#fragment",
            "https://example.org:99999",
            "ftp://example.org",
            "https://example.org\r\nX: evil",
            "https://example.org/../private",
        ):
            with self.subTest(url=url), self.assertRaises(plugin.PluginError) as raised:
                plugin.parse_endpoint(url)
            self.assertEqual(raised.exception.code, "configuration_error")
        self.assertEqual(
            plugin.parse_endpoint("http://127.0.0.1:8000/v1")[3], "/v1/chat/completions"
        )

    def test_policy_and_dns(self):
        endpoint = plugin.parse_endpoint(INPUT["base_url"])
        self.assertFalse(plugin.check_policy(endpoint, POLICY))
        for policy in (
            {},
            {"network": {"mode": "none"}},
            {"network": {"mode": "any"}},
            {"network": {"mode": "loopback"}},
            {"network": {"mode": "allowlist", "allow_hosts": ["wrong.example"]}},
        ):
            with self.assertRaises(plugin.PluginError):
                plugin.check_policy(endpoint, policy)
        with mock.patch.object(
            plugin.socket,
            "getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
            ],
        ):
            with self.assertRaises(plugin.PluginError) as raised:
                plugin.resolve_address(endpoint, False)
            self.assertEqual(raised.exception.code, "configuration_error")
        with mock.patch.object(
            plugin.socket,
            "getaddrinfo",
            side_effect=socket.gaierror("sensitive DNS text"),
        ):
            with self.assertRaises(plugin.PluginError) as raised:
                plugin.resolve_address(endpoint, False)
            self.assertEqual(str(raised.exception), "DNS resolution failed")

    def test_response_mapping_and_usage(self):
        output, usage = plugin.parse_response(json.dumps(RESPONSE).encode())
        self.assertEqual(
            output,
            {
                "response_text": "ok",
                "finish_reason": "stop",
                "response_id": "response-1",
                "response_model": "sample",
            },
        )
        self.assertEqual(usage["total_tokens"], 7)
        self.assertEqual(usage["provider"], "openai-compatible")
        for partial in (
            {"prompt_tokens": 3},
            {"prompt_tokens": 3, "completion_tokens": -1},
            {"prompt_tokens": True, "completion_tokens": 2},
        ):
            self.assertIsNone(
                plugin.parse_response(json.dumps(RESPONSE | {"usage": partial}))[1]
            )
        self.assertEqual(
            plugin.parse_response(
                json.dumps(
                    RESPONSE
                    | {
                        "usage": {
                            "prompt_tokens": 2,
                            "completion_tokens": 3,
                            "total_tokens": 9,
                        }
                    }
                )
            )[1]["total_tokens"],
            9,
        )

    def test_invalid_responses(self):
        for body in (
            b"{",
            b"{}",
            json.dumps(RESPONSE | {"choices": []}).encode(),
            json.dumps(
                RESPONSE
                | {"choices": [{"message": {"role": "assistant", "content": None}}]}
            ).encode(),
            json.dumps(
                RESPONSE | {"choices": [{"message": {"role": "user", "content": "hi"}}]}
            ).encode(),
        ):
            with self.subTest(body=body), self.assertRaises(
                plugin.PluginError
            ) as raised:
                plugin.parse_response(body)
            self.assertEqual(raised.exception.code, "protocol_error")

    def test_status_codes(self):
        for status, code in (
            (301, "tool_execution_error"),
            (308, "tool_execution_error"),
            (408, "timeout_error"),
            (401, "auth_error"),
            (403, "auth_error"),
            (429, "rate_limit_error"),
            (500, "tool_execution_error"),
        ):
            self.assertEqual(plugin.classify_status(status).code, code)
        self.assertIsNone(plugin.classify_status(200))

    def test_invoke_no_credential_in_error(self):
        request = {
            "invoke_id": "test",
            "action": "chat_complete",
            "input": INPUT,
            "policy": POLICY,
        }
        canary = "credential-canary-private"
        with mock.patch.dict(plugin.os.environ, {plugin.SECRET_NAME: canary}):
            for exc, expected in (
                (ssl.SSLError(canary), "transport_error"),
                (TimeoutError(canary), "timeout_error"),
            ):
                with mock.patch.object(
                    plugin,
                    "perform_request",
                    side_effect=plugin.PluginError(
                        expected,
                        (
                            "provider connection failed"
                            if expected == "transport_error"
                            else "provider request timed out"
                        ),
                    ),
                ):
                    result = plugin.invoke(request)
                    self.assertEqual(result["error"]["code"], expected)
                    self.assertNotIn(canary, json.dumps(result))
            with mock.patch.object(
                plugin, "perform_request", return_value=json.dumps(RESPONSE).encode()
            ):
                self.assertEqual(plugin.invoke(request)["usage"]["input_tokens"], 3)
        with mock.patch.dict(plugin.os.environ, {}, clear=True):
            self.assertEqual(
                plugin.invoke(request)["error"]["code"], "secret_resolution_error"
            )


if __name__ == "__main__":
    unittest.main()
