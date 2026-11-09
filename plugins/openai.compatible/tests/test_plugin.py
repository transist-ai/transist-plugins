"""Offline contract tests for the OpenAI-compatible provider boundary."""

import importlib.util
import json
import pathlib
import socket
import ssl
import unittest
from unittest import mock

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
