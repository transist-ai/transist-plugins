#!/usr/bin/env python3
import fnmatch
import json
import os
import queue
import re
import subprocess
import sys
import time

# Script execution and package-local loading use the same selected helper.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from process_lifecycle import (  # noqa: E402
    PipeReader,
    ProcessLifecycle,
    finish_readers,
)
import lean_policy  # noqa: E402
from gate_review import valid_review_request  # noqa: E402

APP_PROTOCOL = "app.v1"
PLUGIN_ID = "cursor.cli"
PLUGIN_VERSION = "0.1.0"
ACTIONS = ["exec_task", "exec_task_cli"]
DEFAULT_TIMEOUT_SECONDS = 600
TIMEOUT_GRACE_SECONDS = 5
HEARTBEAT_INTERVAL_SECONDS = 1
APP_INVOKE_PROGRESS_TYPE = "app.invoke.progress.v1"
DEFAULT_IGNORE_PATHS = ("**/_build/**", "**/deps/*/_build/**")
TITLE_BASED_SECTION_HEADINGS = {"## what changed", "## review response"}
DIRECT_SECTION_HEADINGS = {"# problem"}
PLAN_START_RE = re.compile(r"^1\.\s+goal$")
FEEDBACK_CONTEXT_MODES = {"full", "focused", "feedback-only"}
DEFAULT_CURSOR_COMMAND = "agent"


class BinaryNotFoundError(Exception):
    pass


class PromptTooLongError(Exception):
    pass


class CleanupUnconfirmedError(Exception):
    """Carry ordinary captured output without exposing lifecycle diagnostics."""

    def __init__(self, stdout: str = "", stderr: str = "", response: str = "") -> None:
        super().__init__("Cursor process cleanup could not be confirmed")
        self.stdout = stdout
        self.stderr = stderr
        self.response = response


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


MAX_ARTIFACT_HINT_BYTES = 240 * 1024


def artifact_hint(name, contents):
    """Bound inline artifact hints without truncating their persisted files."""
    text = str(contents or "")
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_ARTIFACT_HINT_BYTES:
        return {"name": name, "contents": text}
    truncated = encoded[:MAX_ARTIFACT_HINT_BYTES].decode("utf-8", errors="ignore")
    omitted = len(encoded) - len(truncated.encode("utf-8"))
    return {
        "name": name,
        "contents": f"{truncated}\n\n[truncated {omitted} bytes from artifact hint]",
    }


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
        return lean_policy.prompt_source(input_payload)
    prompt = input_payload.get("prompt")
    prompt_file = input_payload.get("prompt_file")
    prompt_set = isinstance(prompt, str) and bool(prompt.strip())
    prompt_file_set = isinstance(prompt_file, str) and bool(prompt_file.strip())
    if prompt_set == prompt_file_set:
        raise ValueError("exactly one of prompt or prompt_file is required")
    if prompt_set:
        return str(prompt)
    return read_text(str(prompt_file))


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
            target = os.path.join(request["paths"]["artifacts_dir"], "cursor_response")
        base, extension = os.path.splitext(target)
        if not extension:
            base = target
    else:
        base = os.path.join(request["paths"]["artifacts_dir"], "cursor_response")
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
            parts.append("### Cursor response\n\n" + response_text)
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
    sections = ["# Cursor interaction history"]
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
            parts.append("### Prompt sent to Cursor\n\n" + composed_prompt)
        response_text = str(entry.get("response_text") or "").strip()
        if response_text:
            parts.append("### Cursor response\n\n" + response_text)
        stdout_text = str(entry.get("stdout") or "").strip()
        if stdout_text and stdout_text != response_text:
            parts.append("### Cursor stdout\n\n" + stdout_text)
        stderr_text = str(entry.get("stderr") or "").strip()
        if stderr_text:
            parts.append("### Cursor stderr\n\n" + stderr_text)
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


def compose_prompt(request, input_payload, base_prompt):
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
        mode = feedback_context_mode(input_payload)
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
                    "# Prior Cursor interaction history\n\n" + prior_history
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
    sections.append("# Task\n\n" + base_prompt.strip())
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
        timeout_seconds = max(1, raw_ms // 1000)
        return max(1, timeout_seconds - TIMEOUT_GRACE_SECONDS)
    return DEFAULT_TIMEOUT_SECONDS


def effective_force(input_payload):
    """Return True unless the user explicitly opts out.

    Cursor Agent is used in autonomous Transist workflows, so the default is to
    pass --force. Users can disable it with force: false or
    dangerously_skip_permissions: false.
    """
    force = input_payload.get("force")
    if force is not None:
        return bool(force)
    skip = input_payload.get("dangerously_skip_permissions")
    if skip is not None:
        return bool(skip)
    return True


def cursor_binary_path():
    env_path = os.environ.get("CURSOR_AGENT_PATH")
    if env_path:
        return env_path
    for candidate in ["agent", "cursor-agent"]:
        resolved = shutil_which(candidate)
        if resolved:
            return resolved
    versions_dir = os.path.join(
        os.path.expanduser("~"), ".local", "share", "cursor-agent", "versions"
    )
    try:
        for entry in sorted(os.listdir(versions_dir), reverse=True):
            candidate = os.path.join(versions_dir, entry, "cursor-agent")
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    except OSError:
        pass
    return DEFAULT_CURSOR_COMMAND


def shutil_which(command):
    try:
        import shutil

        return shutil.which(command)
    except Exception:
        return None


def command_size_limit():
    try:
        return os.sysconf("SC_ARG_MAX")
    except (ValueError, AttributeError):
        return 1048576


def new_usage_accumulator():
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
    }


def token_int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def accumulate_cursor_usage(accumulator, usage):
    if not isinstance(usage, dict):
        return
    input_tokens = token_int(usage.get("inputTokens"))
    output_tokens = token_int(usage.get("outputTokens"))
    cache_read_tokens = token_int(usage.get("cacheReadTokens"))
    cache_write_tokens = token_int(usage.get("cacheWriteTokens"))
    total_tokens = token_int(usage.get("totalTokens"))
    accumulator["input_tokens"] += input_tokens
    accumulator["output_tokens"] += output_tokens
    accumulator["cache_read_tokens"] += cache_read_tokens
    accumulator["cache_write_tokens"] += cache_write_tokens
    accumulator["total_tokens"] += total_tokens or (input_tokens + output_tokens)


def finalize_usage(accumulator, model=""):
    if not accumulator:
        return None
    if not any(
        [
            accumulator["input_tokens"],
            accumulator["output_tokens"],
            accumulator["cache_read_tokens"],
            accumulator["cache_write_tokens"],
        ]
    ):
        return None
    usage = {
        "input_tokens": accumulator["input_tokens"],
        "output_tokens": accumulator["output_tokens"],
        "total_tokens": (
            accumulator["total_tokens"]
            or (accumulator["input_tokens"] + accumulator["output_tokens"])
        ),
    }
    if accumulator["cache_read_tokens"]:
        usage["cache_read_tokens"] = accumulator["cache_read_tokens"]
    if accumulator["cache_write_tokens"]:
        usage["cache_write_tokens"] = accumulator["cache_write_tokens"]
    if model:
        usage["model"] = model
    return usage


def truncate_preview(text, limit=180):
    preview = " ".join(str(text or "").strip().split())
    if len(preview) <= limit:
        return preview
    return preview[: limit - 3].rstrip() + "..."


def first_nonempty_line(text):
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def assistant_message_text(message):
    """Extract text from a Cursor assistant message payload."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content") or []
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, dict) and str(part.get("type") or "").strip() == "text":
            text = str(part.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def tool_call_preview(event):
    """Build a short progress preview from a Cursor tool_call event."""
    tool_call = (event or {}).get("tool_call") or {}
    subtype = str((event or {}).get("subtype") or "").strip()
    for key in [
        "shellToolCall",
        "globToolCall",
        "fileToolCall",
        "editToolCall",
        "searchToolCall",
    ]:
        payload = tool_call.get(key)
        if isinstance(payload, dict):
            args = payload.get("args") or {}
            description = str(args.get("description") or "").strip()
            command = str(args.get("command") or "").strip()
            title = description or command or key.replace("ToolCall", "").lower()
            prefix = "Cursor tool"
            if subtype == "completed":
                prefix = "Cursor tool done"
            elif subtype == "started":
                prefix = "Cursor tool"
            elif subtype == "error":
                prefix = "Cursor tool failed"
            return truncate_preview(f"{prefix}: {title}")
    if subtype == "error":
        return "Cursor tool failed"
    return "Cursor tool"


def event_preview(event):
    event_type = str((event or {}).get("type") or "").strip()
    subtype = str((event or {}).get("subtype") or "").strip()
    if event_type == "assistant":
        text = assistant_message_text((event or {}).get("message"))
        return truncate_preview(first_nonempty_line(text))
    if event_type == "thinking":
        preview = first_nonempty_line((event or {}).get("text"))
        if preview:
            return truncate_preview("Thinking: " + preview)
        return ""
    if event_type == "tool_call":
        return tool_call_preview(event)
    if event_type == "system":
        if subtype == "init":
            return "Cursor Agent initialized"
        return ""
    if event_type == "error":
        error = (event or {}).get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("error") or "").strip()
            name = str(error.get("name") or "").strip()
            if message and name:
                return truncate_preview(f"{name}: {message}")
            return truncate_preview(message or name or "Cursor Agent error")
        return truncate_preview(str(error or "Cursor Agent error"))
    return ""


def render_response_text(parts):
    if not parts:
        return ""
    return "\n\n".join(part for part in parts if str(part).strip()).strip()


def build_command(request, input_payload, prompt):
    repo_cwd = command_cwd(request, input_payload)
    command = [
        cursor_binary_path(),
        "--print",
        "--output-format",
        "stream-json",
        "--workspace",
        repo_cwd,
        "--trust",
    ]
    if input_payload.get("model"):
        command.extend(["--model", str(input_payload["model"])])
    if input_payload.get("mode"):
        command.extend(["--mode", str(input_payload["mode"])])
    if effective_force(input_payload):
        command.append("--force")
    if input_payload.get("approve_mcps"):
        command.append("--approve-mcps")
    if input_payload.get("sandbox"):
        command.extend(["--sandbox", str(input_payload["sandbox"])])
    for extra_arg in input_payload.get("extra_args") or []:
        command.append(str(extra_arg))
    command.append(prompt)
    return command, repo_cwd


def _stringify_error(error):
    if isinstance(error, dict):
        name = str(error.get("name") or "").strip()
        message = str(error.get("message") or error.get("error") or "").strip()
        if name and message:
            return f"{name}: {message}"
        return message or name or json.dumps(error, sort_keys=True)
    return str(error or "").strip()


def _error_contains(error, needles):
    text = _stringify_error(error).lower()
    return any(needle in text for needle in needles)


def classify_error_from_result(result, last_error=None, stderr_text="", stdout_text=""):
    if not isinstance(result, dict):
        result = {}
    subtype = str(result.get("subtype") or "").strip().lower()
    is_error = bool(result.get("is_error"))
    result_text = str(result.get("result") or "").strip()
    combined = " ".join(
        part
        for part in [result_text, stderr_text, stdout_text]
        if isinstance(part, str) and part.strip()
    ).strip()
    combined_lower = combined.lower()

    auth_needles = [
        "api key",
        "unauthorized",
        "forbidden",
        "authentication",
        "login",
        "auth",
    ]
    rate_needles = ["rate limit", "too many requests", "quota exceeded", "ratelimit"]
    timeout_needles = ["timeout", "timed out"]
    transport_needles = [
        "network",
        "connection reset",
        "connection refused",
        "econn",
        "enotfound",
        "dns",
        "fetch failed",
        "socket",
        "transport",
    ]

    # Cursor result subtype may indicate the error class directly.
    if (
        subtype == "auth"
        or "authentication" in combined_lower
        or _error_contains(last_error, auth_needles)
    ):
        message = _stringify_error(last_error) or "Cursor authentication failed"
        return "auth_error", message
    if (
        subtype == "rate_limit"
        or any(needle in combined_lower for needle in rate_needles)
        or _error_contains(last_error, rate_needles)
    ):
        message = _stringify_error(last_error) or "Cursor rate limit reached"
        return "rate_limit_error", message
    if subtype == "timeout" or any(
        needle in combined_lower for needle in timeout_needles
    ):
        return "timeout_error", "Cursor timed out"
    if subtype == "transport" or any(
        needle in combined_lower for needle in transport_needles
    ):
        return "transport_error", "Cursor transport failed"
    if is_error and result_text:
        return "tool_execution_error", result_text
    if last_error is not None:
        return (
            "tool_execution_error",
            _stringify_error(last_error) or "Cursor execution failed",
        )
    return "tool_execution_error", "Cursor execution failed"


def run_command(request, command, input_payload, env_overrides=None, env_unset=()):
    try:
        timeout_seconds = command_timeout_seconds(input_payload)
        env = os.environ.copy()
        env.setdefault("CI", "1")
        env.setdefault("NO_COLOR", "1")
        env.setdefault("CURSOR_CLIENT", PLUGIN_ID)
        if env_overrides:
            env.update(env_overrides)
        for name in env_unset:
            env.pop(name, None)
        emit_status_preview(request["invoke_id"], "Launching Cursor Agent...")

        # Defensive check: prevent blowing past the OS argv limit.
        try:
            if lean_policy.bounded(input_payload) or lean_policy.answer_mode(
                input_payload
            ):
                lean_policy.check_exec(command, env, command_size_limit())
            else:
                arg_size = sum(len(str(arg)) for arg in command) + len(command)
                limit = command_size_limit()
                if limit > 0 and arg_size > limit - 4096:
                    raise PromptTooLongError(
                        "composed prompt exceeds safe command-line length "
                        f"({arg_size} >"
                        f" {limit - 4096})"
                    )
        except ValueError as err:
            raise PromptTooLongError(str(err)) from err

        try:
            lifecycle = ProcessLifecycle()
        except Exception as err:
            raise CleanupUnconfirmedError() from err
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=command[command.index("--workspace") + 1],
            env=env,
            bufsize=1,
        )
        stdout_lines = []
        stderr_lines = []
        events = queue.Queue()
        readers = []
        first_output = False
        last_heartbeat = time.monotonic()
        reader_failed = False
        state = {
            "session_id": "",
            "request_id": "",
            "model": "",
            "assistant_parts": [],
            "result_event": None,
            "usage_accumulator": new_usage_accumulator(),
            "last_error": None,
            "closed": False,
        }

        def handle_event(event):
            nonlocal first_output
            event_type = str((event or {}).get("type") or "").strip()
            subtype = str((event or {}).get("subtype") or "").strip()
            if not state["session_id"]:
                state["session_id"] = str((event or {}).get("session_id") or "").strip()
            if event_type == "system" and subtype == "init" and not state["model"]:
                state["model"] = str((event or {}).get("model") or "").strip()
            elif event_type == "assistant":
                text = assistant_message_text((event or {}).get("message"))
                if text:
                    state["assistant_parts"].append(text)
            elif event_type == "result":
                state["result_event"] = event
                usage = (event or {}).get("usage")
                if isinstance(usage, dict):
                    accumulate_cursor_usage(state["usage_accumulator"], usage)
                request_id = str(event.get("request_id") or "").strip()
                if request_id:
                    state["request_id"] = request_id
            elif event_type == "error":
                state["last_error"] = event.get("error")
            preview = event_preview(event)
            if preview:
                first_output = True
                emit(
                    progress_frame(
                        request["invoke_id"], stream="stdout", line_preview=preview
                    )
                )

        def tick() -> None:
            """Capture queued lines and emit main-thread progress/heartbeat."""
            nonlocal first_output, last_heartbeat, reader_failed
            # A bounded batch prevents a noisy provider from delaying cleanup.
            for _ in range(256):
                try:
                    stream, line = events.get_nowait()
                except queue.Empty:
                    break
                if stream == "reader_error":
                    reader_failed = True
                elif stream == "stdout":
                    stdout_lines.append(line)
                    try:
                        payload = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        handle_event(payload)
                else:
                    stderr_lines.append(line)
                    preview = line.rstrip("\r\n")
                    if preview:
                        first_output = True
                        emit(
                            progress_frame(
                                request["invoke_id"],
                                stream="stderr",
                                line_preview=preview,
                            )
                        )
            if time.monotonic() - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                last_heartbeat = time.monotonic()
                emit_status_preview(
                    request["invoke_id"],
                    (
                        "Cursor Agent is still working..."
                        if first_output
                        else "Waiting for Cursor Agent output..."
                    ),
                )

        primary_error = None
        returncode = None
        try:
            emit_status_preview(
                request["invoke_id"], "Waiting for Cursor Agent output..."
            )
            readers = [
                PipeReader(process.stdout, events, "stdout"),
                PipeReader(process.stderr, events, "stderr"),
            ]
            for reader in readers:
                reader.start()
            deadline = time.monotonic() + timeout_seconds
            while True:
                tick()
                left = deadline - time.monotonic()
                if left <= 0:
                    raise subprocess.TimeoutExpired(command, timeout_seconds)
                try:
                    returncode = process.wait(timeout=min(0.1, left))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except Exception as err:
            primary_error = err
        finally:
            # Heartbeats are main-thread-only, including throughout provider cleanup.
            # Neither reader ever mutates captured output or emits APP frames.
            finalization_deadline = time.monotonic() + 5

            def finalization_tick() -> None:
                # A parser/progress exception must not interrupt native cleanup.
                nonlocal primary_error
                try:
                    tick()
                except Exception as err:
                    primary_error = primary_error or err

            cleanup_ok = lifecycle.cleanup(finalization_tick)
            reader_deadline = min(finalization_deadline, time.monotonic() + 1)
            readers_ok = finish_readers(readers, reader_deadline)
            # Reap the immediate child without a blocking wait or pipe drain.
            try:
                process.poll()
                while not events.empty() and time.monotonic() < reader_deadline:
                    finalization_tick()
                readers_ok &= events.empty()
            except Exception as err:
                primary_error = primary_error or err
        if not cleanup_ok or not readers_ok or reader_failed:
            result = state["result_event"] or {}
            response = str(result.get("result") or "") or render_response_text(
                state["assistant_parts"]
            )
            raise CleanupUnconfirmedError(
                "".join(stdout_lines), "".join(stderr_lines), response
            )
        if isinstance(primary_error, subprocess.TimeoutExpired):
            raise subprocess.TimeoutExpired(
                command,
                timeout_seconds,
                output="".join(stdout_lines),
                stderr="".join(stderr_lines),
            )
        if primary_error is not None:
            raise primary_error
        completed = subprocess.CompletedProcess(
            command,
            returncode,
            stdout="".join(stdout_lines),
            stderr="".join(stderr_lines),
        )
        response_text = ""
        result_event = state["result_event"]
        if isinstance(result_event, dict):
            result_text = str(result_event.get("result") or "").strip()
            if result_text:
                response_text = result_text
        if not response_text and state["assistant_parts"]:
            response_text = render_response_text(state["assistant_parts"])
        return {
            "completed": completed,
            "session_id": state["session_id"],
            "request_id": state["request_id"],
            "response_text": response_text,
            "result_event": result_event,
            "last_error": state["last_error"],
            "usage": finalize_usage(state["usage_accumulator"], model=state["model"]),
        }
    except FileNotFoundError as err:
        raise BinaryNotFoundError(f"{command[0]} CLI not found in PATH") from err


def _handle_request(request):
    input_payload = request.get("input") or {}
    try:
        action = request.get("action")
        if action not in ACTIONS:
            return failed_result(
                request["invoke_id"],
                "validation_error",
                f"unsupported action {action}",
            )
        try:
            prompt = resolve_prompt_input(input_payload)
        except (OSError, ValueError) as err:
            return failed_result(
                request["invoke_id"],
                "validation_error",
                str(err),
            )
        artifacts_dir = request["paths"]["artifacts_dir"]
        os.makedirs(artifacts_dir, exist_ok=True)
        cursor_config_dir = os.environ["CURSOR_CONFIG_DIR"]
        cursor_data_dir = os.environ["CURSOR_DATA_DIR"]
        cursor_tmp_dir = os.environ["TMPDIR"]
        if action == "exec_task":
            cursor_api_key = os.environ.get("CURSOR_API_KEY")
            if not cursor_api_key:
                return failed_result(
                    request["invoke_id"],
                    "secret_resolution_error",
                    "CURSOR_API_KEY is unavailable",
                )
        elif any(
            arg == "--api-key"
            or arg.startswith("--api-key=")
            or arg in ("--header", "-H")
            for arg in (input_payload.get("extra_args") or [])
        ):
            return failed_result(
                request["invoke_id"],
                "validation_error",
                "CLI-login action cannot pass authentication arguments",
            )
        persist_skills_artifacts(artifacts_dir, input_payload)
        base_prompt = prompt
        prompt, feedback, context_count = compose_prompt(request, input_payload, prompt)
        emit_status_preview(
            request["invoke_id"],
            f"Prepared Cursor task from {context_count} context item(s).",
        )
        repo_cwd = command_cwd(request, input_payload)
        emit_status_preview(
            request["invoke_id"],
            f"Using repository context: {os.path.basename(repo_cwd) or repo_cwd}",
        )
        command, _ = build_command(request, input_payload, prompt)
        # CLI login uses the provider's own account lookup, not invocation-private
        # config/data roots or an inherited API key. The API-key action stays isolated.
        env_overrides = {"TMPDIR": cursor_tmp_dir}
        env_unset = ("CURSOR_API_KEY", "CURSOR_CONFIG_DIR", "CURSOR_DATA_DIR", "HOME")
        if action == "exec_task":
            env_overrides.update(
                {
                    "CURSOR_CONFIG_DIR": cursor_config_dir,
                    "CURSOR_DATA_DIR": cursor_data_dir,
                    "HOME": cursor_config_dir,
                    "CURSOR_API_KEY": cursor_api_key,
                }
            )
            env_unset = ()
        command_result = run_command(
            request,
            command,
            input_payload,
            env_overrides=env_overrides,
            env_unset=env_unset,
        )
        completed = command_result["completed"]
        response_text = str(command_result.get("response_text") or "").strip()
        session_id = str(command_result.get("session_id") or "").strip()
        usage = command_result.get("usage")
        result_event = command_result.get("result_event")
        last_error = command_result.get("last_error")
        response_file = ""
        summary_title = ""
        summary_title_file = ""
        pr_body_text = ""
        pr_body_file = ""
        history_artifacts = None
        usage_json_path = ""
        usage_json_contents = ""
        canonical_response_text = lean_policy.authoritative_response(
            globals(), input_payload, response_text
        )
        persisted_response_text = canonical_response_text or response_text
        try:
            if input_payload.get("response_file"):
                response_file = workspace_path(
                    request, input_payload["response_file"], input_payload
                )
                ensure_not_ignored_path(
                    request, input_payload, response_file, "response_file"
                )
                write_text(response_file, persisted_response_text)
            if input_payload.get("summary_title_file"):
                summary_title = extract_summary_title(persisted_response_text)
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
                pr_body_text = extract_pr_body(persisted_response_text)
                pr_body_file = workspace_path(
                    request, input_payload["pr_body_file"], input_payload
                )
                ensure_not_ignored_path(
                    request, input_payload, pr_body_file, "pr_body_file"
                )
                write_text(pr_body_file, pr_body_text)
            if usage:
                usage_json_path = os.path.join(artifacts_dir, "usage.json")
                write_json(usage_json_path, usage)
                usage_json_contents = json.dumps(usage, indent=2, sort_keys=True)
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
        except OSError as err:
            return failed_result(
                request["invoke_id"],
                "tool_execution_error",
                f"failed persisting Cursor outputs: {err}",
            )
        artifact_hints = [
            artifact_hint("stdout.txt", completed.stdout),
            artifact_hint("stderr.txt", completed.stderr),
            artifact_hint("response.txt", response_text),
        ]
        if response_file:
            artifact_hints.append(
                artifact_hint(os.path.basename(response_file), persisted_response_text)
            )
        if summary_title_file:
            artifact_hints.append(
                artifact_hint(os.path.basename(summary_title_file), summary_title)
            )
        if pr_body_file:
            artifact_hints.append(
                artifact_hint(os.path.basename(pr_body_file), pr_body_text)
            )
        if usage_json_path:
            artifact_hints.append(
                artifact_hint(os.path.basename(usage_json_path), usage_json_contents)
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
        status = "success"
        error = None
        if (
            completed.returncode != 0
            or result_event
            and result_event.get("is_error")
            or last_error is not None
        ):
            status = "failed"
            code, message = classify_error_from_result(
                result_event,
                last_error=last_error,
                stderr_text=completed.stderr,
                stdout_text=completed.stdout,
            )
            if (
                code == "tool_execution_error"
                and completed.returncode != 0
                and not message
            ):
                message = f"command exited with code {completed.returncode}"
            error = protocol_error(code, message)
        if (
            not response_text
            and lean_policy.answer_mode(input_payload)
            and error is None
        ):
            status = "failed"
            error = protocol_error(
                "protocol_error", "Cursor authoritative answer events are unavailable"
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
                "session_id": session_id,
                "share_url": "",
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "exit_code": completed.returncode,
            },
            artifact_hints=artifact_hints,
            error=error,
            usage=usage,
        )
    except CleanupUnconfirmedError as err:
        if "_lean_context" in request:
            request["_lean_context"]["logs"] = (err.stdout, err.stderr)
            request["_lean_context"]["answer"] = err.response
        return failed_result(
            request["invoke_id"],
            "protocol_error",
            "Cursor process cleanup could not be confirmed",
            artifact_hints=[
                artifact_hint("stdout.txt", err.stdout),
                artifact_hint("stderr.txt", err.stderr),
                artifact_hint("response.txt", err.response),
            ],
        )
    except BinaryNotFoundError as err:
        return failed_result(
            request["invoke_id"],
            "configuration_error",
            str(err),
        )
    except PromptTooLongError as err:
        return failed_result(
            request["invoke_id"],
            "validation_error",
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
            f"Cursor Agent timed out after {int(err.timeout)} seconds",
            output={
                "ok": False,
                "response_text": response_text,
                "canonical_response_text": canonical_response_text,
                "feedback": "",
                "response_file": "",
                "summary_title": "",
                "summary_title_file": "",
                "pr_body_file": "",
                "session_id": "",
                "share_url": "",
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
    except Exception as err:
        return failed_result(
            request["invoke_id"],
            "adapter_internal_error",
            f"unexpected cursor.cli plugin error: {type(err).__name__}",
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
