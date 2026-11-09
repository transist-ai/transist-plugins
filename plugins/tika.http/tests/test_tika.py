"""Step 2 unit/schema/wire tests; no installation, workflows or real Tika required."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import time
import unittest
from unittest.mock import patch
from typing import Any

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
SPEC = importlib.util.spec_from_file_location("tika_service", ROOT / "service.py")
assert SPEC and SPEC.loader
service = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service)
AUTH = "Bearer " + "a" * 48


def invoke(action: str = "extract_text", data: bytes = b"document") -> dict[str, Any]:
    """Build the actual runtime APP request shape, including observational fields."""
    return {
        "type": "app.invoke.request.v1",
        "protocol_version": "app.v1",
        "invoke_id": "i-1",
        "run_id": "r-1",
        "workflow_id": "w-1",
        "step": {"id": "extract", "execution": 1},
        "plugin": service.IDENTITY,
        "action": action,
        "input": {
            "document_base64": base64.b64encode(data).decode(),
            "content_type": "text/plain",
        },
        "paths": {
            key: "unopened://private"
            for key in ("run_root", "workspace_dir", "artifacts_dir", "evidence_dir")
        },
        "requested_capabilities": [],
        "policy": {},
        "metadata": {},
        "timeout_ms": None,
        "max_timeout_ms": 30000,
    }


class UnitTests(unittest.TestCase):
    """Exercise semantic bounds not fully expressible as JSON Schema."""

    def test_document_types_and_boundaries(self) -> None:
        for size in (1, service.MAX_DOCUMENT):
            for content_type in service.TYPES:
                value = invoke(data=b"x" * size)["input"]
                value["content_type"] = content_type
                self.assertEqual(
                    service.decode_document(value), (b"x" * size, content_type)
                )

    def test_noncanonical_base64(self) -> None:
        for encoded in (
            "",
            "Zg",
            "Zg=",
            "Zg===",
            "Zh==",
            "Zm9=",
            "Zg==\n",
            " Zg==",
            "_w==",
            "-w==",
            "é===",
            "====",
            "Zg==Zg==",
        ):
            with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                service.decode_document(
                    {"document_base64": encoded, "content_type": "text/plain"}
                )

    def test_input_rejections(self) -> None:
        values = [
            invoke(data=b"")["input"],
            invoke(data=b"x" * (service.MAX_DOCUMENT + 1))["input"],
            {"document_base64": "Zg==", "content_type": "text/html"},
            {"document_base64": "Zg==", "content_type": []},
            {"document_base64": 1, "content_type": "text/plain"},
            {**invoke()["input"], "path": "/private"},
            {**invoke()["input"], "secret_refs": ["TIKA_AUTHORIZATION"]},
            {},
            [],
            None,
        ]
        for value in values:
            with self.subTest(value_type=type(value)), self.assertRaises(ValueError):
                service.decode_document(value)

    def test_strict_json(self) -> None:
        for raw in (
            b'{"a":1,"a":2}',
            b'{"a":{"x":1,"x":2}}',
            b"NaN",
            b"Infinity",
            b"1e999",
            b"{} trailing",
            b"\xff",
            b"\xef\xbb\xbf{}",
        ):
            with self.subTest(raw=raw), self.assertRaises((ValueError, UnicodeError)):
                service.parse_json(raw)

    def test_metadata_whitelist_and_scalar_normalization(self) -> None:
        output = service.normalize_metadata(
            {
                "dc:title": "Title",
                "dc:creator": ["A", "B"],
                "private": {"credentials": "not returned"},
            },
            "0" * 64,
        )
        self.assertEqual(
            output["metadata"], {"dc:title": ["Title"], "dc:creator": ["A", "B"]}
        )
        self.assertEqual(service.normalize_metadata({}, "0" * 64)["metadata"], {})

    def test_metadata_bounds(self) -> None:
        for value in (
            [],
            {"dc:title": ["x"] * 17},
            {"dc:title": "x" * 4097},
            {"dc:title": 2},
            {"dc:title": [None]},
            {"dc:title": [["x"]]},
            {"dc:title": ["世" * 4096] * 16},
        ):
            with self.subTest(value_type=type(value)), self.assertRaises(
                service.Failure
            ):
                service.normalize_metadata(value, "0" * 64)
        output = service.normalize_metadata({"dc:title": ["x" * 4096] * 16}, "0" * 64)
        self.assertEqual(len(output["metadata"]["dc:title"]), 16)
        # Escaping also counts toward the serialized bound.
        with self.assertRaises(service.Failure):
            service.normalize_metadata({"dc:title": ["\x00" * 4096] * 16}, "0" * 64)

        value = {"dc:title": ["x" * 4096] * 16, "dc:creator": ["x" * 4096] * 16}
        excess = (
            len(service.serialize({"document_sha256": "0" * 64, "metadata": value}))
            - service.MAX_OUTPUT
        )
        value["dc:creator"][-1] = "x" * (4096 - excess)
        output = service.normalize_metadata(value, "0" * 64)
        self.assertEqual(len(service.serialize(output)), service.MAX_OUTPUT)
        value["dc:creator"][-1] += "x"
        with self.assertRaises(service.Failure):
            service.normalize_metadata(value, "0" * 64)

    def test_startup_configuration(self) -> None:
        for auth in ("", "Bearer short", "Basic " + "a" * 48, AUTH + "\n"):
            with self.assertRaises(ValueError):
                service.TikaApp(auth, "http://tika:9998")
        for url in (
            "file:///private",
            "https://user:pass@host",
            "http://host/path",
            "http://host?x=1",
        ):
            with self.assertRaises(ValueError):
                service.TikaApp(AUTH, url)
        with patch.dict(
            os.environ,
            {"TIKA_AUTHORIZATION": AUTH, "TIKA_BACKEND_URL": "http://tika:9998"},
        ):
            self.assertEqual(service.create_app().backend, "http://tika:9998")


class AppTests(unittest.IsolatedAsyncioTestCase):
    """Verify APP requests/results and safe backend boundary behavior."""

    async def request(
        self,
        message: Any = None,
        raw: bytes | None = None,
        response: httpx.Response | None = None,
        error: Exception | None = None,
    ) -> tuple[httpx.Response, list[httpx.Request]]:
        """Dispatch in memory and capture backend requests without logging data."""
        calls: list[httpx.Request] = []

        async def backend(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if error:
                raise error
            return (
                response
                if response is not None
                else httpx.Response(
                    200,
                    headers={"content-type": "text/plain"},
                    stream=httpx.ByteStream(b" text\r\n"),
                )
            )

        app = service.TikaApp(AUTH, "http://backend", httpx.MockTransport(backend))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://app"
        ) as client:
            result = await client.post(
                "/app/invoke",
                content=(
                    raw
                    if raw is not None
                    else service.serialize(invoke() if message is None else message)
                ),
                headers={"Authorization": AUTH, "Content-Type": "application/json"},
            )
        return result, calls

    async def test_text_preservation_and_backend_wire_shape(self) -> None:
        for content_type in service.TYPES:
            message = invoke()
            message["input"]["content_type"] = content_type
            response, calls = await self.request(message)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["output"],
                {
                    "document_sha256": hashlib.sha256(b"document").hexdigest(),
                    "text": " text\r\n",
                },
            )
            self.assertEqual(len(calls), 1)
            call = calls[0]
            self.assertEqual(
                (call.method, call.url.path, call.content),
                ("PUT", "/tika", b"document"),
            )
            self.assertEqual(call.headers["content-type"], content_type)
            self.assertEqual(call.headers["x-tika-skip-embedded"], "true")
            self.assertNotIn("authorization", call.headers)

    async def test_authenticated_invalid_input_is_app_failure(self) -> None:
        for change in (
            {"action": "unknown"},
            {"action": []},
            {"input": {}},
            {"input": {**invoke()["input"], "url": "https://private"}},
            {"step": {"id": "s", "execution": True}},
            {"extra": 1},
        ):
            response, calls = await self.request({**invoke(), **change})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["error"]["code"], "validation_error")
            self.assertEqual(calls, [])

    async def test_malformed_request_json_is_app_failure(self) -> None:
        for raw in (b'{"input":1,"input":2}', b"{", b"NaN", b"[]"):
            response, calls = await self.request(raw=raw)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["error"]["code"], "validation_error")
            self.assertEqual(calls, [])

    async def test_backend_failures_are_fixed_and_do_not_retry(self) -> None:
        for error, code in (
            (httpx.ConnectError("sensitive"), "transport_error"),
            (httpx.ReadTimeout("sensitive"), "timeout_error"),
            (httpx.RemoteProtocolError("sensitive"), "protocol_error"),
        ):
            response, calls = await self.request(error=error)
            self.assertEqual(response.json()["error"]["code"], code)
            self.assertNotIn("sensitive", response.text)
            self.assertEqual(len(calls), 1)
        for status, code in (
            (302, "protocol_error"),
            (401, "transport_error"),
            (500, "transport_error"),
        ):
            response, calls = await self.request(
                response=httpx.Response(
                    status, headers={"Location": "http://private"}, content=b"sensitive"
                )
            )
            self.assertEqual(response.json()["error"]["code"], code)
            self.assertNotIn("sensitive", response.text)
            self.assertEqual(len(calls), 1)

    async def test_text_utf8_byte_limits(self) -> None:
        for text, ok in (
            (b"x" * service.MAX_OUTPUT, True),
            (b"x" * (service.MAX_OUTPUT + 1), False),
            (("世" * 43691).encode(), False),
            (b"\xff", False),
        ):
            response, _ = await self.request(
                response=httpx.Response(
                    200,
                    headers={"content-type": "text/plain; charset=UTF-8"},
                    stream=httpx.ByteStream(text),
                )
            )
            self.assertEqual(response.json()["status"], "success" if ok else "failed")
            if not ok:
                self.assertEqual(response.json()["error"]["code"], "protocol_error")

    async def test_metadata_backend_json_and_response_types(self) -> None:
        for raw, content_type, ok in (
            (b'{"dc:title":"T","private":"secret"}', "application/json", True),
            (b'{"dc:title":"a","dc:title":"b"}', "application/json", False),
            (b"NaN", "application/json", False),
            (b"[]", "application/json", False),
            (b"{}", "text/html", False),
            (b"x" * (service.MAX_METADATA_RESPONSE + 1), "application/json", False),
        ):
            response, calls = await self.request(
                invoke("extract_metadata"),
                response=httpx.Response(
                    200,
                    headers={"content-type": content_type},
                    stream=httpx.ByteStream(raw),
                ),
            )
            self.assertEqual(calls[0].url.path, "/meta")
            self.assertEqual(response.json()["status"], "success" if ok else "failed")
            if ok:
                self.assertEqual(
                    response.json()["output"]["metadata"], {"dc:title": ["T"]}
                )
            else:
                self.assertEqual(response.json()["error"]["code"], "protocol_error")

    async def test_compressed_backend_is_rejected(self) -> None:
        response, _ = await self.request(
            response=httpx.Response(
                200,
                headers={"content-type": "text/plain", "content-encoding": "gzip"},
                stream=httpx.ByteStream(b"private"),
            )
        )
        self.assertEqual(response.json()["error"]["code"], "protocol_error")

    async def test_discovery_and_auth_before_any_body_read(self) -> None:
        app = service.TikaApp(AUTH, "http://backend")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://app"
        ) as client:
            handshake = (await client.get("/app/handshake")).json()
            self.assertEqual(
                set(handshake["capabilities"]["actions"]), set(service.ACTIONS)
            )
            self.assertTrue(handshake["capabilities"]["supports_health"])
            self.assertFalse(handshake["capabilities"]["supports_cancel"])
            self.assertEqual((await client.get("/app/health")).json(), {"status": "ok"})
            self.assertEqual((await client.get("/other")).status_code, 404)

        async def unread() -> Any:
            raise AssertionError("Authentication must precede receive")

        for headers in (
            [],
            [(b"authorization", b"wrong")],
            [(b"authorization", AUTH.encode())] * 2,
        ):
            sent: list[Any] = []

            async def send(message: Any) -> None:
                sent.append(message)

            await app(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/app/invoke",
                    "headers": headers,
                },
                unread,
                send,
            )
            self.assertEqual(sent[0]["status"], 401)

    async def test_incoming_body_trickle_absolute_deadline(self) -> None:
        app = service.TikaApp(AUTH, "http://backend", deadline=0.12)
        sent: list[Any] = []

        async def receive() -> Any:
            await asyncio.sleep(0.03)
            return {"type": "http.request", "body": b" ", "more_body": True}

        async def send(message: Any) -> None:
            sent.append(message)

        start = time.monotonic()
        await app(
            {
                "type": "http",
                "method": "POST",
                "path": "/app/invoke",
                "headers": [
                    (b"authorization", AUTH.encode()),
                    (b"content-type", b"application/json"),
                ],
            },
            receive,
            send,
        )
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(json.loads(sent[-1]["body"])["error"]["code"], "timeout_error")

    async def test_body_and_backend_share_one_deadline(self) -> None:
        # Keep each phase below the total deadline, but their sum above it.
        # Millisecond-scale slack can expire during cold client/TLS setup on VMs
        # and never exercise the backend phase this test is intended to cover.
        calls: list[httpx.Request] = []

        async def backend(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            await asyncio.sleep(0.6)
            return httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                stream=httpx.ByteStream(b"ok"),
            )

        app = service.TikaApp(
            AUTH, "http://backend", httpx.MockTransport(backend), deadline=1.0
        )
        sent: list[Any] = []

        async def receive() -> Any:
            await asyncio.sleep(0.6)
            return {
                "type": "http.request",
                "body": service.serialize(invoke()),
                "more_body": False,
            }

        async def send(message: Any) -> None:
            sent.append(message)

        start = time.monotonic()
        await app(
            {
                "type": "http",
                "method": "POST",
                "path": "/app/invoke",
                "headers": [
                    (b"authorization", AUTH.encode()),
                    (b"content-type", b"application/json"),
                ],
            },
            receive,
            send,
        )
        self.assertLess(time.monotonic() - start, 1.4)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(sent[-1]["body"])["error"]["code"], "timeout_error")

    async def test_body_bound_and_bad_content_type(self) -> None:
        app = service.TikaApp(AUTH, "http://backend")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://app"
        ) as client:
            for data, content_type in (
                (b" " * (service.MAX_BODY + 1), "application/json"),
                (b"{}", "text/plain"),
            ):
                response = await client.post(
                    "/app/invoke",
                    content=data,
                    headers={"Authorization": AUTH, "Content-Type": content_type},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["error"]["code"], "validation_error")


class WireTests(unittest.IsolatedAsyncioTestCase):
    """Use real TCP sockets to prove transport, auth and trickling behavior."""

    async def test_http_authenticated_body_trickle(self) -> None:
        # Finish transmitting the deliberate partial body well before expiry,
        # including VM scheduling overhead, then require an absolute timeout.
        app = service.TikaApp(AUTH, "http://127.0.0.1:1", deadline=1.0)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, access_log=False, log_level="critical", lifespan="off")
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        writer = None
        try:
            while not server.started:
                await asyncio.sleep(0.01)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                (
                    "POST /app/invoke HTTP/1.1\r\nHost: app\r\n"
                    "Content-Length: 999\r\nContent-Type: application/json\r\n"
                    "Authorization: " + AUTH + "\r\nConnection: close\r\n\r\n"
                ).encode()
            )
            await writer.drain()
            start = time.monotonic()
            for _ in range(4):
                writer.write(b" ")
                await writer.drain()
                await asyncio.sleep(0.15)
            raw = await asyncio.wait_for(reader.read(), 1.0)
            self.assertLess(time.monotonic() - start, 1.4)
            self.assertIn(b"200 OK", raw)
            self.assertIn(b'"code":"timeout_error"', raw)
            self.assertNotIn(AUTH.encode(), raw)
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()
            server.should_exit = True
            await asyncio.wait_for(task, 2)
            listener.close()

    async def test_backend_trickle_default_thirty_second_deadline(self) -> None:
        closed = asyncio.Event()

        async def backend(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                    b"Transfer-Encoding: chunked\r\n\r\n"
                )
                await writer.drain()
                while True:
                    writer.write(b"1\r\nx\r\n")
                    await writer.drain()
                    await asyncio.sleep(0.1)
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                writer.close()
                closed.set()

        server = await asyncio.start_server(backend, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        app = service.TikaApp(AUTH, f"http://127.0.0.1:{port}")
        with patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://127.0.0.1:1",
                "ALL_PROXY": "http://127.0.0.1:1",
                "NO_PROXY": "",
            },
        ):
            async with server, httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://app"
            ) as client:
                start = time.monotonic()
                response = await client.post(
                    "/app/invoke", json=invoke(), headers={"Authorization": AUTH}
                )
                elapsed = time.monotonic() - start
                self.assertEqual(response.json()["error"]["code"], "timeout_error")
                self.assertGreaterEqual(elapsed, 29.5)
                self.assertLess(elapsed, 32)
                await asyncio.wait_for(closed.wait(), 2)

    async def test_http_auth_rejects_without_waiting_for_declared_body(self) -> None:
        app = service.TikaApp(AUTH, "http://127.0.0.1:1", deadline=0.2)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        config = uvicorn.Config(
            app, access_log=False, log_level="critical", lifespan="off"
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                await asyncio.sleep(0.01)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"POST /app/invoke HTTP/1.1\r\nHost: app\r\n"
                b"Content-Length: 999999\r\nContent-Type: application/json\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(), 1)
            self.assertIn(b"401 Unauthorized", raw)
            self.assertNotIn(b"authorization", raw.lower())
            writer.close()
            await writer.wait_closed()
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 2)
            listener.close()


if __name__ == "__main__":
    unittest.main()
