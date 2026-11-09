"""Bounded pending-review projection ported from the delivery workflow helper.

Valid projections preserve the helper's decisions. The action's stricter output
shape is checked before publication. File data may live in runtime-private staging;
it never selects a workspace. No provider calls or workflow orchestration here.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid
from typing import Any

MAX_RAW_BYTES = 4 * 1024 * 1024
MAX_CONTEXT_BYTES = 65536
COMMENT_FIELDS = (
    "id",
    "source",
    "author",
    "body",
    "path",
    "line",
    "original_line",
    "side",
    "start_line",
    "url",
    "thread_id",
    "review_comment_id",
    "created_at",
    "outdated",
)
PR_FIELDS = ("number", "state", "url", "title")
PROJECTED_STRING_FIELDS = (
    "id",
    "source",
    "author",
    "body",
    "path",
    "url",
    "created_at",
    "thread_id",
    "review_comment_id",
    "diff_hunk_header",
)


def is_json_integer(value: Any) -> bool:
    """Match JSON Schema integers, including integral floats but excluding bools."""
    return type(value) is int or (type(value) is float and value.is_integer())


def validate_projection(value: Any) -> None:
    """Require the complete projected file shape before any output is written.

    These are the explicit constraints of pr_prepare_review_context's output
    schema, without the APP-owned ok/mode/diagnostics fields. Locations have no
    positivity bounds or side enum. Validate the final merged projection so
    filtering, retirement and supersession retain the helper's decision rules.
    """
    if not isinstance(value, dict) or set(value) != {
        "comments",
        "count",
        "pull_request",
    }:
        raise ValueError("invalid projected review context fields")
    if not is_json_integer(value["count"]) or value["count"] < 0:
        raise ValueError("invalid projected review count")
    if not isinstance(value["pull_request"], dict) or not isinstance(
        value["comments"], list
    ):
        raise ValueError("invalid projected review context shape")
    allowed = {*COMMENT_FIELDS, "diff_hunk_header"}
    for comment in value["comments"]:
        if (
            not isinstance(comment, dict)
            or not {"id", "source", "body"}.issubset(comment)
            or set(comment) - allowed
        ):
            raise ValueError("invalid projected review comment fields")
        for field in PROJECTED_STRING_FIELDS:
            if field in comment and not isinstance(comment[field], str):
                raise ValueError("invalid projected review comment text")
        if not comment["id"] or comment["source"] not in {
            "review_thread",
            "pr_conversation",
        }:
            raise ValueError("invalid projected review comment identity/source")
        for field in ("line", "original_line", "start_line"):
            if (
                field in comment
                and comment[field] is not None
                and not is_json_integer(comment[field])
            ):
                raise ValueError("invalid projected review comment line")
        if (
            "side" in comment
            and comment["side"] is not None
            and not isinstance(comment["side"], str)
        ):
            raise ValueError("invalid projected review comment side")
        if "outdated" in comment and type(comment["outdated"]) is not bool:
            raise ValueError("invalid projected review comment status")


def timestamp(value: str) -> datetime:
    """Parse an aware fetch/comment timestamp without exposing its value on error."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except (ValueError, AttributeError):
        raise ValueError("invalid review timestamp") from None


def actionable(comment: dict[str, Any]) -> bool:
    """Exclude bot follow-ups and slash commands, preserving mandatory requests."""
    body = comment.get("body")
    if not isinstance(body, str):
        raise ValueError("invalid review request body")
    first = next((line.strip() for line in body.splitlines() if line.strip()), "")
    return not (
        first.startswith("<!-- transist-review-reply -->")
        or re.match(r"^/[A-Za-z][A-Za-z0-9_-]*(?:\s|$)", first)
    )


def validate_comment(value: Any, raw: bool = False) -> dict[str, Any]:
    """Validate normalized identity/source before filtering or retiring a request."""
    if not isinstance(value, dict):
        raise ValueError("invalid review request")
    for field in ("id", "source", "body"):
        if not isinstance(value.get(field), str):
            raise ValueError("invalid review request field")
    if not value["id"].strip() or value["source"] not in {
        "review_thread",
        "pr_conversation",
    }:
        raise ValueError("invalid review request identity/source")
    for field in ("author", "path", "diff_hunk", "created_at", "url"):
        if (raw or field in value) and not isinstance(value.get(field), str):
            raise ValueError("incomplete normalized review comment")
    if (raw or "outdated" in value) and type(value.get("outdated")) is not bool:
        raise ValueError("invalid normalized review comment status")
    if raw:
        timestamp(value["created_at"])
    for field in ("thread_id", "review_comment_id"):
        if field in value and not isinstance(value[field], str):
            raise ValueError("invalid normalized review comment location")
    return value


def complete_fetch(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Require complete successful fetch evidence before retiring unresolved threads."""
    if (
        payload.get("ok") is not True
        or type(payload.get("exit_code")) is not int
        or payload["exit_code"] != 0
        or not isinstance(payload.get("comments"), list)
    ):
        raise ValueError("incomplete or failed raw review fetch")
    cutoff = payload.get("pr_conversation_comments_after")
    if not isinstance(cutoff, str):
        raise ValueError("incomplete raw review checkpoint")
    if cutoff:
        timestamp(cutoff)
    timestamp(payload.get("last_successful_fetch_completed_at", ""))
    comments = [validate_comment(comment, raw=True) for comment in payload["comments"]]
    counts = {
        "count": len(comments),
        "review_thread_comment_count": sum(
            c["source"] == "review_thread" for c in comments
        ),
        "pr_conversation_comment_count": sum(
            c["source"] == "pr_conversation" for c in comments
        ),
    }
    if any(
        type(payload.get(k)) is not int or payload[k] != n for k, n in counts.items()
    ):
        raise ValueError("incomplete or conflicting raw review counts")
    return comments


def project(current: dict, pending: dict, pr: dict) -> dict:
    """Merge by source/identity; cutoff applies only to new conversation requests.

    Current unresolved threads replace pending threads. Pending conversations
    survive absence from a later fetch; a fresh copy supersedes prior text.
    Full bodies/locations remain, duplicate transport/diff bodies do not.
    """
    merged: dict[tuple[str, str], dict] = {}
    current_comments = complete_fetch(current)
    cutoff = current.get("pr_conversation_comments_after") or ""
    after = timestamp(cutoff) if cutoff else None
    pending_comments = pending.get("comments", [])
    if not isinstance(pending_comments, list) or (
        pending and "comments" not in pending
    ):
        raise ValueError("invalid pending review request list")
    if "count" in pending and (
        type(pending["count"]) is not int or pending["count"] != len(pending_comments)
    ):
        raise ValueError("invalid pending review request count")
    for comments, fresh in ((pending_comments, False), (current_comments, True)):
        for comment in comments:
            comment = validate_comment(comment)
            if not fresh and comment["source"] == "review_thread":
                continue
            if not actionable(comment):
                continue
            identity, source = comment["id"], comment["source"]
            key = (source, identity)
            if (
                fresh
                and source == "pr_conversation"
                and after is not None
                and key not in merged
            ):
                if timestamp(comment.get("created_at", "")) <= after:
                    continue
            projected = {key: comment[key] for key in COMMENT_FIELDS if key in comment}
            header = re.search(
                r"(?m)^(@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@)",
                str(comment.get("diff_hunk") or ""),
            )
            if header:
                projected["diff_hunk_header"] = header[1]
            elif "diff_hunk_header" in comment:
                projected["diff_hunk_header"] = comment["diff_hunk_header"]
            merged[key] = projected
    pull_request = pr.get("pull_request", pr)
    if not isinstance(pull_request, dict):
        raise ValueError("invalid pull request state")
    result = {
        "comments": list(merged.values()),
        "count": len(merged),
        "pull_request": {
            key: pull_request[key] for key in PR_FIELDS if key in pull_request
        },
    }
    validate_projection(result)
    encode(result)
    return result


def encode(value: dict) -> bytes:
    """Encode complete context; overflow fails instead of dropping requests."""
    data = (
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(data) > MAX_CONTEXT_BYTES:
        raise ValueError("review context exceeds 64 KiB; no requests were dropped")
    return data


def _directory_flags() -> int:
    """Search pinned ancestors without requesting directory-listing permission."""
    search = getattr(os, "O_SEARCH", None)
    if search is None:
        # Darwin's public <sys/fcntl.h> O_SEARCH ABI (O_EXEC | O_DIRECTORY),
        # also exported as 0x40100000 by CPython 3.14; absent in older Pythons.
        search = (
            0x40100000
            if sys.platform == "darwin"
            else getattr(os, "O_PATH", os.O_RDONLY)
        )
    return search | os.O_DIRECTORY | os.O_NOFOLLOW


def parent_fd(path: Path) -> int:
    """Pin existing nonsymlink parents of an absolute data path, including staging."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("invalid review context data path")
    flags = _directory_flags()
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except Exception:
        os.close(fd)
        raise


def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON keys rather than silently replacing request lists."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate review-context JSON key")
        result[key] = value
    return result


def read(path: Path) -> dict:
    """Read bounded regular JSON data, rejecting duplicate keys and nonobjects."""
    parent = parent_fd(path)
    try:
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
    finally:
        os.close(parent)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("review input must be a regular file")
        data = handle.read(MAX_RAW_BYTES + 1)
    if len(data) > MAX_RAW_BYTES:
        raise ValueError("raw review input exceeds 4 MiB")
    value = json.loads(data, object_pairs_hook=unique_keys)
    if not isinstance(value, dict):
        raise ValueError("review input must be a JSON object")
    return value


def write(path: Path, value: dict) -> None:
    """Atomically replace bounded projected context, never following output links."""
    validate_projection(value)
    data = encode(value)
    parent = parent_fd(path)
    temporary = f".review-context-{uuid.uuid4().hex}"
    try:
        try:
            mode = os.stat(path.name, dir_fd=parent, follow_symlinks=False).st_mode
        except FileNotFoundError:
            mode = stat.S_IFREG
        if not stat.S_ISREG(mode):
            raise ValueError("review output must be a regular file")
        fd = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)
