"""Fixture-only HTTPS wrapper around Tika and the unmodified official MCP tools."""

import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from mcp_server_qdrant.mcp_server import QdrantMCPServer
from mcp_server_qdrant.settings import QdrantSettings, ToolSettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from provider import ProvisionedProvider


class DiscoveryAuth:
    """Authorize fixture calls and bind native MCP session IDs to an identity."""

    def __init__(self, app: Any, key: str) -> None:
        """Wrap the official ASGI app with a single disposable gateway key."""
        self.app = app
        self.key = key
        self.sessions: dict[str, str] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Forward allowed requests intact; reject unauthenticated tool calls."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        supplied = headers.get(b"api-key", b"").decode()
        if supplied and not secrets.compare_digest(supplied, self.key):
            await Response(status_code=401)(scope, receive, send)
            return
        identity = "authenticated" if supplied else "anonymous"
        session = headers.get(b"mcp-session-id", b"").decode()
        if session and self.sessions.get(session) != identity:
            await Response(status_code=403)(scope, receive, send)
            return
        body = b""
        if scope["method"] == "POST":
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body += message.get("body", b"")
                if len(body) > 32768:
                    await Response(status_code=413)(scope, receive, send)
                    return
                if not message.get("more_body"):
                    break
            try:
                method = json.loads(body)["method"]
            except (ValueError, KeyError, TypeError):
                await Response(status_code=400)(scope, receive, send)
                return
            public = {
                "initialize",
                "notifications/initialized",
                "notifications/cancelled",
                "tools/list",
                "ping",
            }
            if method not in public and not (
                method == "tools/call" and identity == "authenticated"
            ):
                await Response(status_code=401)(scope, receive, send)
                return
        replayed = False

        async def replay() -> dict:
            nonlocal replayed
            if not replayed and scope["method"] == "POST":
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        async def capture(message: dict) -> None:
            if message["type"] == "http.response.start":
                response_headers = dict(message.get("headers", []))
                created = response_headers.get(b"mcp-session-id")
                if created:
                    self.sessions[created.decode()] = identity
                if scope["method"] == "DELETE" and message["status"] == 200:
                    self.sessions.pop(session, None)
            await send(message)

        await self.app(scope, replay, capture)


def create_app() -> Starlette:
    """Construct fixture endpoints with offline official embeddings and DB reader."""
    credentials = json.loads(Path("/assets/credentials.json").read_text())
    official = QdrantMCPServer(
        tool_settings=ToolSettings(),
        qdrant_settings=QdrantSettings(
            QDRANT_URL="http://qdrant:6333",
            QDRANT_API_KEY=credentials["database_readonly"],
            COLLECTION_NAME="fixture_documents",
            QDRANT_READ_ONLY=True,
            QDRANT_SEARCH_LIMIT=5,
            QDRANT_ALLOW_ARBITRARY_FILTER=False,
        ),
        embedding_provider=ProvisionedProvider(),
    ).http_app(path="/mcp/", transport="streamable-http")

    @asynccontextmanager
    async def lifespan(app: Starlette) -> Any:
        async with official.router.lifespan_context(official):
            yield

    async def discovery(request: Request) -> JSONResponse:
        return JSONResponse({"fixture": "tika-qdrant-step1", "https": True})

    async def extract(request: Request) -> Response:
        if not secrets.compare_digest(
            request.headers.get("authorization", ""),
            "Bearer " + credentials["tika"],
        ):
            return Response(status_code=401)
        endpoint = request.path_params["endpoint"]
        if endpoint not in {"tika", "meta"}:
            return Response(status_code=404)
        async with httpx.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=30
        ) as client:
            response = await client.put(
                f"http://tika:9998/{endpoint}",
                content=await request.body(),
                headers={
                    "Content-Type": request.headers["content-type"],
                    "Accept": (
                        "application/json" if endpoint == "meta" else "text/plain"
                    ),
                    "X-Tika-Skip-Embedded": "true",
                },
            )
        if response.status_code != 200:
            return Response(status_code=502)
        return Response(
            response.content,
            media_type="application/json" if endpoint == "meta" else "text/plain",
        )

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/app/handshake", discovery),
            Route("/app/health", discovery),
            Route("/fixture/{endpoint}", extract, methods=["PUT"]),
            Mount("/", app=DiscoveryAuth(official, credentials["gateway"])),
        ],
    )


if __name__ == "__main__":
    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=8443,
        ssl_certfile="/assets/server.pem",
        ssl_keyfile="/assets/server.key",
        access_log=False,
        log_level="warning",
    )
