"""Fixture-only TLS launcher and memory counters around the production Tika APP."""

import json
import os
from pathlib import Path

import uvicorn
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from service import create_app


class CounterApp:
    """Observe route/status totals only; never retain headers or document bytes."""

    def __init__(self) -> None:
        """Load the private deployment credential and construct the actual service."""
        credentials = json.loads(Path("/assets/credentials.json").read_text())
        os.environ["TIKA_AUTHORIZATION"] = "Bearer " + credentials["tika"]
        self.app = create_app()
        self.counts: dict[str, int] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Expose test counters separately; delegate all APP behavior unchanged."""
        if scope["type"] == "http" and scope["path"] == "/fixture/counts":
            await JSONResponse(self.counts)(scope, receive, send)
            return

        async def observe(message: dict) -> None:
            if message["type"] == "http.response.start":
                key = f"{scope['method']} {scope['path']} {message['status']}"
                self.counts[key] = self.counts.get(key, 0) + 1
            await send(message)

        await self.app(scope, receive, observe)


if __name__ == "__main__":
    uvicorn.run(
        CounterApp(),
        host="0.0.0.0",
        port=8443,
        ssl_certfile="/assets/server.pem",
        ssl_keyfile="/assets/server.key",
        access_log=False,
        log_level="critical",
        lifespan="off",
    )
