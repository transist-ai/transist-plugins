"""Read public model digest entries without changing the verification manifest."""

from __future__ import annotations

import json
from pathlib import Path
import re


def load_model_lock(path: Path) -> dict:
    """Read a lock and return the existing filename-to-SHA-256 manifest shape.

    Accept structured file/digest entries or the historical mapping. Reject
    duplicate entries and malformed digests; never replace or infer a pin.
    Performs a local read only, with no downloads or credential handling.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    pins = document["sha256"]
    if isinstance(pins, list):
        normalized = {}
        for entry in pins:
            if not isinstance(entry, dict) or set(entry) != {"file", "digest"}:
                raise ValueError("Invalid model digest entry")
            name = entry["file"]
            if not isinstance(name, str) or name in normalized:
                raise ValueError("Invalid or duplicate model asset name")
            normalized[name] = entry["digest"]
        pins = normalized
    if not isinstance(pins, dict) or not pins:
        raise ValueError("Missing model digests")
    for name, digest in pins.items():
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in {".", ".."}
            or not isinstance(digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", digest) is None
        ):
            raise ValueError("Invalid model asset digest")
    return {**document, "sha256": pins}
