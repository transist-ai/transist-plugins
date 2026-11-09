#!/usr/bin/env python3
import json
import os
from pathlib import Path
import stat
import sys
import time
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from process_runner import CleanupUnconfirmedError, run_process  # noqa: E402
from git_output import compact_result, validate_options  # noqa: E402
import instruction_comments  # noqa: E402

APP_PROTOCOL = "app.v1"
PLUGIN_ID = "git.cli"
PLUGIN_VERSION = "0.1.0"
ACTIONS = [
    "current_branch",
    "list_branches",
    "create_branch",
    "checkout_branch",
    "ensure_branch",
    "commit_worktree",
    "push_current_branch",
    "pull_current_branch",
    "sync_branch_from_pr",
    "delete_branch",
    "worktree_add",
    "worktree_remove",
    "scan_instruction_comments",
]


class BinaryNotFoundError(Exception):
    pass


class CommandRunnerError(Exception):
    """Keep unexpected runner diagnostics out of APP results."""


class CaptureLimitError(RuntimeError):
    """A completed command exceeded the caller's capture budget."""


def command_heartbeat(request: dict) -> Callable[[], None]:
    """Return a main-thread callback emitting safe status at most once per second."""
    last = time.monotonic()

    def tick() -> None:
        nonlocal last
        now = time.monotonic()
        if now - last >= 1:
            last = now
            emit(
                {
                    "type": "app.invoke.progress.v1",
                    "protocol_version": APP_PROTOCOL,
                    "invoke_id": request["invoke_id"],
                    "stream": "status",
                    "delta_lines": 0,
                    "line_preview": "Command is still running...",
                }
            )

    return tick


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


def failed_result(invoke_id, code, error_class, message, retryability="none"):
    return {
        "type": "app.invoke.result.v1",
        "protocol_version": APP_PROTOCOL,
        "invoke_id": invoke_id,
        "status": "failed",
        "output": {},
        "artifact_hints": [],
        "evidence_hints": [],
        "metrics": {},
        "error": {
            "code": code,
            "class": error_class,
            "message": message,
            "retryability": retryability,
        },
    }


def invoke_result(request, status, output, artifact_hints=None, error=None):
    return {
        "type": "app.invoke.result.v1",
        "protocol_version": APP_PROTOCOL,
        "invoke_id": request["invoke_id"],
        "status": status,
        "output": output,
        "artifact_hints": artifact_hints or [],
        "evidence_hints": [],
        "metrics": {},
        **({"error": error} if error is not None else {}),
    }


def command_result(request, completed, output):
    status = "success" if completed.returncode == 0 else "failed"
    error = None
    if completed.returncode != 0:
        error = {
            "code": "tool_execution_error",
            "class": "tool",
            "message": (
                completed.stderr.strip()
                or completed.stdout.strip()
                or f"command exited with code {completed.returncode}"
            ),
            "retryability": "safe",
        }
    return invoke_result(
        request,
        status,
        output,
        [
            artifact_hint("stdout.txt", completed.stdout),
            artifact_hint("stderr.txt", completed.stderr),
        ],
        error,
    )


def command_cwd(request, input_payload):
    return request["paths"]["workspace_dir"]


def run_command(request, command, input_payload=None):
    read_only = command[1] in {"status", "rev-parse"} or (
        command[1] == "branch"
        and (
            "--show-current" in command
            or any(arg.startswith("--format=") for arg in command)
        )
    )
    if not read_only:
        request["_mutation_dispatched"] = True
    try:
        completed = run_process(
            command,
            cwd=command_cwd(request, input_payload),
            timeout=None,
            on_tick=command_heartbeat(request),
        )
    except FileNotFoundError as err:
        raise BinaryNotFoundError(f"{command[0]} CLI not found in PATH") from err
    except CleanupUnconfirmedError:
        raise
    except Exception as err:
        raise CommandRunnerError() from err
    request.setdefault("_diagnostic_commands", []).append(
        {
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "exit_code": completed.returncode,
        }
    )
    checkpoint = request.get("_mutation_output", {})
    if completed.returncode == 0 and checkpoint:
        if command[1] == "commit":
            checkpoint["committed"] = True
        elif command[1:] == ["rev-parse", "HEAD"] and checkpoint.get("committed"):
            checkpoint["commit_sha"] = completed.stdout.strip()
    cap = (input_payload or {}).get("max_capture_bytes")
    if (
        cap is not None
        and sum(
            len(c[stream].encode("utf-8"))
            for c in request["_diagnostic_commands"]
            for stream in ("stdout", "stderr")
        )
        > cap
    ):
        raise CaptureLimitError(
            "command capture exceeds max_capture_bytes; result incomplete; "
            "command may have completed"
        )
    return completed


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
        candidate = os.path.join(
            root_run_dir(request), candidate[len("run://") :].lstrip("/")
        )
    elif candidate.startswith("repo://"):
        candidate = os.path.join(
            request["paths"]["workspace_dir"],
            ".transist",
            candidate[len("repo://") :].lstrip("/"),
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


def read_json_file(request, raw_path, input_payload=None):
    path = workspace_path(request, raw_path, input_payload)
    return json.loads(read_file_text(path, 5 * 1024 * 1024))


def read_file_text(path: str, limit: int) -> str:
    """Read complete bounded regular UTF-8 file data, without newline conversion."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("file input is nonregular or exceeds byte limit")
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("file input exceeds byte limit")
    return raw.decode("utf-8")


def branch_exists(request, input_payload, name):
    completed = run_command(
        request, ["git", "rev-parse", "--verify", f"refs/heads/{name}"], input_payload
    )
    return completed.returncode == 0


def parse_branch_lines(stdout):
    branches = []
    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        parts = raw_line.split("\t")
        if len(parts) != 3:
            continue
        short_name, head_marker, full_name = parts
        branches.append(
            {
                "name": short_name.strip(),
                "current": head_marker.strip() == "*",
                "remote": full_name.strip().startswith("refs/remotes/"),
            }
        )
    return branches


def resolve_commit_message(request, input_payload):
    explicit = (input_payload.get("message") or "").strip()
    if explicit:
        return explicit
    jira_ticket_id = (input_payload.get("jira_ticket_id") or "").strip()
    summary_file = (input_payload.get("summary_file") or "").strip()
    if not jira_ticket_id or not summary_file:
        raise ValueError("set message or provide both jira_ticket_id and summary_file")
    path = workspace_path(request, summary_file, input_payload)
    for line in read_file_text(path, 65536).splitlines():
        summary = " ".join(line.strip().split())
        if summary:
            return f"[{jira_ticket_id}] {summary}"
    raise ValueError(
        f"summary_file '{summary_file}' did not contain a non-empty title line"
    )


def working_tree_has_changes(request, input_payload):
    completed = run_command(request, ["git", "status", "--porcelain"], input_payload)
    return completed.returncode == 0 and bool(completed.stdout.strip()), completed


def resolve_target_branch(input_payload):
    name = str(input_payload.get("name") or "").strip()
    default_name = str(input_payload.get("default_name") or "").strip()
    target = name or default_name
    if not target:
        raise ValueError("name or default_name is required")
    return target


def normalize_command_texts(*completed_commands):
    stdout = []
    stderr = []
    exit_code = 0
    for completed in completed_commands:
        if completed is None:
            continue
        stdout.append(completed.stdout)
        stderr.append(completed.stderr)
        if exit_code == 0 and completed.returncode != 0:
            exit_code = completed.returncode
    return "".join(stdout), "".join(stderr), exit_code


def command_artifacts(stdout, stderr):
    return [
        artifact_hint("stdout.txt", stdout),
        artifact_hint("stderr.txt", stderr),
    ]


def base_branch_from_pr_file(request, input_payload):
    payload = read_json_file(request, input_payload["pr_file"], input_payload)
    pull_request = payload.get("pull_request") or {}
    base_branch = str(pull_request.get("baseRefName") or "").strip()
    if not base_branch:
        raise ValueError("pr_file must contain pull_request.baseRefName")
    return base_branch


def handle_request(request: dict) -> dict:
    """Validate output options and finalize one isolated legacy or compact invoke."""
    local = dict(request)
    local["_diagnostic_commands"] = []
    local["_mutation_dispatched"] = False
    local["_mutation_output"] = {}
    try:
        validate_options(local.get("input") or {})
    except ValueError as err:
        return failed_result(
            request["invoke_id"], "validation_error", "validation", str(err)
        )
    result = handle_action(local)
    if (
        result.get("error", {}).get("retryability") == "safe"
        and local["_mutation_dispatched"]
    ):
        result["error"]["retryability"] = "operator"
        recovery = "; mutation dispatched; inspect effects before retrying"
        result["error"]["message"] += recovery
    try:
        return compact_result(local, result)
    except (TypeError, ValueError, UnicodeError):
        return failed_result(
            request["invoke_id"],
            "adapter_internal_error",
            "internal",
            "Result could not be serialized completely",
            "operator",
        )


def handle_action(request: dict) -> dict:
    """Execute a named Git action and preserve its complete legacy business fields."""
    try:
        action = request.get("action")
        input_payload = request.get("input") or {}

        if action == "scan_instruction_comments":
            try:
                output = instruction_comments.scan(
                    Path(request["paths"]["workspace_dir"]),
                    Path(workspace_path(request, input_payload["output_file"])),
                )
            except instruction_comments.SourceScanError as error:
                return failed_result(
                    request["invoke_id"],
                    "validation_error",
                    "validation",
                    str(error),
                )
            except Exception:
                return failed_result(
                    request["invoke_id"],
                    "validation_error",
                    "validation",
                    "Reviewer scan failed; no partial context published",
                )
            return invoke_result(request, "success", output)

        if action == "current_branch":
            completed = run_command(
                request, ["git", "branch", "--show-current"], input_payload
            )
            branch = completed.stdout.strip()
            return command_result(
                request,
                completed,
                {
                    "ok": completed.returncode == 0,
                    "branch": branch,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "exit_code": completed.returncode,
                },
            )

        if action == "list_branches":
            command = ["git", "branch"]
            if input_payload.get("all"):
                command.append("--all")
            command.extend(
                [
                    "--format=%(refname:short)\t%(HEAD)\t%(refname)",
                ]
            )
            completed = run_command(request, command, input_payload)
            return command_result(
                request,
                completed,
                {
                    "ok": completed.returncode == 0,
                    "branches": parse_branch_lines(completed.stdout),
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "exit_code": completed.returncode,
                },
            )

        if action == "create_branch":
            name = input_payload.get("name")
            if not name:
                return failed_result(
                    request["invoke_id"],
                    "validation_error",
                    "validation",
                    "name is required",
                )
            command = ["git", "branch", str(name)]
            start_point = input_payload.get("start_point")
            if start_point:
                command.append(str(start_point))
            completed = run_command(request, command, input_payload)
            return command_result(
                request,
                completed,
                {
                    "ok": completed.returncode == 0,
                    "branch": str(name),
                    "start_point": start_point or "",
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "exit_code": completed.returncode,
                },
            )

        if action == "checkout_branch":
            name = input_payload.get("name")
            if not name:
                return failed_result(
                    request["invoke_id"],
                    "validation_error",
                    "validation",
                    "name is required",
                )
            create = bool(input_payload.get("create"))
            start_point = input_payload.get("start_point")
            command = ["git", "switch"]
            if create:
                command.extend(["-c", str(name)])
                if start_point:
                    command.append(str(start_point))
            else:
                command.append(str(name))
            completed = run_command(request, command, input_payload)
            return command_result(
                request,
                completed,
                {
                    "ok": completed.returncode == 0,
                    "branch": str(name),
                    "created": create,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "exit_code": completed.returncode,
                },
            )

        if action == "ensure_branch":
            try:
                target = resolve_target_branch(input_payload)
            except ValueError as err:
                return failed_result(
                    request["invoke_id"],
                    "validation_error",
                    "validation",
                    str(err),
                )
            created = False
            if branch_exists(request, input_payload, target):
                completed = run_command(
                    request, ["git", "switch", target], input_payload
                )
            else:
                created = True
                command = ["git", "switch", "-c", target]
                start_point = input_payload.get("start_point")
                if start_point:
                    command.append(str(start_point))
                completed = run_command(request, command, input_payload)
            return command_result(
                request,
                completed,
                {
                    "ok": completed.returncode == 0,
                    "branch": target,
                    "created": created,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "exit_code": completed.returncode,
                },
            )

        if action == "commit_worktree":
            try:
                message = resolve_commit_message(request, input_payload)
            except (OSError, ValueError) as err:
                return failed_result(
                    request["invoke_id"],
                    "validation_error",
                    "validation",
                    str(err),
                )
            request["_mutation_output"] = {
                "ok": False,
                "committed": False,
                "commit_sha": "",
                "message": message,
            }
            combined_stdout = []
            combined_stderr = []
            if input_payload.get("add_all", True):
                add_completed = run_command(
                    request, ["git", "add", "-A"], input_payload
                )
                combined_stdout.append(add_completed.stdout)
                combined_stderr.append(add_completed.stderr)
                if add_completed.returncode != 0:
                    return command_result(
                        request,
                        add_completed,
                        {
                            "ok": False,
                            "committed": False,
                            "commit_sha": "",
                            "message": message,
                            "stdout": "".join(combined_stdout),
                            "stderr": "".join(combined_stderr),
                            "exit_code": add_completed.returncode,
                        },
                    )
            has_changes, status_completed = working_tree_has_changes(
                request, input_payload
            )
            combined_stdout.append(status_completed.stdout)
            combined_stderr.append(status_completed.stderr)
            if status_completed.returncode != 0:
                return command_result(
                    request,
                    status_completed,
                    {
                        "ok": False,
                        "committed": False,
                        "commit_sha": "",
                        "message": message,
                        "stdout": "".join(combined_stdout),
                        "stderr": "".join(combined_stderr),
                        "exit_code": status_completed.returncode,
                    },
                )
            if not has_changes:
                if input_payload.get("allow_no_changes"):
                    return {
                        "type": "app.invoke.result.v1",
                        "protocol_version": APP_PROTOCOL,
                        "invoke_id": request["invoke_id"],
                        "status": "success",
                        "output": {
                            "ok": True,
                            "committed": False,
                            "commit_sha": "",
                            "message": message,
                            "stdout": (
                                "".join(combined_stdout) + "no changes to commit\n"
                            ),
                            "stderr": "".join(combined_stderr),
                            "exit_code": 0,
                        },
                        "artifact_hints": [
                            artifact_hint(
                                "stdout.txt",
                                "".join(combined_stdout) + "no changes to commit\n",
                            ),
                            artifact_hint("stderr.txt", "".join(combined_stderr)),
                        ],
                        "evidence_hints": [],
                        "metrics": {},
                    }
                return failed_result(
                    request["invoke_id"],
                    "validation_error",
                    "validation",
                    "working tree has no staged or unstaged changes to commit",
                )
            commit_completed = run_command(
                request, ["git", "commit", "-m", message], input_payload
            )
            combined_stdout.append(commit_completed.stdout)
            combined_stderr.append(commit_completed.stderr)
            commit_sha = ""
            if commit_completed.returncode == 0:
                rev_completed = run_command(
                    request, ["git", "rev-parse", "HEAD"], input_payload
                )
                combined_stdout.append(rev_completed.stdout)
                combined_stderr.append(rev_completed.stderr)
                if rev_completed.returncode == 0:
                    commit_sha = rev_completed.stdout.strip()
            return command_result(
                request,
                commit_completed,
                {
                    "ok": commit_completed.returncode == 0,
                    "committed": commit_completed.returncode == 0,
                    "commit_sha": commit_sha,
                    "message": message,
                    "stdout": "".join(combined_stdout),
                    "stderr": "".join(combined_stderr),
                    "exit_code": commit_completed.returncode,
                },
            )

        if action == "push_current_branch":
            remote = str(input_payload.get("remote") or "origin")
            branch = str(input_payload.get("branch") or "").strip()
            if not branch:
                current = run_command(
                    request, ["git", "branch", "--show-current"], input_payload
                )
                if current.returncode != 0:
                    return command_result(
                        request,
                        current,
                        {
                            "ok": False,
                            "branch": "",
                            "remote": remote,
                            "stdout": current.stdout,
                            "stderr": current.stderr,
                            "exit_code": current.returncode,
                        },
                    )
                branch = current.stdout.strip()
            command = ["git", "push"]
            if input_payload.get("force_with_lease"):
                command.append("--force-with-lease")
            if input_payload.get("set_upstream", True):
                command.extend(["--set-upstream", remote, branch])
            else:
                command.extend([remote, branch])
            request["_mutation_output"] = {
                "ok": False,
                "branch": branch,
                "remote": remote,
            }
            completed = run_command(request, command, input_payload)
            return command_result(
                request,
                completed,
                {
                    "ok": completed.returncode == 0,
                    "branch": branch,
                    "remote": remote,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "exit_code": completed.returncode,
                },
            )

        if action == "pull_current_branch":
            remote = str(input_payload.get("remote") or "origin")
            branch = str(input_payload.get("branch") or "").strip()
            if not branch:
                current = run_command(
                    request, ["git", "branch", "--show-current"], input_payload
                )
                if current.returncode != 0 or not current.stdout.strip():
                    return command_result(
                        request,
                        current,
                        {
                            "ok": False,
                            "branch": "",
                            "remote": remote,
                            "stdout": current.stdout,
                            "stderr": (
                                current.stderr or "no current branch (detached HEAD?)"
                            ),
                            "exit_code": (
                                current.returncode if current.returncode != 0 else 1
                            ),
                        },
                    )
                branch = current.stdout.strip()
            ff_only = bool(input_payload.get("ff_only"))
            rebase = input_payload.get("rebase")
            rebase = True if rebase is None else bool(rebase)
            autostash = input_payload.get("autostash")
            autostash = True if autostash is None else bool(autostash)
            if ff_only and rebase and "rebase" in input_payload:
                return failed_result(
                    request["invoke_id"],
                    "validation_error",
                    "validation",
                    "ff_only and rebase are mutually exclusive",
                )
            command = ["git", "pull"]
            if ff_only:
                command.append("--ff-only")
            elif rebase:
                command.append("--rebase")
            if autostash:
                command.append("--autostash")
            command.extend([remote, branch])
            completed = run_command(request, command, input_payload)
            return command_result(
                request,
                completed,
                {
                    "ok": completed.returncode == 0,
                    "branch": branch,
                    "remote": remote,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "exit_code": completed.returncode,
                },
            )

        if action == "sync_branch_from_pr":
            try:
                pr_file = str(input_payload.get("pr_file") or "").strip()
                if not pr_file:
                    raise ValueError("pr_file is required")
                remote = str(input_payload.get("remote") or "").strip()
                if not remote:
                    raise ValueError("remote is required")
                base_branch = base_branch_from_pr_file(request, input_payload)
            except (OSError, ValueError, json.JSONDecodeError) as err:
                return failed_result(
                    request["invoke_id"],
                    "validation_error",
                    "validation",
                    str(err),
                )
            switch_completed = run_command(
                request, ["git", "switch", base_branch], input_payload
            )
            if switch_completed.returncode != 0:
                payload = {
                    "ok": False,
                    "branch": base_branch,
                    "remote": remote,
                    "stdout": switch_completed.stdout,
                    "stderr": switch_completed.stderr,
                    "exit_code": switch_completed.returncode,
                }
                return command_result(request, switch_completed, payload)
            pull_completed = run_command(
                request,
                ["git", "pull", remote, base_branch],
                input_payload,
            )
            stdout, stderr, exit_code = normalize_command_texts(
                switch_completed, pull_completed
            )
            payload = {
                "ok": pull_completed.returncode == 0,
                "branch": base_branch,
                "remote": remote,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
            }
            return invoke_result(
                request,
                "success" if pull_completed.returncode == 0 else "failed",
                payload,
                command_artifacts(stdout, stderr),
                (
                    None
                    if pull_completed.returncode == 0
                    else {
                        "code": "tool_execution_error",
                        "class": "tool",
                        "message": (
                            pull_completed.stderr.strip()
                            or pull_completed.stdout.strip()
                            or f"command exited with code {pull_completed.returncode}"
                        ),
                        "retryability": "safe",
                    }
                ),
            )

        if action == "delete_branch":
            try:
                target = resolve_target_branch(input_payload)
            except ValueError as err:
                return failed_result(
                    request["invoke_id"],
                    "validation_error",
                    "validation",
                    str(err),
                )
            allow_missing = bool(input_payload.get("allow_missing"))
            if allow_missing and not branch_exists(request, input_payload, target):
                payload = {
                    "ok": True,
                    "branch": target,
                    "deleted": False,
                    "missing": True,
                    "stdout": f"branch '{target}' not present; skipped delete\n",
                    "stderr": "",
                    "exit_code": 0,
                }
                return invoke_result(
                    request,
                    "success",
                    payload,
                    command_artifacts(payload["stdout"], payload["stderr"]),
                    None,
                )
            command = [
                "git",
                "branch",
                "-D" if input_payload.get("force") else "-d",
                target,
            ]
            completed = run_command(request, command, input_payload)
            payload = {
                "ok": completed.returncode == 0,
                "branch": target,
                "deleted": completed.returncode == 0,
                "missing": False,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "exit_code": completed.returncode,
            }
            return command_result(request, completed, payload)

        if action == "worktree_add":
            path = str(input_payload.get("path") or "").strip()
            if not path:
                return failed_result(
                    request["invoke_id"],
                    "validation_error",
                    "validation",
                    "path is required",
                )
            resolved_path = workspace_path(request, path, input_payload)
            new_branch = str(input_payload.get("new_branch") or "").strip()
            branch = str(input_payload.get("branch") or "").strip()
            start_point = str(input_payload.get("start_point") or "").strip()
            command = ["git", "worktree", "add"]
            if new_branch:
                command.extend(["-b", new_branch])
            command.append(resolved_path)
            if new_branch and start_point:
                command.append(start_point)
            elif not new_branch and branch:
                command.append(branch)
            completed = run_command(request, command, input_payload)
            return command_result(
                request,
                completed,
                {
                    "ok": completed.returncode == 0,
                    "path": resolved_path,
                    "branch": new_branch or branch,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "exit_code": completed.returncode,
                },
            )

        if action == "worktree_remove":
            path = str(input_payload.get("path") or "").strip()
            if not path:
                return failed_result(
                    request["invoke_id"],
                    "validation_error",
                    "validation",
                    "path is required",
                )
            resolved_path = workspace_path(request, path, input_payload)
            command = ["git", "worktree", "remove"]
            if input_payload.get("force"):
                command.append("--force")
            command.append(resolved_path)
            completed = run_command(request, command, input_payload)
            return command_result(
                request,
                completed,
                {
                    "ok": completed.returncode == 0,
                    "path": resolved_path,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "exit_code": completed.returncode,
                },
            )

        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            f"unsupported action {action}",
        )
    except CleanupUnconfirmedError as err:
        result = failed_result(
            request["invoke_id"],
            "protocol_error",
            "protocol",
            "Command process cleanup could not be confirmed",
        )
        result["artifact_hints"] = [
            artifact_hint(name, text)
            for name, text in (("stdout.txt", err.stdout), ("stderr.txt", err.stderr))
            if isinstance(text, str) and len(text.encode("utf-8")) <= 256 * 1024
        ]
        return result
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
            "configuration_error",
            "configuration",
            str(err),
        )
    except CaptureLimitError as err:
        result = failed_result(
            request["invoke_id"], "tool_execution_error", "tool", str(err), "safe"
        )
        if request.get("_mutation_output"):
            commands = request["_diagnostic_commands"]
            result["output"] = {
                **request["_mutation_output"],
                "stdout": "".join(command["stdout"] for command in commands),
                "stderr": "".join(command["stderr"] for command in commands),
                "exit_code": commands[-1]["exit_code"],
            }
        return result
    except ValueError as err:
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            str(err),
        )


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
