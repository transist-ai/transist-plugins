"""Supported official-provider injection; never use FastEmbed's download path."""

import hashlib
import json
from pathlib import Path

from fastembed import TextEmbedding
from mcp_server_qdrant.embeddings.fastembed import FastEmbedProvider

from model_lock import load_model_lock

MODEL = "sentence-transformers/all-MiniLM-L6-v2"
REPOSITORY = "Qdrant/all-MiniLM-L6-v2-onnx"
REVISION = "d13954661f83248295ba75c1ed411eef3b7b936e"
FILES = (
    "config.json",
    "model.onnx",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
)
LOCK = load_model_lock(Path(__file__).with_name("model.lock.json"))


class ProvisionedProvider(FastEmbedProvider):
    """Retain official embedding methods with verified, local-only model loading."""

    def __init__(self, path: Path = Path("/assets/model")) -> None:
        """Verify snapshot identity/hashes and eagerly load ONNX/tokenizer assets."""
        manifest = json.loads((path / "manifest.json").read_text())
        if manifest != LOCK:
            raise ValueError("Wrong model snapshot identity or hashes")
        if set(manifest["sha256"]) != set(FILES):
            raise ValueError("Incomplete model manifest")
        for name in FILES:
            if (
                hashlib.sha256((path / name).read_bytes()).hexdigest()
                != manifest["sha256"][name]
            ):
                raise ValueError("Model asset hash mismatch")
        self.model_name = MODEL
        self.embedding_model = TextEmbedding(
            MODEL,
            specific_model_path=str(path),
            local_files_only=True,
            cache_dir="/tmp/fixture-model-cache",
            threads=2,
        )
