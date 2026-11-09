"""Structural review metadata checks; approval authority remains in the kernel."""

from __future__ import annotations

import math
import re

U32_MAX = 4294967295
U64_MAX = 18446744073709551615


def _identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is not None
    )


def _digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _integer(value: object, minimum: int, maximum: int) -> bool:
    # Compare the represented value directly: float(u64::MAX) rounds out of range.
    integral = type(value) is int or (
        type(value) is float and math.isfinite(value) and value.is_integer()
    )
    return integral and minimum <= value <= maximum


def _revision(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "revision_id",
        "root_run_id",
        "document",
        "run_id",
        "step_id",
        "execution",
        "commit_sequence",
        "kind",
        "blob",
        "size_bytes",
        "file_count",
    }:
        return False
    blob = value["blob"]
    if not (
        value["schema"] == "transist.document_revision.v1"
        and _digest(value["revision_id"])
        and all(
            _identifier(value[key]) for key in ("root_run_id", "document", "run_id")
        )
        and _integer(value["execution"], 0, U32_MAX)
        and _integer(value["commit_sequence"], 0, U64_MAX)
        and value["kind"] in ("file", "tree")
        and _integer(value["size_bytes"], 0, U64_MAX)
        and _integer(value["file_count"], 0, U32_MAX)
        and isinstance(blob, dict)
        and set(blob) == {"sha256", "size_bytes"}
        and _digest(blob["sha256"])
        and _integer(blob["size_bytes"], 0, U64_MAX)
    ):
        return False
    if value["execution"] == 0:
        if not (
            value["step_id"] == "__transist_document_initialization"
            and value["commit_sequence"] == 0
            and value["kind"] == "file"
        ):
            return False
    elif not (_identifier(value["step_id"]) and value["commit_sequence"] >= 1):
        return False
    return value["kind"] != "file" or value["file_count"] == 1


def valid_review_request(value: object) -> bool:
    """Check closed runtime review/revision shapes without reading document content.

    Return False for malformed metadata. No I/O or contextual authority checks:
    the runtime owns identity/digest equality with the committed waiting request.
    Nested integers accept finite integral floats; identifiers/digests must match
    the whole string, excluding the trailing-newline edge of schema '$' patterns.
    """
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "run_id",
        "gate_id",
        "step_id",
        "execution",
        "review_documents",
    }:
        return False
    documents = value["review_documents"]
    return (
        value["schema"] == "transist.gate_review_request.v1"
        and all(_identifier(value[key]) for key in ("run_id", "gate_id", "step_id"))
        and _integer(value["execution"], 1, U32_MAX)
        and isinstance(documents, list)
        and bool(documents)
        and all(_revision(revision) for revision in documents)
    )
