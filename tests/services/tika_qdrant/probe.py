"""Targeted acceptance through verified HTTPS and the official MCP client SDK."""

import asyncio
import json
import ssl
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from fixtures import TEXT, TYPES

URL = "https://gateway:8443"
QUERY = "How should I calibrate a temperature sensor?"


async def probe() -> None:
    """Validate real parsing, native discovery/results, TLS and fixture auth."""
    credentials = json.loads(Path("/assets/credentials.json").read_text())
    assert len(set(credentials.values())) == 4
    context = ssl.create_default_context(cafile="/assets/ca.pem")
    async with httpx.AsyncClient(
        verify=context, trust_env=False, follow_redirects=False, timeout=30
    ) as http:
        for attempt in range(120):
            try:
                response = await http.get(URL + "/app/health")
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(1)
        else:
            raise RuntimeError("HTTPS fixture readiness failed")
        for endpoint in ("/app/handshake", "/app/health"):
            response = await http.get(URL + endpoint)
            assert response.json() == {"fixture": "tika-qdrant-step1", "https": True}
            assert not any(secret in response.text for secret in credentials.values())
        for suffix, content_type in TYPES.items():
            data = Path(f"/assets/documents/document.{suffix}").read_bytes()
            denied = await http.put(
                URL + "/fixture/tika",
                content=data,
                headers={"content-type": content_type},
            )
            assert denied.status_code == 401
            for endpoint in ("tika", "meta"):
                response = await http.put(
                    URL + "/fixture/" + endpoint,
                    content=data,
                    headers={
                        "content-type": content_type,
                        "authorization": "Bearer " + credentials["tika"],
                    },
                )
                assert response.status_code == 200
                if endpoint == "tika":
                    assert TEXT in response.text
                else:
                    metadata = response.json()
                    assert "Content-Type" in metadata
            print(f"PASS: actual Tika text/metadata from {suffix.upper()} over HTTPS")
        async with streamable_http_client(URL + "/mcp/", http_client=http) as (
            read,
            write,
            session_id,
        ):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.protocolVersion == "2025-06-18"
                listed = await session.list_tools()
                assert [tool.name for tool in listed.tools] == ["qdrant-find"]
                assert set(listed.tools[0].inputSchema["properties"]) == {"query"}
                await session.send_ping()
                anonymous_id = session_id()
                assert anonymous_id
                call = {
                    "jsonrpc": "2.0",
                    "id": 91,
                    "method": "tools/call",
                    "params": {"name": "qdrant-find", "arguments": {"query": QUERY}},
                }
                headers = {
                    "mcp-session-id": anonymous_id,
                    "accept": "application/json, text/event-stream",
                    "mcp-protocol-version": "2025-06-18",
                }
                denied = await http.post(URL + "/mcp/", json=call, headers=headers)
                assert denied.status_code == 401
                upgraded = await http.post(
                    URL + "/mcp/",
                    json=call,
                    headers={**headers, "api-key": credentials["gateway"]},
                )
                assert upgraded.status_code == 403
        closed = await http.delete(
            URL + "/mcp/", headers={"mcp-session-id": anonymous_id}
        )
        assert closed.status_code == 403
    async with httpx.AsyncClient(
        verify=context,
        trust_env=False,
        follow_redirects=False,
        timeout=30,
        headers={"api-key": credentials["gateway"]},
    ) as http:
        async with streamable_http_client(URL + "/mcp/", http_client=http) as (
            read,
            write,
            session_id,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("qdrant-find", {"query": QUERY})
                assert not result.isError
                assert len(result.content) == 1 and result.content[0].type == "text"
                assert result.structuredContent is None
                entries = json.loads(result.content[0].text)
                assert entries[0] == f"Results for the query '{QUERY}'"
                assert len(entries) == 4
                assert '"source_id": "calibration"' in entries[1]
                assert all("<entry><content>" in entry for entry in entries[1:])
                native = result.model_dump(mode="json", exclude_none=True)
                mapped = {"content": native["content"]}
                capture = {
                    "tools": listed.model_dump(mode="json", exclude_none=True),
                    "native": native,
                    "mapped": mapped,
                }
                serialized = json.dumps(capture, indent=2)
                assert not any(secret in serialized for secret in credentials.values())
                Path("/evidence/native-output.json").write_text(serialized + "\n")
                authenticated_id = session_id()
                async with httpx.AsyncClient(
                    verify=context, trust_env=False
                ) as anonymous:
                    for method in ("GET", "DELETE"):
                        denied = await anonymous.request(
                            method,
                            URL + "/mcp/",
                            headers={"mcp-session-id": authenticated_id},
                        )
                        assert denied.status_code == 403
        wrong = await http.post(
            URL + "/mcp/", json=call, headers={"api-key": "invalid"}
        )
        assert wrong.status_code == 401
        db_key = await http.post(
            URL + "/mcp/",
            json=call,
            headers={"api-key": credentials["database_readonly"]},
        )
        assert db_key.status_code == 401
    async with httpx.AsyncClient(trust_env=False, timeout=5) as untrusted:
        try:
            await untrusted.get(URL + "/app/health")
        except httpx.ConnectError:
            pass
        else:
            raise AssertionError("Untrusted fixture TLS unexpectedly accepted")
    async with httpx.AsyncClient(
        base_url="http://qdrant:6333",
        headers={"api-key": credentials["database_readonly"]},
        trust_env=False,
    ) as reader:
        info = await reader.get("/collections/fixture_documents")
        assert info.json()["result"]["points_count"] == 3
        snapshot = await reader.post(
            "/collections/fixture_documents/points/scroll",
            json={"limit": 10, "with_vector": True, "with_payload": True},
        )
        snapshot.raise_for_status()
        points = snapshot.json()["result"]["points"]
    # Exercise the native zero-block contract against the real empty collection,
    # then restore the exact original points using this fixture's DB admin key.
    async with httpx.AsyncClient(
        base_url="http://qdrant:6333",
        headers={"api-key": credentials["database_admin"]},
        trust_env=False,
        timeout=30,
    ) as admin:
        deleted = await admin.post(
            "/collections/fixture_documents/points/delete?wait=true",
            json={"points": [point["id"] for point in points]},
        )
        deleted.raise_for_status()
        try:
            async with httpx.AsyncClient(
                verify=context,
                trust_env=False,
                follow_redirects=False,
                headers={"api-key": credentials["gateway"]},
                timeout=30,
            ) as http:
                async with streamable_http_client(URL + "/mcp/", http_client=http) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        empty = await session.call_tool("qdrant-find", {"query": QUERY})
                        assert not empty.isError and empty.content == []
                        assert empty.structuredContent is None
                        capture["empty_native"] = empty.model_dump(
                            mode="json", exclude_none=True
                        )
                        capture["empty_mapped"] = {"content": []}
        finally:
            restored = await admin.put(
                "/collections/fixture_documents/points?wait=true",
                json={"points": points},
            )
            restored.raise_for_status()
        restored = await admin.post(
            "/collections/fixture_documents/points/scroll",
            json={"limit": 10, "with_vector": True, "with_payload": True},
        )
        assert restored.json()["result"]["points"] == points
    Path("/evidence/native-output.json").write_text(
        json.dumps(capture, indent=2) + "\n"
    )
    print("PASS: official qdrant-find only; native one-text-block contract captured")
    print(
        "PASS: anonymous discovery/lifecycle; calls denied; session identities isolated"
    )
    print("PASS: verified HTTPS; bad gateway/DB keys denied; collection unchanged")
    print("PASS: real empty collection returns zero native blocks; exact seed restored")


if __name__ == "__main__":
    asyncio.run(probe())
