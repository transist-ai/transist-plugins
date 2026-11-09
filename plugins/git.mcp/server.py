#!/usr/bin/env -S python3 -I
"""Bounded, read-only Git tools over MCP 2025-06-18 NDJSON stdio."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import sys
import time
from typing import Any

PROTOCOL = "2025-06-18"
MAX_MESSAGE = 1024 * 1024
MAX_OUTPUT = 256 * 1024
MAX_METADATA_ENTRIES = 10000
GIT_TIMEOUT = 5.0
PACKAGE = Path(__file__).resolve().parent
# -I excludes the script directory. Only the trusted selected package root is
# added, never CWD, PYTHONPATH, or a provider-controlled directory.
sys.path.insert(0, str(PACKAGE))
from process_lifecycle import ProcessLifecycle  # noqa: E402


class ToolFailure(Exception):
    """An operational failure safe to return without host paths or Git stderr."""


def _git_environment() -> dict[str, str]:
    # Construct rather than filter: no inherited Git redirection, config,
    # executable loader, credential, pager, or Python variables reach Git.
    env = {
        "PATH": os.defpath,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
    }
    if "TMPDIR" in os.environ:
        env["TMPDIR"] = os.environ["TMPDIR"]
    return env


def _git(args: list[str], allowed_codes: tuple[int, ...] = (0,)) -> tuple[int, str]:
    executable = shutil.which("git", path=os.environ.get("PATH", os.defpath))
    if executable is None:
        raise ToolFailure("git_unavailable")
    command = [
        executable,
        "--no-pager",
        "--no-optional-locks",
        "--no-replace-objects",
        "--git-dir=.git",
        "-c",
        "core.bare=false",
        "-c",
        "core.worktree=.",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.untrackedCache=false",
        *args,
    ]
    deadline = time.monotonic() + GIT_TIMEOUT
    output = bytearray()
    total = 0
    lifecycle = ProcessLifecycle()
    process = subprocess.Popen(
        command,
        cwd=Path.cwd(),
        env=_git_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        try:
            assert process.stdout is not None and process.stderr is not None
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, True)
                selector.register(process.stderr, selectors.EVENT_READ, False)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ToolFailure("git_timeout")
                    for key, _ in selector.select(remaining):
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > MAX_OUTPUT:
                            raise ToolFailure("git_output_limit")
                        if key.data:
                            output.extend(chunk)
            code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except (ToolFailure, subprocess.TimeoutExpired):
            raise ToolFailure("git_timeout_or_output_limit") from None
    finally:
        # Includes unexpected errors and BaseException cancellation paths.
        # No Popen context manager: __exit__ performs an unbounded wait.
        confirmed = False
        try:
            confirmed = lifecycle.cleanup(lambda: None)
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                confirmed = False
        if not confirmed:
            raise ToolFailure("git_cleanup_unverified") from None
    if code not in allowed_codes:
        raise ToolFailure("git_failed")
    try:
        return code, output.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolFailure("git_non_utf8_output") from None


def _regular_metadata(path: Path, directory: bool = False) -> None:
    mode = path.lstat().st_mode
    if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
        raise ToolFailure("unsupported_repository_metadata")
    if not directory and path.stat().st_size > MAX_OUTPUT:
        raise ToolFailure("repository_metadata_limit")


def _validate_repository() -> None:
    # No parent discovery, .git files (linked worktrees), or symlink escapes.
    root = Path.cwd() / ".git"
    _regular_metadata(root, directory=True)
    _regular_metadata(root / "HEAD")
    _regular_metadata(root / "config")
    for name in (
        "packed-refs",
        "commondir",
        "config.worktree",
        "objects/info/alternates",
        "objects/info/http-alternates",
    ):
        path = root / name
        if path.exists() or path.is_symlink():
            if name != "packed-refs":
                raise ToolFailure("unsupported_repository_metadata")
            _regular_metadata(path)
    _regular_metadata(root / "refs", directory=True)
    count = 0
    size = 0
    deadline = time.monotonic() + GIT_TIMEOUT
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > MAX_METADATA_ENTRIES or time.monotonic() > deadline:
                    raise ToolFailure("repository_metadata_limit")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                else:
                    metadata = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ToolFailure("unsupported_repository_metadata")
                    if Path(entry.path).is_relative_to(root / "refs"):
                        size += metadata.st_size
                        if size > MAX_OUTPUT:
                            raise ToolFailure("repository_metadata_limit")
    # Inspect config BEFORE starting Git: even Git's config builtin can perform
    # repository setup. A conservative syntax subset prevents include and
    # worktree indirection without consulting external configuration at all.
    descriptor = os.open(root / "config", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ToolFailure("unsupported_repository_metadata")
        config = source.read(MAX_OUTPUT + 1)
    if len(config) > MAX_OUTPUT:
        raise ToolFailure("repository_metadata_limit")
    section = ""
    for line in config.decode("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.endswith("\\"):
            raise ToolFailure("unsupported_config_continuation")
        if line.startswith("["):
            match = re.fullmatch(
                r'\[([A-Za-z0-9.-]+)(?:\s+"[^"\\]*")?\]\s*(?:[#;].*)?', line
            )
            if match is None:
                raise ToolFailure("unsupported_config_syntax")
            section = match[1].lower()
            if section.startswith(("include", "extensions.")):
                raise ToolFailure("repository_config_indirection")
        else:
            match = re.fullmatch(r"([A-Za-z][A-Za-z0-9-]*)\s*(?:=.*)?", line)
            if match is None or not section:
                raise ToolFailure("unsupported_config_syntax")
            key = match[1].lower()
            if section == "core" and key == "worktree":
                raise ToolFailure("repository_config_indirection")
            if section == "extensions" and key != "objectformat":
                raise ToolFailure("unsupported_repository_extension")


def _branch_name(ref: str) -> str:
    if not ref.startswith("refs/heads/"):
        raise ToolFailure("unsupported_head_reference")
    name = ref[len("refs/heads/") :]
    if not name or len(name) > 1024 or any(ord(char) < 32 for char in name):
        raise ToolFailure("unsupported_branch_name")
    return name


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        _validate_repository()
        if name == "current_branch":
            code, ref = _git(["symbolic-ref", "--quiet", "HEAD"], (0, 1))
            data = {
                "branch": None if code == 1 else _branch_name(ref.rstrip("\n")),
                "detached": code == 1,
            }
        else:
            limit = arguments.get("limit", 20)
            _, refs = _git(
                [
                    "for-each-ref",
                    "--sort=refname",
                    f"--count={limit + 1}",
                    "--format=%(refname)",
                    "refs/heads/",
                ]
            )
            names = [_branch_name(ref) for ref in refs.rstrip("\n").split("\n") if ref]
            data = {"branches": names[:limit], "truncated": len(names) > limit}
        return {
            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=True)}],
            "structuredContent": data,
            "isError": False,
        }
    except (OSError, UnicodeError, ToolFailure):
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Git inspection failed: repository unsupported, unavailable, "
                        "or execution limit reached."
                    ),
                }
            ],
            "isError": True,
        }


def _tools() -> list[dict[str, Any]]:
    tools = []
    for name, description in (
        ("current_branch", "Read the workspace HEAD branch; null when detached."),
        ("list_branches", "List at most 100 local branches in refname order."),
    ):

        def schema(kind: str) -> dict[str, Any]:
            return json.loads(
                (PACKAGE / "schemas" / f"{name}.{kind}.schema.json").read_text()
            )

        tools.append(
            {
                "name": name,
                "description": description,
                "inputSchema": schema("input"),
                "outputSchema": schema("output"),
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            }
        )
    return tools


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _reject_constant(value: str) -> Any:
    raise ValueError("Non-JSON numeric constant")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def serve() -> None:
    """Serve stdin until EOF with bounded JSON-RPC replies on stdout only.

    Implements initialize, initialized notification, ping and two tools. Inputs
    come from stdio; Git reads the current directory and never mutates it.
    """
    initialized = False
    ready = False
    while True:
        line = sys.stdin.buffer.readline(MAX_MESSAGE + 3)
        if not line:
            return
        if not line.endswith(b"\n") or len(line.rstrip(b"\r\n")) > MAX_MESSAGE:
            response = _error(None, -32700, "Invalid message framing or size")
            _send(response)
            return  # Never parse the remainder of an oversized frame as a call.
        try:
            message = json.loads(
                line.decode("utf-8"),
                parse_constant=_reject_constant,
                object_pairs_hook=_unique_object,
            )
        except (UnicodeDecodeError, ValueError, RecursionError):
            _send(_error(None, -32700, "Parse error"))
            continue
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
        ):
            _send(_error(None, -32600, "Invalid request"))
            continue
        request_id = message.get("id")
        if "id" in message and (
            isinstance(request_id, bool) or not isinstance(request_id, (str, int))
        ):
            _send(_error(None, -32600, "Invalid request ID"))
            continue
        method = message["method"]
        params = message.get("params", {})
        if "id" not in message:
            if method == "notifications/initialized" and initialized and params == {}:
                ready = True
            continue  # Notifications never receive replies.
        if not isinstance(params, dict):
            response = _error(request_id, -32602, "Invalid params")
        elif method == "ping":
            response = {"jsonrpc": "2.0", "id": request_id, "result": {}}
        elif method == "initialize":
            if (
                initialized
                or params.get("protocolVersion") != PROTOCOL
                or not isinstance(params.get("capabilities"), dict)
                or not isinstance(params.get("clientInfo"), dict)
                or not all(
                    isinstance(params["clientInfo"].get(key), str)
                    and params["clientInfo"][key]
                    for key in ("name", "version")
                )
            ):
                response = _error(request_id, -32602, "Invalid initialization")
            else:
                initialized = True
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": PROTOCOL,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "git.mcp", "version": "0.1.0"},
                    },
                }
        elif method in ("tools/list", "tools/call") and not ready:
            response = _error(request_id, -32600, "Initialization required")
        elif method == "tools/list":
            if params:
                response = _error(request_id, -32602, "No pagination cursor supported")
            else:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"tools": _tools()},
                }
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            valid = (
                name in ("current_branch", "list_branches")
                and isinstance(arguments, dict)
                and not (set(params) - {"name", "arguments", "_meta"})
            )
            if valid:
                valid = (
                    not arguments
                    if name == "current_branch"
                    else not (set(arguments) - {"limit"})
                    and (
                        "limit" not in arguments
                        or type(arguments["limit"]) is int
                        and 1 <= arguments["limit"] <= 100
                    )
                )
            if not valid:
                response = _error(
                    request_id, -32602, "Unknown tool or invalid arguments"
                )
            else:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": _call(name, arguments),
                }
        else:
            response = _error(request_id, -32601, "Method not found")
        _send(response)


def _send(response: dict[str, Any]) -> None:
    encoded = json.dumps(response, ensure_ascii=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_MESSAGE:
        encoded = json.dumps(
            _error(response.get("id"), -32603, "Response limit exceeded")
        )
        if len(encoded.encode("utf-8")) > MAX_MESSAGE:
            encoded = json.dumps(_error(None, -32603, "Response limit exceeded"))
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    serve()
