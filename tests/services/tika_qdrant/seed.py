"""Seed three real embeddings and prove Qdrant's database read-only enforcement."""

import asyncio
import json
import time
from pathlib import Path

import httpx
from qdrant_client import QdrantClient, models

from fixtures import DOCUMENTS
from provider import ProvisionedProvider

COLLECTION = "fixture_documents"
VECTOR = "fast-all-minilm-l6-v2"


async def seed() -> None:
    """Create disposable named cosine vectors; test reader writes return 403."""
    credentials = json.loads(Path("/assets/credentials.json").read_text())
    with httpx.Client(trust_env=False, timeout=5) as http:
        for attempt in range(90):
            try:
                response = http.get(
                    "http://qdrant:6333/collections",
                    headers={"api-key": credentials["database_admin"]},
                )
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("Qdrant readiness failed")
    provider = ProvisionedProvider()
    vectors = await provider.embed_documents(list(DOCUMENTS.values()))
    assert len(vectors) == 3 and all(len(vector) == 384 for vector in vectors)
    client = QdrantClient(
        url="http://qdrant:6333", api_key=credentials["database_admin"]
    )
    client.create_collection(
        COLLECTION,
        vectors_config={
            VECTOR: models.VectorParams(size=384, distance=models.Distance.COSINE)
        },
    )
    client.upsert(
        COLLECTION,
        points=[
            models.PointStruct(
                id=index,
                vector={VECTOR: vector},
                payload={
                    "document": DOCUMENTS[source],
                    "metadata": {"source_id": source},
                },
            )
            for index, (source, vector) in enumerate(zip(DOCUMENTS, vectors), 1)
        ],
        wait=True,
    )
    client.close()
    with httpx.Client(
        base_url="http://qdrant:6333",
        headers={"api-key": credentials["database_readonly"]},
        trust_env=False,
        timeout=10,
    ) as reader:
        info = reader.get(f"/collections/{COLLECTION}")
        assert info.status_code == 200
        result = info.json()["result"]
        assert result["points_count"] == 3
        assert result["config"]["params"]["vectors"][VECTOR] == {
            "size": 384,
            "distance": "Cosine",
        }
        writes = [
            reader.put(
                f"/collections/{COLLECTION}/points?wait=true",
                json={"points": [{"id": 99, "vector": {VECTOR: vectors[0]}}]},
            ),
            reader.put(
                "/collections/forbidden",
                json={"vectors": {"size": 384, "distance": "Cosine"}},
            ),
            reader.delete(f"/collections/{COLLECTION}"),
        ]
        assert all(response.status_code == 403 for response in writes)
        assert (
            reader.get(f"/collections/{COLLECTION}").json()["result"]["points_count"]
            == 3
        )
        assert reader.get("/collections").json()["result"]["collections"] == [
            {"name": COLLECTION}
        ]
    print("PASS: 3 named 384-dimensional cosine vectors; DB reader writes denied (403)")


if __name__ == "__main__":
    asyncio.run(seed())
