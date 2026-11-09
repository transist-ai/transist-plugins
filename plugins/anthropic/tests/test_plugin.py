"""Offline Anthropic contract/security tests; all DNS and HTTP are mocked."""

import ast
import base64
import copy
import http.client
import json
import pathlib
import signal
import socket
import ssl
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from typing import Any
from collections.abc import Iterator
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))
from support import (  # noqa: E402
    CANONICAL_ERRORS,
    assert_canonical_error,
    load_plugin_module,
    run_process_plugin_main,
    validate_json_schema,
)

PACKAGE = ROOT / "plugins" / "anthropic"
plugin = load_plugin_module(PACKAGE)
INPUT = {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]}
POLICY = {
    "network": {
        "mode": "allowlist",
        "allow_hosts": ["api.anthropic.com"],
        "allow_ports": [443],
        "tls_required": True,
    }
}
RESPONSE = {
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "ok"}],
    "id": "msg_123",
    "model": "native-model",
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 3, "output_tokens": 4},
}
ADDRESS = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
SECRET = "???~~~dummy-unit-canary-private-xy"


def encoded(value: Any) -> bytes:
    """Serialize mocked native provider bodies without touching a real provider."""
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def request(change: dict | None = None) -> dict:
    """Build an isolated APP request with effective network policy."""
    return {
        "invoke_id": "unit-invoke",
        "action": "chat_complete",
        "input": copy.deepcopy(INPUT),
        "policy": copy.deepcopy(POLICY),
    } | (change or {})


@contextmanager
def transport(
    status: int = 200, body: bytes | None = None, headers: dict | None = None
) -> Iterator[tuple]:
    """Mock socket/DNS/TLS/HTTPResponse while exercising the transport code."""
    raw = mock.Mock()
    response = mock.Mock(status=status, length=None)
    response.read.side_effect = [encoded(RESPONSE) if body is None else body, b""]
    values = headers or {}
    response.getheader.side_effect = lambda name, default=None: values.get(
        name, default
    )
    context = mock.Mock()
    context.wrap_socket.return_value = raw
    with (
        mock.patch.object(plugin, "resolve_address", return_value=ADDRESS) as resolve,
        mock.patch.object(plugin.socket, "socket", return_value=raw) as create,
        mock.patch.object(plugin.ssl, "SSLContext", return_value=context),
        mock.patch.object(plugin.http.client, "HTTPResponse", return_value=response),
    ):
        yield raw, response, context, resolve, create


class ContractTests(unittest.TestCase):
    """Exercise structural validation, mapping, bounds and sanitized APP behavior."""

    def setUp(self) -> None:
        """Isolate credentials and install no network-capable test dependencies."""
        self.environment = mock.patch.dict(
            plugin.os.environ, {plugin.SECRET_NAME: SECRET}, clear=True
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def assert_error(self, call: Any, code: str) -> None:
        """Assert a pure operation fails with the requested canonical code."""
        with self.assertRaises(plugin.PluginError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_exact_native_request_defaults_and_unicode(self) -> None:
        """Preserve text bytes/order and separate system from native message history."""
        messages = [
            {"role": role, "content": text}
            for role, text in (
                ("user", "  héllo\n"),
                ("user", "世界"),
                ("assistant", "answer"),
                ("user", "next"),
            )
        ]
        endpoint, body, timeout = plugin.validate_input(
            INPUT
            | {
                "system": " rules\n",
                "messages": messages,
                "secret_refs": [plugin.SECRET_NAME],
            }
        )
        self.assertEqual(endpoint[1:], ("api.anthropic.com", 443, "/v1/messages"))
        self.assertEqual(timeout, 30000)
        self.assertEqual(
            json.loads(body),
            {
                "model": "test-model",
                "system": " rules\n",
                "messages": [
                    {
                        "role": item["role"],
                        "content": [{"type": "text", "text": item["content"]}],
                    }
                    for item in messages
                ],
                "max_tokens": 256,
                "stream": False,
            },
        )
        self.assertIn("世界".encode(), body)
        self.assertNotIn("system", json.loads(plugin.validate_input(INPUT)[1]))

    def test_numeric_and_text_boundaries(self) -> None:
        """Accept inclusive bounds and reject boolean/coerced/out-of-range values."""
        for field, high in (("max_tokens", 4096), ("timeout_ms", 120000)):
            for number in (1, high):
                plugin.validate_input(INPUT | {field: number})
            for number in (0, -1, high + 1, True, False, 1.5, "1", None):
                with self.subTest(field=field, number=number):
                    self.assert_error(
                        lambda: plugin.validate_input(INPUT | {field: number}),
                        "validation_error",
                    )
        plugin.validate_input(
            INPUT
            | {
                "model": "é" * 256,
                "system": "é" * 32768,
                "messages": [{"role": "user", "content": "x" * 65536}],
            }
        )
        plugin.validate_input(INPUT | {"messages": INPUT["messages"] * 64})
        for change in (
            {"model": "x" * 257},
            {"system": "é" * 32769},
            {"messages": [{"role": "user", "content": "é" * 32769}]},
            {"messages": INPUT["messages"] * 65},
        ):
            self.assert_error(
                lambda: plugin.validate_input(INPUT | change), "validation_error"
            )

    def test_invalid_fields_types_and_conversation(self) -> None:
        """Reject unsupported input and invalid conversation endpoints."""
        changes = [
            {"model": value}
            for value in (None, True, "", " \t", "x\x00", "x\x7f", "x\x85", "\ud800")
        ]
        changes += [{"system": value} for value in (None, [], "", "\n\t", "\ud800")]
        changes += [
            {"messages": value} for value in (None, {}, [], [None], [{"role": "user"}])
        ]
        changes += [
            {"messages": [{"role": role, "content": "hi"}]}
            for role in ("system", "tool", "assistant", None, [])
        ]
        changes += [
            {"messages": [{"role": "user", "content": value}]}
            for value in (None, [], {}, True, "", " \n", "\ud800")
        ]
        changes += [
            {"messages": [{"role": "user", "content": "hi", "extra": 1}]},
            {
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "prefill"},
                ]
            },
        ]
        changes += [
            {field: True}
            for field in (
                "tools",
                "stop_sequences",
                "stream",
                "temperature",
                "thinking",
                "beta",
            )
        ]
        changes += [
            {"secret_refs": value}
            for value in (
                None,
                [],
                ["wrong"],
                [plugin.SECRET_NAME] * 2,
                plugin.SECRET_NAME,
            )
        ]
        for change in changes:
            with self.subTest(change=repr(change)):
                self.assert_error(
                    lambda: plugin.validate_input(INPUT | change), "validation_error"
                )
        for value in (None, [], {}, {"model": "test"}):
            self.assert_error(lambda: plugin.validate_input(value), "validation_error")

    def test_serialized_request_size_boundary(self) -> None:
        """Enforce the complete UTF-8 serialized body bound, including JSON escaping."""
        value = INPUT | {"messages": [{"role": "user", "content": "x" * 65000}] * 4}
        _, body, _ = plugin.validate_input(value)
        self.assertLess(len(body), plugin.MAX_REQUEST)
        overhead = len(body) - 4 * 65000
        content_bytes = plugin.MAX_REQUEST - overhead
        lengths = [65536] * 3 + [content_bytes - 3 * 65536]
        value["messages"] = [
            {"role": "user", "content": "x" * length} for length in lengths
        ]
        self.assertEqual(len(plugin.validate_input(value)[1]), plugin.MAX_REQUEST)
        value["messages"][-1]["content"] += "x"
        self.assert_error(lambda: plugin.validate_input(value), "validation_error")
        self.assert_error(
            lambda: plugin.validate_input(
                INPUT | {"messages": [{"role": "user", "content": "\x00" * 65536}]}
            ),
            "validation_error",
        )

    def test_origin_validation_and_normalization(self) -> None:
        """Permit origins only, with HTTP limited to recognized loopback hosts."""
        for url, host, port in (
            ("https://API.Anthropic.COM./", "api.anthropic.com", 443),
            ("http://localhost:8080", "localhost", 8080),
            ("http://127.0.0.2/", "127.0.0.2", 80),
            ("http://[::1]:8080/", "::1", 8080),
            ("https://[2001:4860:4860::8888]", "2001:4860:4860::8888", 443),
        ):
            self.assertEqual(
                plugin.parse_endpoint(url)[1:], (host, port, "/v1/messages")
            )
        for url in (
            None,
            "",
            "http://example.org",
            "ftp://localhost",
            "https://u:p@example.org",
            "https://@example.org",
            "https://example.org/v1",
            "https://example.org//",
            "https://example.org?",
            "https://example.org#",
            "https://example.org/?q=x",
            "https://example.org/#x",
            "https://example.org:0",
            "https://example.org:65536",
            "https://example.org:",
            "https://example.org:abc",
            "https://example.org\n",
            "https://example.org\x7f",
            "https://exa mple.org",
            "https://-bad.org",
            "https://bad..org",
            "https://[::1%25en0]",
            "https://[v1.test]",
            "https://éxample.org",
        ):
            with self.subTest(url=url):
                self.assert_error(
                    lambda: plugin.parse_endpoint(url), "configuration_error"
                )

    def test_policy_restrictions_in_both_modes(self) -> None:
        """Enforce policy restrictions in both allowlist and loopback modes."""
        endpoint = plugin.parse_endpoint("https://api.anthropic.com")
        self.assertFalse(plugin.check_policy(endpoint, POLICY))
        self.assertFalse(
            plugin.check_policy(
                endpoint,
                {
                    "network": {
                        "mode": "allowlist",
                        "allow_hosts": ["API.ANTHROPIC.COM."],
                    }
                },
            )
        )
        for network in (
            None,
            {},
            {"mode": "none"},
            {"mode": "any"},
            {"mode": "loopback"},
            {"mode": "allowlist"},
            {"mode": "allowlist", "allow_hosts": ["wrong"]},
            POLICY["network"] | {"allow_ports": [80]},
            POLICY["network"] | {"allow_hosts": "api.anthropic.com"},
            POLICY["network"] | {"allow_ports": [True]},
            POLICY["network"] | {"tls_required": "yes"},
        ):
            self.assert_error(
                lambda: plugin.check_policy(endpoint, {"network": network}),
                "configuration_error",
            )
        endpoint = plugin.parse_endpoint("http://localhost:8000")
        self.assertTrue(
            plugin.check_policy(endpoint, {"network": {"mode": "loopback"}})
        )
        for restriction in (
            {"allow_hosts": ["wrong"]},
            {"allow_ports": [9000]},
            {"tls_required": True},
        ):
            for mode in ("loopback", "allowlist"):
                policy = {
                    "network": (
                        {
                            "mode": mode,
                            "allow_hosts": ["localhost"],
                            "allow_ports": [8000],
                        }
                        | restriction
                    )
                }
                self.assert_error(
                    lambda: plugin.check_policy(endpoint, policy), "configuration_error"
                )

    def test_success_text_projection_and_metadata(self) -> None:
        """Concatenate text blocks and preserve additive stop values."""
        native = RESPONSE | {
            "content": [
                {"type": "text", "text": " first "},
                {"type": "thinking", "thinking": "never returned"},
                {"type": "text", "text": "世界"},
                {"type": "text", "text": ""},
            ],
            "stop_reason": "new_additive_value",
            "extra": {"anything": True},
        }
        output, usage = plugin.parse_response(encoded(native), "input-model", "req_1")
        self.assertEqual(
            output,
            {
                "response_text": " first 世界",
                "finish_reason": "new_additive_value",
                "response_id": "msg_123",
                "response_model": "native-model",
                "request_id": "req_1",
            },
        )
        self.assertEqual(
            usage,
            {
                "input_tokens": 3,
                "output_tokens": 4,
                "total_tokens": 7,
                "provider": "anthropic",
                "model": "native-model",
            },
        )
        minimal = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
        }
        self.assertEqual(
            plugin.parse_response(encoded(minimal), "input"),
            ({"response_text": "ok"}, None),
        )
        self.assertEqual(
            plugin.parse_response(
                encoded(
                    minimal
                    | {"id": None, "model": None, "stop_reason": None, "usage": None}
                ),
                "input",
            ),
            ({"response_text": "ok"}, None),
        )
        for identifier in (
            None,
            "unsafe space",
            "bad\nheader",
            "é",
            "x" * 257,
            1,
            True,
        ):
            output, _ = plugin.parse_response(
                encoded(RESPONSE | {"id": identifier}), "input", identifier
            )
            self.assertNotIn("response_id", output)
            self.assertNotIn("request_id", output)
        self.assertEqual(
            plugin.parse_response(
                encoded(minimal | {"usage": RESPONSE["usage"]}), "fallback"
            )[1]["model"],
            "fallback",
        )

    def test_invalid_success_and_unsupported_continuations(self) -> None:
        """Reject malformed structures, continuations and blank text projections."""
        changes = [
            {"type": "error"},
            {"role": "user"},
            {"content": []},
            {"content": None},
            {"content": [None]},
            {"content": [{"type": ""}]},
            {"content": [{"type": True}]},
            {"content": [{"type": "text"}]},
            {"content": [{"type": "text", "text": []}]},
            {"content": [{"type": "text", "text": " \n"}]},
            {"content": [{"type": "image", "data": "secret"}]},
        ]
        changes += [
            {"stop_reason": value}
            for value in ("tool_use", "pause_turn", "", " ", True, [], {}, "bad\n")
        ]
        for change in changes:
            self.assert_error(
                lambda: plugin.parse_response(encoded(RESPONSE | change), "input"),
                "protocol_error",
            )
        for body in (
            b"{",
            b"{}",
            b"[]",
            b"null",
            b"\xff",
            b"[" * 2000 + b"0" + b"]" * 2000,
            b"x" * (plugin.MAX_RESPONSE + 1),
        ):
            self.assert_error(
                lambda: plugin.parse_response(body, "input"), "protocol_error"
            )
        surrogate = (
            b'{"type":"message","role":"assistant",'
            b'"content":[{"type":"text","text":"\\ud800"}]}'
        )
        self.assert_error(
            lambda: plugin.parse_response(surrogate, "input"), "protocol_error"
        )
        for field in ("stop_reason", "model"):
            raw = json.dumps(RESPONSE | {field: "\ud800"}).encode()
            if field == "stop_reason":
                self.assert_error(
                    lambda: plugin.parse_response(raw, "input"), "protocol_error"
                )
            else:
                self.assertNotIn(
                    "response_model", plugin.parse_response(raw, "input")[0]
                )
        for constant in ("NaN", "Infinity", "-Infinity"):
            raw = encoded(RESPONSE)[:-1] + b',"additive":' + constant.encode() + b"}"
            self.assert_error(
                lambda: plugin.parse_response(raw, "input"), "protocol_error"
            )

    def test_usage_invalid_and_uint64_boundaries(self) -> None:
        """Omit incomplete/invalid counts and overflow instead of failing valid text."""
        for counts in (None, [], {}, {"input_tokens": 1}, {"output_tokens": 1}):
            self.assertIsNone(
                plugin.parse_response(encoded(RESPONSE | {"usage": counts}), "input")[1]
            )
        for number in (-1, True, False, 1.5, "1", None, plugin.UINT64_MAX + 1):
            for field in ("input_tokens", "output_tokens"):
                self.assertIsNone(
                    plugin.parse_response(
                        encoded(
                            RESPONSE | {"usage": RESPONSE["usage"] | {field: number}}
                        ),
                        "input",
                    )[1]
                )
        self.assertIsNone(
            plugin.parse_response(
                encoded(
                    RESPONSE
                    | {"usage": {"input_tokens": plugin.UINT64_MAX, "output_tokens": 1}}
                ),
                "input",
            )[1]
        )
        for incoming, outgoing in (
            (0, 0),
            (plugin.UINT64_MAX, 0),
            (0, plugin.UINT64_MAX),
        ):
            usage = plugin.parse_response(
                encoded(
                    RESPONSE
                    | {
                        "usage": {
                            "input_tokens": incoming,
                            "output_tokens": outgoing,
                            "total_tokens": 99,
                        }
                    }
                ),
                "input",
            )[1]
            self.assertEqual(usage["total_tokens"], incoming + outgoing)

    def test_error_table_and_canonical_mappings(self) -> None:
        """Cover canonical mappings and the exact four-field APP error shape."""
        statuses = {
            400: "validation_error",
            404: "validation_error",
            413: "validation_error",
            422: "validation_error",
            401: "auth_error",
            403: "auth_error",
            429: "rate_limit_error",
            408: "timeout_error",
            504: "timeout_error",
            201: "tool_execution_error",
            301: "tool_execution_error",
            302: "tool_execution_error",
            307: "tool_execution_error",
            308: "tool_execution_error",
            500: "tool_execution_error",
            503: "tool_execution_error",
            529: "tool_execution_error",
        }
        self.assertIsNone(plugin.classify_status(200))
        for status, code in statuses.items():
            self.assertEqual(plugin.classify_status(status).code, code)
        for code, (kind, retryability) in plugin.ERROR_KINDS.items():
            self.assertEqual(
                {"class": kind, "retryability": retryability}, CANONICAL_ERRORS[code]
            )
            with mock.patch.object(
                plugin,
                "perform_request",
                side_effect=plugin.PluginError(code, "fixed diagnostic"),
            ):
                frame = plugin.invoke(request())
            assert_canonical_error(frame["error"], code)
            self.assertEqual(
                set(frame["error"]), {"code", "class", "retryability", "message"}
            )
            self.assertNotIn("usage", frame)

    def test_preflight_failures_do_not_dispatch(self) -> None:
        """Invalid input, endpoint, policy and credentials fail before transport/DNS."""
        cases = [
            (request({"action": "wrong"}), "validation_error"),
            (request({"input": INPUT | {"tools": []}}), "validation_error"),
            (
                request({"input": INPUT | {"base_url": "http://example.org"}}),
                "configuration_error",
            ),
            (request({"policy": {}}), "configuration_error"),
            (request({"timeout_ms": True}), "validation_error"),
        ]
        with (
            mock.patch.object(plugin, "perform_request") as dispatch,
            mock.patch.object(plugin.socket, "getaddrinfo") as dns,
        ):
            for item, code in cases:
                assert_canonical_error(plugin.invoke(item)["error"], code)
            for secret in (
                None,
                "",
                " ",
                "key with space",
                "key\n",
                "key\t",
                "clé",
                "key\x00",
                "key\x7f",
            ):
                # A plain mapping can represent malformed credentials (including NUL)
                # that the host's os.environ refuses before the plugin can inspect them.
                with mock.patch.object(
                    plugin.os,
                    "environ",
                    {} if secret is None else {plugin.SECRET_NAME: secret},
                ):
                    assert_canonical_error(
                        plugin.invoke(request())["error"], "secret_resolution_error"
                    )
            dispatch.assert_not_called()
            dns.assert_not_called()

    def test_timeout_selection(self) -> None:
        """Select the minimum action/APP budget and reject invalid APP limits."""
        for action, supplied, expected in (
            (30000, {}, 30000),
            (100, {"timeout_ms": 200}, 100),
            (30000, {"timeout_ms": 100}, 100),
            (30000, {"timeout_ms": 200, "max_timeout_ms": 50}, 50),
        ):
            with mock.patch.object(
                plugin, "perform_request", return_value=(encoded(RESPONSE), None)
            ) as dispatch:
                self.assertEqual(
                    plugin.invoke(
                        request({"input": INPUT | {"timeout_ms": action}} | supplied)
                    )["status"],
                    "success",
                )
            self.assertEqual(dispatch.call_args.args[2], expected)
        for field in ("timeout_ms", "max_timeout_ms"):
            for limit in (0, -1, True, None, 1.5, "5"):
                with mock.patch.object(plugin, "perform_request") as dispatch:
                    assert_canonical_error(
                        plugin.invoke(request({field: limit}))["error"],
                        "validation_error",
                    )
                    dispatch.assert_not_called()

    def test_secret_redaction_all_strings_and_artifacts(self) -> None:
        """Redact raw/base64 canaries from all reflected strings and usage hints."""
        variants = {SECRET}
        for encoder in (base64.b64encode, base64.urlsafe_b64encode):
            variant = encoder(SECRET.encode()).decode()
            variants.update((variant, variant.rstrip("=")))
        self.assertNotEqual(
            base64.b64encode(SECRET.encode()), base64.urlsafe_b64encode(SECRET.encode())
        )
        self.assertTrue(any(item.endswith("=") for item in variants))
        text = " ".join(sorted(variants))
        native = RESPONSE | {
            "content": [{"type": "text", "text": text}],
            "model": text,
            "id": base64.urlsafe_b64encode(SECRET.encode()).decode().rstrip("="),
            "stop_reason": text,
        }
        with mock.patch.object(
            plugin, "perform_request", return_value=(encoded(native), native["id"])
        ):
            frame = plugin.invoke(request({"invoke_id": text}))
        self.assertEqual(frame["status"], "success")
        self.assertNotIn("error", frame)
        self.assertEqual(
            json.loads(frame["artifact_hints"][0]["contents"]), frame["usage"]
        )
        emitted = json.dumps(frame)
        for variant in variants:
            self.assertNotIn(variant, emitted)
        with mock.patch.object(
            plugin,
            "perform_request",
            side_effect=plugin.classify_status(401, native["id"]),
        ):
            emitted = json.dumps(plugin.invoke(request()))
        for variant in variants:
            self.assertNotIn(variant, emitted)

    def test_app_handshake_result_and_silence(self) -> None:
        """Emit exactly handshake/result frames, correct capabilities and no stderr."""
        with mock.patch.object(
            plugin, "perform_request", return_value=(encoded(RESPONSE), "req_1")
        ):
            code, frames, stderr = run_process_plugin_main(plugin, request())
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0]["plugin"], {"id": "anthropic", "version": "0.1.0"})
        self.assertEqual(
            frames[0]["capabilities"],
            {
                "actions": ["chat_complete"],
                "supports_cancel": True,
                "supports_health": False,
                "supports_artifact_hints": True,
                "supports_evidence_hints": False,
            },
        )
        self.assertEqual(frames[1]["metrics"], {})
        self.assertEqual(frames[1]["evidence_hints"], [])
        with mock.patch.object(
            plugin,
            "perform_request",
            return_value=(encoded(RESPONSE | {"usage": None}), None),
        ):
            frame = plugin.invoke(request())
        self.assertNotIn("usage", frame)
        self.assertEqual(frame["artifact_hints"], [])

    def test_cancellation_after_transport_still_emits_terminal_frame(self) -> None:
        """The invocation handler covers response projection after transport cleanup."""
        previous = signal.getsignal(signal.SIGTERM)
        with (
            mock.patch.object(
                plugin, "perform_request", return_value=(encoded(RESPONSE), None)
            ),
            mock.patch.object(
                plugin,
                "parse_response",
                side_effect=lambda *_args: plugin._cancel(signal.SIGTERM, None),
            ),
        ):
            code, frames, stderr = run_process_plugin_main(plugin, request())
        self.assertEqual((code, stderr), (0, ""))
        assert_canonical_error(frames[-1]["error"], "cancelled")
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_selected_payload_and_schemas(self) -> None:
        """Check explicit runtime payload and Draft 2020-12 structural contracts."""
        manifest = (PACKAGE / "plugin.yaml").read_text()
        self.assertIn(
            "files: [plugin.py, schemas/chat_complete.input.schema.json, "
            "schemas/chat_complete.output.schema.json]",
            manifest,
        )
        self.assertIn("default_provider_kind: external", manifest)
        self.assertIn("- name: python3", manifest)
        self.assertTrue((PACKAGE / "plugin.py").stat().st_mode & 0o111)
        output = plugin.parse_response(encoded(RESPONSE), "input-model", "req_1")[0]
        for name, value in (
            ("input", INPUT | {"system": "rules", "secret_refs": [plugin.SECRET_NAME]}),
            ("output", output),
        ):
            schema = json.loads(
                (PACKAGE / "schemas" / f"chat_complete.{name}.schema.json").read_text()
            )
            self.assertEqual(
                schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
            )
            validate_json_schema(schema, value)

    def test_runtime_is_standard_library_and_python310_compatible(self) -> None:
        """Check syntax and imports without adding dependencies to the payload."""
        source = (PACKAGE / "plugin.py").read_text()
        tree = ast.parse(source, feature_version=(3, 10))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], sys.stdlib_module_names)
            elif isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                self.assertIn(node.module.split(".")[0], sys.stdlib_module_names)


class TransportTests(unittest.TestCase):
    """Mock transport to exercise DNS safety, deadlines and resource cleanup."""

    def endpoint(self) -> tuple:
        """Return the official default endpoint for transport tests."""
        return plugin.parse_endpoint("https://api.anthropic.com")

    def call(self, budget: int = 30000) -> tuple:
        """Call the transport under mocks using a harmless dummy credential."""
        return plugin.perform_request(
            self.endpoint(), b'{"test":true}', budget, SECRET, POLICY
        )

    def test_pinned_tls_headers_exactly_one_request_and_proxy_ignored(self) -> None:
        """Verify TLS hostnames while connecting only to the pinned address."""
        with (
            transport(headers={"request-id": "req_1"}) as (
                raw,
                response,
                context,
                resolve,
                create,
            ),
            mock.patch.dict(
                plugin.os.environ,
                {
                    "HTTPS_PROXY": "http://unsafe.invalid",
                    "ALL_PROXY": "http://unsafe.invalid",
                },
            ),
        ):
            body, request_id = self.call()
            self.assertEqual((body, request_id), (encoded(RESPONSE), "req_1"))
            raw.connect.assert_called_once_with(ADDRESS[4])
            create.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM, 6)
            context.wrap_socket.assert_called_once_with(
                raw, server_hostname="api.anthropic.com"
            )
            context.load_default_certs.assert_called_once_with()
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertIs(context.check_hostname, True)
            raw.sendall.assert_called_once()
            sent = raw.sendall.call_args.args[0]
            self.assertEqual(
                sent,
                (
                    "POST /v1/messages HTTP/1.1\r\nHost: api.anthropic.com\r\n"
                    f"Authorization: Bearer {SECRET}\r\n"
                    "anthropic-version: 2023-06-01\r\n"
                    "Content-Type: application/json\r\nAccept: application/json\r\n"
                    "Accept-Encoding: identity\r\nContent-Length: 13\r\n"
                    "Connection: close\r\n\r\n"
                    '{"test":true}'
                ).encode(),
            )
            self.assertEqual(resolve.call_count, 1)
            response.close.assert_called_once()
            raw.close.assert_called_once()
            self.assertIsNone(plugin.ACTIVE_SOCKET)

    def test_tls_key_logging_environment_is_not_consumed(self) -> None:
        """Avoid the key-logging helper while retaining TLS verification."""
        with (
            transport() as (_, _, context, _, _),
            mock.patch.dict(
                plugin.os.environ, {"SSLKEYLOGFILE": "/tmp/must-not-be-created"}
            ),
            mock.patch.object(plugin.ssl, "create_default_context") as default_context,
        ):
            self.call()
            default_context.assert_not_called()
            context.load_default_certs.assert_called_once()

    def test_loopback_http_and_ipv6_host_header(self) -> None:
        """Never initialize TLS for allowed loopback HTTP; preserve IPv6 Host syntax."""
        with transport() as (raw, _, context, _, _):
            plugin.perform_request(
                plugin.parse_endpoint("http://[::1]:8000"),
                b"{}",
                1000,
                SECRET,
                {"network": {"mode": "loopback"}},
            )
            context.wrap_socket.assert_not_called()
            self.assertIn(b"Host: [::1]:8000\r\n", raw.sendall.call_args.args[0])

    def test_dns_safe_unsafe_mixed_and_failed(self) -> None:
        """Reject private, multicast and mixed DNS answers before connection."""
        endpoint = self.endpoint()
        with mock.patch.object(plugin.socket, "getaddrinfo", return_value=[ADDRESS]):
            self.assertEqual(
                plugin.resolve_address(endpoint, False, time.monotonic() + 1), ADDRESS
            )
        ipv6 = (
            socket.AF_INET6,
            socket.SOCK_STREAM,
            6,
            "",
            ("2001:4860:4860::8888", 443, 0, 0),
        )
        with mock.patch.object(plugin.socket, "getaddrinfo", return_value=[ipv6]):
            self.assertEqual(
                plugin.resolve_address(endpoint, False, time.monotonic() + 1), ipv6
            )
        for address in (
            "127.0.0.1",
            "10.0.0.1",
            "169.254.1.1",
            "192.168.1.1",
            "0.0.0.0",
            "224.0.0.1",
            "::1",
            "fc00::1",
            "fe80::1",
            "ff02::1",
            "4000::1",
            "fec0::1",
        ):
            unsafe = (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, 443),
            )
            with (
                self.subTest(address=address),
                mock.patch.object(
                    plugin.socket, "getaddrinfo", return_value=[ADDRESS, unsafe]
                ),
            ):
                with self.assertRaises(plugin.PluginError) as caught:
                    plugin.resolve_address(endpoint, False, time.monotonic() + 1)
                self.assertEqual(caught.exception.code, "configuration_error")
        local = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))
        with mock.patch.object(plugin.socket, "getaddrinfo", return_value=[local]):
            self.assertEqual(
                plugin.resolve_address(
                    plugin.parse_endpoint("http://localhost"),
                    True,
                    time.monotonic() + 1,
                ),
                local,
            )
        for answers in ([ADDRESS], [local, ADDRESS]):
            with (
                mock.patch.object(plugin.socket, "getaddrinfo", return_value=answers),
                self.assertRaises(plugin.PluginError) as caught,
            ):
                plugin.resolve_address(
                    plugin.parse_endpoint("http://localhost"),
                    True,
                    time.monotonic() + 1,
                )
            self.assertEqual(caught.exception.code, "configuration_error")
        for failure in ([], socket.gaierror("must not leak")):
            patch = (
                {"side_effect": failure}
                if isinstance(failure, Exception)
                else {"return_value": failure}
            )
            with (
                mock.patch.object(plugin.socket, "getaddrinfo", **patch),
                self.assertRaises(plugin.PluginError) as caught,
            ):
                plugin.resolve_address(endpoint, False, time.monotonic() + 1)
            self.assertEqual(
                (caught.exception.code, str(caught.exception)),
                ("transport_error", "DNS resolution failed"),
            )

    def test_bounded_dns_wait_no_socket(self) -> None:
        """A blocked mocked libc resolver cannot consume the invocation indefinitely."""
        release, entered, done = threading.Event(), threading.Event(), threading.Event()

        def blocked(*_args: Any, **_kwargs: Any) -> list:
            """Simulate stalled DNS and release the daemon after the assertion."""
            entered.set()
            release.wait(2)
            done.set()
            return [ADDRESS]

        with (
            mock.patch.object(plugin.socket, "getaddrinfo", side_effect=blocked),
            mock.patch.object(plugin.socket, "socket") as create,
        ):
            started = time.monotonic()
            try:
                with self.assertRaises(plugin.PluginError) as caught:
                    self.call(50)
                self.assertEqual(caught.exception.code, "timeout_error")
                self.assertLess(time.monotonic() - started, 1)
                self.assertTrue(entered.is_set())
                create.assert_not_called()
            finally:
                release.set()
                self.assertTrue(done.wait(1))

    def test_policy_dns_failure_never_opens_socket(self) -> None:
        """Transport independently checks policy and DNS before socket creation."""
        with (
            mock.patch.object(plugin.socket, "socket") as create,
            mock.patch.object(plugin, "resolve_address") as dns,
        ):
            with self.assertRaises(plugin.PluginError):
                plugin.perform_request(self.endpoint(), b"{}", 1000, SECRET, {})
            dns.assert_not_called()
            create.assert_not_called()
            dns.side_effect = plugin.PluginError(
                "configuration_error", "DNS returned an unsafe address"
            )
            with self.assertRaises(plugin.PluginError):
                self.call()
            create.assert_not_called()

    def test_http_errors_id_fallback_and_no_redirect(self) -> None:
        """Classify HTTP errors, prefer safe headers and refuse redirects."""
        for status in (400, 401, 403, 404, 408, 413, 422, 429, 500, 503, 504, 529, 302):
            with transport(
                status,
                encoded({"request_id": "body_id", "error": {"message": "NEVER_EMIT"}}),
                {"request-id": "header_id", "Location": "https://redirect.invalid"},
            ) as (raw, response, _, _, _):
                with self.assertRaises(plugin.PluginError) as caught:
                    self.call()
                self.assertEqual(
                    caught.exception.code, plugin.classify_status(status).code
                )
                self.assertEqual(
                    str(caught.exception),
                    f"provider returned HTTP {status}; request_id=header_id",
                )
                response.read.assert_not_called()
                raw.sendall.assert_called_once()
                raw.close.assert_called_once()
        for header in (None, "unsafe space"):
            with transport(
                401, encoded({"request_id": "body_id"}), {"request-id": header}
            ):
                with self.assertRaises(plugin.PluginError) as caught:
                    self.call()
                self.assertEqual(
                    str(caught.exception),
                    "provider returned HTTP 401; request_id=body_id",
                )
        for body in (
            b"{",
            b"[]",
            b"[" * 2000 + b"0" + b"]" * 2000,
            encoded({"request_id": "unsafe space"}),
            b"x" * (plugin.MAX_RESPONSE + 1),
        ):
            with transport(429, body), self.assertRaises(plugin.PluginError) as caught:
                self.call()
            self.assertEqual(
                (caught.exception.code, str(caught.exception)),
                ("rate_limit_error", "provider returned HTTP 429"),
            )
        for error in (
            http.client.IncompleteRead(b"private"),
            OSError("private"),
            plugin.PluginError("timeout_error", "fixed"),
        ):
            with transport(503) as (_, response, _, _, _):
                response.read.side_effect = error
                with self.assertRaises(plugin.PluginError) as caught:
                    self.call()
                self.assertEqual(caught.exception.code, "tool_execution_error")

    def test_truncated_oversized_compressed_and_exact_bound(self) -> None:
        """Reject bad framing and accept the exact response byte bound."""
        for body in (b"x" * plugin.MAX_RESPONSE, b"x" * (plugin.MAX_RESPONSE + 1)):
            with transport(body=body) as (raw, response, _, _, _):
                if len(body) == plugin.MAX_RESPONSE:
                    self.assertEqual(len(self.call()[0]), plugin.MAX_RESPONSE)
                else:
                    with self.assertRaises(plugin.PluginError) as caught:
                        self.call()
                    self.assertEqual(caught.exception.code, "protocol_error")
                raw.close.assert_called_once()
                response.close.assert_called_once()
        with transport() as (_, response, _, _, _):
            response.length = 1
            with self.assertRaises(plugin.PluginError) as caught:
                self.call()
            self.assertEqual(caught.exception.code, "protocol_error")
        with transport() as (_, response, _, _, _):
            response.read.side_effect = http.client.IncompleteRead(b"private")
            with self.assertRaises(plugin.PluginError) as caught:
                self.call()
            self.assertEqual(caught.exception.code, "protocol_error")
        with (
            transport(headers={"Content-Encoding": "gzip"}),
            self.assertRaises(plugin.PluginError) as caught,
        ):
            self.call()
        self.assertEqual(caught.exception.code, "protocol_error")

    def test_invalid_successful_http_framing(self) -> None:
        """Reject malformed/ambiguous lengths and unsupported transfer encodings."""
        for headers in (
            {"Content-Length": "-1"},
            {"Content-Length": "not-a-number"},
            {"Content-Length": "2, 3"},
            {"Content-Length": "9" * 21},
            {"Content-Length": str(plugin.MAX_RESPONSE + 1)},
            {"Transfer-Encoding": "gzip"},
            {"Transfer-Encoding": "chunked", "Content-Length": "2"},
        ):
            with (
                transport(headers=headers),
                self.assertRaises(plugin.PluginError) as caught,
            ):
                self.call()
            self.assertEqual(caught.exception.code, "protocol_error")
        with transport(headers={"Transfer-Encoding": "chunked"}):
            self.assertEqual(self.call()[0], encoded(RESPONSE))
        with transport() as (_, response, _, _, _):
            response.read.side_effect = http.client.LineTooLong("private")
            with self.assertRaises(plugin.PluginError) as caught:
                self.call()
            self.assertEqual(caught.exception.code, "protocol_error")

    def test_transport_failures_and_cleanup_in_every_phase(self) -> None:
        """Hide exception text and close resources after failures in all phases."""
        for phase in ("connect", "tls", "send", "headers", "body"):
            for exception, expected in (
                (OSError(SECRET), "transport_error"),
                (ssl.SSLError(SECRET), "transport_error"),
                (TimeoutError(SECRET), "timeout_error"),
            ):
                with (
                    self.subTest(phase=phase, exception=type(exception).__name__),
                    transport() as (raw, response, context, _, _),
                ):
                    target = {
                        "connect": raw.connect,
                        "tls": context.wrap_socket,
                        "send": raw.sendall,
                        "headers": response.begin,
                        "body": response.read,
                    }[phase]
                    target.side_effect = exception
                    with self.assertRaises(plugin.PluginError) as caught:
                        self.call()
                    self.assertEqual(caught.exception.code, expected)
                    self.assertNotIn(SECRET, str(caught.exception))
                    raw.close.assert_called_once()
                    if phase in ("headers", "body"):
                        response.close.assert_called_once()
                    self.assertIsNone(plugin.ACTIVE_SOCKET)
                    self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_cleanup_failures_do_not_leak_or_skip_socket_close(self) -> None:
        """A failed close cannot replace the primary result or skip the other close."""
        with transport() as (raw, response, _, _, _):
            response.close.side_effect = OSError(SECRET)
            raw.close.side_effect = OSError(SECRET)
            self.assertEqual(self.call()[0], encoded(RESPONSE))
            response.close.assert_called_once()
            raw.close.assert_called_once()
            self.assertIsNone(plugin.ACTIVE_SOCKET)

    def test_absolute_deadline_trickling_headers_and_body(self) -> None:
        """SIGALRM interrupts internal repeated reads, not merely socket inactivity."""
        for phase in ("connect", "tls", "send", "headers", "body"):
            with transport() as (raw, response, context, _, _):

                def trickle(*_args: Any, **_kwargs: Any) -> None:
                    """Simulate endless small reads inside an HTTP operation."""
                    while True:
                        time.sleep(0.002)

                target = {
                    "connect": raw.connect,
                    "tls": context.wrap_socket,
                    "send": raw.sendall,
                    "headers": response.begin,
                    "body": response.read,
                }[phase]
                target.side_effect = trickle
                started = time.monotonic()
                with self.assertRaises(plugin.PluginError) as caught:
                    self.call(30)
                self.assertEqual(caught.exception.code, "timeout_error")
                self.assertLess(time.monotonic() - started, 1)
                raw.close.assert_called_once()
                self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_budget_checked_without_timer_and_not_renewed(self) -> None:
        """Monotonic checks independently enforce an exhausted absolute budget."""
        raw, response = mock.Mock(), mock.Mock(length=None)
        response.read.side_effect = [b"a", b"b", b""]
        with mock.patch.object(plugin.time, "monotonic", side_effect=[1, 2, 3, 5]):
            with self.assertRaises(plugin.PluginError) as caught:
                plugin.read_body(response, raw, 4)
        self.assertEqual(caught.exception.code, "timeout_error")
        self.assertEqual(
            [call.args[0] for call in raw.settimeout.call_args_list], [3, 1]
        )
        with (
            mock.patch.object(plugin.time, "monotonic", return_value=4),
            self.assertRaises(plugin.PluginError),
        ):
            plugin.remaining(4)

    def test_cancellation_during_dns_and_all_io_phases(self) -> None:
        """Interrupt every blocking phase and return canonical cancellation."""
        for phase in ("dns", "connect", "tls", "send", "headers", "body"):
            with (
                self.subTest(phase=phase),
                transport() as (raw, response, context, resolve, _),
            ):
                target = {
                    "dns": resolve,
                    "connect": raw.connect,
                    "tls": context.wrap_socket,
                    "send": raw.sendall,
                    "headers": response.begin,
                    "body": response.read,
                }[phase]

                def cancel(*_args: Any, **_kwargs: Any) -> None:
                    """Call the SIGTERM handler without signalling the runner."""
                    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

                target.side_effect = cancel
                with mock.patch.dict(plugin.os.environ, {plugin.SECRET_NAME: SECRET}):
                    frame = plugin.invoke(request())
                assert_canonical_error(frame["error"], "cancelled")
                self.assertIsNone(plugin.ACTIVE_SOCKET)
                if phase != "dns":
                    self.assertGreaterEqual(raw.close.call_count, 1)
                self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_signal_handlers_and_timer_restored(self) -> None:
        """Restore pre-existing handlers/timers on normal and exceptional exits."""
        alarm, term = signal.getsignal(signal.SIGALRM), signal.getsignal(signal.SIGTERM)
        for failure in (False, True):
            try:
                with plugin.deadline_scope(time.monotonic() + 1):
                    self.assertIs(signal.getsignal(signal.SIGTERM), plugin._cancel)
                    self.assertIs(signal.getsignal(signal.SIGALRM), plugin._expire)
                    if failure:
                        raise plugin.PluginError("protocol_error", "fixed")
            except plugin.PluginError:
                pass
            self.assertEqual(signal.getsignal(signal.SIGALRM), alarm)
            self.assertEqual(signal.getsignal(signal.SIGTERM), term)
            self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        with (
            mock.patch.object(plugin.signal, "getitimer", return_value=(10.0, 2.0)),
            mock.patch.object(plugin.signal, "setitimer") as timer,
        ):
            with plugin.deadline_scope(time.monotonic() + 1):
                pass
        self.assertEqual(timer.call_args.args[0], signal.ITIMER_REAL)
        self.assertGreater(timer.call_args.args[1], 9)
        self.assertEqual(timer.call_args.args[2], 2.0)


if __name__ == "__main__":
    unittest.main()
