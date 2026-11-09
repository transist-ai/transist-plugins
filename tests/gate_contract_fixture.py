"""Finite runtime-shaped gate decisions shared by offline contract regressions."""

from __future__ import annotations

from copy import deepcopy
from typing import Iterator

PACKAGES = (
    "codex.cli",
    "cursor.cli",
    "github.copilot.cli",
    "opencode.cli",
    "openspec",
    "shell",
)


def decision(metadata: bool = True, outcome: str = "request_changes") -> dict:
    """Return a legacy or document decision, including an initialization revision."""
    value = {
        "schema": "transist.gate_decision.v1",
        "run_id": "run",
        "gate_id": "review",
        "step_id": "review",
        "decision": outcome,
        "actor": "Fixture Reviewer",
        "actor_source": "cli",
        "reason": "Tighten the wording café",
        "decided_at_ms": 1,
    }
    if metadata:
        value["review_request"] = {
            "schema": "transist.gate_review_request.v1",
            "run_id": "run",
            "gate_id": "review",
            "step_id": "review",
            "execution": 1,
            "review_documents": [
                {
                    "schema": "transist.document_revision.v1",
                    "revision_id": "a" * 64,
                    "root_run_id": "run",
                    "document": "draft",
                    "run_id": "run",
                    "step_id": "__transist_document_initialization",
                    "execution": 0,
                    "commit_sequence": 0,
                    "kind": "file",
                    "blob": {"sha256": "b" * 64, "size_bytes": 7},
                    "size_bytes": 7,
                    "file_count": 1,
                }
            ],
        }
    return value


def malformed_decisions() -> Iterator[tuple[str, dict]]:
    """Yield missing, mistyped, unknown and boundary-breaking nested metadata."""
    original = decision()
    paths = [
        ("review_request",),
        ("review_request", "review_documents", 0),
        ("review_request", "review_documents", 0, "blob"),
    ]
    for path in paths:
        parent = original
        for key in path:
            parent = parent[key]
        for field in (*parent, "unknown"):
            value = deepcopy(original)
            target = value
            for key in path:
                target = target[key]
            if field == "unknown":
                target[field] = "must not be accepted"
            else:
                del target[field]
            yield f"{path}/missing-or-unknown/{field}", value
        for field in parent:
            for invalid in (None, True, [], {}, -1, 1.5, ""):
                value = deepcopy(original)
                target = value
                for key in path:
                    target = target[key]
                target[field] = invalid
                yield f"{path}/{field}/{invalid!r}", value
    for path, invalid in (
        (("review_request",), None),
        (("review_request", "execution"), 0),
        (("review_request", "execution"), 4294967296),
        (("review_request", "run_id"), "invalid/id"),
        (("review_request", "review_documents"), []),
        (("review_request", "review_documents"), ["bad"]),
        (("review_request", "review_documents", 0, "execution"), 4294967296),
        (("review_request", "review_documents", 0, "step_id"), "producer"),
        (("review_request", "review_documents", 0, "commit_sequence"), 1),
        (("review_request", "review_documents", 0, "commit_sequence"), 2**64),
        (("review_request", "review_documents", 0, "kind"), "tree"),
        (("review_request", "review_documents", 0, "file_count"), 2),
        (("review_request", "review_documents", 0, "size_bytes"), 2**64),
        (("review_request", "review_documents", 0, "blob", "size_bytes"), 2**64),
        (("review_request", "review_documents", 0, "blob", "sha256"), "F" * 64),
        (("review_request", "review_documents", 0, "revision_id"), "a" * 63),
    ):
        value = deepcopy(original)
        target = value
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = invalid
        yield f"{path}/boundary/{invalid!r}", value
