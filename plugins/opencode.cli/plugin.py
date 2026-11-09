#!/usr/bin/env python3
from __future__ import annotations

import copy
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
PLUGIN_ID = "opencode.cli"
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
SHELL_RULE_RE = re.compile(r"^(shell|bash)\((.*)\)$", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+")


class BinaryNotFoundError(Exception):
    pass


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
            target = os.path.join(
                request["paths"]["artifacts_dir"], "opencode_response"
            )
        base, extension = os.path.splitext(target)
        if not extension:
            base = target
    else:
        base = os.path.join(request["paths"]["artifacts_dir"], "opencode_response")
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
            parts.append("### OpenCode response\n\n" + response_text)
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
    sections = ["# OpenCode interaction history"]
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
            parts.append("### Prompt sent to OpenCode\n\n" + composed_prompt)
        response_text = str(entry.get("response_text") or "").strip()
        if response_text:
            parts.append("### OpenCode response\n\n" + response_text)
        stdout_text = str(entry.get("stdout") or "").strip()
        if stdout_text and stdout_text != response_text:
            parts.append("### OpenCode stdout\n\n" + stdout_text)
        stderr_text = str(entry.get("stderr") or "").strip()
        if stderr_text:
            parts.append("### OpenCode stderr\n\n" + stderr_text)
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
                    "# Prior OpenCode interaction history\n\n" + prior_history
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


def normalize_shell_rule(raw, field_name):
    text = str(raw or "").strip()
    if not text:
        raise ValueError(f"{field_name} entries must be non-empty strings")
    wrapped = False
    match = SHELL_RULE_RE.match(text)
    if match:
        wrapped = True
        text = match.group(2).strip()
    elif re.match(r"^[A-Za-z0-9_.-]+\(", text):
        raise ValueError(
            f"unsupported {field_name} descriptor '{raw}'; only shell(...), bash(...),"
            " or plain bash command patterns are supported"
        )
    if not text:
        raise ValueError(f"{field_name} entries must include a shell command pattern")
    if wrapped:
        text = text.replace(":*", " *").replace(":", " ")
    return " ".join(text.split())


def build_shimmed_bash_rules(input_payload):
    allowed = [
        normalize_shell_rule(item, "allowed_tools")
        for item in (input_payload.get("allowed_tools") or [])
    ]
    denied = [
        normalize_shell_rule(item, "denied_tools")
        for item in (input_payload.get("denied_tools") or [])
    ]
    if not allowed and not denied:
        return None
    rules = {}
    if allowed:
        rules["*"] = "deny"
    for pattern in allowed:
        rules[pattern] = "allow"
    for pattern in denied:
        rules[pattern] = "deny"
    return {"bash": rules}


def merged_permission_config(input_payload):
    shimmed = build_shimmed_bash_rules(input_payload)
    explicit = input_payload.get("permission")
    if explicit is None:
        return shimmed
    if isinstance(explicit, str):
        if explicit.strip():
            return explicit.strip()
        return shimmed
    if not isinstance(explicit, dict):
        raise ValueError("permission must be a string or object when provided")
    if shimmed is None:
        return copy.deepcopy(explicit)
    merged = copy.deepcopy(shimmed)
    for key, value in explicit.items():
        merged[key] = copy.deepcopy(value)
    return merged


def deep_merge_dicts(base, overlay):
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge_dicts(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def runtime_config_content(env, input_payload):
    permission = merged_permission_config(input_payload)
    if permission is None:
        return None
    existing_raw = str(env.get("OPENCODE_CONFIG_CONTENT") or "").strip()
    existing = {}
    if existing_raw:
        try:
            existing = json.loads(existing_raw)
        except json.JSONDecodeError as err:
            raise ValueError(
                "existing OPENCODE_CONFIG_CONTENT is not valid JSON"
            ) from err
        if not isinstance(existing, dict):
            raise ValueError(
                "existing OPENCODE_CONFIG_CONTENT must decode to an object"
            )
    merged = deep_merge_dicts(existing, {"permission": permission})
    return json.dumps(merged, sort_keys=True)


def effective_variant(input_payload):
    variant = str((input_payload or {}).get("variant") or "").strip()
    if variant:
        return variant
    effort = str((input_payload or {}).get("reasoning_effort") or "").strip()
    if not effort:
        return ""
    mapping = {"low": "minimal", "medium": "", "high": "high", "xhigh": "max"}
    if effort not in mapping:
        raise ValueError("reasoning_effort must be one of: high, low, medium, xhigh")
    return mapping[effort]


def new_usage_accumulator():
    return {
        "input_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
    }


def token_int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def accumulate_step_usage(accumulator, tokens):
    if not isinstance(tokens, dict):
        return
    input_tokens = token_int(tokens.get("input"))
    completion_tokens = token_int(tokens.get("output"))
    reasoning_tokens = token_int(tokens.get("reasoning"))
    cache = tokens.get("cache") or {}
    cache_read_tokens = token_int(cache.get("read"))
    cache_write_tokens = token_int(cache.get("write"))
    total_tokens = token_int(tokens.get("total"))
    accumulator["input_tokens"] += input_tokens
    accumulator["completion_tokens"] += completion_tokens
    accumulator["reasoning_tokens"] += reasoning_tokens
    accumulator["cache_read_tokens"] += cache_read_tokens
    accumulator["cache_write_tokens"] += cache_write_tokens
    accumulator["total_tokens"] += total_tokens or (
        input_tokens + completion_tokens + reasoning_tokens
    )


def finalize_usage(accumulator, model=""):
    if not accumulator:
        return None
    output_tokens = accumulator["completion_tokens"] + accumulator["reasoning_tokens"]
    if not any(
        [
            accumulator["input_tokens"],
            output_tokens,
            accumulator["cache_read_tokens"],
            accumulator["cache_write_tokens"],
        ]
    ):
        return None
    usage = {
        "input_tokens": accumulator["input_tokens"],
        "output_tokens": output_tokens,
        "total_tokens": (
            accumulator["total_tokens"] or (accumulator["input_tokens"] + output_tokens)
        ),
    }
    if accumulator["reasoning_tokens"]:
        usage["reasoning_tokens"] = accumulator["reasoning_tokens"]
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


def summarize_tool_input(state_input):
    if not isinstance(state_input, dict):
        return ""
    for key in [
        "command",
        "description",
        "prompt",
        "query",
        "filePath",
        "path",
        "url",
        "name",
    ]:
        value = str(state_input.get(key) or "").strip()
        if value:
            return value
    try:
        rendered = json.dumps(state_input, sort_keys=True)
    except TypeError:
        rendered = str(state_input)
    return rendered


def stringify_error_payload(error_payload):
    if isinstance(error_payload, str):
        return error_payload.strip()
    if isinstance(error_payload, dict):
        name = str(error_payload.get("name") or "").strip()
        data = error_payload.get("data") or {}
        message = ""
        if isinstance(data, dict):
            message = str(data.get("message") or data.get("error") or "").strip()
        if not message:
            message = str(error_payload.get("message") or "").strip()
        if name and message:
            return f"{name}: {message}"
        if message:
            return message
        if name:
            return name
        try:
            return json.dumps(error_payload, sort_keys=True)
        except TypeError:
            return str(error_payload)
    return str(error_payload or "").strip()


def event_preview(event):
    event_type = str((event or {}).get("type") or "").strip()
    part = (event or {}).get("part") or {}
    if event_type == "text":
        return truncate_preview(first_nonempty_line(part.get("text")))
    if event_type == "reasoning":
        preview = first_nonempty_line(part.get("text"))
        if preview:
            return truncate_preview("Thinking: " + preview)
        return ""
    if event_type == "tool_use":
        tool_name = str(part.get("tool") or "tool").strip()
        state = part.get("state") or {}
        title = str(state.get("title") or "").strip() or summarize_tool_input(
            state.get("input")
        )
        status = str(state.get("status") or "").strip()
        prefix = "OpenCode tool"
        if status == "error":
            prefix = "OpenCode tool failed"
        if title:
            return truncate_preview(f"{prefix}: {tool_name} {title}")
        return truncate_preview(f"{prefix}: {tool_name}")
    if event_type == "step_start":
        return "OpenCode started a step."
    if event_type == "step_finish":
        reason = str(part.get("reason") or "").strip()
        if reason:
            return truncate_preview(f"OpenCode finished a step ({reason}).")
        return "OpenCode finished a step."
    if event_type == "error":
        return truncate_preview(stringify_error_payload(event.get("error")))
    return ""


def append_response_text(parts, text):
    cleaned = str(text or "").strip()
    if not cleaned:
        return
    if parts and parts[-1] == cleaned:
        return
    parts.append(cleaned)


def render_response_text(parts):
    if not parts:
        return ""
    return "\n\n".join(part for part in parts if str(part).strip()).strip()


def extract_share_url(text):
    match = URL_RE.search(str(text or ""))
    if not match:
        return ""
    return match.group(0).rstrip(").,")


class JsonEventBuffer:
    def __init__(self):
        self.buffer = ""
        self.decoder = json.JSONDecoder()

    def feed(self, chunk):
        self.buffer += chunk
        plain_lines = []
        events = []
        while True:
            stripped = self.buffer.lstrip()
            if not stripped:
                self.buffer = ""
                break
            self.buffer = stripped
            if not self.buffer:
                break
            if not self.buffer.startswith("{"):
                newline_index = self.buffer.find("\n")
                if newline_index == -1:
                    break
                plain_lines.append(self.buffer[:newline_index].rstrip("\r"))
                self.buffer = self.buffer[newline_index + 1 :]
                continue
            try:
                payload, index = self.decoder.raw_decode(self.buffer)
            except json.JSONDecodeError:
                break
            events.append(payload)
            self.buffer = self.buffer[index:]
        return plain_lines, events

    def flush_plain(self):
        leftover = self.buffer.strip()
        self.buffer = ""
        if leftover:
            return [leftover]
        return []


def classify_error_from_payload(
    error_payload, stderr_text="", stdout_text="", exit_code=None
):
    message = stringify_error_payload(error_payload)
    status_code = None
    name = ""
    if isinstance(error_payload, dict):
        name = str(error_payload.get("name") or "").strip().lower()
        data = error_payload.get("data") or {}
        if isinstance(data, dict):
            try:
                status_code = int(data.get("statusCode"))
            except (TypeError, ValueError):
                status_code = None
    combined = " ".join(
        part
        for part in [message, stderr_text, stdout_text]
        if isinstance(part, str) and part.strip()
    ).strip()
    combined_lower = combined.lower()
    if (
        status_code in {401, 403}
        or "auth" in name
        or any(
            needle in combined_lower
            for needle in [
                "api key",
                "unauthorized",
                "forbidden",
                "authentication",
                "providerautherror",
                "login",
            ]
        )
    ):
        return "auth_error", message or "OpenCode authentication failed"
    if status_code == 429 or any(
        needle in combined_lower
        for needle in ["rate limit", "too many requests", "quota exceeded", "ratelimit"]
    ):
        return "rate_limit_error", message or "OpenCode rate limit reached"
    if any(needle in combined_lower for needle in ["timeout", "timed out"]):
        return "timeout_error", message or "OpenCode timed out"
    if status_code and status_code >= 500:
        return "transport_error", message or "OpenCode transport failed"
    if any(
        needle in combined_lower
        for needle in [
            "network",
            "connection reset",
            "connection refused",
            "econn",
            "enotfound",
            "dns",
            "fetch failed",
            "socket",
            "transport",
            "stream disconnected",
        ]
    ):
        return "transport_error", message or "OpenCode transport failed"
    if message:
        return "tool_execution_error", message
    if exit_code is not None:
        return "tool_execution_error", f"command exited with code {exit_code}"
    return "tool_execution_error", "OpenCode execution failed"


def export_session_data(
    session_id: str, repo_cwd: str, env: dict, bounded_exec: bool = False
) -> dict | None:
    """Read export JSON; optionally guard argv/env before a secondary launch."""
    if not session_id:
        return None
    try:
        if bounded_exec:
            lean_policy.check_exec(["opencode", "export", session_id], env)
        completed = run_process(
            ["opencode", "export", session_id],
            cwd=repo_cwd,
            env=env,
            timeout=30,
        )
    except CleanupUnconfirmedError:
        raise
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None


def extract_export_details(payload: dict, whole_answer: bool = False) -> dict:
    """Extract session details and the last assistant answer from an export.

    Whole-answer mode joins all text parts of that message; legacy keeps its
    last text-part projection. This function does not mutate provider state.
    """
    if not isinstance(payload, dict):
        return {"response_text": "", "share_url": "", "session_id": "", "model": ""}
    info = payload.get("info") or {}
    session_id = str(info.get("id") or "").strip()
    share_url = str(info.get("shareURL") or info.get("share_url") or "").strip()
    response_text = ""
    model = ""
    for message in payload.get("messages") or []:
        message_info = message.get("info") or {}
        if str(message_info.get("role") or "").strip() != "assistant":
            continue
        provider_id = str(message_info.get("providerID") or "").strip()
        model_id = str(message_info.get("modelID") or "").strip()
        if provider_id and model_id:
            model = f"{provider_id}/{model_id}"
        elif model_id:
            model = model_id
        message_parts = []
        for part in message.get("parts") or []:
            if str(part.get("type") or "").strip() == "text":
                candidate = str(part.get("text") or "").strip()
                if candidate:
                    response_text = candidate
                    message_parts.append(candidate)
        if whole_answer:
            response_text = "\n\n".join(message_parts)
    return {
        "response_text": response_text,
        "share_url": share_url,
        "session_id": session_id,
        "model": model,
    }


def run_command(
    request, command, stdin_text=None, input_payload=None, env_overrides=None
):
    try:
        timeout_seconds = command_timeout_seconds(input_payload)
        env = os.environ.copy()
        env.setdefault("CI", "1")
        env.setdefault("NO_COLOR", "1")
        env.setdefault("OPENCODE_CLIENT", PLUGIN_ID)
        if env_overrides:
            env.update(env_overrides)
        config_content = runtime_config_content(env, input_payload)
        if config_content is not None:
            env["OPENCODE_CONFIG_CONTENT"] = config_content
        if lean_policy.bounded(input_payload or {}) or lean_policy.answer_mode(
            input_payload or {}
        ):
            lean_policy.check_exec(command, env)
        emit_status_preview(request["invoke_id"], "Launching opencode run...")
        if stdin_text and not stdin_text.endswith("\n"):
            stdin_text += "\n"
        first_output = False
        next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
        heartbeat_deadline = time.monotonic() + timeout_seconds
        parser = JsonEventBuffer()
        state = {
            "env": env,
            "session_id": "",
            "share_url": "",
            "response_parts": [],
            "event_error": None,
            "usage_accumulator": new_usage_accumulator(),
        }

        def handle_plain_stdout(preview):
            nonlocal first_output
            cleaned = str(preview or "").strip()
            if not cleaned:
                return
            first_output = True
            if not state["share_url"]:
                state["share_url"] = extract_share_url(cleaned)

        def handle_event(event):
            nonlocal first_output
            if not state["session_id"]:
                state["session_id"] = str(event.get("sessionID") or "").strip()
            event_type = str(event.get("type") or "").strip()
            if event_type == "text":
                append_response_text(
                    state["response_parts"], (event.get("part") or {}).get("text")
                )
            elif event_type == "step_finish":
                accumulate_step_usage(
                    state["usage_accumulator"],
                    ((event.get("part") or {}).get("tokens") or {}),
                )
            elif event_type == "error":
                state["event_error"] = event.get("error")
            preview = event_preview(event)
            if preview:
                first_output = True
                emit(
                    progress_frame(
                        request["invoke_id"], stream="stdout", line_preview=preview
                    )
                )

        def on_line(stream: str, line: str) -> None:
            nonlocal first_output
            if stream == "stdout":
                plain_lines, events = parser.feed(line)
                for plain in plain_lines:
                    handle_plain_stdout(plain)
                for event in events:
                    handle_event(event)
            else:
                preview = line.rstrip("\r\n")
                if preview:
                    first_output = True
                    emit(
                        progress_frame(
                            request["invoke_id"], stream="stderr", line_preview=preview
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
                        "OpenCode is still working..."
                        if first_output
                        else "Waiting for OpenCode output..."
                    ),
                )

        emit_status_preview(request["invoke_id"], "Waiting for OpenCode output...")
        try:
            completed = run_process(
                command,
                cwd=command_cwd(request, input_payload),
                env=env,
                timeout=timeout_seconds,
                input=stdin_text,
                on_line=on_line,
                on_tick=on_tick,
            )
        except ValueError as err:
            raise RuntimeError("provider stream processing failed") from err
        for plain in parser.flush_plain():
            handle_plain_stdout(plain)
        return {
            "completed": completed,
            "env": env,
            "session_id": state["session_id"],
            "share_url": state["share_url"],
            "response_text": render_response_text(state["response_parts"]),
            "event_error": state["event_error"],
            "usage": finalize_usage(state["usage_accumulator"]),
        }
    except FileNotFoundError as err:
        raise BinaryNotFoundError(f"{command[0]} CLI not found in PATH") from err


def _handle_request(request):
    input_payload = request.get("input") or {}
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
        except (OSError, ValueError) as err:
            return failed_result(
                request["invoke_id"],
                "validation_error",
                str(err),
            )
        artifacts_dir = request["paths"]["artifacts_dir"]
        os.makedirs(artifacts_dir, exist_ok=True)
        opencode_data_dir = os.environ["XDG_DATA_HOME"]
        opencode_state_dir = os.environ["XDG_STATE_HOME"]
        opencode_cache_dir = os.environ["XDG_CACHE_HOME"]
        opencode_tmp_dir = os.environ["TMPDIR"]
        persist_skills_artifacts(artifacts_dir, input_payload)
        base_prompt = prompt
        prompt, feedback, context_count = compose_prompt(request, input_payload, prompt)
        emit_status_preview(
            request["invoke_id"],
            f"Prepared OpenCode task from {context_count} context item(s).",
        )
        repo_cwd = command_cwd(request, input_payload)
        emit_status_preview(
            request["invoke_id"],
            f"Using repository context: {os.path.basename(repo_cwd) or repo_cwd}",
        )
        command = [
            "opencode",
            "run",
            "--format",
            "json",
            "--dir",
            repo_cwd,
        ]
        if input_payload.get("model"):
            command.extend(["--model", str(input_payload["model"])])
        if input_payload.get("agent"):
            command.extend(["--agent", str(input_payload["agent"])])
        variant = effective_variant(input_payload)
        if variant:
            command.extend(["--variant", variant])
        if (
            "session_title" in input_payload
            and input_payload.get("session_title") is not None
        ):
            command.extend(["--title", str(input_payload.get("session_title") or "")])
        if input_payload.get("share"):
            command.append("--share")
        if input_payload.get("dangerously_skip_permissions"):
            command.append("--dangerously-skip-permissions")
        for extra_arg in input_payload.get("extra_args") or []:
            command.append(str(extra_arg))
        command.append(prompt)
        command_result = run_command(
            request,
            command,
            input_payload=input_payload,
            env_overrides={
                "XDG_DATA_HOME": opencode_data_dir,
                "XDG_STATE_HOME": opencode_state_dir,
                "XDG_CACHE_HOME": opencode_cache_dir,
                "TMPDIR": opencode_tmp_dir,
            },
        )
        completed = command_result["completed"]
        response_text = str(command_result.get("response_text") or "").strip()
        session_id = str(command_result.get("session_id") or "").strip()
        share_url = str(command_result.get("share_url") or "").strip()
        usage = command_result.get("usage")
        export_details = None
        if session_id and (
            not response_text or (input_payload.get("share") and not share_url)
        ):
            export_payload = export_session_data(
                session_id,
                repo_cwd,
                command_result["env"],
                bounded_exec=lean_policy.bounded(input_payload)
                or lean_policy.answer_mode(input_payload),
            )
            export_details = extract_export_details(
                export_payload, whole_answer=lean_policy.answer_mode(input_payload)
            )
            if not response_text:
                response_text = str(export_details.get("response_text") or "").strip()
            if not share_url:
                share_url = str(export_details.get("share_url") or "").strip()
        if usage:
            if not export_details and session_id:
                export_payload = export_session_data(
                    session_id,
                    repo_cwd,
                    command_result["env"],
                    bounded_exec=lean_policy.bounded(input_payload)
                    or lean_policy.answer_mode(input_payload),
                )
                export_details = extract_export_details(
                    export_payload, whole_answer=lean_policy.answer_mode(input_payload)
                )
            export_model = str((export_details or {}).get("model") or "").strip()
            if export_model:
                usage["model"] = export_model
            else:
                input_model = str((input_payload or {}).get("model") or "").strip()
                if input_model:
                    usage["model"] = input_model
        missing_response = not response_text and (
            input_payload.get("output_mode") == "compact"
            or input_payload.get("history_mode") == "invocation"
        )
        if not response_text and not missing_response:
            response_text = completed.stdout.strip()
        canonical_response_text = lean_policy.authoritative_response(
            globals(), input_payload, response_text
        )
        response_file = ""
        summary_title = ""
        summary_title_file = ""
        pr_body_text = ""
        pr_body_file = ""
        history_artifacts = None
        usage_json_path = ""
        usage_json_contents = ""
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
        except OSError:
            return failed_result(
                request["invoke_id"],
                "adapter_internal_error",
                "failed persisting OpenCode outputs",
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
        event_error = command_result.get("event_error")
        if event_error is not None or completed.returncode != 0:
            status = "failed"
            code, message = classify_error_from_payload(
                event_error,
                stderr_text=completed.stderr,
                stdout_text=completed.stdout,
                exit_code=completed.returncode,
            )
            if (
                code == "tool_execution_error"
                and completed.returncode != 0
                and not stringify_error_payload(event_error)
            ):
                message = f"command exited with code {completed.returncode}"
            error = protocol_error(code, message)
        if missing_response and error is None:
            status = "failed"
            error = protocol_error(
                "protocol_error",
                "OpenCode authoritative answer events/export are unavailable",
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
                "session_id": (
                    session_id or str((export_details or {}).get("session_id") or "")
                ),
                "share_url": share_url,
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
            f"opencode timed out after {int(err.timeout)} seconds",
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
    except Exception:
        return failed_result(
            request["invoke_id"],
            "adapter_internal_error",
            "unexpected opencode.cli plugin error",
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
