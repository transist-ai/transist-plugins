#!/usr/bin/env python3
import datetime
import fnmatch
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from process_runner import CleanupUnconfirmedError, run_process  # noqa: E402
import lean_policy  # noqa: E402
from gate_review import valid_review_request  # noqa: E402

APP_PROTOCOL = "app.v1"
PLUGIN_ID = "github.copilot.cli"
PLUGIN_VERSION = "0.1.0"
ACTIONS = ["exec_task"]
DEFAULT_TIMEOUT_SECONDS = 600
TIMEOUT_GRACE_SECONDS = 5
HEARTBEAT_INTERVAL_SECONDS = 1
APP_INVOKE_PROGRESS_TYPE = "app.invoke.progress.v1"
DEFAULT_IGNORE_PATHS = ("**/_build/**", "**/deps/*/_build/**")
TITLE_BASED_SECTION_HEADINGS = {"## what changed", "## review response"}
DIRECT_SECTION_HEADINGS = {"# problem"}
PLAN_START_RE = re.compile(r"^1\.\s+goal$")
FEEDBACK_CONTEXT_MODES = {"full", "focused", "feedback-only"}
MIN_TIMEOUT_MS = (TIMEOUT_GRACE_SECONDS + 1) * 1000


class BinaryNotFoundError(Exception):
    pass


class JsonTransportUnavailableError(Exception):
    """Signal unavailable advertised CLI transport before sending any task prompt."""


CANONICAL_ERRORS = {
    "configuration_error": {"class": "configuration", "retryability": "none"},
    "auth_error": {"class": "auth", "retryability": "operator"},
    "secret_resolution_error": {"class": "secret", "retryability": "operator"},
    "validation_error": {"class": "validation", "retryability": "none"},
    "timeout_error": {"class": "timeout", "retryability": "safe"},
    "cancelled": {"class": "cancellation", "retryability": "none"},
    "rate_limit_error": {"class": "rate_limit", "retryability": "safe"},
    "transport_error": {"class": "transport", "retryability": "safe"},
    "protocol_error": {"class": "protocol", "retryability": "none"},
    "tool_execution_error": {"class": "tool", "retryability": "safe"},
    "adapter_internal_error": {"class": "internal", "retryability": "operator"},
}


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def read_frame():
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


def artifact_hint(name, contents):
    return {"name": name, "contents": contents}


def normalize_error_code(code, message):
    normalized = str(code or "").strip()
    if normalized in CANONICAL_ERRORS:
        return normalized, str(message)
    fallback_message = (
        f"unexpected plugin error: invalid canonical error code '{normalized}'"
    )
    if message:
        fallback_message += f": {message}"
    return "adapter_internal_error", fallback_message


def protocol_error(code, message):
    normalized_code, normalized_message = normalize_error_code(code, message)
    details = CANONICAL_ERRORS[normalized_code]
    return {
        "code": normalized_code,
        "class": details["class"],
        "message": normalized_message,
        "retryability": details["retryability"],
    }


def invoke_result(
    invoke_id, status, output=None, artifact_hints=None, error=None, usage=None
):
    return {
        "type": "app.invoke.result.v1",
        "protocol_version": APP_PROTOCOL,
        "invoke_id": invoke_id,
        "status": status,
        "output": output or {},
        "artifact_hints": artifact_hints or [],
        "evidence_hints": [],
        "metrics": {},
        **(
            {
                "usage": {
                    key: value
                    for key, value in usage.items()
                    if key
                    in {
                        "provider",
                        "model",
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                    }
                    and value is not None
                }
            }
            if usage
            else {}
        ),
        **({"error": error} if error is not None else {}),
    }


def failed_result(invoke_id, code, message, output=None, artifact_hints=None):
    return invoke_result(
        invoke_id,
        "failed",
        output=output,
        artifact_hints=artifact_hints,
        error=protocol_error(code, message),
    )


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


def _copilot_config_dir(input_payload=None):
    override = str(
        os.environ.get("COPILOT_CONFIG_DIR")
        or (input_payload or {}).get("_copilot_config_dir")
        or ""
    ).strip()
    if override:
        return os.path.expanduser(override)
    extra_args = [
        str(arg).strip()
        for arg in (input_payload or {}).get("extra_args") or []
        if str(arg).strip()
    ]
    for index, arg in enumerate(extra_args):
        if arg == "--config-dir" and index + 1 < len(extra_args):
            return os.path.expanduser(extra_args[index + 1])
        if arg.startswith("--config-dir="):
            return os.path.expanduser(arg.split("=", 1)[1])
    return os.path.expanduser("~/.copilot")


def _copilot_session_state_dir(input_payload=None):
    override = str(os.environ.get("COPILOT_SESSION_STATE_DIR") or "").strip()
    if override:
        return os.path.expanduser(override)
    return os.path.join(_copilot_config_dir(input_payload), "session-state")


def snapshot_copilot_sessions(input_payload=None):
    session_state_dir = _copilot_session_state_dir(input_payload)
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


def _coerce_token_count(raw):
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


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
    repo_cwd, input_payload=None, started_at_epoch=None, session_snapshot=None
):
    session_state_dir = _copilot_session_state_dir(input_payload)
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


def progress_frame(invoke_id, stream="stdout", delta_lines=1, line_preview=None):
    payload = {
        "type": APP_INVOKE_PROGRESS_TYPE,
        "protocol_version": APP_PROTOCOL,
        "invoke_id": invoke_id,
        "stream": stream,
        "delta_lines": delta_lines,
    }
    if isinstance(line_preview, str) and line_preview:
        payload["line_preview"] = line_preview
    return payload


def emit_status_preview(invoke_id, message):
    if isinstance(message, str) and message.strip():
        emit(
            progress_frame(
                invoke_id, stream="status", delta_lines=0, line_preview=message.strip()
            )
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


def root_run_id(request):
    return str(request.get("run_id") or "").split("__", 1)[0]


def workspace_path(request, raw_path, _input_payload=None):
    return lean_policy.file_path(globals(), request, raw_path)


def command_cwd(request, input_payload):
    return request["paths"]["workspace_dir"]


def gate_decision_feedback(input_payload):
    payload = (input_payload or {}).get("gate_decision")
    if not valid_gate_decision(payload):
        if isinstance(payload, dict) and "review_request" in payload:
            raise ValueError("invalid gate_decision with review_request")
        return ""
    if payload.get("decision") != "request_changes":
        return ""
    reason = str(payload.get("reason") or "").strip()
    actor = str(payload.get("actor") or "").strip()
    actor_source = str(payload.get("actor_source") or "").strip()
    prefix = "request_changes"
    if actor:
        prefix += f" by {actor}" + (f" ({actor_source})" if actor_source else "")
    return f"{prefix}: {reason}" if reason else prefix


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


def is_ignored_path(request, input_payload, path):
    relative_path = path_relative_to_base(path, command_cwd(request, input_payload))
    return relative_path_matches_ignore(relative_path, ignore_paths(input_payload))


def ensure_not_ignored_path(request, input_payload, path, field_name):
    if is_ignored_path(request, input_payload, path):
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


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def read_text_preserving(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


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


def resolve_prompt_input(input_payload):
    if lean_policy.bounded(input_payload):
        return lean_policy.prompt_source(input_payload, preserving=True)
    prompt = input_payload.get("prompt")
    prompt_file = input_payload.get("prompt_file")
    prompt_set = isinstance(prompt, str) and bool(prompt.strip())
    prompt_file_set = isinstance(prompt_file, str) and bool(prompt_file.strip())
    if prompt_set == prompt_file_set:
        raise ValueError("exactly one of prompt or prompt_file is required")
    if prompt_set:
        return str(prompt)
    return read_text_preserving(str(prompt_file))


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


def interaction_paths(request, input_payload, response_file=""):
    target = str(response_file or "").strip()
    if not target:
        raw_response_file = str(
            (input_payload or {}).get("response_file") or ""
        ).strip()
        if raw_response_file:
            target = workspace_path(request, raw_response_file, input_payload)
    if target:
        if (
            os.path.commonpath([request["paths"]["workspace_dir"], target])
            != request["paths"]["workspace_dir"]
        ):
            target = os.path.join(request["paths"]["artifacts_dir"], "copilot_response")
        base, extension = os.path.splitext(target)
        if not extension:
            base = target
    else:
        base = os.path.join(request["paths"]["artifacts_dir"], "copilot_response")
    return base + ".interaction.json", base + ".interaction.md"


def load_interaction_entries(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [entry for entry in payload if isinstance(entry, dict)]


def current_run_interaction_entries(request, entries):
    current_root_run_id = root_run_id(request)
    scoped_entries = [
        entry
        for entry in entries
        if str(entry.get("root_run_id") or "").strip() == current_root_run_id
    ]
    if scoped_entries:
        return scoped_entries
    return [
        entry for entry in entries if not str(entry.get("root_run_id") or "").strip()
    ]


def render_interaction_context(entries):
    sections = []
    for index, entry in enumerate(entries, start=1):
        parts = [f"## Execution {index}"]
        feedback = str(entry.get("feedback") or "").strip()
        if feedback:
            parts.append("### Operator feedback\n\n" + feedback)
        task_prompt = str(entry.get("task_prompt") or "").strip()
        if task_prompt:
            parts.append("### Task prompt\n\n" + task_prompt)
        response_text = str(entry.get("response_text") or "").strip()
        if response_text:
            parts.append("### Copilot response\n\n" + response_text)
        result_status = str(entry.get("status") or "").strip()
        exit_code = entry.get("exit_code")
        if result_status or exit_code is not None:
            result_line = result_status or "unknown"
            if exit_code is not None:
                result_line = f"{result_line} (exit_code {exit_code})"
            parts.append("### Result\n\n" + result_line)
        sections.append("\n\n".join(part for part in parts if part.strip()))
    return "\n\n".join(section for section in sections if section.strip())


def render_interaction_history(entries):
    sections = ["# Copilot interaction history"]
    for index, entry in enumerate(entries, start=1):
        parts = [f"## Execution {index}"]
        feedback = str(entry.get("feedback") or "").strip()
        if feedback:
            parts.append("### Operator feedback\n\n" + feedback)
        task_prompt = str(entry.get("task_prompt") or "").strip()
        if task_prompt:
            parts.append("### Task prompt\n\n" + task_prompt)
        composed_prompt = str(entry.get("composed_prompt") or "").strip()
        if composed_prompt:
            parts.append("### Prompt sent to Copilot\n\n" + composed_prompt)
        response_text = str(entry.get("response_text") or "").strip()
        if response_text:
            parts.append("### Copilot response\n\n" + response_text)
        stdout_text = str(entry.get("stdout") or "").strip()
        if stdout_text and stdout_text != response_text:
            parts.append("### Copilot stdout\n\n" + stdout_text)
        stderr_text = str(entry.get("stderr") or "").strip()
        if stderr_text:
            parts.append("### Copilot stderr\n\n" + stderr_text)
        result_status = str(entry.get("status") or "").strip()
        exit_code = entry.get("exit_code")
        if result_status or exit_code is not None:
            result_line = result_status or "unknown"
            if exit_code is not None:
                result_line = f"{result_line} (exit_code {exit_code})"
            parts.append("### Result\n\n" + result_line)
        sections.append("\n\n".join(part for part in parts if part.strip()))
    return "\n\n".join(section for section in sections if section.strip())


def persist_interaction_history(
    request,
    input_payload,
    task_prompt,
    composed_prompt,
    feedback,
    completed,
    response_text,
    response_file="",
):
    if input_payload.get("history_mode", "legacy") == "invocation":
        return None
    history_json_path, history_markdown_path = interaction_paths(
        request,
        input_payload,
        response_file=response_file,
    )
    all_entries = load_interaction_entries(history_json_path)
    all_entries.append(
        {
            "root_run_id": root_run_id(request),
            "run_id": request.get("run_id"),
            "step_id": (request.get("step") or {}).get("id"),
            "invoke_id": request.get("invoke_id"),
            "feedback": feedback,
            "task_prompt": str(task_prompt or "").strip(),
            "composed_prompt": str(composed_prompt or "").strip(),
            "response_text": str(response_text or "").strip(),
            "stdout": str(completed.stdout or "").strip(),
            "stderr": str(completed.stderr or "").strip(),
            "status": "success" if completed.returncode == 0 else "failed",
            "exit_code": completed.returncode,
        }
    )
    current_entries = current_run_interaction_entries(request, all_entries)
    history_markdown = render_interaction_history(current_entries)
    write_json(history_json_path, all_entries)
    write_text(history_markdown_path, history_markdown)
    return {
        "json_path": history_json_path,
        "json_contents": json.dumps(all_entries, indent=2, sort_keys=True),
        "markdown_path": history_markdown_path,
        "markdown_contents": history_markdown,
    }


def compose_prompt(request, input_payload, base_prompt, mode):
    if lean_policy.bounded(input_payload):
        return lean_policy.compose(globals(), request, input_payload, base_prompt)
    sections = []
    context_count = 0
    skills_rendered = input_payload.get("skills_rendered")
    if isinstance(skills_rendered, str) and skills_rendered.strip():
        sections.append("# Workflow skills\n\n" + skills_rendered.strip())
        context_count += 1
    ignored_paths = ignore_paths_section(input_payload)
    if ignored_paths:
        sections.append(ignored_paths)
    feedback = gate_decision_feedback(input_payload)
    if feedback:
        sections.append("# Operator feedback\n\n" + feedback)
        context_count += 1
        if mode in {"full", "focused"}:
            response_file = str(input_payload.get("response_file") or "").strip()
            if response_file:
                path = workspace_path(request, response_file, input_payload)
                try:
                    contents = read_text(path)
                except OSError:
                    contents = ""
                if contents:
                    label = os.path.basename(path)
                    sections.append(f"# Current draft: {label}\n\n{contents}")
                    context_count += 1
        if mode == "full":
            history_json_path, _ = interaction_paths(request, input_payload)
            prior_history = render_interaction_context(
                current_run_interaction_entries(
                    request,
                    load_interaction_entries(history_json_path),
                )
            )
            if prior_history:
                sections.append(
                    "# Prior Copilot interaction history\n\n" + prior_history
                )
                context_count += 1
    for raw_path in input_payload.get("context_files") or []:
        path = workspace_path(request, raw_path, input_payload)
        if is_ignored_path(request, input_payload, path):
            continue
        try:
            contents = read_text(path)
        except OSError:
            continue
        if not contents:
            continue
        label = os.path.basename(path)
        sections.append(f"# Context: {label}\n\n{contents}")
        context_count += 1
    sections.append("# Task\n\n" + str(base_prompt))
    return (
        "\n\n".join(section for section in sections if section.strip()),
        feedback,
        context_count,
    )


def extract_summary_title(text):
    canonical_text = extract_canonical_response(text)
    lines = canonical_text.splitlines()
    heading_index = find_canonical_heading_index(lines)
    if (
        heading_index is not None
        and normalize_line(lines[heading_index]) in TITLE_BASED_SECTION_HEADINGS
    ):
        title_index = previous_meaningful_line_index(lines, heading_index - 1)
        if title_index is not None:
            return cleanup_summary_line(lines[title_index])
    for line in lines:
        summary = cleanup_summary_line(line)
        if summary:
            return summary
    return ""


def extract_pr_body(text):
    lines = extract_canonical_response(text).splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        normalized = stripped.lstrip("#").strip().lower()
        if normalized == "what changed":
            return "\n".join(lines[index:]).strip()

    first_content_index = None
    for index, line in enumerate(lines):
        if line.strip():
            first_content_index = index
            break
    if first_content_index is None:
        return ""

    remainder = "\n".join(lines[first_content_index + 1 :]).strip()
    if remainder:
        return remainder
    return extract_canonical_response(text)


def normalize_line(line):
    return " ".join(str(line or "").strip().split()).lower()


def is_divider_line(line):
    stripped = str(line or "").strip()
    return bool(stripped) and all(ch in "-=*_`" for ch in stripped)


def cleanup_summary_line(line):
    stripped = str(line or "").strip()
    if not stripped or is_divider_line(stripped):
        return ""
    return stripped.lstrip("#*- ").strip()


def previous_meaningful_line_index(lines, start_index):
    for index in range(start_index, -1, -1):
        if cleanup_summary_line(lines[index]):
            return index
    return None


def find_canonical_heading_index(lines):
    for index, line in enumerate(lines):
        normalized = normalize_line(line)
        if (
            normalized in TITLE_BASED_SECTION_HEADINGS
            or normalized in DIRECT_SECTION_HEADINGS
        ):
            return index
        if PLAN_START_RE.match(normalized):
            return index
    return None


def extract_canonical_response(text):
    stripped_text = str(text or "").strip()
    if not stripped_text:
        return ""
    lines = stripped_text.splitlines()
    heading_index = find_canonical_heading_index(lines)
    if heading_index is None:
        return stripped_text
    normalized = normalize_line(lines[heading_index])
    start_index = heading_index
    if normalized in TITLE_BASED_SECTION_HEADINGS:
        title_index = previous_meaningful_line_index(lines, heading_index - 1)
        if title_index is not None:
            start_index = title_index
    return "\n".join(lines[start_index:]).strip()


def require_json_transport(
    request: dict, input_payload: dict, env_overrides: dict
) -> None:
    """Probe bounded local CLI help before a JSON-mode task, without sending a prompt.

    Uses the same final child environment and workspace as the task. Missing
    advertised JSON/stream options fail configuration instead of parsing a text
    transcript as an authoritative answer. Legacy invocations do not call this.
    """
    env = os.environ.copy()
    env.setdefault("CI", "1")
    env.setdefault("NO_COLOR", "1")
    env.update(env_overrides)
    command = ["gh", "copilot", "--", "--help"]
    lean_policy.check_exec(command, env)
    try:
        completed = run_process(
            command,
            cwd=command_cwd(request, input_payload),
            env=env,
            timeout=min(30, command_timeout_seconds(input_payload)),
        )
    except FileNotFoundError as err:
        raise BinaryNotFoundError("gh CLI not found in PATH") from err
    except subprocess.TimeoutExpired as err:
        raise JsonTransportUnavailableError(
            "Copilot transport help probe timed out"
        ) from err
    if completed.returncode != 0:
        raise JsonTransportUnavailableError("Copilot transport help probe failed")
    text = completed.stdout + "\n" + completed.stderr
    for option, choice in (("--output-format", "json"), ("--stream", "on")):
        section = re.search(
            re.escape(option) + r"\b(.*?)(?:\n\s*-(?:-|[A-Za-z])|\Z)", text, re.DOTALL
        )
        if (
            section is None
            or re.search(r"\b" + choice + r"\b", section.group(1), re.IGNORECASE)
            is None
        ):
            raise JsonTransportUnavailableError(
                f"Copilot CLI does not advertise required {option} {choice} transport"
            )


def run_command(request, command, input_payload=None, env_overrides=None):
    try:
        timeout_seconds = command_timeout_seconds(input_payload)
        env = os.environ.copy()
        env.setdefault("CI", "1")
        env.setdefault("NO_COLOR", "1")
        if env_overrides:
            env.update(env_overrides)
        if lean_policy.bounded(input_payload or {}) or lean_policy.answer_mode(
            input_payload or {}
        ):
            lean_policy.check_exec(command, env)
        emit_status_preview(request["invoke_id"], "Launching gh copilot...")
        first_output = False
        next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
        heartbeat_deadline = time.monotonic() + timeout_seconds

        def on_line(stream: str, line: str) -> None:
            nonlocal first_output
            preview = line.rstrip("\r\n")
            if preview:
                first_output = True
                emit(
                    progress_frame(
                        request["invoke_id"], stream=stream, line_preview=preview
                    )
                )

        def on_tick() -> None:
            nonlocal next_heartbeat
            now = time.monotonic()
            if next_heartbeat <= now < heartbeat_deadline:
                next_heartbeat = now + HEARTBEAT_INTERVAL_SECONDS
                emit_status_preview(
                    request["invoke_id"],
                    (
                        "Copilot is still working..."
                        if first_output
                        else "Waiting for Copilot output..."
                    ),
                )

        emit_status_preview(request["invoke_id"], "Waiting for Copilot output...")
        try:
            return run_process(
                command,
                cwd=command_cwd(request, input_payload),
                env=env,
                timeout=timeout_seconds,
                on_line=on_line,
                on_tick=on_tick,
            )
        except ValueError as err:
            raise RuntimeError("provider stream processing failed") from err
    except FileNotFoundError as err:
        raise BinaryNotFoundError(f"{command[0]} CLI not found in PATH") from err


def command_timeout_seconds(input_payload):
    raw = (input_payload or {}).get("timeout_seconds")
    if raw is not None:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise ValueError("timeout_seconds must be a positive integer")
        return raw
    raw_ms = (input_payload or {}).get("timeout_ms")
    if raw_ms is not None:
        if isinstance(raw_ms, bool) or not isinstance(raw_ms, int) or raw_ms <= 0:
            raise ValueError("timeout_ms must be a positive integer")
        if raw_ms < MIN_TIMEOUT_MS:
            raise ValueError(f"timeout_ms must be >= {MIN_TIMEOUT_MS}")
        timeout_seconds = raw_ms // 1000
        return timeout_seconds - TIMEOUT_GRACE_SECONDS
    return DEFAULT_TIMEOUT_SECONDS


def append_list_flag(command, flag, values):
    cleaned = [str(value).strip() for value in values or [] if str(value).strip()]
    if cleaned:
        command.extend([flag, ",".join(cleaned)])


def _handle_request(request):
    input_payload = request.get("input") or {}
    copilot_session_snapshot = None
    copilot_started_at_epoch = None
    repo_cwd = ""
    copilot_config_dir = ""
    try:
        action = request.get("action")
        if action != "exec_task":
            return failed_result(
                request["invoke_id"],
                "validation_error",
                f"unsupported action {action}",
            )
        try:
            prompt = resolve_prompt_input(input_payload)
            mode = feedback_context_mode(input_payload)
        except (OSError, ValueError) as err:
            return failed_result(
                request["invoke_id"],
                "validation_error",
                str(err),
            )
        artifacts_dir = request["paths"]["artifacts_dir"]
        os.makedirs(artifacts_dir, exist_ok=True)
        copilot_input_payload = dict(input_payload)
        copilot_config_dir = os.environ["COPILOT_CONFIG_DIR"]
        copilot_input_payload["_copilot_config_dir"] = copilot_config_dir
        copilot_cache_dir = os.environ["XDG_CACHE_HOME"]
        persist_skills_artifacts(artifacts_dir, input_payload)
        base_prompt = prompt
        prompt, feedback, context_count = compose_prompt(
            request, input_payload, prompt, mode
        )
        emit_status_preview(
            request["invoke_id"],
            f"Prepared Copilot task from {context_count} context item(s).",
        )
        repo_cwd = command_cwd(request, input_payload)
        emit_status_preview(
            request["invoke_id"],
            f"Using repository context: {os.path.basename(repo_cwd) or repo_cwd}",
        )
        command = [
            "gh",
            "copilot",
            "--",
            "-p",
            prompt,
            "--allow-all-tools",
            "--allow-all-paths",
            "--no-ask-user",
            "--no-color",
            "--output-format",
            "json" if lean_policy.answer_mode(input_payload) else "text",
            "--stream",
            "on",
            "--add-dir",
            repo_cwd,
        ]
        available_tools = input_payload.get("available_tools")
        legacy_allowed_tools = input_payload.get("allowed_tools")
        if available_tools is not None and legacy_allowed_tools is not None:
            emit_status_preview(
                request["invoke_id"],
                "Both available_tools and deprecated allowed_tools were set; using"
                " available_tools.",
            )
        elif available_tools is None and legacy_allowed_tools is not None:
            available_tools = legacy_allowed_tools
            emit_status_preview(
                request["invoke_id"],
                "DEPRECATED: allowed_tools is a compatibility alias; use"
                " available_tools.",
            )
        append_list_flag(command, "--available-tools", available_tools or [])
        append_list_flag(
            command, "--deny-tool", input_payload.get("denied_tools") or []
        )
        if input_payload.get("model"):
            command.extend(["--model", str(input_payload["model"])])
        if input_payload.get("reasoning_effort"):
            command.extend(
                ["--reasoning-effort", str(input_payload["reasoning_effort"])]
            )
        for extra_arg in input_payload.get("extra_args") or []:
            command.append(str(extra_arg))
        child_env = {
            "COPILOT_HOME": copilot_config_dir,
            "COPILOT_CONFIG_DIR": copilot_config_dir,
            "XDG_CACHE_HOME": copilot_cache_dir,
            "COPILOT_SESSION_STATE_DIR": os.environ["COPILOT_SESSION_STATE_DIR"],
            "TMPDIR": os.environ["TMPDIR"],
        }
        if lean_policy.answer_mode(input_payload):
            require_json_transport(request, copilot_input_payload, child_env)
        copilot_session_snapshot = snapshot_copilot_sessions(copilot_input_payload)
        copilot_started_at_epoch = time.time()
        completed = run_command(
            request,
            command,
            copilot_input_payload,
            env_overrides=child_env,
        )
        emit_status_preview(
            request["invoke_id"], "Copilot finished, processing output..."
        )
        emit_status_preview(request["invoke_id"], "Parsing Copilot session usage...")
        usage = extract_copilot_session_usage(
            repo_cwd,
            input_payload=copilot_input_payload,
            started_at_epoch=copilot_started_at_epoch,
            session_snapshot=copilot_session_snapshot,
        )
        if usage is None:
            usage = parse_copilot_stats(completed.stdout)
        emit_status_preview(request["invoke_id"], "Extracting canonical response...")
        response_text = (
            lean_policy.partial_response(globals(), completed.stdout)
            if lean_policy.answer_mode(input_payload)
            else completed.stdout.strip()
        )
        canonical_response_text = lean_policy.authoritative_response(
            globals(), input_payload, response_text
        )
        pr_body_text = ""
        response_file = ""
        summary_title = ""
        summary_title_file = ""
        pr_body_file = ""
        history_artifacts = None
        try:
            if input_payload.get("response_file"):
                emit_status_preview(
                    request["invoke_id"], "Writing response artifact..."
                )
                response_file = workspace_path(
                    request, input_payload["response_file"], input_payload
                )
                ensure_not_ignored_path(
                    request, input_payload, response_file, "response_file"
                )
                write_text(response_file, canonical_response_text or response_text)
            if input_payload.get("summary_title_file"):
                emit_status_preview(
                    request["invoke_id"], "Writing summary title artifact..."
                )
                summary_title = extract_summary_title(
                    canonical_response_text or response_text
                )
                summary_title_file = workspace_path(
                    request,
                    input_payload["summary_title_file"],
                    input_payload,
                )
                ensure_not_ignored_path(
                    request, input_payload, summary_title_file, "summary_title_file"
                )
                write_text(summary_title_file, summary_title)
            if input_payload.get("pr_body_file"):
                emit_status_preview(request["invoke_id"], "Writing PR body artifact...")
                pr_body_text = extract_pr_body(canonical_response_text or response_text)
                pr_body_file = workspace_path(
                    request, input_payload["pr_body_file"], input_payload
                )
                ensure_not_ignored_path(
                    request, input_payload, pr_body_file, "pr_body_file"
                )
                write_text(pr_body_file, pr_body_text)
            emit_status_preview(
                request["invoke_id"], "Persisting interaction history..."
            )
            history_artifacts = persist_interaction_history(
                request,
                input_payload,
                base_prompt,
                prompt,
                feedback,
                completed,
                response_text,
                response_file=response_file,
            )
            emit_status_preview(request["invoke_id"], "Finalizing artifacts...")
        except OSError:
            return failed_result(
                request["invoke_id"],
                "adapter_internal_error",
                "failed persisting Copilot outputs",
            )
        artifact_hints = [
            artifact_hint("stdout.txt", completed.stdout),
            artifact_hint("stderr.txt", completed.stderr),
            artifact_hint("response.txt", response_text),
        ]
        if usage:
            artifact_hints.append(
                artifact_hint("usage.json", json.dumps(usage, sort_keys=True))
            )
        if response_file:
            artifact_hints.append(
                artifact_hint(os.path.basename(response_file), response_text)
            )
        if summary_title_file:
            artifact_hints.append(
                artifact_hint(os.path.basename(summary_title_file), summary_title)
            )
        if pr_body_file:
            artifact_hints.append(
                artifact_hint(os.path.basename(pr_body_file), pr_body_text)
            )
        if history_artifacts:
            artifact_hints.append(
                artifact_hint(
                    os.path.basename(history_artifacts["json_path"]),
                    history_artifacts["json_contents"],
                )
            )
            artifact_hints.append(
                artifact_hint(
                    os.path.basename(history_artifacts["markdown_path"]),
                    history_artifacts["markdown_contents"],
                )
            )
        status = "success" if completed.returncode == 0 else "failed"
        error = None
        if completed.returncode != 0:
            error = protocol_error(
                "tool_execution_error",
                f"command exited with code {completed.returncode}",
            )
        elif lean_policy.answer_mode(input_payload) and not response_text:
            status = "failed"
            error = protocol_error(
                "protocol_error", "Copilot authoritative answer events are unavailable"
            )
        return invoke_result(
            request["invoke_id"],
            status,
            output={
                "ok": status == "success",
                "response_text": response_text,
                "canonical_response_text": canonical_response_text,
                "feedback": feedback,
                "response_file": response_file,
                "summary_title": summary_title,
                "summary_title_file": summary_title_file,
                "pr_body_file": pr_body_file,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "exit_code": completed.returncode,
            },
            artifact_hints=artifact_hints,
            error=error,
            usage=usage,
        )
    except CleanupUnconfirmedError as err:
        return failed_result(
            request["invoke_id"],
            "protocol_error",
            "Process cleanup could not be confirmed",
            artifact_hints=[
                artifact_hint("stdout.txt", err.stdout or ""),
                artifact_hint("stderr.txt", err.stderr or ""),
            ],
        )
    except JsonTransportUnavailableError as err:
        return failed_result(request["invoke_id"], "configuration_error", str(err))
    except BinaryNotFoundError as err:
        return failed_result(
            request["invoke_id"],
            "configuration_error",
            str(err),
        )
    except subprocess.TimeoutExpired as err:
        stdout_text = str(err.output or "")
        stderr_text = str(err.stderr or "")
        response_text = stdout_text.strip()
        canonical_response_text = extract_canonical_response(response_text)
        return failed_result(
            request["invoke_id"],
            "timeout_error",
            f"gh copilot timed out after {int(err.timeout)} seconds",
            output={
                "ok": False,
                "response_text": response_text,
                "canonical_response_text": canonical_response_text,
                "feedback": "",
                "response_file": "",
                "summary_title": "",
                "summary_title_file": "",
                "pr_body_file": "",
                "stdout": stdout_text,
                "stderr": stderr_text,
                "exit_code": None,
            },
            artifact_hints=[
                artifact_hint("stdout.txt", stdout_text),
                artifact_hint("stderr.txt", stderr_text),
                artifact_hint("response.txt", response_text),
            ],
        )
    except ValueError as err:
        return failed_result(
            request["invoke_id"],
            "validation_error",
            str(err),
        )
    except Exception:
        return failed_result(
            request["invoke_id"],
            "adapter_internal_error",
            "unexpected github.copilot.cli plugin error",
        )


def handle_request(request: dict) -> dict:
    """Apply opt-in package policy to one APP request and its terminal result."""
    try:
        gate_decision_feedback(request.get("input") or {})
    except ValueError as err:
        return failed_result(request["invoke_id"], "validation_error", str(err))
    return lean_policy.handle(globals(), request)


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
