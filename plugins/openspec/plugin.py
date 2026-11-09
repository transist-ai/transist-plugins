#!/usr/bin/env python3
from __future__ import annotations

import datetime
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from process_runner import CleanupUnconfirmedError, run_process  # noqa: E402
from gate_review import valid_review_request  # noqa: E402
from lean_io import (  # noqa: E402
    PromptSections,
    ContributorReadError,
    bounded_text,
    finalize_result,
    output_mode,
    payload_hint,
    positive_limit,
    record_logs,
    save_artifact,
    strict_bounded,
)

APP_PROTOCOL = "app.v1"
PLUGIN_ID = "openspec"
PLUGIN_VERSION = "0.1.0"
ACTIONS = ["generate_plan", "verify"]
DEFAULT_TIMEOUT_SECONDS = 600
TIMEOUT_GRACE_SECONDS = 5
APP_INVOKE_PROGRESS_TYPE = "app.invoke.progress.v1"
DEFAULT_IGNORE_PATHS = ("**/_build/**", "**/deps/*/_build/**")
BACKEND_GH_COPILOT = "gh-copilot"
BACKEND_CODEX = "codex"
BACKEND_CURSOR = "cursor"
APPROVED_PLAN_MODE_OPENSPEC_CHANGE = "openspec_change"
APPROVED_PLAN_MODE_PROSE_BUNDLE = "prose_bundle"
FEEDBACK_CONTEXT_MODES = {"full", "focused", "feedback-only"}
REPO_CONTEXT_MODES = {"none", "full", "paths"}
DETAIL_LEVELS = {"concise", "standard", "detailed"}
ALLOWED_DELTA_TYPES = ("ADDED", "MODIFIED", "REMOVED", "RENAMED")
SPEC_DELTA_HEADER_RE = re.compile(
    r"^## (ADDED|MODIFIED|REMOVED|RENAMED) Requirements\s*$", re.MULTILINE
)
SPEC_REQUIREMENT_HEADER_RE = re.compile(r"^### Requirement:\s+\S.*$", re.MULTILINE)
SPEC_SCENARIO_HEADER_RE = re.compile(r"^#### Scenario:\s+\S.*$", re.MULTILINE)
MAX_HINT_COUNT = 32
MAX_HINT_BYTES = 256 * 1024
MAX_HINT_TOTAL_BYTES = 1024 * 1024


class BinaryNotFoundError(Exception):
    pass


class CommandRunnerError(Exception):
    """Keep unexpected runner diagnostics out of APP results."""


def command_heartbeat(request: dict) -> Callable[[], None]:
    """Return a safe, main-thread status callback for a running command."""
    last = time.monotonic()

    def tick() -> None:
        nonlocal last
        now = time.monotonic()
        if now - last >= 1:
            last = now
            emit_status_preview(request["invoke_id"], "Command is still running...")

    return tick


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def read_frame():
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


def artifact_hint(name: str, contents: str) -> dict:
    """Return a bounded inline hint with a safe, path-disambiguated filename."""
    raw_name = str(name or "")
    if not raw_name.strip():
        raw_name = "hint.txt"
    safe_name = raw_name
    if "--" in raw_name or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", raw_name
    ):
        label = re.sub(r"[^A-Za-z0-9._-]+", "__", raw_name)[:80].strip("._-") or "hint"
        safe_name = f"{label}--{hashlib.sha256(raw_name.encode('utf-8')).hexdigest()}"
    if len(contents.encode("utf-8")) > MAX_HINT_BYTES:
        raise ValueError(
            f"artifact hint '{raw_name[:120]}' exceeds {MAX_HINT_BYTES} bytes"
        )
    return {"name": safe_name, "contents": contents, "original_path": raw_name}


def progress_frame(invoke_id, stream="stdout", delta_lines=1, line_preview=None):
    payload = {
        "type": APP_INVOKE_PROGRESS_TYPE,
        "protocol_version": APP_PROTOCOL,
        "invoke_id": invoke_id,
        "stream": stream,
        "delta_lines": delta_lines,
    }
    if isinstance(line_preview, str) and line_preview.strip():
        payload["line_preview"] = line_preview.strip()
    return payload


def emit_status_preview(invoke_id, message):
    if isinstance(message, str) and message.strip():
        emit(
            progress_frame(
                invoke_id, stream="status", delta_lines=0, line_preview=message
            )
        )


def build_result(
    invoke_id: str,
    status: str,
    output: dict,
    artifact_hints: list | None = None,
    error: dict | None = None,
    usage: dict | None = None,
) -> dict:
    """Build strict APP output, preserving usage and hint paths as artifacts.

    Exceeding runtime hint limits returns a bounded validation failure, never a
    nominal success whose artifacts the adapter cannot persist.
    """
    hints = list(artifact_hints or [])
    if usage and not any(hint["name"] == "usage.json" for hint in hints):
        hints.append(artifact_hint("usage.json", json.dumps(usage, sort_keys=True)))
    associations = {
        hint["name"]: hint["original_path"]
        for hint in hints
        if hint.get("original_path")
    }
    if associations:
        hints.append(
            artifact_hint(
                "artifact-paths.json", json.dumps(associations, sort_keys=True)
            )
        )
    total = sum(len(hint.get("contents", "").encode("utf-8")) for hint in hints)
    if len(hints) > MAX_HINT_COUNT or total > MAX_HINT_TOTAL_BYTES:
        status, hints = "failed", []
        error = {
            "code": "validation_error",
            "class": "validation",
            "retryability": "none",
            "message": (
                f"artifact hints exceed limits: {MAX_HINT_COUNT} files,"
                f" {MAX_HINT_TOTAL_BYTES} total bytes"
            ),
        }
    frame = {
        "type": "app.invoke.result.v1",
        "protocol_version": APP_PROTOCOL,
        "invoke_id": invoke_id,
        "status": status,
        "output": output or {},
        "artifact_hints": hints,
        "evidence_hints": [],
        "metrics": {},
    }
    if usage:
        canonical_usage = {
            key: value
            for key, value in usage.items()
            if (key in {"provider", "model"} and isinstance(value, str))
            or (
                key in {"input_tokens", "output_tokens", "total_tokens"}
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            )
        }
        if canonical_usage:
            frame["usage"] = canonical_usage
    if error is not None:
        frame["error"] = error
    return frame


def failed_result(
    invoke_id,
    code,
    error_class,
    message,
    retryability="none",
    output=None,
    artifact_hints=None,
    usage=None,
):
    return build_result(
        invoke_id,
        "failed",
        output or {},
        artifact_hints=artifact_hints,
        error={
            "code": code,
            "class": error_class,
            "message": message,
            "retryability": retryability,
        },
        usage=usage,
    )


def root_run_dir(request):
    run_root = os.path.normpath(request["paths"]["run_root"])
    while os.path.basename(os.path.dirname(run_root)) == "contained_runs":
        run_root = os.path.dirname(os.path.dirname(run_root))
    run_name = os.path.basename(run_root)
    parent = os.path.dirname(run_root)
    if run_name.startswith("run_"):
        root_run_id = run_name[len("run_") :].split("__", 1)[0]
        return os.path.join(parent, f"run_{root_run_id}")
    return run_root


def workspace_path(request, raw_path, _input_payload=None):
    if not raw_path:
        return ""
    candidate = str(raw_path).strip()
    if not candidate:
        return ""
    if "\0" in candidate or "\\" in candidate or ".." in candidate.split("/"):
        raise ValueError("invalid file data path")
    if candidate.startswith("run://"):
        candidate = os.path.join(root_run_dir(request), candidate[6:].lstrip("/"))
    elif candidate.startswith("repo://"):
        candidate = os.path.join(
            request["paths"]["workspace_dir"], ".transist", candidate[7:].lstrip("/")
        )
    elif "://" in candidate:
        raise ValueError("unsupported file path scheme")
    target = (
        Path(candidate)
        if os.path.isabs(candidate)
        else Path(request["paths"]["workspace_dir"]) / candidate
    )
    if any(p.is_symlink() for p in (target, *target.parents)):
        raise ValueError("file data paths must not traverse symlinks")
    return str(target)


def gate_decision_feedback(input_payload):
    payload = (input_payload or {}).get("gate_decision")
    if not valid_gate_decision(payload):
        if isinstance(payload, dict) and "review_request" in payload:
            raise ValueError("invalid gate_decision with review_request")
        return "", "", ""
    if payload.get("decision") != "request_changes":
        return "", "", ""
    actor = str(payload.get("actor") or "").strip()
    actor_source = str(payload.get("actor_source") or "").strip()
    attribution = actor
    if actor_source:
        attribution = f"{attribution} ({actor_source})" if attribution else actor_source
    return "request_changes", str(payload.get("reason") or "").strip(), attribution


def valid_gate_decision(payload):
    if not isinstance(payload, dict):
        return False
    allowed = {
        "schema",
        "run_id",
        "gate_id",
        "step_id",
        "decision",
        "actor",
        "actor_source",
        "reason",
        "decided_at_ms",
        "review_request",
    }
    if set(payload) - allowed:
        return False
    if "review_request" in payload and not valid_review_request(
        payload["review_request"]
    ):
        return False
    if payload.get("schema") != "transist.gate_decision.v1":
        return False
    for field in ("run_id", "gate_id", "step_id"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            return False
    decision = payload.get("decision")
    if not isinstance(decision, str) or decision not in {
        "approved",
        "rejected",
        "request_changes",
    }:
        return False
    timestamp = payload.get("decided_at_ms")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0:
        return False
    return all(
        field not in payload or isinstance(payload[field], str)
        for field in ("actor", "actor_source", "reason")
    )


def feedback_context_mode(input_payload):
    raw = str((input_payload or {}).get("feedback_context_mode") or "").strip()
    if not raw:
        return "full"
    if raw not in FEEDBACK_CONTEXT_MODES:
        choices = ", ".join(sorted(FEEDBACK_CONTEXT_MODES))
        raise ValueError(f"feedback_context_mode must be one of: {choices}")
    return raw


def repo_context_mode(input_payload):
    raw = str((input_payload or {}).get("repo_context_mode") or "").strip()
    if not raw:
        return "none"
    if raw not in REPO_CONTEXT_MODES:
        choices = ", ".join(sorted(REPO_CONTEXT_MODES))
        raise ValueError(f"repo_context_mode must be one of: {choices}")
    return raw


def ignore_paths(input_payload):
    raw_paths = (input_payload or {}).get("ignore_paths") or []
    if not isinstance(raw_paths, list):
        raise ValueError("ignore_paths must be an array of strings")
    patterns = list(DEFAULT_IGNORE_PATHS)
    for raw_path in raw_paths:
        text = str(raw_path or "").strip()
        if not text:
            raise ValueError("ignore_paths entries must be non-empty strings")
        patterns.append(text)
    seen = set()
    result = []
    for pattern in patterns:
        if pattern in seen:
            continue
        seen.add(pattern)
        result.append(pattern)
    return result


def path_relative_to_base(path, base_dir):
    if not path or not base_dir:
        return ""
    try:
        absolute_path = os.path.abspath(path)
        absolute_base = os.path.abspath(base_dir)
        if os.path.commonpath([absolute_base, absolute_path]) != absolute_base:
            return ""
        return os.path.relpath(absolute_path, absolute_base)
    except ValueError:
        return ""


def ignore_pattern_variants(pattern):
    normalized = str(pattern or "").replace("\\", "/").strip().lstrip("./")
    variants = [normalized]
    if normalized.startswith("**/"):
        variants.append(normalized[3:])
    if normalized.endswith("/**"):
        variants.append(normalized[:-3])
    return [variant for variant in variants if variant]


def relative_path_matches_ignore(relative_path, patterns):
    normalized_path = str(relative_path or "").replace("\\", "/").strip("/")
    if not normalized_path or normalized_path == ".":
        return False
    for pattern in patterns:
        for variant in ignore_pattern_variants(pattern):
            if fnmatch.fnmatchcase(normalized_path, variant):
                return True
            if variant.endswith("/**"):
                prefix = variant[:-3].rstrip("/")
                if normalized_path == prefix or normalized_path.startswith(
                    prefix + "/"
                ):
                    return True
    return False


def is_ignored_path(input_payload, path, base_dir):
    relative_path = path_relative_to_base(path, base_dir)
    return relative_path_matches_ignore(relative_path, ignore_paths(input_payload))


def ensure_not_ignored_path(input_payload, path, field_name, base_dir):
    if is_ignored_path(input_payload, path, base_dir):
        raise ValueError(f"{field_name} points at an ignored path: {path}")


def ignore_paths_section(input_payload):
    patterns = ignore_paths(input_payload)
    if not patterns:
        return ""
    lines = "\n".join(f"- `{pattern}`" for pattern in patterns)
    return (
        "# Ignored paths\n\nDo not inspect, search, read, edit, write, format, or"
        " delete files matching these path patterns. Treat them as out of scope for"
        f" this task.\n\n{lines}"
    )


def resolve_detail_level(input_payload):
    """Return the requested detail level, defaulting to 'standard'.

    Args:
        input_payload: dict of action inputs.

    Returns:
        One of 'concise', 'standard', or 'detailed'.

    Raises:
        ValueError: when the value is not one of the allowed levels.
    """
    raw = str((input_payload or {}).get("detail_level") or "").strip()
    if not raw:
        return "standard"
    if raw not in DETAIL_LEVELS:
        choices = ", ".join(sorted(DETAIL_LEVELS))
        raise ValueError(f"detail_level must be one of: {choices}")
    return raw


def detail_level_style_block(input_payload):
    """Return a prompt-injected style section for the requested detail level.

    Returns an empty string for 'standard' (no injection; workflow prompts
    already carry the default style rules).  Returns a non-empty section
    string for 'concise' and 'detailed' that is prepended to the task prompt
    so the model adjusts artifact verbosity accordingly.

    Args:
        input_payload: dict of action inputs.

    Returns:
        A markdown section string, or '' for the standard level.
    """
    level = resolve_detail_level(input_payload)
    if level == "standard":
        return ""
    if level == "concise":
        return (
            "## Style: ultra-concise\n\n- Terse bullets only. Zero paragraphs. One idea"
            " per bullet. Imperative voice.\n- No filler, no meta-commentary, no"
            " restating the brief or other artifacts.\n- `proposal.md`: ≤ 5 bullets"
            " total across Why / What / Non-goals.\n- `design.md`: ≤ 60 words per"
            " section; diagrams/tables only; no narrative prose.\n- `tasks.md`:"
            " one-liner per task; no sub-bullets.\n- `specs/<capability>/spec.md`:"
            " acceptance criteria and invariants only; no narrative.\n- `README.md`: ≤"
            " 100 words; links into other artifacts only.\n- Implementation report: ≤"
            " 100 words total; bullets only.\n- Validation report: ≤ 80 words total;"
            " single verdict line and minimal findings.\n- Empty sections: write `-"
            " None` and stop. Never pad."
        )
    # detailed
    return (
        "## Style: detailed\n\n- Use prose paragraphs alongside bullets where they aid"
        " clarity.\n- `proposal.md`: up to 20 bullets; short explanatory paragraphs"
        " allowed.\n- `design.md`: no word cap per section; narrative rationale"
        " encouraged; diagrams/tables preferred but not required.\n- `tasks.md`:"
        " sub-bullets and brief explanations allowed per task.\n-"
        " `specs/<capability>/spec.md`: full scenario narratives with context, edge"
        " cases, and rationale.\n- `README.md`: up to 500 words; include context and"
        " usage guidance.\n- Implementation report: up to 500 words; include rationale"
        " for non-obvious changes.\n- Validation report: up to 400 words; detail all"
        " findings with file citations.\n- Populate all sections even when content is"
        " thin; avoid `- None` unless genuinely empty."
    )


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def write_text(path, contents):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(contents)
        if contents and not contents.endswith("\n"):
            handle.write("\n")


def write_json(path, payload):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def resolve_prompt_input(input_payload, request=None):
    limit = positive_limit(input_payload, "max_prompt_bytes")
    prompt = input_payload.get("prompt")
    prompt_file = input_payload.get("prompt_file")
    prompt_set = isinstance(prompt, str) and bool(prompt.strip())
    prompt_file_set = isinstance(prompt_file, str) and bool(prompt_file.strip())
    if prompt_set == prompt_file_set:
        raise ValueError("exactly one of prompt or prompt_file is required")
    if prompt_set:
        if limit is not None and len(prompt.encode("utf-8")) > limit:
            if request is not None:
                save_artifact(
                    request,
                    "prompt-composition.json",
                    json.dumps(
                        {
                            "sections": [
                                {"label": "Task", "bytes": len(prompt.encode("utf-8"))}
                            ],
                            "exceeded": True,
                        }
                    ),
                )
            raise ValueError(f"task prompt exceeds {limit} UTF-8 bytes")
        return str(prompt)
    try:
        return bounded_text(
            str(prompt_file),
            limit,
            label="prompt_file",
            strict=strict_bounded(input_payload),
        )
    except ContributorReadError as error:
        if request is not None:
            save_artifact(
                request,
                "prompt-composition.json",
                json.dumps(
                    {
                        "sections": [error.diagnostic()],
                        "read_failed": True,
                        "exceeded": error.reason == "exceeds budget",
                    }
                ),
            )
        raise


def persist_skills_artifacts(artifacts_dir, input_payload):
    skills_rendered = input_payload.get("skills_rendered")
    skills_bundle = input_payload.get("skills_bundle")
    if isinstance(skills_rendered, str) and skills_rendered.strip():
        with open(
            os.path.join(artifacts_dir, "workflow_skills.md"), "w", encoding="utf-8"
        ) as handle:
            handle.write(skills_rendered)
            if not skills_rendered.endswith("\n"):
                handle.write("\n")
    if skills_bundle is not None:
        with open(
            os.path.join(artifacts_dir, "workflow_skills.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump(skills_bundle, handle, indent=2, sort_keys=True)
            handle.write("\n")


def command_timeout_seconds(input_payload):
    raw_ms = (input_payload or {}).get("timeout_ms")
    if raw_ms is None:
        return DEFAULT_TIMEOUT_SECONDS
    if isinstance(raw_ms, bool) or not isinstance(raw_ms, int) or raw_ms <= 0:
        raise ValueError("timeout_ms must be a positive integer")
    timeout_seconds = max(1, raw_ms // 1000)
    return max(1, timeout_seconds - TIMEOUT_GRACE_SECONDS)


def run_command(
    command, cwd, timeout_seconds, stdin_text=None, env_overrides=None, *, request=None
):
    """Run and finalize a command; optional request enables safe APP heartbeats."""
    try:
        env = os.environ.copy()
        env.setdefault("CI", "1")
        env.setdefault("NO_COLOR", "1")
        if env_overrides:
            env.update(env_overrides)
        completed = run_process(
            command,
            cwd=cwd,
            timeout=timeout_seconds,
            input=stdin_text,
            env=env,
            on_tick=command_heartbeat(request) if request is not None else None,
        )
        if request is not None:
            label = "validate" if command[0] == "openspec" else "backend"
            record_logs(request, label, completed.stdout, completed.stderr)
        return completed
    except FileNotFoundError as err:
        raise BinaryNotFoundError(f"{command[0]} CLI not found in PATH") from err
    except (CleanupUnconfirmedError, subprocess.TimeoutExpired) as err:
        if request is not None:
            label = "validate" if command[0] == "openspec" else "backend"
            record_logs(request, label, err.stdout or "", err.stderr or "")
        raise
    except Exception as err:
        raise CommandRunnerError() from err


# ---------------------------------------------------------------------------
# Backend usage extraction
#
# The openspec plugin shells out to raw backend CLIs (`gh copilot`, `codex
# exec`) instead of invoking sibling plugins, so we duplicate the minimal
# usage-extraction logic needed to surface token counts to the runtime. The
# logic mirrors what `github.copilot.cli` and `codex.cli` already do.
# ---------------------------------------------------------------------------


_COPILOT_INPUT_RE = re.compile(
    r"(?:input|prompt)\s+tokens?\s*[:\s=]\s*([\d,]+)", re.IGNORECASE
)
_COPILOT_OUTPUT_RE = re.compile(
    r"(?:output|completion)\s+tokens?\s*[:\s=]\s*([\d,]+)", re.IGNORECASE
)
_COPILOT_TOTAL_RE = re.compile(r"total\s+tokens?\s*[:\s=]\s*([\d,]+)", re.IGNORECASE)


def _parse_token_int(s):
    try:
        return int(str(s).replace(",", "").replace("_", ""))
    except (ValueError, TypeError):
        return None


def _coerce_token_count(raw):
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _parse_iso8601_epoch(raw):
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _normalize_path(raw):
    text = str(raw or "").strip()
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(os.path.expanduser(text)))


def _copilot_session_state_dir():
    override = str(os.environ.get("COPILOT_SESSION_STATE_DIR") or "").strip()
    if override:
        return os.path.expanduser(override)
    config_dir = str(os.environ.get("COPILOT_CONFIG_DIR") or "").strip()
    if not config_dir:
        config_dir = os.path.expanduser("~/.copilot")
    else:
        config_dir = os.path.expanduser(config_dir)
    return os.path.join(config_dir, "session-state")


def snapshot_copilot_sessions(session_state_dir=None):
    session_state_dir = session_state_dir or _copilot_session_state_dir()
    snapshot = {}
    try:
        session_ids = os.listdir(session_state_dir)
    except OSError:
        return snapshot
    for session_id in session_ids:
        events_path = os.path.join(session_state_dir, session_id, "events.jsonl")
        try:
            snapshot[events_path] = os.stat(events_path).st_mtime
        except OSError:
            continue
    return snapshot


def _copilot_usage_from_shutdown(shutdown_data):
    model_metrics = shutdown_data.get("modelMetrics") or {}
    if not isinstance(model_metrics, dict) or not model_metrics:
        return None
    input_tokens = 0
    completion_tokens = 0
    reasoning_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    for metric in model_metrics.values():
        usage = (metric or {}).get("usage") or {}
        input_tokens += _coerce_token_count(usage.get("inputTokens"))
        completion_tokens += _coerce_token_count(usage.get("outputTokens"))
        reasoning_tokens += _coerce_token_count(usage.get("reasoningTokens"))
        cache_read_tokens += _coerce_token_count(usage.get("cacheReadTokens"))
        cache_write_tokens += _coerce_token_count(usage.get("cacheWriteTokens"))
    output_tokens = completion_tokens + reasoning_tokens
    if not any([input_tokens, output_tokens, cache_read_tokens, cache_write_tokens]):
        return None
    result = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if reasoning_tokens:
        result["reasoning_tokens"] = reasoning_tokens
    if cache_read_tokens:
        result["cache_read_tokens"] = cache_read_tokens
    if cache_write_tokens:
        result["cache_write_tokens"] = cache_write_tokens
    current_model = str(shutdown_data.get("currentModel") or "").strip()
    if current_model:
        result["model"] = current_model
    elif len(model_metrics) == 1:
        result["model"] = next(iter(model_metrics.keys()))
    return result


def _parse_copilot_session_file(events_path, repo_cwd):
    repo_root = _normalize_path(repo_cwd)
    if not repo_root:
        return None
    start_epoch = None
    shutdown_epoch = None
    shutdown_data = None
    session_context = {}
    try:
        with open(events_path, "r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entry_type = str(entry.get("type") or "")
                entry_data = entry.get("data") or {}
                if entry_type == "session.start":
                    if start_epoch is None:
                        start_epoch = _parse_iso8601_epoch(
                            entry_data.get("startTime") or entry.get("timestamp")
                        )
                        session_context = entry_data.get("context") or {}
                elif entry_type == "session.shutdown":
                    shutdown_epoch = _parse_iso8601_epoch(entry.get("timestamp"))
                    shutdown_data = entry_data
    except OSError:
        return None
    if not shutdown_data:
        return None
    session_cwd = _normalize_path(session_context.get("cwd"))
    session_git_root = _normalize_path(session_context.get("gitRoot"))
    if repo_root not in {session_cwd, session_git_root}:
        return None
    usage = _copilot_usage_from_shutdown(shutdown_data)
    if not usage:
        return None
    return {
        "start_epoch": start_epoch,
        "shutdown_epoch": shutdown_epoch,
        "usage": usage,
    }


def extract_copilot_session_usage(
    repo_cwd, started_at_epoch=None, session_snapshot=None, session_state_dir=None
):
    session_state_dir = session_state_dir or _copilot_session_state_dir()
    candidates = []
    try:
        session_ids = os.listdir(session_state_dir)
    except OSError:
        return None
    for session_id in session_ids:
        events_path = os.path.join(session_state_dir, session_id, "events.jsonl")
        try:
            mtime = os.stat(events_path).st_mtime
        except OSError:
            continue
        previous_mtime = (session_snapshot or {}).get(events_path)
        if previous_mtime is not None and mtime <= previous_mtime:
            continue
        parsed = _parse_copilot_session_file(events_path, repo_cwd)
        if not parsed:
            continue
        start_epoch = parsed.get("start_epoch")
        shutdown_epoch = parsed.get("shutdown_epoch") or 0
        if started_at_epoch is None:
            distance_score = 0
            recent_score = 0
        else:
            anchor = start_epoch if start_epoch is not None else mtime
            distance_score = -abs(anchor - started_at_epoch)
            recent_score = (
                1
                if start_epoch is not None and start_epoch >= started_at_epoch - 60
                else 0
            )
        candidates.append(
            (
                (
                    1 if previous_mtime is None else 0,
                    recent_score,
                    distance_score,
                    shutdown_epoch,
                    mtime,
                ),
                parsed["usage"],
            )
        )
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def parse_copilot_stats(stdout_text):
    """Best-effort extraction of token counts from gh copilot non-silent stdout."""
    if not stdout_text:
        return None
    input_tokens = None
    output_tokens = None
    total_tokens = None
    for line in stdout_text.splitlines():
        m = _COPILOT_INPUT_RE.search(line)
        if m:
            input_tokens = _parse_token_int(m.group(1))
        m = _COPILOT_OUTPUT_RE.search(line)
        if m:
            output_tokens = _parse_token_int(m.group(1))
        m = _COPILOT_TOTAL_RE.search(line)
        if m:
            total_tokens = _parse_token_int(m.group(1))
    if not any(v is not None for v in [input_tokens, output_tokens, total_tokens]):
        return None
    usage = {}
    if input_tokens is not None:
        usage["input_tokens"] = input_tokens
    if output_tokens is not None:
        usage["output_tokens"] = output_tokens
    if total_tokens is not None:
        usage["total_tokens"] = total_tokens
    elif input_tokens is not None and output_tokens is not None:
        usage["total_tokens"] = input_tokens + output_tokens
    return usage if usage else None


def _codex_home():
    return os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))


def _parse_codex_session_file(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    if not lines:
        return None
    try:
        meta = json.loads(lines[0])
        if meta.get("type") != "session_meta":
            return None
        originator = str((meta.get("payload") or {}).get("originator") or "")
        if not originator.startswith("codex") or "Desktop" in originator:
            return None
        provider = str((meta.get("payload") or {}).get("model_provider") or "openai")
    except (json.JSONDecodeError, KeyError, AttributeError):
        return None
    model = None
    last_total = None
    for line in lines[1:]:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        entry_type = entry.get("type", "")
        payload = entry.get("payload") or {}
        if entry_type == "turn_context" and payload.get("model"):
            model = payload["model"]
        if (
            entry_type == "event_msg"
            and payload.get("type") == "token_count"
            and payload.get("info")
        ):
            total = (payload["info"] or {}).get("total_token_usage")
            if total:
                last_total = total
    if not last_total:
        return None
    input_tokens = last_total.get("input_tokens", 0) or 0
    output_tokens = (last_total.get("output_tokens") or 0) + (
        last_total.get("reasoning_output_tokens") or 0
    )
    total_tokens = last_total.get("total_tokens", 0) or 0
    result = {
        "provider": provider,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens or (input_tokens + output_tokens),
    }
    if model:
        result["model"] = model
    return result


def extract_codex_usage(started_at_epoch):
    sessions_dir = os.path.join(_codex_home(), "sessions")
    now = datetime.datetime.utcnow()
    candidates = []
    for delta in range(2):
        day = now - datetime.timedelta(days=delta)
        day_dir = os.path.join(
            sessions_dir,
            f"{day.year:04d}",
            f"{day.month:02d}",
            f"{day.day:02d}",
        )
        try:
            names = os.listdir(day_dir)
        except OSError:
            continue
        for name in names:
            if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                continue
            fpath = os.path.join(day_dir, name)
            try:
                mtime = os.stat(fpath).st_mtime
            except OSError:
                continue
            if mtime >= started_at_epoch:
                candidates.append((mtime, fpath))
    candidates.sort(reverse=True)
    for _, fpath in candidates:
        result = _parse_codex_session_file(fpath)
        if result:
            return result
    return None


def extract_backend_usage(
    backend,
    working_dir,
    started_at_epoch,
    session_snapshot,
    completed,
    session_state_dir=None,
):
    """Return a usage dict for the given backend, or None if unavailable."""
    try:
        if backend == BACKEND_GH_COPILOT:
            usage = extract_copilot_session_usage(
                working_dir,
                started_at_epoch=started_at_epoch,
                session_snapshot=session_snapshot,
                session_state_dir=session_state_dir,
            )
            if usage is None and completed is not None:
                usage = parse_copilot_stats(completed.stdout)
            return usage
        if backend == BACKEND_CODEX:
            return extract_codex_usage(started_at_epoch)
    except (
        Exception
    ):  # noqa: BLE001 - usage extraction must never break the primary flow
        return None
    return None


def sanitize_slug(raw):
    slug = re.sub(r"[^a-z0-9]+", "-", str(raw or "").strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "change"


def ensure_openspec_initialized(repo_root):
    openspec_dir = os.path.join(repo_root, "openspec")
    if not os.path.isdir(openspec_dir):
        raise ValueError(
            f"target repo '{repo_root}' is missing an initialized openspec/ directory;"
            " run 'openspec init' manually first"
        )
    return openspec_dir


def change_dir(repo_root, change_name):
    return os.path.join(repo_root, "openspec", "changes", change_name)


def resolve_optional_path(request, raw_path, input_payload, field_name):
    if raw_path is None:
        return ""
    path = workspace_path(request, raw_path, input_payload)
    if not path:
        raise ValueError(f"{field_name} must not be empty")
    return os.path.normpath(path)


def transient_repo_root(request, action, effective_change_name):
    return os.path.join(
        root_run_dir(request),
        ".openspec_workspaces",
        action,
        effective_change_name,
        "repo",
    )


def transient_change_dir(request, action, effective_change_name):
    return change_dir(
        transient_repo_root(request, action, effective_change_name),
        effective_change_name,
    )


def ensure_directory(path, label):
    if not os.path.isdir(path):
        raise ValueError(f"{label} '{path}' did not resolve to an existing directory")
    return path


def copy_tree(src, dst, input_payload=None, ignore_base_dir=""):
    parent = os.path.dirname(dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(dst):
        shutil.rmtree(dst)
    if input_payload is not None and ignore_base_dir:

        def ignore_callback(directory, names):
            return {
                name
                for name in names
                if is_ignored_path(
                    input_payload, os.path.join(directory, name), ignore_base_dir
                )
            }

    else:
        ignore_callback = None

    shutil.copytree(src, dst, ignore=ignore_callback)


def collect_context_files(request, input_payload, budget=None):
    sections = []
    repo_root = request["paths"]["workspace_dir"]
    for index, raw_path in enumerate(input_payload.get("context_files") or []):
        # Empty optional entries select no contributor, never the workspace itself.
        if not str(raw_path or "").strip():
            continue
        path = workspace_path(request, raw_path, input_payload)
        if repo_root and is_ignored_path(input_payload, path, repo_root):
            continue
        try:
            contents = (
                budget.read(path, f"context_files[{index}]")
                if budget is not None
                else read_text(path)
            )
        except OSError:
            continue
        if not contents:
            continue
        if budget is not None:
            yield os.path.basename(path), contents
        else:
            sections.append((os.path.basename(path), contents))
    yield from sections


def collect_change_file_sections(
    change_root, budget=None, *, allow_missing=False, label="change_files"
):
    sections = []
    exists = (
        budget.directory(change_root, label, allow_missing=allow_missing)
        if budget is not None
        else os.path.isdir(change_root)
    )
    if not exists:
        return

    def traversal_error(_error: OSError) -> None:
        """Reject unreadable selected replay trees only under explicit byte controls."""
        if budget is not None and budget.strict:
            budget.fail_read(ContributorReadError(label, "unavailable"))

    file_index = 0
    for root, directories, files in os.walk(change_root, onerror=traversal_error):
        directories.sort()
        for filename in sorted(files):
            if not filename.endswith(".md"):
                continue
            path = os.path.join(root, filename)
            relative = os.path.relpath(path, change_root)
            try:
                contents = (
                    budget.read(path, f"{label}[{file_index}]")
                    if budget is not None
                    else read_text(path)
                )
            except OSError:
                continue
            if contents:
                if budget is not None:
                    yield relative, contents
                else:
                    sections.append((relative, contents))
            file_index += 1
    yield from sorted(sections, key=lambda item: item[0])


def ensure_empty_directory(path):
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)
    return path


def resolve_repo_context_paths(input_payload, repo_root):
    raw_paths = input_payload.get("repo_context_paths") or []
    if repo_context_mode(input_payload) != "paths":
        return []
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError(
            "repo_context_paths must be a non-empty array when repo_context_mode=paths"
        )

    resolved_paths = []
    seen = set()
    for entry in raw_paths:
        raw_path = str(entry or "").strip()
        if not raw_path:
            raise ValueError("repo_context_paths entries must not be empty")
        if (
            os.path.isabs(raw_path)
            or raw_path.startswith("run://")
            or raw_path.startswith("repo://")
        ):
            raise ValueError("repo_context_paths entries must be repo-relative paths")
        relative_path = os.path.normpath(raw_path)
        if relative_path in {"", "."}:
            raise ValueError(
                "repo_context_paths entries must not point at the repo root; use"
                " repo_context_mode=full instead"
            )
        resolved_path = os.path.normpath(os.path.join(repo_root, relative_path))
        if os.path.commonpath([repo_root, resolved_path]) != repo_root:
            raise ValueError(
                f"repo_context_paths entry '{raw_path}' resolves outside the workspace"
            )
        if not os.path.exists(resolved_path):
            raise ValueError(
                f"repo_context_paths entry '{raw_path}' did not resolve to an existing"
                " file or directory"
            )
        if is_ignored_path(input_payload, resolved_path, repo_root):
            raise ValueError(
                f"repo_context_paths entry '{raw_path}' points at an ignored path"
            )
        if resolved_path in seen:
            continue
        seen.add(resolved_path)
        resolved_paths.append((relative_path, resolved_path))
    return resolved_paths


def stage_repo_context(
    request, input_payload, effective_change_name, repo_root, repo_context_paths
):
    staging_root = os.path.join(
        root_run_dir(request),
        ".openspec_workspaces",
        "generate_plan_context",
        effective_change_name,
    )
    ensure_empty_directory(staging_root)
    for relative_path, source_path in repo_context_paths:
        destination = os.path.join(staging_root, relative_path)
        if os.path.isdir(source_path):
            copy_tree(
                source_path,
                destination,
                input_payload=input_payload,
                ignore_base_dir=repo_root,
            )
            continue
        parent = os.path.dirname(destination)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copy2(source_path, destination)
    return staging_root


def resolve_target_repo_path(input_payload):
    raw = str((input_payload or {}).get("target_repo_path") or "").strip()
    if not raw:
        return ""
    if not os.path.isabs(raw):
        raise ValueError("target_repo_path must be an absolute path")
    resolved = os.path.normpath(raw)
    if not os.path.isdir(resolved):
        raise ValueError(
            f"target_repo_path '{raw}' did not resolve to an existing directory"
        )
    return resolved


def stage_target_repo_into(staging_root, target_repo_path, input_payload=None):
    """Copy the target reference repo under `staging_root/__target_repo__/` so
    backends that only see one working directory (e.g. codex) can read it.
    Returns the staged absolute path, or "" when no target repo is given."""
    if not target_repo_path:
        return ""
    destination = os.path.join(staging_root, "__target_repo__")
    copy_tree(
        target_repo_path,
        destination,
        input_payload=input_payload,
        ignore_base_dir=target_repo_path,
    )
    return destination


def generate_plan_backend_context(
    request, input_payload, repo_root, effective_change_name
):
    mode = repo_context_mode(input_payload)
    if mode == "full":
        return repo_root, [repo_root]
    if mode == "paths":
        staged_root = stage_repo_context(
            request,
            input_payload,
            effective_change_name,
            repo_root,
            resolve_repo_context_paths(input_payload, repo_root),
        )
        return staged_root, [staged_root]
    # Keep the empty context within the declared artifact directory. The
    # runtime mounts this path for process plugins, unlike private run staging.
    isolated_root = os.path.join(
        request["paths"]["artifacts_dir"],
        ".openspec_context",
        effective_change_name,
        "__empty__",
    )
    ensure_empty_directory(isolated_root)
    return isolated_root, []


def approved_plan_mode(input_payload):
    raw_mode = str(
        (input_payload or {}).get("approved_plan_mode")
        or APPROVED_PLAN_MODE_OPENSPEC_CHANGE
    ).strip()
    mode = raw_mode or APPROVED_PLAN_MODE_OPENSPEC_CHANGE
    if mode not in {
        APPROVED_PLAN_MODE_OPENSPEC_CHANGE,
        APPROVED_PLAN_MODE_PROSE_BUNDLE,
    }:
        raise ValueError(
            f"approved_plan_mode must be one of '{APPROVED_PLAN_MODE_OPENSPEC_CHANGE}'"
            f" or '{APPROVED_PLAN_MODE_PROSE_BUNDLE}'"
        )
    return mode


def load_template(name):
    path = os.path.join(os.path.dirname(__file__), "prompts", name)
    return read_text(path)


def render_template(name, task_text, change_name):
    template = load_template(name)
    return template.replace("{{TASK}}", task_text.strip()).replace(
        "{{CHANGE_NAME}}", change_name
    )


def compose_generate_prompt(
    request,
    input_payload,
    existing_change_dir,
    canonical_plan_dir,
    base_prompt,
    target_repo_path="",
):
    sections = PromptSections(request, input_payload)
    skills_rendered = input_payload.get("skills_rendered")
    if isinstance(skills_rendered, str) and skills_rendered.strip():
        sections.append("# Workflow skills\n\n" + skills_rendered.strip())
    ignored_paths = ignore_paths_section(input_payload)
    if ignored_paths:
        sections.append(ignored_paths)
    if canonical_plan_dir:
        sections.append(
            "# Canonical plan bundle\n\nExport the approved development plan so it"
            f" cleanly fits this canonical folder:\n`{canonical_plan_dir}`\n\nThe"
            " bundle is expected to contain `README.md`, `proposal.md`, `tasks.md`,"
            " `design.md`, and one or more `specs/<capability>/spec.md` files. Keep"
            " `design.md` concise when the change is simple, but always include it."
        )
    if target_repo_path:
        sections.append(
            "# Target reference repository\n\nA target reference repository has been"
            f" made available to you read-only at:\n`{target_repo_path}`\n\nThis is the"
            " codebase the development plan is being authored against. Consult its"
            " source, configuration, and test layout to ground concrete decisions:"
            " file paths, module boundaries, existing conventions, public interfaces,"
            " and verification approach. Prefer references to actual files in the"
            " target repo over generic guidance. Do not modify it; treat it as"
            " read-only context."
        )
    decision, reason, attribution = gate_decision_feedback(input_payload)
    redo = reason.startswith("redo:")
    mode = feedback_context_mode(input_payload)
    # An iteration is any prior request_changes decision on the configured
    # gate, regardless of whether the operator supplied a reason. The
    # current draft and existing OpenSpec files must be included on every
    # iteration so the model revises (not regenerates) when PR comments or
    # other context_files carry the actionable feedback instead of `reason`.
    is_iteration = decision == "request_changes"
    if is_iteration:
        feedback = "Decision: request_changes"
        if attribution:
            feedback += f" by {attribution}"
        if reason:
            feedback += "\n\n" + reason
        sections.append("# Operator feedback\n\n" + feedback)
    if is_iteration and not redo and mode in {"full", "focused"}:
        response_file = str(input_payload.get("response_file") or "").strip()
        if response_file:
            path = workspace_path(request, response_file, input_payload)
            try:
                contents = sections.read(path, "current_draft")
            except OSError:
                contents = ""
            if contents:
                sections.append(
                    f"# Current draft: {os.path.basename(path)}\n\n{contents}"
                )
    if is_iteration and not redo and mode == "full":
        for relative_path, contents in collect_change_file_sections(
            existing_change_dir,
            sections,
            allow_missing=bool(canonical_plan_dir),
            label="existing_change_files",
        ):
            sections.append(f"# Existing OpenSpec file: {relative_path}\n\n{contents}")
        if canonical_plan_dir and os.path.normpath(
            canonical_plan_dir
        ) != os.path.normpath(existing_change_dir):
            for relative_path, contents in collect_change_file_sections(
                canonical_plan_dir,
                sections,
                label="existing_plan_files",
            ):
                sections.append(f"# Existing plan file: {relative_path}\n\n{contents}")
    for label, contents in collect_context_files(request, input_payload, sections):
        sections.append(f"# Context: {label}\n\n{contents}")
    style_block = detail_level_style_block(input_payload)
    if style_block:
        sections.append(style_block)
    sections.append("# Task\n\n" + base_prompt.strip())
    return sections.finish(
        render_template(
            "generate_plan.md",
            "\n\n".join(section for section in sections if section.strip()),
            os.path.basename(existing_change_dir),
        )
    )


def compose_verify_prompt(
    request, input_payload, approved_change_dir, approved_plan_dir, base_prompt
):
    sections = PromptSections(request, input_payload)
    skills_rendered = input_payload.get("skills_rendered")
    plan_mode = approved_plan_mode(input_payload)
    if isinstance(skills_rendered, str) and skills_rendered.strip():
        sections.append("# Workflow skills\n\n" + skills_rendered.strip())
    ignored_paths = ignore_paths_section(input_payload)
    if ignored_paths:
        sections.append(ignored_paths)
    if approved_plan_dir:
        sections.append(
            "# Approved plan bundle\n\n"
            "Use the approved cross-repository plan bundle from:\n"
            f"`{approved_plan_dir}`"
        )
    if plan_mode == APPROVED_PLAN_MODE_PROSE_BUNDLE:
        sections.append(
            "# Approved plan interpretation\n\nTreat the approved bundle as prose"
            " planning artifacts rather than a strict OpenSpec delta tree. Verify the"
            " implementation against the bundle's requirements, tasks, design notes,"
            " and verification guidance."
        )
    approved_file_label = (
        "Approved plan file"
        if plan_mode == APPROVED_PLAN_MODE_PROSE_BUNDLE
        else "Approved OpenSpec file"
    )
    for relative_path, contents in collect_change_file_sections(
        approved_change_dir,
        sections,
        label="approved_plan_files",
    ):
        sections.append(f"# {approved_file_label}: {relative_path}\n\n{contents}")
    for label, contents in collect_context_files(request, input_payload, sections):
        sections.append(f"# Context: {label}\n\n{contents}")
    style_block = detail_level_style_block(input_payload)
    if style_block:
        sections.append(style_block)
    sections.append("# Task\n\n" + base_prompt.strip())
    return sections.finish(
        render_template(
            "verify.md",
            "\n\n".join(section for section in sections if section.strip()),
            os.path.basename(approved_change_dir),
        )
    )


def parse_json_object(raw_text):
    text = str(raw_text or "").strip()
    if not text:
        raise ValueError("backend returned an empty response")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    if not text:
        raise ValueError("backend returned an empty response")
    candidate = select_json_object_candidate(text)
    decoder = json.JSONDecoder()
    try:
        payload, end = decoder.raw_decode(candidate)
    except json.JSONDecodeError as err:
        repaired_candidates = [
            repair_truncated_json(candidate),
            repair_unescaped_string_quotes(candidate),
        ]
        repaired_quotes = repaired_candidates[-1]
        if repaired_quotes:
            repaired_candidates.append(repair_truncated_json(repaired_quotes))
        for repaired_candidate in repaired_candidates:
            if not repaired_candidate:
                continue
            try:
                payload, end = decoder.raw_decode(repaired_candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and not repaired_candidate[end:].strip():
                return payload
        raise ValueError(
            f"backend returned malformed top-level JSON: {err.msg}"
        ) from err
    if not isinstance(payload, dict):
        raise ValueError("backend did not return a top-level JSON object")
    return payload


def select_json_object_candidate(text):
    if text.startswith("{"):
        return text
    preferred_match = re.search(r'\{\s*"(?:summary_markdown|passed)"', text)
    if preferred_match:
        return text[preferred_match.start() :]
    first_brace = text.find("{")
    if first_brace == -1:
        raise ValueError("backend did not return a JSON object")
    return text[first_brace:]


def repair_truncated_json(candidate):
    stack = []
    in_string = False
    escape = False
    pairs = {"{": "}", "[": "]"}
    closers = {"}": "{", "]": "["}

    for char in candidate:
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char in pairs:
            stack.append(char)
            continue
        if char in closers:
            if not stack or stack[-1] != closers[char]:
                return ""
            stack.pop()

    if in_string or escape or not stack:
        return ""
    return candidate + "".join(pairs[char] for char in reversed(stack))


def repair_unescaped_string_quotes(candidate):
    repaired = []
    in_string = False
    escape = False
    length = len(candidate)
    index = 0

    while index < length:
        char = candidate[index]
        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
                escape = False
            index += 1
            continue

        if escape:
            repaired.append(char)
            escape = False
            index += 1
            continue

        if char == "\\":
            repaired.append(char)
            escape = True
            index += 1
            continue

        if char == '"':
            lookahead = index + 1
            while lookahead < length and candidate[lookahead] in " \t\r\n":
                lookahead += 1
            next_char = candidate[lookahead] if lookahead < length else ""
            if next_char and next_char not in ",}]:":
                repaired.append('\\"')
                index += 1
                continue
            repaired.append(char)
            in_string = False
            index += 1
            continue

        repaired.append(char)
        index += 1

    if in_string or escape:
        return ""
    repaired_text = "".join(repaired)
    if repaired_text == candidate:
        return ""
    return repaired_text


def require_string(payload, field, allow_empty=False):
    value = payload.get(field)
    if not isinstance(value, str):
        raise ValueError(f"backend response field '{field}' must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"backend response field '{field}' must not be empty")
    return value


def require_bool(payload, field):
    value = payload.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"backend response field '{field}' must be a boolean")
    return value


def require_array(payload, field, min_items=0):
    value = payload.get(field)
    if not isinstance(value, list):
        raise ValueError(f"backend response field '{field}' must be an array")
    if len(value) < min_items:
        qualifier = " item" if min_items == 1 else " items"
        raise ValueError(
            f"backend response field '{field}' must contain at least"
            f" {min_items}{qualifier}"
        )
    return value


def validate_spec_markdown(capability, spec_markdown):
    normalized = str(spec_markdown or "").strip()
    if not normalized:
        raise ValueError(
            f"backend response spec for capability '{capability}' must not be empty"
        )

    missing = []
    if not SPEC_DELTA_HEADER_RE.search(normalized):
        missing.append("a '## ADDED|MODIFIED|REMOVED|RENAMED Requirements' section")
    if not SPEC_REQUIREMENT_HEADER_RE.search(normalized):
        missing.append("a '### Requirement:' heading")
    if not SPEC_SCENARIO_HEADER_RE.search(normalized):
        missing.append("a '#### Scenario:' heading")
    if missing:
        raise ValueError(
            f"backend response spec for capability '{capability}' must include "
            + ", ".join(missing)
        )
    return normalized


def render_structured_spec_markdown(capability, entry):
    deltas = require_array(entry, "deltas", min_items=1)
    rendered_sections = ["# Spec Delta"]

    for delta_index, delta in enumerate(deltas, start=1):
        if not isinstance(delta, dict):
            raise ValueError(
                f"backend response spec for capability '{capability}' contains a"
                f" non-object delta at index {delta_index - 1}"
            )
        delta_type = require_string(delta, "type").strip().upper()
        if delta_type not in ALLOWED_DELTA_TYPES:
            allowed = ", ".join(ALLOWED_DELTA_TYPES)
            raise ValueError(
                f"backend response spec for capability '{capability}' has unsupported"
                f" delta type '{delta_type}'; expected one of {allowed}"
            )
        requirements = require_array(delta, "requirements", min_items=1)
        rendered_sections.extend(["", f"## {delta_type} Requirements"])

        for requirement_index, requirement in enumerate(requirements, start=1):
            if not isinstance(requirement, dict):
                raise ValueError(
                    f"backend response spec for capability '{capability}' contains a"
                    f" non-object requirement at delta index {delta_index - 1},"
                    f" requirement index {requirement_index - 1}"
                )
            title = require_string(requirement, "title").strip()
            body_markdown = require_string(requirement, "body_markdown").strip()
            scenarios = require_array(requirement, "scenarios", min_items=1)
            rendered_sections.extend(
                ["", f"### Requirement: {title}", "", body_markdown]
            )

            for scenario_index, scenario in enumerate(scenarios, start=1):
                if not isinstance(scenario, dict):
                    raise ValueError(
                        f"backend response spec for capability '{capability}' contains"
                        f" a non-object scenario at delta index {delta_index - 1},"
                        f" requirement index {requirement_index - 1}, scenario index"
                        f" {scenario_index - 1}"
                    )
                scenario_title = require_string(scenario, "title").strip()
                scenario_body = require_string(scenario, "body_markdown").strip()
                rendered_sections.extend(
                    ["", f"#### Scenario: {scenario_title}", "", scenario_body]
                )

    return validate_spec_markdown(capability, "\n".join(rendered_sections))


def generate_backend_response(
    request,
    input_payload,
    working_dir,
    prompt_text,
    repo_context_dirs=None,
    target_repo_path="",
):
    backend = (
        str(input_payload.get("backend") or BACKEND_GH_COPILOT).strip()
        or BACKEND_GH_COPILOT
    )
    timeout_seconds = command_timeout_seconds(input_payload)
    repo_context_dirs = [path for path in (repo_context_dirs or []) if path]
    session_snapshot = {}
    started_at_epoch = time.time()
    if backend == BACKEND_GH_COPILOT:
        emit_status_preview(
            request["invoke_id"], "Generating OpenSpec artifacts with GitHub Copilot..."
        )
        command = [
            "gh",
            "copilot",
            "--",
            "-p",
            prompt_text,
            "--no-ask-user",
            "--silent",
            "--no-color",
            "--output-format",
            "text",
        ]
        for repo_context_dir in repo_context_dirs:
            command.extend(["--add-dir", repo_context_dir])
        # Target reference repo: exposed as an additional read-only directory
        # without copying. Distinct from planning-repo context.
        if target_repo_path:
            command.extend(["--add-dir", target_repo_path])
        if input_payload.get("model"):
            command.extend(["--model", str(input_payload["model"])])
        if input_payload.get("reasoning_effort"):
            command.extend(
                ["--reasoning-effort", str(input_payload["reasoning_effort"])]
            )
        child_copilot_dir = os.environ["COPILOT_HOME"]
        child_session_state_dir = os.environ["COPILOT_SESSION_STATE_DIR"]
        session_snapshot = snapshot_copilot_sessions(child_session_state_dir)
        completed = run_command(
            command,
            working_dir,
            timeout_seconds,
            request=request,
            env_overrides={
                "COPILOT_HOME": child_copilot_dir,
                "COPILOT_CONFIG_DIR": child_copilot_dir,
                "XDG_CACHE_HOME": os.environ["XDG_CACHE_HOME"],
                "TMPDIR": os.environ["TMPDIR"],
                "COPILOT_SESSION_STATE_DIR": child_session_state_dir,
            },
        )
        usage = extract_backend_usage(
            backend,
            working_dir,
            started_at_epoch,
            session_snapshot,
            completed,
            session_state_dir=child_session_state_dir,
        )
        return backend, completed, completed.stdout.strip(), usage
    if backend == BACKEND_CODEX:
        emit_status_preview(
            request["invoke_id"], "Generating OpenSpec artifacts with Codex..."
        )
        # Codex sees only its working_dir under --sandbox read-only. To make the
        # target reference repo visible, stage a copy of it inside working_dir.
        # Only safe when working_dir is a transient staging root (not the
        # planning repo itself), so disallow the unsafe combination.
        if target_repo_path:
            if repo_context_mode(input_payload) == "full":
                raise ValueError(
                    "target_repo_path with backend=codex requires repo_context_mode of"
                    " 'none' or 'paths'; it cannot be combined with"
                    " repo_context_mode=full because that would write into the planning"
                    " repo working tree"
                )
            stage_target_repo_into(
                working_dir, target_repo_path, input_payload=input_payload
            )
        message_path = os.path.join(
            request["paths"]["artifacts_dir"], "codex_last_message.txt"
        )
        command = [
            "codex",
            "exec",
            prompt_text,
            "--cd",
            working_dir,
            "--output-last-message",
            message_path,
            "--color",
            "never",
            "--sandbox",
            "read-only",
        ]
        if input_payload.get("model"):
            command.extend(["--model", str(input_payload["model"])])
        if input_payload.get("reasoning_effort"):
            command.extend(
                ["-c", f'model_reasoning_effort={input_payload["reasoning_effort"]}']
            )
        completed = run_command(command, working_dir, timeout_seconds, request=request)
        if os.path.exists(message_path):
            response_text = read_text(message_path)
        else:
            response_text = completed.stdout.strip()
        usage = extract_backend_usage(
            backend, working_dir, started_at_epoch, session_snapshot, completed
        )
        return backend, completed, response_text, usage
    if backend == BACKEND_CURSOR:
        emit_status_preview(
            request["invoke_id"], "Generating OpenSpec artifacts with Cursor Agent..."
        )
        # Cursor Agent reads its working tree via --workspace and does not
        # support --add-dir. Stage target_repo_path inside working_dir so it
        # is visible through the workspace. Only safe when working_dir is a
        # transient staging root, so disallow the unsafe combination.
        if target_repo_path:
            if repo_context_mode(input_payload) == "full":
                raise ValueError(
                    "target_repo_path with backend=cursor requires repo_context_mode of"
                    " 'none' or 'paths'; it cannot be combined with"
                    " repo_context_mode=full because that would write into the planning"
                    " repo working tree"
                )
            stage_target_repo_into(
                working_dir, target_repo_path, input_payload=input_payload
            )
        command = [
            "cursor-agent",
            "--print",
            "--output-format",
            "text",
            "--workspace",
            working_dir,
            "--force",
        ]
        if input_payload.get("model"):
            command.extend(["--model", str(input_payload["model"])])
        command.append(prompt_text)
        completed = run_command(command, working_dir, timeout_seconds, request=request)
        return backend, completed, completed.stdout.strip(), None
    raise ValueError(f"unsupported backend '{backend}'")


def run_openspec_validate(request, input_payload, repo_root, effective_change_name):
    emit_status_preview(
        request["invoke_id"], f"Running openspec validate {effective_change_name}..."
    )
    return run_command(
        ["openspec", "validate", effective_change_name],
        repo_root,
        command_timeout_seconds(input_payload),
        request=request,
    )


def unique_capability_slug(raw_capability, used):
    base = sanitize_slug(raw_capability)
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def write_change_files(target_change_dir, payload):
    if os.path.exists(target_change_dir):
        shutil.rmtree(target_change_dir)
    os.makedirs(target_change_dir, exist_ok=True)

    proposal_markdown = require_string(payload, "proposal_markdown")
    tasks_markdown = require_string(payload, "tasks_markdown")
    design_markdown = require_string(payload, "design_markdown")
    specs = require_array(payload, "specs", min_items=1)

    proposal_file = os.path.join(target_change_dir, "proposal.md")
    tasks_file = os.path.join(target_change_dir, "tasks.md")
    design_file = os.path.join(target_change_dir, "design.md")
    write_text(proposal_file, proposal_markdown)
    write_text(tasks_file, tasks_markdown)
    write_text(design_file, design_markdown)

    spec_files = []
    used_capabilities = set()
    for entry in specs:
        if not isinstance(entry, dict):
            raise ValueError("every specs entry must be an object")
        capability = unique_capability_slug(
            require_string(entry, "capability"), used_capabilities
        )
        legacy_spec_markdown = entry.get("spec_markdown")
        if isinstance(legacy_spec_markdown, str) and legacy_spec_markdown.strip():
            spec_markdown = validate_spec_markdown(capability, legacy_spec_markdown)
        else:
            spec_markdown = render_structured_spec_markdown(capability, entry)
        spec_file = os.path.join(target_change_dir, "specs", capability, "spec.md")
        write_text(spec_file, spec_markdown)
        spec_files.append(spec_file)

    return target_change_dir, proposal_file, tasks_file, design_file, spec_files


def export_canonical_plan_bundle(
    target_change_dir, canonical_plan_dir, summary_markdown
):
    if os.path.exists(canonical_plan_dir):
        shutil.rmtree(canonical_plan_dir)
    os.makedirs(canonical_plan_dir, exist_ok=True)

    readme_file = os.path.join(canonical_plan_dir, "README.md")
    write_text(readme_file, summary_markdown)

    for filename in ("proposal.md", "tasks.md", "design.md"):
        source = os.path.join(target_change_dir, filename)
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(canonical_plan_dir, filename))

    source_specs_dir = os.path.join(target_change_dir, "specs")
    if os.path.isdir(source_specs_dir):
        copy_tree(source_specs_dir, os.path.join(canonical_plan_dir, "specs"))

    return readme_file


def failure_artifacts(stdout_text, stderr_text, extra=None, request=None):
    artifact_hints = [
        artifact_hint(
            "stdout.txt",
            stdout_text.encode("utf-8")[:MAX_HINT_BYTES].decode(
                "utf-8", errors="ignore"
            ),
        ),
        artifact_hint(
            "stderr.txt",
            stderr_text.encode("utf-8")[:MAX_HINT_BYTES].decode(
                "utf-8", errors="ignore"
            ),
        ),
    ]
    for name, contents in extra or []:
        artifact_hints.append(
            payload_hint(request, name, contents, artifact_hint)
            if request is not None
            else artifact_hint(name, contents)
        )
    return artifact_hints


def build_generate_output(
    backend,
    effective_change_name,
    target_change_dir,
    proposal_file,
    tasks_file,
    design_file,
    spec_files,
    response_text,
    response_file,
    change_metadata_file,
    canonical_plan_dir,
    canonical_readme_file,
    stdout_text,
    stderr_text,
    exit_code,
):
    return {
        "ok": exit_code == 0,
        "backend": backend,
        "effective_change_name": effective_change_name,
        "change_dir": target_change_dir,
        "proposal_file": proposal_file,
        "tasks_file": tasks_file,
        "design_file": design_file,
        "spec_files": spec_files,
        "response_text": response_text,
        "response_file": response_file,
        "change_metadata_file": change_metadata_file,
        "canonical_plan_dir": canonical_plan_dir,
        "canonical_readme_file": canonical_readme_file,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "exit_code": exit_code,
    }


def build_verify_output(
    backend,
    effective_change_name,
    report_text,
    report_file,
    findings_count,
    passed,
    approved_plan_dir,
    validation_change_dir,
    stdout_text,
    stderr_text,
    exit_code,
):
    return {
        "ok": exit_code == 0 and passed,
        "passed": passed,
        "backend": backend,
        "effective_change_name": effective_change_name,
        "report_text": report_text,
        "report_file": report_file,
        "findings_count": findings_count,
        "approved_plan_dir": approved_plan_dir,
        "validation_change_dir": validation_change_dir,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "exit_code": exit_code,
    }


def handle_generate_plan(request):
    input_payload = request.get("input") or {}
    repo_root = request["paths"]["workspace_dir"]
    target_repo_path = resolve_target_repo_path(input_payload)
    base_prompt = resolve_prompt_input(input_payload, request)
    os.makedirs(request["paths"]["artifacts_dir"], exist_ok=True)
    persist_skills_artifacts(request["paths"]["artifacts_dir"], input_payload)
    effective_change_name = sanitize_slug(input_payload.get("change_name"))
    canonical_plan_dir = resolve_optional_path(
        request,
        input_payload.get("canonical_plan_dir"),
        input_payload,
        "canonical_plan_dir",
    )
    if canonical_plan_dir:
        ensure_not_ignored_path(
            input_payload, canonical_plan_dir, "canonical_plan_dir", repo_root
        )
    if canonical_plan_dir:
        target_change_dir = transient_change_dir(
            request, "generate_plan", effective_change_name
        )
        validation_repo_root = transient_repo_root(
            request, "generate_plan", effective_change_name
        )
    else:
        ensure_openspec_initialized(repo_root)
        target_change_dir = change_dir(repo_root, effective_change_name)
        validation_repo_root = repo_root
    prompt_text = compose_generate_prompt(
        request,
        input_payload,
        target_change_dir,
        canonical_plan_dir,
        base_prompt,
        target_repo_path=target_repo_path,
    )
    backend_working_dir, repo_context_dirs = generate_plan_backend_context(
        request,
        input_payload,
        repo_root,
        effective_change_name,
    )

    backend, backend_result, response_text, backend_usage = generate_backend_response(
        request,
        input_payload,
        backend_working_dir,
        prompt_text,
        repo_context_dirs=repo_context_dirs,
        target_repo_path=target_repo_path,
    )
    if backend_result.returncode != 0:
        output = build_generate_output(
            backend,
            effective_change_name,
            target_change_dir,
            "",
            "",
            "",
            [],
            "",
            (
                workspace_path(
                    request, input_payload.get("response_file"), input_payload
                )
                if input_payload.get("response_file")
                else ""
            ),
            (
                workspace_path(
                    request, input_payload.get("change_metadata_file"), input_payload
                )
                if input_payload.get("change_metadata_file")
                else ""
            ),
            canonical_plan_dir,
            "",
            backend_result.stdout,
            backend_result.stderr,
            backend_result.returncode,
        )
        return failed_result(
            request["invoke_id"],
            "tool_execution_error",
            "tool",
            f"backend command exited with code {backend_result.returncode}",
            "safe",
            output=output,
            artifact_hints=failure_artifacts(
                backend_result.stdout, backend_result.stderr
            ),
            usage=backend_usage,
        )

    try:
        parsed = parse_json_object(response_text)
        summary_markdown = require_string(parsed, "summary_markdown")
        target_change_dir, proposal_file, tasks_file, design_file, spec_files = (
            write_change_files(
                target_change_dir,
                parsed,
            )
        )
    except (OSError, ValueError) as err:
        output = build_generate_output(
            backend,
            effective_change_name,
            target_change_dir,
            "",
            "",
            "",
            [],
            "",
            (
                workspace_path(
                    request, input_payload.get("response_file"), input_payload
                )
                if input_payload.get("response_file")
                else ""
            ),
            (
                workspace_path(
                    request, input_payload.get("change_metadata_file"), input_payload
                )
                if input_payload.get("change_metadata_file")
                else ""
            ),
            canonical_plan_dir,
            "",
            backend_result.stdout,
            backend_result.stderr,
            backend_result.returncode,
        )
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            str(err),
            "safe",
            output=output,
            artifact_hints=failure_artifacts(
                backend_result.stdout,
                backend_result.stderr,
                [("response.txt", response_text)],
                request=request,
            ),
            usage=backend_usage,
        )

    canonical_readme_file = ""
    if canonical_plan_dir:
        canonical_readme_file = export_canonical_plan_bundle(
            target_change_dir, canonical_plan_dir, summary_markdown
        )

    validate_result = run_openspec_validate(
        request, input_payload, validation_repo_root, effective_change_name
    )

    response_file = ""
    if input_payload.get("response_file"):
        response_file = workspace_path(
            request, input_payload["response_file"], input_payload
        )
        ensure_not_ignored_path(
            input_payload, response_file, "response_file", repo_root
        )
        write_text(response_file, summary_markdown)

    change_metadata_file = ""
    metadata_payload = {
        "plugin_id": PLUGIN_ID,
        "plugin_version": PLUGIN_VERSION,
        "backend": backend,
        "effective_change_name": effective_change_name,
        "change_dir": target_change_dir,
        "proposal_file": proposal_file,
        "tasks_file": tasks_file,
        "design_file": design_file,
        "spec_files": spec_files,
        "canonical_plan_dir": canonical_plan_dir,
        "canonical_readme_file": canonical_readme_file,
    }
    if input_payload.get("change_metadata_file"):
        change_metadata_file = workspace_path(
            request, input_payload["change_metadata_file"], input_payload
        )
        ensure_not_ignored_path(
            input_payload, change_metadata_file, "change_metadata_file", repo_root
        )
        write_json(change_metadata_file, metadata_payload)

    stdout_text = backend_result.stdout
    if validate_result.stdout:
        stdout_text = (
            stdout_text + ("\n" if stdout_text else "") + validate_result.stdout
        )
    stderr_text = backend_result.stderr
    if validate_result.stderr:
        stderr_text = (
            stderr_text + ("\n" if stderr_text else "") + validate_result.stderr
        )
    exit_code = (
        validate_result.returncode
        if validate_result.returncode != 0
        else backend_result.returncode
    )

    output = build_generate_output(
        backend,
        effective_change_name,
        target_change_dir,
        proposal_file,
        tasks_file,
        design_file,
        spec_files,
        summary_markdown,
        response_file,
        change_metadata_file,
        canonical_plan_dir,
        canonical_readme_file,
        stdout_text,
        stderr_text,
        exit_code,
    )
    artifact_hints = [
        payload_hint(request, "response.txt", response_text, artifact_hint),
        payload_hint(request, "proposal.md", read_text(proposal_file), artifact_hint),
        payload_hint(request, "tasks.md", read_text(tasks_file), artifact_hint),
    ]
    if backend_usage:
        artifact_hints.append(
            artifact_hint(
                "usage.json", json.dumps(backend_usage, indent=2, sort_keys=True)
            )
        )
    if response_file:
        artifact_hints.append(
            payload_hint(
                request,
                f"summary/{os.path.basename(response_file)}",
                summary_markdown,
                artifact_hint,
            )
        )
    if change_metadata_file:
        artifact_hints.append(
            payload_hint(
                request,
                f"metadata/{os.path.basename(change_metadata_file)}",
                json.dumps(metadata_payload, indent=2, sort_keys=True),
                artifact_hint,
            )
        )
    artifact_hints.append(
        payload_hint(
            request,
            os.path.basename(design_file),
            read_text(design_file),
            artifact_hint,
        )
    )
    for spec_file in spec_files:
        artifact_hints.append(
            payload_hint(
                request,
                os.path.relpath(spec_file, target_change_dir),
                read_text(spec_file),
                artifact_hint,
            )
        )
    if canonical_plan_dir:
        for relative_path, contents in collect_change_file_sections(canonical_plan_dir):
            artifact_hints.append(
                payload_hint(
                    request, f"canonical/{relative_path}", contents, artifact_hint
                )
            )

    if validate_result.returncode != 0:
        return failed_result(
            request["invoke_id"],
            "tool_execution_error",
            "tool",
            f"openspec validate exited with code {validate_result.returncode}",
            "safe",
            output=output,
            artifact_hints=artifact_hints,
            usage=backend_usage,
        )

    return build_result(
        request["invoke_id"],
        "success",
        output,
        artifact_hints=artifact_hints,
        usage=backend_usage,
    )


def validate_verify_payload(parsed):
    passed = require_bool(parsed, "passed")
    report_markdown = require_string(parsed, "report_markdown")
    findings = parsed.get("findings")
    if not isinstance(findings, list):
        findings = []
    return passed, report_markdown, findings


def render_validation_failure_report(title, details):
    return (
        "# Validation Result\n\n"
        "## Verdict\n\n"
        "FAIL\n\n"
        "## Findings\n\n"
        f"- {title}\n\n"
        "## Evidence\n\n"
        "```text\n"
        f"{details.strip()}\n"
        "```\n\n"
        "## Next Steps\n\n"
        "- Fix the issue and rerun validation.\n"
    )


def handle_verify(request):
    input_payload = request.get("input") or {}
    repo_root = request["paths"]["workspace_dir"]
    base_prompt = resolve_prompt_input(input_payload, request)
    os.makedirs(request["paths"]["artifacts_dir"], exist_ok=True)
    persist_skills_artifacts(request["paths"]["artifacts_dir"], input_payload)
    effective_change_name = sanitize_slug(input_payload.get("change_name"))
    approved_plan_dir = resolve_optional_path(
        request,
        input_payload.get("approved_plan_dir"),
        input_payload,
        "approved_plan_dir",
    )
    if approved_plan_dir:
        ensure_not_ignored_path(
            input_payload, approved_plan_dir, "approved_plan_dir", repo_root
        )
    plan_mode = approved_plan_mode(input_payload)
    contributor_budget = PromptSections(request, input_payload)
    if approved_plan_dir:
        contributor_budget.directory(approved_plan_dir, "approved_plan_files")
        approved_change_dir = ensure_directory(approved_plan_dir, "approved_plan_dir")
        validation_repo_root = transient_repo_root(
            request, "verify", effective_change_name
        )
        target_change_dir = transient_change_dir(
            request, "verify", effective_change_name
        )
        try:
            copy_tree(approved_change_dir, target_change_dir)
        except OSError:
            if contributor_budget.strict:
                contributor_budget.fail_read(
                    ContributorReadError("approved_plan_files", "unavailable")
                )
            raise
    else:
        ensure_openspec_initialized(repo_root)
        target_change_dir = change_dir(repo_root, effective_change_name)
        contributor_budget.directory(target_change_dir, "approved_plan_files")
        if not os.path.isdir(target_change_dir):
            raise ValueError(f"missing OpenSpec change directory '{target_change_dir}'")
        approved_change_dir = target_change_dir
        validation_repo_root = repo_root

    prompt_text = compose_verify_prompt(
        request, input_payload, approved_change_dir, approved_plan_dir, base_prompt
    )
    validate_result = None
    if not (approved_plan_dir and plan_mode == APPROVED_PLAN_MODE_PROSE_BUNDLE):
        validate_result = run_openspec_validate(
            request, input_payload, validation_repo_root, effective_change_name
        )
    report_file = ""
    if input_payload.get("report_file"):
        report_file = workspace_path(
            request, input_payload["report_file"], input_payload
        )
        ensure_not_ignored_path(input_payload, report_file, "report_file", repo_root)
    backend = (
        str(input_payload.get("backend") or BACKEND_GH_COPILOT).strip()
        or BACKEND_GH_COPILOT
    )

    if validate_result is not None and validate_result.returncode != 0:
        report_text = render_validation_failure_report(
            f"OpenSpec validation failed for {effective_change_name}.",
            (validate_result.stdout or "")
            + ("\n" if validate_result.stdout and validate_result.stderr else "")
            + (validate_result.stderr or ""),
        )
        if report_file:
            write_text(report_file, report_text)
        output = build_verify_output(
            backend,
            effective_change_name,
            report_text,
            report_file,
            1,
            False,
            approved_plan_dir,
            target_change_dir,
            validate_result.stdout,
            validate_result.stderr,
            validate_result.returncode,
        )
        artifact_hints = failure_artifacts(
            validate_result.stdout,
            validate_result.stderr,
            (
                [(f"report/{os.path.basename(report_file)}", report_text)]
                if report_file
                else []
            ),
            request=request,
        )
        return failed_result(
            request["invoke_id"],
            "tool_execution_error",
            "tool",
            f"openspec validate exited with code {validate_result.returncode}",
            "safe",
            output=output,
            artifact_hints=artifact_hints,
        )

    backend, backend_result, response_text, backend_usage = generate_backend_response(
        request,
        input_payload,
        repo_root,
        prompt_text,
        repo_context_dirs=[repo_root],
    )
    if backend_result.returncode != 0:
        output = build_verify_output(
            backend,
            effective_change_name,
            "",
            report_file,
            0,
            False,
            approved_plan_dir,
            target_change_dir,
            backend_result.stdout,
            backend_result.stderr,
            backend_result.returncode,
        )
        return failed_result(
            request["invoke_id"],
            "tool_execution_error",
            "tool",
            f"backend command exited with code {backend_result.returncode}",
            "safe",
            output=output,
            artifact_hints=failure_artifacts(
                backend_result.stdout, backend_result.stderr
            ),
            usage=backend_usage,
        )

    try:
        parsed = parse_json_object(response_text)
        passed, report_text, findings = validate_verify_payload(parsed)
        request["_findings"] = findings
    except ValueError as err:
        output = build_verify_output(
            backend,
            effective_change_name,
            "",
            report_file,
            0,
            False,
            approved_plan_dir,
            target_change_dir,
            backend_result.stdout,
            backend_result.stderr,
            backend_result.returncode,
        )
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            str(err),
            "safe",
            output=output,
            artifact_hints=failure_artifacts(
                backend_result.stdout,
                backend_result.stderr,
                [("response.txt", response_text)],
                request=request,
            ),
            usage=backend_usage,
        )

    if report_file:
        write_text(report_file, report_text)
    output = build_verify_output(
        backend,
        effective_change_name,
        report_text,
        report_file,
        len(findings),
        passed,
        approved_plan_dir,
        target_change_dir,
        backend_result.stdout,
        backend_result.stderr,
        backend_result.returncode,
    )
    artifact_hints = [
        payload_hint(request, "response.txt", response_text, artifact_hint),
    ]
    if backend_usage:
        artifact_hints.append(
            artifact_hint(
                "usage.json", json.dumps(backend_usage, indent=2, sort_keys=True)
            )
        )
    if report_file:
        artifact_hints.append(
            payload_hint(
                request,
                f"report/{os.path.basename(report_file)}",
                report_text,
                artifact_hint,
            )
        )

    if not passed and bool(input_payload.get("fail_on_mismatch", True)):
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "tool",
            "implementation does not satisfy OpenSpec change"
            f" '{effective_change_name}'",
            "safe",
            output=output,
            artifact_hints=artifact_hints,
            usage=backend_usage,
        )

    return build_result(
        request["invoke_id"],
        "success",
        output,
        artifact_hints=artifact_hints,
        usage=backend_usage,
    )


def _handle_request(request):
    try:
        action = request.get("action")
        if action == "generate_plan":
            return handle_generate_plan(request)
        if action == "verify":
            return handle_verify(request)
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            f"unsupported action {action}",
        )
    except CleanupUnconfirmedError as err:
        if not request.get("_logs"):
            record_logs(request, "command", err.stdout or "", err.stderr or "")
        hints = []
        for name, contents in (("stdout.txt", err.stdout), ("stderr.txt", err.stderr)):
            if (
                isinstance(contents, str)
                and len(contents.encode("utf-8")) <= MAX_HINT_BYTES
            ):
                hints.append(artifact_hint(name, contents))
        return failed_result(
            request["invoke_id"],
            "protocol_error",
            "protocol",
            "Command process cleanup could not be confirmed",
            artifact_hints=hints,
        )
    except CommandRunnerError:
        return failed_result(
            request["invoke_id"],
            "adapter_internal_error",
            "internal",
            "Command runner failed",
            "operator",
        )
    except BinaryNotFoundError as err:
        return failed_result(
            request["invoke_id"],
            "tool_execution_error",
            "tool",
            str(err),
        )
    except subprocess.TimeoutExpired as err:
        if not request.get("_logs"):
            record_logs(request, "timeout", err.stdout or "", err.stderr or "")
        return failed_result(
            request["invoke_id"],
            "timeout_error",
            "tool",
            f"command timed out after {int(err.timeout)} seconds",
            "safe",
        )
    except (OSError, ValueError) as err:
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            str(err),
            "safe",
        )


def handle_request(request: dict) -> dict:
    """Validate local controls and finalize one bounded APP result with evidence."""
    for key in ("_logs", "_produced", "_output_limit", "_findings"):
        request.pop(key, None)
    try:
        payload = request.get("input") or {}
        output_mode(payload)
        positive_limit(payload, "max_prompt_bytes")
        positive_limit(payload, "max_context_file_bytes")
        if request.get("action") == "generate_plan":
            gate_decision_feedback(payload)
    except ValueError as err:
        return failed_result(
            request["invoke_id"], "validation_error", "validation", str(err)
        )
    return finalize_result(request, _handle_request(request), artifact_hint)


def main():
    handshake = read_frame()
    if handshake is None:
        return 1
    emit(
        {
            "type": "app.handshake.response.v1",
            "protocol_version": APP_PROTOCOL,
            "plugin": {"id": PLUGIN_ID, "version": PLUGIN_VERSION},
            "adapter_kind": "process",
            "capabilities": {
                "actions": ACTIONS,
                "supports_cancel": True,
                "supports_health": False,
                "supports_artifact_hints": True,
                "supports_evidence_hints": False,
            },
        }
    )
    request = read_frame()
    if request is None:
        return 1
    emit(handle_request(request))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
