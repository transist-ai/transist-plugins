"""Package-local opt-in prompt, invocation-history and APP envelope policy.

This file is selected independently by each CLI package; it imports no sibling.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import sys
import time
from pathlib import Path
from typing import Any

HINT_BYTES = 256 * 1024
HINT_COUNT = 32
FAMILY_BYTES = 1024 * 1024
# Package policy, not an APP schema/transport limit. Includes JSON escaping.
RESULT_BYTES = 1024 * 1024


def size(text: str) -> int:
    """Return the UTF-8 byte count, without normalizing whitespace."""
    return len(text.encode("utf-8"))


def serialized(value: Any) -> str:
    """Use the entrypoint's actual JSON encoding for envelope accounting."""
    return json.dumps(value)


def bounded(payload: dict) -> bool:
    """Select strict composition only for explicit limits or invocation history."""
    return payload.get("history_mode") == "invocation" or any(
        key in payload for key in ("max_context_file_bytes", "max_prompt_bytes")
    )


def answer_mode(payload: dict) -> bool:
    """Select authoritative answer handling independently of prompt limits."""
    return (
        payload.get("output_mode") == "compact"
        or payload.get("history_mode") == "invocation"
    )


def authoritative_response(api: dict, payload: dict, text: str) -> str:
    """Preserve complete answer events/reports; legacy retains markdown heuristics."""
    return (
        text.strip()
        if answer_mode(payload)
        else api["extract_canonical_response"](text)
    )


def validate(payload: dict) -> None:
    """Reject invalid opt-in controls before any provider launch."""
    if answer_mode(payload) or bounded(payload):
        names = set()
        for field in ("response_file", "summary_title_file", "pr_body_file"):
            name = Path(str(payload.get(field) or "").strip()).name
            if not name:
                continue
            if (
                name
                in {
                    "stdout.txt",
                    "stderr.txt",
                    "invocation.json",
                    "usage.json",
                    "last_message.txt",
                }
                or name in names
                or (field != "response_file" and name == "response.txt")
            ):
                raise ValueError(
                    "handoff artifact names must be distinct from diagnostics"
                )
            names.add(name)
    for field, choices in (
        ("output_mode", ("legacy", "compact")),
        ("history_mode", ("legacy", "invocation")),
    ):
        if payload.get(field, "legacy") not in choices:
            raise ValueError(f"{field} must be one of: {', '.join(choices)}")
    for field in ("max_context_file_bytes", "max_prompt_bytes"):
        if field in payload and (
            type(payload[field]) is not int or payload[field] <= 0
        ):
            raise ValueError(f"{field} must be a positive integer")
    if answer_mode(payload) and any(
        str(arg).split("=", 1)[0]
        in {"--output-format", "--format", "--output-last-message", "-o"}
        for arg in payload.get("extra_args") or []
    ):
        raise ValueError("extra_args cannot override authoritative answer transport")


def check_text(text: str, label: str, limit: int | None) -> str:
    """Fail with a logical contributor and counts, never contents or paths."""
    count = size(text)
    if limit is not None and count > limit:
        raise ValueError(
            serialized({"contributor": label, "bytes": count, "limit_bytes": limit})
        )
    return text


def read_bounded(path: str | Path, label: str, payload: dict) -> str:
    """Read at most the effective byte ceiling plus one; missing inputs fail."""
    limits = [
        payload[k]
        for k in ("max_context_file_bytes", "max_prompt_bytes")
        if k in payload
    ]
    ceiling = min(limits) if limits else HINT_BYTES
    try:
        if any(p.is_symlink() for p in (Path(path), *Path(path).parents)):
            raise ValueError("file contributors must not traverse symlinks")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("file contributors must be regular files")
            count = os.fstat(handle.fileno()).st_size
            if count > ceiling:
                raise ValueError(
                    serialized(
                        {"contributor": label, "bytes": count, "limit_bytes": ceiling}
                    )
                )
            raw = handle.read(ceiling + 1)
    except OSError as err:
        raise ValueError(
            serialized({"contributor": label, "error": "unreadable"})
        ) from err
    if len(raw) > ceiling:
        raise ValueError(
            serialized(
                {
                    "contributor": label,
                    "bytes_at_least": len(raw),
                    "limit_bytes": ceiling,
                }
            )
        )
    try:
        return raw.decode("utf-8")
    except UnicodeError as err:
        raise ValueError(
            serialized({"contributor": label, "error": "invalid_utf8"})
        ) from err


def prompt_source(payload: dict, preserving: bool = False) -> str:
    """Resolve exactly one prompt source using bounded UTF-8 file reads."""
    prompt, path = payload.get("prompt"), payload.get("prompt_file")
    inline = isinstance(prompt, str) and bool(prompt.strip())
    file_set = isinstance(path, str) and bool(path.strip())
    if inline == file_set:
        raise ValueError("exactly one of prompt or prompt_file is required")
    text = prompt if inline else read_bounded(path, "base_prompt_file", payload)
    check_text(text, "base_prompt", payload.get("max_prompt_bytes"))
    return text if inline or preserving else text.strip()


def file_path(api: dict, request: dict, raw: str) -> str:
    """Resolve file data, including staged absolute paths, without changing cwd."""
    if not raw:
        return ""
    if not isinstance(raw, str) or "\0" in raw or "\\" in raw or ".." in raw.split("/"):
        raise ValueError("invalid file data path")
    if raw.startswith("run://"):
        path = Path(api["root_run_dir"](request)) / raw[6:].lstrip("/")
    elif raw.startswith("repo://"):
        path = (
            Path(request["paths"]["workspace_dir"]) / ".transist" / raw[7:].lstrip("/")
        )
    elif "://" in raw:
        raise ValueError("unsupported file path scheme")
    else:
        path = Path(request["paths"]["workspace_dir"]) / raw
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("file data paths must not traverse symlinks")
    return str(path)


def history_dir(api: dict, request: dict, payload: dict) -> Path | None:
    """Resolve durable replay storage, or None for invocation-private run:// output."""
    raw = str(payload.get("response_file") or "").strip()
    if raw and os.path.isabs(raw):
        staging = Path(request["paths"]["run_root"]).resolve()
        target = Path(raw).resolve()
        workspace = Path(request["paths"]["workspace_dir"]).resolve()
        if staging in target.parents or workspace not in target.parents:
            return None
    if raw.startswith("run://"):
        staging = Path(request["paths"]["run_root"]).resolve()
        target = Path(api["workspace_path"](request, raw, payload)).resolve()
        if staging not in target.parents:
            raise ValueError("run:// response target must remain in invocation staging")
        return None
    if raw.startswith("runtime://"):
        raise ValueError(
            "runtime:// is not supported by the action response path resolver"
        )
    workspace = Path(request["paths"]["workspace_dir"]).resolve()
    if raw:
        target = Path(api["workspace_path"](request, raw, payload)).resolve()
        if workspace not in target.parents:
            raise ValueError(
                "invocation history response target must be inside workspace"
            )
        base = Path(str(target.with_suffix("")) + ".invocations")
    else:
        base = workspace / ".transist" / api["PLUGIN_ID"] / "invocations"
    root = api["root_run_id"](request)
    if not root:
        raise ValueError("invocation history requires a root run identity")
    directory = (
        base / hashlib.sha256((api["PLUGIN_ID"] + "\0" + root).encode()).hexdigest()
    )
    if (
        any(
            path.is_symlink()
            for path in (directory, *directory.parents)
            if path != workspace and workspace in path.parents
        )
        or workspace not in directory.resolve().parents
    ):
        raise ValueError(
            "invocation history must remain in its workspace root namespace"
        )
    return directory.resolve()


def record_paths(directory: Path) -> list[Path]:
    """List only immutable replay records; no accumulated snapshot is loaded."""
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValueError("invocation history storage must be a directory")
    return sorted(directory.glob("*.json"))


def read_record(path: Path, payload: dict) -> dict:
    """Read one minimal replay record, rejecting malformed/path-escaping data."""
    try:
        if path.is_symlink():
            raise ValueError("invocation replay records must not be symlinks")
        record = json.loads(read_bounded(path, "replay_record", payload))
        if not isinstance(record, dict) or not all(
            isinstance(record.get(key), str) for key in ("feedback", "status")
        ):
            raise ValueError("invalid invocation replay record")
        name = record["response_name"]
        if not isinstance(name, str) or Path(name).name != name or name in (".", ".."):
            raise ValueError("invalid invocation replay response name")
        if path.parent.resolve() not in (path.parent / name).resolve().parents:
            raise ValueError("invocation replay response escapes history directory")
        response = read_bounded(path.parent / name, "replay_response", payload)
        return {**record, "response_text": response}
    except (KeyError, TypeError, json.JSONDecodeError) as err:
        raise ValueError("invalid invocation replay record") from err


def render_history(paths: list[Path], payload: dict) -> str:
    """Render selected root-scoped responses/feedback with an aggregate bound."""
    sections = []
    ceiling = payload.get("max_prompt_bytes", HINT_BYTES)
    for index, path in enumerate(paths, 1):
        record = read_record(path, payload)
        sections.append(
            f"## Execution {index}\n\n### Operator feedback\n\n"
            f"{record['feedback']}\n\n### Response\n\n"
            f"{record['response_text']}\n\n### Result\n\n{record['status']}"
        )
        check_text("\n\n".join(sections), "replay_history", ceiling)
    return "\n\n".join(sections)


def compose(api: dict, request: dict, payload: dict, base: str) -> tuple[str, str, int]:
    """Compose and bound all contributors/headings; never skip read failures."""
    validate(payload)
    limit = payload.get("max_prompt_bytes")
    check_text(base, "base_prompt", limit)
    sections: list[str] = []
    contributors: list[dict] = []
    count = 0

    def add(label: str, text: str) -> None:
        """Account a contributor and the exact joined headings/separators."""
        contributors.append({"contributor": label, "bytes": size(text)})
        try:
            check_text(text, label, limit)
            sections.append(text)
            check_text("\n\n".join(sections), "composed_prompt:" + label, limit)
        except ValueError as err:
            raise ValueError(
                serialized({**json.loads(str(err)), "contributors": contributors})
            ) from err

    skills = payload.get("skills_rendered")
    if isinstance(skills, str) and skills.strip():
        check_text(skills, "skills", limit)
        add("skills", "# Workflow skills\n\n" + skills.strip())
        count += 1
    ignored = api["ignore_paths_section"](payload)
    if ignored:
        add("ignore_paths", ignored)
    feedback = api["gate_decision_feedback"](payload)
    mode = api["feedback_context_mode"](payload)
    invocation = payload.get("history_mode") == "invocation"
    directory = history_dir(api, request, payload) if invocation else None
    if invocation and directory is None and feedback and mode == "full":
        raise ValueError(
            "full feedback replay requires durable invocation history; "
            "run:// responses retain artifact-only history"
        )
    paths = record_paths(directory) if directory is not None else []
    if feedback:
        add("gate_feedback", "# Operator feedback\n\n" + feedback)
        count += 1
        raw = payload.get("response_file")
        if mode in {"full", "focused"} and (raw or paths) and (not invocation or paths):
            path = (
                api["workspace_path"](request, raw, payload) if raw else "response.txt"
            )
            contents = (
                read_record(paths[-1], payload)["response_text"]
                if invocation
                else read_bounded(path, "current_draft", payload)
            ).strip()
            if contents:
                add(
                    "current_draft",
                    f"# Current draft: {os.path.basename(path)}\n\n{contents}",
                )
                count += 1
        if mode == "full":
            if invocation:
                history = render_history(paths, payload)
            else:
                path, _ = api["interaction_paths"](request, payload)
                entries = (
                    json.loads(read_bounded(path, "legacy_history", payload))
                    if os.path.exists(path)
                    else []
                )
                if not isinstance(entries, list) or any(
                    not isinstance(entry, dict) for entry in entries
                ):
                    raise ValueError(
                        serialized(
                            {
                                "contributor": "legacy_history",
                                "error": "invalid_records",
                            }
                        )
                    )
                history = api["render_interaction_context"](
                    api["current_run_interaction_entries"](request, entries)
                )
            if history:
                add("replay_history", "# Prior interaction history\n\n" + history)
                count += 1
    for index, raw in enumerate(payload.get("context_files") or []):
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        path = api["workspace_path"](request, raw, payload)
        if api["is_ignored_path"](request, payload, path):
            continue
        contents = read_bounded(path, f"context[{index}]", payload)
        if contents:
            add(
                f"context[{index}]",
                f"# Context: {os.path.basename(path)}\n\n{contents}",
            )
            count += 1
    task = base if api["PLUGIN_ID"] == "github.copilot.cli" else base.strip()
    add("task_and_headings", "# Task\n\n" + task)
    composed = "\n\n".join(sections)
    request.setdefault("_lean_context", {}).update(
        task_prompt=base,
        composed_prompt=composed,
        feedback=feedback,
        prompt_budget={
            "contributors": contributors,
            "total_bytes": size(composed),
            "limit_bytes": limit,
        },
    )
    return composed, feedback, count


def check_exec(command: list[str], env: dict, limit: int | None = None) -> None:
    """Check actual argv/environment bytes, NULs, pointers and Linux string limits."""
    if limit is None:
        try:
            limit = os.sysconf("SC_ARG_MAX")
        except (ValueError, OSError, AttributeError):
            limit = 131072
    strings = [os.fsencode(str(arg)) for arg in command]
    strings += [os.fsencode(f"{key}={value}") for key, value in env.items()]
    if any(b"\0" in value for value in strings):
        raise ValueError("command-line contains a NUL byte")
    count = sum(len(value) + 1 for value in strings)
    count += (len(strings) + 2) * struct.calcsize("P")
    if (limit > 0 and count > limit - 4096) or (
        sys.platform.startswith("linux")
        and any(len(value) + 1 > 131072 for value in strings)
    ):
        raise ValueError(
            f"command-line length exceeds byte budget (bytes={count}, "
            f"limit_bytes={max(0, limit - 4096)})"
        )


def persist_record(api: dict, request: dict, result: dict, response: str) -> dict:
    """Build one metadata hint; no workspace snapshot or raw-log copy is made."""
    context = {
        key: value
        for key, value in (request.get("_lean_context") or {}).items()
        if key in {"composed_prompt", "feedback", "prompt_budget"}
    }
    metadata = {
        "schema": "transist.cli.invocation.v1",
        "plugin_id": api["PLUGIN_ID"],
        "plugin_version": "0.1.0",
        "root_run_id": api["root_run_id"](request),
        "run_id": request.get("run_id"),
        "step_id": (request.get("step") or {}).get("id"),
        "invoke_id": request["invoke_id"],
        **context,
        "status": result["status"],
        "exit_code": result["output"].get("exit_code"),
        "artifacts": {
            key: (
                name
                if any(h["name"] == name for h in result["artifact_hints"])
                else None
            )
            for key, name in (
                ("stdout", "stdout.txt"),
                ("stderr", "stderr.txt"),
                ("response", "response.txt"),
            )
        },
    }
    return {
        "name": "invocation.json",
        "contents": json.dumps(metadata, ensure_ascii=False),
    }


def persist_replay(api: dict, request: dict, result: dict) -> None:
    """Write durable replay after the outcome; scratch responses never write replay."""
    directory = history_dir(api, request, request["input"])
    if directory is None:
        return
    context = request.get("_lean_context") or {}
    response = result["output"].get(
        "canonical_response_text", result["output"].get("response_text", "")
    )
    directory.mkdir(parents=True, exist_ok=True)
    token = (
        f"{time.time_ns():020d}-"
        + hashlib.sha256(str(request["invoke_id"]).encode()).hexdigest()[:16]
    )
    response_name = token + ".md"
    (directory / response_name).write_text(response, encoding="utf-8")
    record = {
        "feedback": context.get("feedback", ""),
        "status": result["status"],
        "exit_code": result["output"].get("exit_code"),
        "response_name": response_name,
    }
    (directory / (token + ".json")).write_text(serialized(record), encoding="utf-8")


def handle(api: dict, request: dict) -> dict:
    """Run one request, preserving legacy defaults and enforcing opt-in envelopes."""
    payload = request.get("input") or {}
    try:
        validate(payload)
        for field in ("response_file", "summary_title_file", "pr_body_file"):
            if payload.get(field):
                path = Path(file_path(api, request, payload[field]))
                if path.exists() and not path.is_file():
                    raise ValueError("output data paths must be regular files")
        if payload.get("history_mode") == "invocation":
            history_dir(api, request, payload)
    except ValueError as err:
        return api["failed_result"](request["invoke_id"], "validation_error", str(err))
    active = bounded(payload) or payload.get("output_mode") == "compact"
    if not active:
        return api["_handle_request"](request)
    local = {**request, "input": payload, "_lean_context": {}}
    result = api["_handle_request"](local)
    try:
        result = finish(api, local, result)
        if payload.get("history_mode") == "invocation" and local["_lean_context"].get(
            "composed_prompt"
        ):
            persist_replay(api, local, result)
        return result
    except (OSError, ValueError, TypeError):
        return api["failed_result"](
            request["invoke_id"],
            "adapter_internal_error",
            "failed enforcing CLI package result contract",
        )


def partial_response(api: dict, stdout: str) -> str:
    """Extract only authoritative answer events from partial structured CLI logs."""
    if api["PLUGIN_ID"] == "codex.cli":
        return ""
    if api["PLUGIN_ID"] == "github.copilot.cli":
        answer = ""
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "assistant.message":
                continue
            data = event.get("data") or {}
            if isinstance(data, dict) and data.get("toolRequests"):
                answer = ""
            if (
                isinstance(data, dict)
                and not data.get("toolRequests")
                and isinstance(data.get("content"), str)
            ):
                answer = data["content"].strip()
        return answer
    parts = []
    result_text = ""
    if api["PLUGIN_ID"] == "opencode.cli":
        _, events = api["JsonEventBuffer"]().feed(stdout)
        for event in events:
            if isinstance(event, dict) and event.get("type") == "text":
                api["append_response_text"](
                    parts, (event.get("part") or {}).get("text")
                )
    else:
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "result" and isinstance(event.get("result"), str):
                result_text = event["result"].strip()
            if event.get("type") == "assistant":
                parts.append(api["assistant_message_text"](event.get("message")))
    return result_text or api["render_response_text"](parts)


def diagnostic_preview(text: str, ceiling: int) -> str:
    """Keep a UTF-8 log prefix with an explicit standalone diagnostic-only label."""
    marker = "\n[diagnostic preview: full log unavailable]"
    if ceiling < size(marker):
        return ""
    return (
        text.encode("utf-8")[: ceiling - size(marker)].decode("utf-8", errors="ignore")
        + marker
    )


def finish(api: dict, request: dict, result: dict) -> dict:
    """Keep authoritative output whole; diagnose or honestly fail excess hint data."""
    payload, output = request["input"], result["output"]
    hints = result["artifact_hints"]
    logs = request["_lean_context"].get("logs")
    if logs:
        hints = [h for h in hints if h["name"] not in ("stdout.txt", "stderr.txt")]
        hints += [
            {"name": name, "contents": text}
            for name, text in zip(("stdout.txt", "stderr.txt"), logs)
        ]
    else:
        for stream in ("stdout", "stderr"):
            if stream in output:
                hints = [h for h in hints if h["name"] != stream + ".txt"]
                hints.append({"name": stream + ".txt", "contents": output[stream]})
    compact = payload.get("output_mode") == "compact"
    if (
        answer_mode(payload)
        and (result.get("error") or {}).get("code") == "timeout_error"
        and api["PLUGIN_ID"] != "codex.cli"
    ):
        answer = partial_response(api, output.get("stdout", ""))
        output["response_text"] = answer
        output["canonical_response_text"] = answer
    if compact and output:
        output.pop("stdout", None)
        output.pop("stderr", None)
        if "canonical_response_text" in output:
            output["response_text"] = output["canonical_response_text"]
            output["pr_body_text"] = api["extract_pr_body"](output["response_text"])
        hints = [h for h in hints if h["name"] != "response.txt"]
        hints.append(
            {"name": "response.txt", "contents": output.get("response_text", "")}
        )
    if compact and not output and any(h["name"] == "stdout.txt" for h in hints):
        response_hint = next(
            (h["contents"] for h in hints if h["name"] == "response.txt"), ""
        )
        response_hint = request["_lean_context"].get("answer", response_hint)
        if not response_hint:
            response_hint = (
                partial_response(
                    api, next(h["contents"] for h in hints if h["name"] == "stdout.txt")
                )
                if api["PLUGIN_ID"] != "codex.cli"
                else ""
            )
        output.update(
            ok=False, response_text=response_hint, response_file="", exit_code=None
        )
        if api["PLUGIN_ID"] != "codex.cli":
            output.update(
                canonical_response_text=response_hint,
                feedback="",
                summary_title="",
                summary_title_file="",
                pr_body_file="",
                pr_body_text=api["extract_pr_body"](response_hint),
            )
            if api["PLUGIN_ID"] != "github.copilot.cli":
                output.update(session_id="", share_url="")
    if answer_mode(payload) and output:
        response = output.get("canonical_response_text", output["response_text"])
        output["response_text"] = response
        aliases = {"response.txt"}
        if output.get("response_file"):
            aliases.add(os.path.basename(output["response_file"]))
        aliases -= {"stdout.txt", "stderr.txt"}
        hints = [
            {**hint, "contents": response} if hint["name"] in aliases else hint
            for hint in hints
        ]
        if not any(hint["name"] == "response.txt" for hint in hints):
            hints.append({"name": "response.txt", "contents": response})
    hints = list({hint["name"]: hint for hint in hints}.values())
    response = output.get("canonical_response_text", output.get("response_text", ""))
    if (
        any(
            size(h.get("contents", "")) > HINT_BYTES
            for h in hints
            if h["name"] not in {"stdout.txt", "stderr.txt"}
        )
        or sum(
            size(h.get("contents", ""))
            for h in hints
            if h["name"] not in {"stdout.txt", "stderr.txt"}
        )
        >= FAMILY_BYTES
    ):
        result.update(
            status="failed",
            error=api["protocol_error"](
                "protocol_error", "CLI artifact contract exceeded"
            ),
        )
        if output:
            output["ok"] = False
    if payload.get("history_mode") == "invocation" and request["_lean_context"].get(
        "composed_prompt"
    ):
        hints.append(persist_record(api, request, result, response))
    # Reserve complete business/metadata artifacts before optional diagnostic logs.
    hints.sort(key=lambda hint: hint["name"] in {"stdout.txt", "stderr.txt"})
    diagnostics = []
    retained = []
    total = 0
    overflow = False
    if len(hints) > HINT_COUNT:
        return api["failed_result"](
            request["invoke_id"],
            "protocol_error",
            "CLI artifact hint count exceeds adapter contract",
            artifact_hints=[
                {
                    "name": "diagnostics.json",
                    "contents": serialized(
                        {
                            "hint_count": len(hints),
                            "limit_count": HINT_COUNT,
                            "total_bytes": sum(
                                size(h.get("contents", "")) for h in hints
                            ),
                            "retained_bytes": 0,
                            "full_log_available": False,
                        }
                    ),
                }
            ],
        )
    for index, hint in enumerate(hints):
        text = hint.get("contents", "")
        full = size(text)
        ceiling = min(HINT_BYTES, max(0, FAMILY_BYTES - 1 - total))
        kept = text
        if full > ceiling:
            kept = (
                diagnostic_preview(text, ceiling)
                if hint["name"] in {"stdout.txt", "stderr.txt"}
                else ""
            )
        count = size(kept)
        overflow |= count != full and (
            not compact or hint["name"] not in {"stdout.txt", "stderr.txt"}
        )
        # Manifest labels are fixed logical names, never caller file basenames.
        name = (
            hint["name"]
            if hint["name"]
            in {
                "stdout.txt",
                "stderr.txt",
                "response.txt",
                "invocation.json",
                "usage.json",
            }
            else f"artifact-{index}"
        )
        diagnostics.append(
            {
                "artifact": name,
                "total_bytes": full,
                "retained_bytes": count,
                "full_log_available": count == full,
                "retention": (
                    "full"
                    if count == full
                    else ("diagnostic_preview" if kept else "unavailable")
                ),
            }
        )
        if index < HINT_COUNT and (kept or full == 0):
            retained.append({**hint, "contents": kept})
            total += count
    result["artifact_hints"] = retained
    if compact and output:
        output.update(output_mode="compact", diagnostics={"artifacts": diagnostics})
    if overflow:
        result.update(
            status="failed",
            error=api["protocol_error"](
                "protocol_error",
                "CLI artifact contract exceeded; byte manifest reports retained data",
            ),
        )
        if output:
            output["ok"] = False
        if not compact:
            result["output"] = {}
        if not compact:
            for item in diagnostics:
                item.update(
                    retained_bytes=0, full_log_available=False, retention="unavailable"
                )
            result["artifact_hints"] = [
                {
                    "name": "diagnostics.json",
                    "contents": serialized({"artifacts": diagnostics}),
                }
            ]
    for hint in result["artifact_hints"]:
        if hint["name"] == "invocation.json":
            hint["contents"] = persist_record(api, request, result, response)[
                "contents"
            ]
            item = next(d for d in diagnostics if d["artifact"] == "invocation.json")
            item.update(
                total_bytes=size(hint["contents"]),
                retained_bytes=size(hint["contents"]),
            )
    result_count = size(serialized(result) + "\n")
    if compact and result_count > RESULT_BYTES:
        # Diagnostic previews cannot change the backend's outcome or metadata.
        for hint in retained:
            if (
                hint["name"] not in {"stdout.txt", "stderr.txt"}
                or size(serialized(result) + "\n") <= RESULT_BYTES
            ):
                continue
            text = hint["contents"]
            item = next(d for d in diagnostics if d["artifact"] == hint["name"])
            low, high = 0, size(text)
            while low < high:
                middle = (low + high + 1) // 2
                hint["contents"] = diagnostic_preview(text, middle)
                item.update(
                    retained_bytes=size(hint["contents"]),
                    full_log_available=False,
                    retention=(
                        "diagnostic_preview" if hint["contents"] else "unavailable"
                    ),
                )
                if size(serialized(result) + "\n") <= RESULT_BYTES:
                    low = middle
                else:
                    high = middle - 1
            hint["contents"] = diagnostic_preview(text, low)
            item.update(
                retained_bytes=size(hint["contents"]),
                full_log_available=False,
                retention="diagnostic_preview" if hint["contents"] else "unavailable",
            )
    if size(serialized(result) + "\n") > RESULT_BYTES:
        # Do not turn a partial authoritative JSON response into apparent success.
        for item in diagnostics:
            item.update(
                retained_bytes=0, full_log_available=False, retention="unavailable"
            )
        result = api["failed_result"](
            request["invoke_id"],
            "protocol_error",
            "CLI serialized result exceeds package envelope; "
            "authoritative output was not truncated",
            artifact_hints=[
                {
                    "name": "diagnostics.json",
                    "contents": serialized(
                        {
                            "result_bytes": result_count,
                            "limit_bytes": RESULT_BYTES,
                            "artifacts": diagnostics,
                        }
                    ),
                }
            ],
        )
    return result
