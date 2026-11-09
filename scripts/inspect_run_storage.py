#!/usr/bin/env python3
"""Inventory explicit run/workspace trees read-only, with redacted JSON metadata."""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import argparse

CHUNK_BYTES = 64 * 1024
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass
class FileRecord:
    """Private traversal metadata; paths are never emitted in reports."""

    root: int
    relative: tuple[str, ...]
    metadata: os.stat_result
    category: str
    scope: str
    identifier: str


def open_directory(path: Path) -> int:
    """Open an explicit directory, rejecting symlinks in every ancestor and '..'."""
    if ".." in path.parts:
        raise ValueError("parent traversal is not allowed")
    absolute = Path(os.path.abspath(path))
    descriptor = os.open(absolute.anchor, DIRECTORY_FLAGS)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def classify(relative: tuple[str, ...], workspace: bool = False) -> tuple[str, str]:
    """Classify by layout only, not JSON contents or user-controlled basenames."""
    if workspace:
        return "workspace", "workspace"
    parts = relative
    contained = False
    while len(parts) >= 3 and parts[0] == "contained_runs":
        contained = True
        parts = parts[2:]
    scope = "contained_run" if contained else "root_run"
    head = parts[0]
    if head == "step_executions":
        category = "immutable_executions"
    elif head == "steps":
        category = "step_projections"
    elif head == "artifacts":
        # Metadata-only inspection cannot verify descriptor JSON kinds. Keep
        # JSON descriptor candidates distinct, including JSON business payloads.
        if len(parts) >= 5 and parts[3] == "evidence":
            category = (
                "evidence_json_descriptors"
                if parts[-1].endswith(".json")
                else "evidence"
            )
        elif len(parts) >= 5 and parts[3] == "artifacts":
            category = (
                "artifact_json_descriptors"
                if parts[-1].endswith(".json")
                else "artifacts"
            )
        else:
            category = "artifact_descriptors"
    elif head == "evidence":
        category = "evidence"
    elif parts == ("state.json",):
        category = "state_projections"
    elif parts == ("summary.json",):
        category = "summaries"
    elif parts == ("provenance.json",):
        category = "provenance"
    elif head == "workflow_assets" or parts == ("recipe.yaml",):
        category = "recipe_assets"
    elif head == "gates":
        category = "gates"
    elif parts in (("events.jsonl",), ("failures.jsonl",)):
        category = "events"
    elif head == "context":
        category = "context"
    else:
        category = "other"
    return category, scope


def signature(metadata: os.stat_result) -> tuple[int, ...]:
    """Identify stable regular-file metadata for bounded duplicate reads."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def digest_file(root_fd: int, record: FileRecord) -> str:
    """Stream one metadata-sized file via no-follow descriptors; reject any drift."""
    descriptor = os.dup(root_fd)
    try:
        for part in record.relative[:-1]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        file_fd = os.open(
            record.relative[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=descriptor,
        )
        with os.fdopen(file_fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or signature(before) != signature(
                record.metadata
            ):
                raise ValueError("file changed")
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                chunk = handle.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise ValueError("file changed")
                digest.update(chunk)
                remaining -= len(chunk)
            if handle.read(1) or signature(os.fstat(handle.fileno())) != signature(
                before
            ):
                raise ValueError("file changed")
            return digest.hexdigest()
    finally:
        os.close(descriptor)


def summarize(records: list[FileRecord]) -> dict:
    """Count unique regular inodes and OS-reported 512-byte allocation blocks."""
    allocated = [getattr(record.metadata, "st_blocks", None) for record in records]
    return {
        "files": len(records),
        "logical_bytes": sum(record.metadata.st_size for record in records),
        "allocated_bytes": (
            sum(blocks * 512 for blocks in allocated)
            if all(blocks is not None for blocks in allocated)
            else None
        ),
    }


def inspect_storage(
    run_root: Path,
    workspace: Path | None = None,
    *,
    hash_duplicates: bool = False,
    max_hash_file_bytes: int = 64 * 1024 * 1024,
    max_hash_total_bytes: int = 256 * 1024 * 1024,
    list_largest: int = 0,
    max_entries: int = 100000,
    max_depth: int = 128,
) -> dict:
    """Inventory explicit trees without writes; optionally stream sized duplicates.

    Return aggregates, skipped counts and per-report salted identifiers, never
    paths, payloads or content hashes. Overlapping roots/hardlinks count once.
    A live tree is a best-effort metadata observation, not an atomic snapshot.
    """
    for value in (max_hash_file_bytes, max_hash_total_bytes, max_entries, max_depth):
        if type(value) is not int or value <= 0:
            raise ValueError("budgets must be positive integers")
    if type(list_largest) is not int or list_largest < 0:
        raise ValueError("listing budget must be nonnegative")
    secret = os.urandom(32)

    def identifier(value: str) -> str:
        """Use an ephemeral salt so report identifiers cannot reveal known names."""
        return hmac.new(
            secret, value.encode("utf-8", errors="surrogateescape"), hashlib.sha256
        ).hexdigest()

    records: list[FileRecord] = []
    skipped: Counter = Counter()
    seen_files: set[tuple[int, int]] = set()
    seen_directories: set[tuple[int, int]] = set()
    entries = 0

    def walk(descriptor: int, root: int, relative: tuple[str, ...]) -> None:
        """Visit contained entries through the anchored directory descriptor."""
        nonlocal entries
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in seen_directories:
            skipped["overlapping_directory"] += 1
            return
        seen_directories.add(identity)
        with os.scandir(descriptor) as children:
            for child in children:
                if entries >= max_entries:
                    skipped["entry_budget"] += 1
                    return
                entries += 1
                path = relative + (child.name,)
                try:
                    metadata = child.stat(follow_symlinks=False)
                    if stat.S_ISLNK(metadata.st_mode):
                        skipped["symlink"] += 1
                    elif stat.S_ISDIR(metadata.st_mode):
                        if len(path) > max_depth:
                            skipped["depth_budget"] += 1
                            continue
                        child_fd = os.open(
                            child.name, DIRECTORY_FLAGS, dir_fd=descriptor
                        )
                        try:
                            walk(child_fd, root, path)
                        finally:
                            os.close(child_fd)
                    elif stat.S_ISREG(metadata.st_mode):
                        identity = (metadata.st_dev, metadata.st_ino)
                        if identity in seen_files:
                            skipped["hardlink_or_overlap"] += 1
                            continue
                        seen_files.add(identity)
                        category, scope = classify(path, workspace=root == 1)
                        records.append(
                            FileRecord(
                                root,
                                path,
                                metadata,
                                category,
                                scope,
                                identifier(
                                    f"{root}:" + PurePosixPath(*path).as_posix()
                                ),
                            )
                        )
                    else:
                        skipped["nonregular"] += 1
                except OSError:
                    skipped["unreadable_or_changed"] += 1

    with ExitStack() as stack:
        descriptors = []
        for path in (run_root, workspace):
            if path is not None:
                descriptor = open_directory(path)
                descriptors.append(descriptor)
                stack.callback(os.close, descriptor)
        for index, descriptor in enumerate(descriptors):
            walk(descriptor, index, ())
        duplicates = {
            "enabled": hash_duplicates,
            "successfully_hashed_bytes": 0,
            "read_budget_reserved_bytes": 0,
            "groups": [],
        }
        if hash_duplicates:
            by_size: dict[int, list[FileRecord]] = defaultdict(list)
            for record in records:
                by_size[record.metadata.st_size].append(record)
            hashed: dict[tuple[int, str], list[FileRecord]] = defaultdict(list)
            for size, candidates in sorted(by_size.items()):
                if len(candidates) < 2:
                    continue
                for record in candidates:
                    if (
                        size > max_hash_file_bytes
                        or duplicates["read_budget_reserved_bytes"] + size + 1
                        > max_hash_total_bytes
                    ):
                        skipped["hash_budget"] += 1
                        continue
                    # Reserve the bounded read including the one-byte growth probe,
                    # even if the file changes or opening fails.
                    duplicates["read_budget_reserved_bytes"] += size + 1
                    try:
                        digest = digest_file(descriptors[record.root], record)
                        duplicates["successfully_hashed_bytes"] += size
                        hashed[size, digest].append(record)
                    except (OSError, ValueError):
                        skipped["hash_unreadable_or_changed"] += 1
            for (size, digest), members in hashed.items():
                if len(members) >= 2:
                    duplicates["groups"].append(
                        {
                            "id": identifier(digest),
                            "files": len(members),
                            "logical_bytes_each": size,
                            "duplicate_logical_bytes": size * (len(members) - 1),
                        }
                    )
        categories = sorted({record.category for record in records})
        scopes = sorted({record.scope for record in records})
        report = {
            "kind": "read_only_storage_inventory",
            "snapshot": "best_effort_metadata",
            "totals": summarize(records),
            "categories": {
                key: summarize([r for r in records if r.category == key])
                for key in categories
            },
            "scopes": {
                key: summarize([r for r in records if r.scope == key]) for key in scopes
            },
            "skipped": dict(sorted(skipped.items())),
            "truncated": bool(skipped["entry_budget"] or skipped["depth_budget"]),
            "duplicates": duplicates,
        }
        if list_largest:
            report["largest_files"] = [
                {
                    "id": record.identifier,
                    "category": record.category,
                    "scope": record.scope,
                    **summarize([record]),
                }
                for record in sorted(
                    records, key=lambda r: r.metadata.st_size, reverse=True
                )[:list_largest]
            ]
        return report


def main() -> int:
    """Print a redacted inventory of explicitly supplied paths; never discover home."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--hash-duplicates", action="store_true")
    parser.add_argument("--max-hash-file-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--max-hash-total-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--list-largest", type=int, default=0)
    parser.add_argument("--max-entries", type=int, default=100000)
    parser.add_argument("--max-depth", type=int, default=128)
    args = parser.parse_args()
    try:
        report = inspect_storage(**vars(args))
    except (OSError, ValueError):
        print(
            "Inventory rejected: unsafe/unreadable root or invalid budget.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
