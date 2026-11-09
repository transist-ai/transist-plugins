"""Finite offline APP/TEP schema registry, excluding provenance JSON records."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

APP_CONTRACTS = (
    "app/handshake.response.v1.schema.json",
    "app/invoke.request.v1.schema.json",
    "app/invoke.result.v1.schema.json",
    "app/invoke.progress.v1.schema.json",
    "tep/error.schema.json",
)


def app_registry(directory: Path) -> Registry:
    """Load canonical contracts; reject missing, invalid or misidentified bytes.

    Read only the finite schema inventory. No network retrieval or metadata-as-schema
    fallback is enabled; malformed selected schemas remain fatal.
    """
    resources = []
    for relative in APP_CONTRACTS:
        schema = json.loads((directory / relative).read_text(encoding="utf-8"))
        expected = "https://transist.dev/schemas/" + relative
        if not isinstance(schema, dict) or schema.get("$id") != expected:
            raise ValueError(f"invalid canonical contract identity: {relative}")
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise ValueError(f"invalid contract dialect: {relative}")
        Draft202012Validator.check_schema(schema)
        resources.append((expected, Resource.from_contents(schema)))
    return Registry().with_resources(resources)
