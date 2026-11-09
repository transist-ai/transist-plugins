#!/usr/bin/env python3
import datetime
import fnmatch
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from process_runner import CleanupUnconfirmedError, run_process  # noqa: E402
import lean_policy  # noqa: E402
from gate_review import valid_review_request  # noqa: E402

APP_PROTOCOL = "app.v1"
PLUGIN_ID = "codex.cli"
PLUGIN_VERSION = "0.1.0"
ACTIONS = ["exec_task"]
DEFAULT_TIMEOUT_SECONDS = 600
TIMEOUT_GRACE_SECONDS = 5
HEARTBEAT_INTERVAL_SECONDS = 1
APP_INVOKE_PROGRESS_TYPE = "app.invoke.progress.v1"
DEFAULT_IGNORE_PATHS = ("**/_build/**", "**/deps/*/_build/**")
FEEDBACK_CONTEXT_MODES = {"full", "focused", "feedback-only"}


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
    now = datetime.datetime.now(datetime.timezone.utc)
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
            target = os.path.join(request["paths"]["artifacts_dir"], "codex_response")
        base, extension = os.path.splitext(target)
        if not extension:
            base = target
    else:
        base = os.path.join(request["paths"]["artifacts_dir"], "codex_response")
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
            parts.append("### Codex response\n\n" + response_text)
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
    sections = ["# Codex interaction history"]
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
            parts.append("### Prompt sent to Codex\n\n" + composed_prompt)
        response_text = str(entry.get("response_text") or "").strip()
        if response_text:
            parts.append("### Codex response\n\n" + response_text)
        stdout_text = str(entry.get("stdout") or "").strip()
        if stdout_text and stdout_text != response_text:
            parts.append("### Codex stdout\n\n" + stdout_text)
        stderr_text = str(entry.get("stderr") or "").strip()
        if stderr_text:
            parts.append("### Codex stderr\n\n" + stderr_text)
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


def run_command(request, command, stdin_text=None, input_payload=None):
    try:
        timeout_seconds = command_timeout_seconds(input_payload)
        env = os.environ.copy()
        env.setdefault("CI", "1")
        env.setdefault("NO_COLOR", "1")
        if lean_policy.bounded(input_payload or {}) or lean_policy.answer_mode(
            input_payload or {}
        ):
            lean_policy.check_exec(command, env)
        emit_status_preview(request["invoke_id"], "Launching codex...")
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
                        "Codex is still working..."
                        if first_output
                        else "Waiting for Codex output..."
                    ),
                )

        emit_status_preview(request["invoke_id"], "Waiting for Codex output...")
        try:
            return run_process(
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
    except FileNotFoundError as err:
        raise BinaryNotFoundError(f"{command[0]} CLI not found in PATH") from err


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


def compose_prompt(request, input_payload, prompt):
    if lean_policy.bounded(input_payload):
        return lean_policy.compose(globals(), request, input_payload, prompt)
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
                sections.append("# Prior Codex interaction history\n\n" + prior_history)
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
    sections.append("# Task\n\n" + prompt.strip())
    return (
        "\n\n".join(section for section in sections if section.strip()),
        feedback,
        context_count,
    )


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
        persist_skills_artifacts(artifacts_dir, input_payload)
        base_prompt = prompt
        prompt, feedback, context_count = compose_prompt(request, input_payload, prompt)
        emit_status_preview(
            request["invoke_id"],
            f"Prepared Codex task from {context_count} context item(s).",
        )
        repo_cwd = command_cwd(request, input_payload)
        emit_status_preview(
            request["invoke_id"],
            f"Using repository context: {os.path.basename(repo_cwd) or repo_cwd}",
        )
        message_path = os.path.join(artifacts_dir, "last_message.txt")
        t0 = time.time()
        command = [
            "codex",
            "exec",
            "-",
            "--cd",
            repo_cwd,
            "--output-last-message",
            message_path,
            "--color",
            "never",
        ]
        if input_payload.get("model"):
            command.extend(["--model", str(input_payload["model"])])
        if input_payload.get("reasoning_effort"):
            command.extend(
                ["-c", f'model_reasoning_effort={input_payload["reasoning_effort"]}']
            )
        if input_payload.get("profile"):
            command.extend(["--profile", str(input_payload["profile"])])
        if input_payload.get("sandbox"):
            command.extend(["--sandbox", str(input_payload["sandbox"])])
        if input_payload.get("skip_git_repo_check"):
            command.append("--skip-git-repo-check")
        for extra_arg in input_payload.get("extra_args") or []:
            command.append(str(extra_arg))
        completed = run_command(
            request,
            command,
            stdin_text=prompt,
            input_payload=input_payload,
        )
        usage = extract_codex_usage(t0)
        response_text = ""
        has_message = os.path.exists(message_path)
        strict_answer = (
            input_payload.get("output_mode") == "compact"
            or input_payload.get("history_mode") == "invocation"
        )
        if has_message:
            response_text = read_text(message_path)
        elif completed.stdout.strip() and not strict_answer:
            response_text = completed.stdout.strip()
        response_file = ""
        history_artifacts = None
        try:
            if input_payload.get("response_file"):
                response_file = workspace_path(
                    request, input_payload["response_file"], input_payload
                )
                ensure_not_ignored_path(
                    request, input_payload, response_file, "response_file"
                )
                write_text(response_file, response_text)
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
                "failed persisting Codex outputs",
            )
        status = "success" if completed.returncode == 0 else "failed"
        error = None
        if completed.returncode != 0:
            error = protocol_error(
                "tool_execution_error",
                f"command exited with code {completed.returncode}",
            )
        elif strict_answer and not response_text:
            status = "failed"
            error = protocol_error(
                "protocol_error",
                "Codex authoritative last-message artifact is unavailable",
            )
        artifact_hints = [
            artifact_hint("last_message.txt", response_text),
            artifact_hint("stdout.txt", completed.stdout),
            artifact_hint("stderr.txt", completed.stderr),
            artifact_hint("response.txt", response_text),
        ]
        if response_file:
            artifact_hints.append(
                artifact_hint(os.path.basename(response_file), response_text)
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
        return invoke_result(
            request["invoke_id"],
            status,
            output={
                "ok": status == "success",
                "response_text": response_text,
                "response_file": response_file,
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
    except subprocess.TimeoutExpired as err:
        artifacts_dir = request["paths"]["artifacts_dir"]
        message_path = os.path.join(artifacts_dir, "last_message.txt")
        response_text = ""
        if os.path.exists(message_path):
            response_text = read_text(message_path)
        elif (
            str(err.output or "").strip()
            and input_payload.get("output_mode") != "compact"
            and input_payload.get("history_mode") != "invocation"
        ):
            response_text = str(err.output).strip()
        stdout_text = str(err.output or "")
        stderr_text = str(err.stderr or "")
        artifact_hints = [
            artifact_hint("last_message.txt", response_text),
            artifact_hint("stdout.txt", stdout_text),
            artifact_hint("stderr.txt", stderr_text),
            artifact_hint("response.txt", response_text),
        ]
        return failed_result(
            request["invoke_id"],
            "timeout_error",
            f"codex timed out after {command_timeout_seconds(input_payload)} seconds",
            output={
                "ok": False,
                "response_text": response_text,
                "response_file": "",
                "stdout": stdout_text,
                "stderr": stderr_text,
                "exit_code": None,
            },
            artifact_hints=artifact_hints,
        )
    except BinaryNotFoundError as err:
        return failed_result(
            request["invoke_id"],
            "configuration_error",
            str(err),
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
            "unexpected codex.cli plugin error",
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
