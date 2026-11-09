#!/usr/bin/env python3
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import stat
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from process_runner import CleanupUnconfirmedError, run_process  # noqa: E402
from github_output import compact_result, project_output, validate_options  # noqa: E402
import mergeability  # noqa: E402
import review_context  # noqa: E402

APP_PROTOCOL = "app.v1"
PLUGIN_ID = "github.cli"
PLUGIN_VERSION = "0.1.0"
ACTIONS = [
    "pr_list",
    "pr_view",
    "pr_checkout",
    "pr_create",
    "pr_unresolved_comments",
    "pr_check_approvals",
    "pr_check_mergeability",
    "pr_prepare_review_context",
    "pr_merge",
    "pr_publish_review_replies",
    "pr_comment_upsert",
]
PR_VIEW_FIELDS = [
    "number",
    "title",
    "state",
    "url",
    "headRefName",
    "baseRefName",
    "isDraft",
    "author",
    "reviewDecision",
    "mergedAt",
]
PR_VIEW_FIELDS_WITH_LATEST_REVIEWS = PR_VIEW_FIELDS + ["latestReviews"]
SUPPORTED_MERGE_METHODS = {"squash": "--squash"}
REVIEW_REPLY_MARKER = "<!-- transist-review-reply -->"
DISCUSSION_COMMENT_ID_RE = re.compile(r"discussion_r(\d+)")
UNRESOLVED_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 100) {
            pageInfo { hasNextPage }
            nodes {
              id
              databaseId
              body
              path
              diffHunk
              createdAt
              url
              author {
                login
              }
            }
          }
        }
      }
      conversationComments: comments(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          id
          body
          createdAt
          url
          author {
            login
          }
        }
      }
    }
  }
}
""".strip()
RESOLVE_REVIEW_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread {
      id
      isResolved
    }
  }
}
""".strip()


class BinaryNotFoundError(Exception):
    pass


class CommandRunnerError(Exception):
    """Keep unexpected runner diagnostics out of APP results."""


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


def parse_json(stdout, default):
    text = stdout.strip()
    if not text:
        return default
    return json.loads(text)


def command_cwd(request, input_payload):
    return request["paths"]["workspace_dir"]


def run_command(request, command, input_payload=None):
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
    # Read budgets never interrupt a successful mutation before its checkpoint.
    is_read = "--json" in command or (
        "api" in command
        and not any(value in command for value in ("POST", "PATCH", "DELETE"))
        and not any("mutation(" in str(value) for value in command)
    )
    if is_read:
        payload = input_payload or {}
        size = len(completed.stdout.encode("utf-8")) + len(
            completed.stderr.encode("utf-8")
        )
        request["_response_bytes"] = request.get("_response_bytes", 0) + size
        if size > payload.get("max_response_bytes", 5 * 1024 * 1024):
            raise RuntimeError(
                "response exceeds max_response_bytes; retrieval incomplete"
            )
        if request["_response_bytes"] > payload.get(
            "max_total_response_bytes", 20 * 1024 * 1024
        ):
            raise RuntimeError(
                "responses exceed max_total_response_bytes; retrieval incomplete"
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


def current_run_id(request):
    run_name = os.path.basename(root_run_dir(request))
    if run_name.startswith("run_"):
        return run_name[len("run_") :]
    return ""


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


def read_text_file(request, raw_path, input_payload=None, first_line_only=False):
    path = workspace_path(request, raw_path, input_payload)
    text = read_file_bytes(
        path, (input_payload or {}).get("max_plan_bytes", 5 * 1024 * 1024)
    ).decode("utf-8")
    if not first_line_only:
        return text
    for line in text.splitlines():
        value = line.strip()
        if value:
            return value
    return ""


def read_file_bytes(path: str, cap: int) -> bytes:
    """Read bounded regular file bytes; no symlink or FIFO provider inputs."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > cap:
            raise ValueError("file input is nonregular or exceeds byte limit")
        data = handle.read(cap + 1)
    if len(data) > cap:
        raise ValueError("JSON input exceeds max_plan_bytes; input incomplete")
    return data


def read_json_file(request, raw_path, input_payload=None):
    path = workspace_path(request, raw_path, input_payload)
    cap = (input_payload or {}).get("max_plan_bytes", 5 * 1024 * 1024)
    return json.loads(read_file_bytes(path, cap).decode("utf-8"))


def maybe_read_json_file(request, raw_path, input_payload=None):
    path = workspace_path(request, raw_path, input_payload)
    if not path or not os.path.exists(path):
        return {}
    try:
        return json.loads(
            read_file_bytes(
                path, (input_payload or {}).get("max_plan_bytes", 5 * 1024 * 1024)
            ).decode("utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}


def parse_iso8601_timestamp(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def utc_now_rfc3339():
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def include_pr_conversation_comments(input_payload):
    value = input_payload.get("include_pr_conversation_comments")
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise ValueError("include_pr_conversation_comments must be a boolean")


def comment_record(
    *,
    comment_id,
    author,
    body,
    path,
    outdated,
    diff_hunk,
    created_at,
    url,
    source,
    thread_id="",
    review_comment_id="",
):
    return {
        "id": str(comment_id or ""),
        "author": str(author or ""),
        "body": str(body or ""),
        "path": str(path or ""),
        "outdated": bool(outdated),
        "diff_hunk": str(diff_hunk or ""),
        "created_at": str(created_at or ""),
        "url": str(url or ""),
        "source": source,
        "thread_id": str(thread_id or ""),
        "review_comment_id": str(review_comment_id or ""),
    }


def sort_comments(comments):
    return sorted(
        comments,
        key=lambda comment: (
            parse_iso8601_timestamp(comment.get("created_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
            str(comment.get("source") or ""),
            str(comment.get("id") or ""),
        ),
    )


def pr_title_from_jira_issue(request, raw_path, input_payload=None):
    payload = read_json_file(request, raw_path, input_payload)
    key = str(payload.get("key") or "").strip()
    summary = str(payload.get("summary") or "").strip()
    if not key or not summary:
        raise ValueError("jira_issue_file must contain non-empty 'key' and 'summary'")
    template = str((input_payload or {}).get("jira_title_template") or "").strip()
    if not template:
        template = "[{key}] {summary}"
    try:
        return template.format(key=key, summary=summary)
    except (IndexError, KeyError, ValueError) as err:
        raise ValueError(
            "jira_title_template must be a valid format string using {key} and"
            " {summary}"
        ) from err


def pr_body_with_prefix(body, prefix):
    if not str(prefix or "").strip():
        return str(body or "")
    cleaned_body = str(body or "").strip()
    cleaned_prefix = str(prefix or "").strip()
    if not cleaned_prefix:
        return cleaned_body
    if not cleaned_body:
        return cleaned_prefix
    if cleaned_body.startswith(cleaned_prefix):
        return cleaned_body
    return f"{cleaned_prefix}\n\n{cleaned_body}"


def write_json_file(request, raw_path, payload, input_payload=None):
    path = workspace_path(request, raw_path, input_payload)
    if not path:
        return ""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def command_result(request, completed, output, extra_artifacts=None):
    artifacts = [
        artifact_hint("stdout.txt", completed.stdout),
        artifact_hint("stderr.txt", completed.stderr),
    ]
    for extra in extra_artifacts or []:
        artifacts.append(extra)
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
    return invoke_result(request, status, output, artifacts, error)


def repo_parts(request, input_payload, repo_value):
    if repo_value:
        parts = str(repo_value).split("/")
        if len(parts) < 2:
            raise ValueError("repo must look like OWNER/REPO or HOST/OWNER/REPO")
        return parts[-2], parts[-1]
    completed = run_command(
        request, ["gh", "repo", "view", "--json", "owner,name"], input_payload
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or "failed to resolve repo"
        )
    repo_json = parse_json(completed.stdout, {})
    return repo_json["owner"]["login"], repo_json["name"]


def resolve_pr_number(request, input_payload, repo_value, pull_request=None):
    command = ["gh", "pr", "view"]
    if pull_request:
        command.append(str(pull_request))
    if repo_value:
        command.extend(["-R", str(repo_value)])
    command.extend(["--json", "number"])
    completed = run_command(request, command, input_payload)
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or "failed to resolve pull request number"
        )
    return int(parse_json(completed.stdout, {})["number"])


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


def command_artifacts(stdout, stderr, extra_artifacts=None):
    artifacts = [
        artifact_hint("stdout.txt", stdout),
        artifact_hint("stderr.txt", stderr),
    ]
    for extra in extra_artifacts or []:
        artifacts.append(extra)
    return artifacts


def first_url(text):
    for token in text.split():
        if token.startswith("http://") or token.startswith("https://"):
            return token
    return ""


def parse_pr_labels(input_payload):
    raw_labels = input_payload.get("labels")
    if raw_labels is None:
        return []
    if not isinstance(raw_labels, list):
        raise ValueError("labels must be an array of strings")
    labels = []
    for raw_label in raw_labels:
        label = str(raw_label or "").strip()
        if not label:
            continue
        if label not in labels:
            labels.append(label)
    return labels


def ensure_repo_label(request, input_payload, owner, repo, label):
    encoded_label = urllib.parse.quote(label, safe="")
    inspect_command = ["gh", "api", f"repos/{owner}/{repo}/labels/{encoded_label}"]
    inspect_completed = run_command(request, inspect_command, input_payload)
    if inspect_completed.returncode == 0:
        return inspect_completed, None
    stderr = str(inspect_completed.stderr or "")
    stdout = str(inspect_completed.stdout or "")
    if (
        "404" not in stderr
        and "404" not in stdout
        and "not found" not in stderr.lower()
    ):
        return inspect_completed, RuntimeError(
            f"failed to inspect label '{label}':"
            f" {stderr.strip() or stdout.strip() or 'unknown error'}"
        )
    create_command = [
        "gh",
        "api",
        f"repos/{owner}/{repo}/labels",
        "-X",
        "POST",
        "-f",
        f"name={label}",
        "-f",
        "color=1D76DB",
        "-f",
        "description=Pull requests created with AI assistance",
    ]
    create_completed = run_command(request, create_command, input_payload)
    if create_completed.returncode != 0:
        detail = (
            create_completed.stderr.strip()
            or create_completed.stdout.strip()
            or "unknown error"
        )
        return create_completed, RuntimeError(
            f"failed to create label '{label}': {detail}"
        )
    return create_completed, None


def persist_optional_output_file(request, input_payload, payload):
    output_file = input_payload.get("output_file")
    if not output_file:
        return []
    if input_payload.get("output_mode", "legacy") == "compact":
        projected, _ = project_output(
            payload, input_payload, request.get("_diagnostic_commands", [])
        )
        write_json_file(request, output_file, projected, input_payload)
        return []
    path = write_json_file(request, output_file, payload, input_payload)
    return [
        artifact_hint(
            os.path.basename(path), json.dumps(payload, indent=2, sort_keys=True)
        )
    ]


def fetch_pr_view(request, input_payload, include_latest_reviews=False):
    fields = list(
        PR_VIEW_FIELDS_WITH_LATEST_REVIEWS if include_latest_reviews else PR_VIEW_FIELDS
    )
    command = ["gh", "pr", "view"]
    pull_request = input_payload.get("pull_request")
    if pull_request:
        command.append(str(pull_request))
    if input_payload.get("repo"):
        command.extend(["-R", str(input_payload["repo"])])
    command.extend(["--json", ",".join(fields)])
    completed = run_command(request, command, input_payload)
    pull_request_json = {}
    if completed.returncode == 0:
        pull_request_json = parse_json(completed.stdout, {})
    return completed, pull_request_json


def pr_is_merged(pull_request_json):
    return str(pull_request_json.get("state") or "").upper() == "MERGED" or bool(
        pull_request_json.get("mergedAt")
    )


def approved_reviewers_from_latest_reviews(pull_request_json):
    author_login = (
        str(((pull_request_json.get("author") or {}).get("login")) or "")
        .strip()
        .lower()
    )
    latest_by_reviewer = {}
    for review in pull_request_json.get("latestReviews") or []:
        reviewer = str(((review.get("author") or {}).get("login")) or "").strip()
        if not reviewer or reviewer.lower() == author_login:
            continue
        submitted_at = str(review.get("submittedAt") or "")
        current = latest_by_reviewer.get(reviewer.lower())
        if current is None or submitted_at >= current["submitted_at"]:
            latest_by_reviewer[reviewer.lower()] = {
                "login": reviewer,
                "state": str(review.get("state") or "").upper(),
                "submitted_at": submitted_at,
            }
    reviewers = [
        review["login"]
        for review in latest_by_reviewer.values()
        if review["state"] == "APPROVED"
    ]
    return sorted(reviewers, key=str.lower)


def approval_denied_message(request, required_approvals, approval_count):
    run_id = current_run_id(request)
    resume_target = run_id if run_id else "<run-id>"
    return (
        f"PR exit approval denied: need {required_approvals} current approvals, "
        f"found {approval_count}. After more reviewers approve, run "
        f"`transist runs resume {resume_target}`."
    )


def parse_required_approvals(input_payload):
    value = input_payload.get("required_approvals")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("required_approvals must be an integer")
    if value < 0:
        raise ValueError("required_approvals must be greater than or equal to 0")
    return value


def parse_merge_method(input_payload):
    method = str(input_payload.get("method") or "").strip().lower()
    if not method:
        raise ValueError("method is required")
    if method not in SUPPORTED_MERGE_METHODS:
        raise ValueError(
            f"unsupported method '{method}'; supported methods:"
            f" {', '.join(sorted(SUPPORTED_MERGE_METHODS))}"
        )
    return method


def delete_missing_branch_response(completed):
    combined = f"{completed.stdout}\n{completed.stderr}".lower()
    return "reference does not exist" in combined


def compact_text(value):
    return " ".join(str(value or "").split())


def sha256_text(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def review_thread_comment_id_from_url(comment_url):
    match = DISCUSSION_COMMENT_ID_RE.search(str(comment_url or ""))
    if match is None:
        return ""
    return match.group(1)


def review_thread_reply_body(reply):
    return f"{REVIEW_REPLY_MARKER}\n{reply}"


def pr_conversation_reply_body(comment_url, reply):
    if comment_url:
        return f"{REVIEW_REPLY_MARKER}\nReply to [this comment]({comment_url}): {reply}"
    return f"{REVIEW_REPLY_MARKER}\nReply: {reply}"


def run_required_command(request, command, input_payload, failure_prefix):
    completed = run_command(request, command, input_payload)
    if completed.returncode != 0:
        message = (
            completed.stderr.strip() or completed.stdout.strip() or "command failed"
        )
        raise RuntimeError(f"{failure_prefix}: {message}")
    return completed


def create_review_thread_reply(
    request, input_payload, owner, name, pr_number, review_comment_id, body
):
    return run_required_command(
        request,
        [
            "gh",
            "api",
            f"repos/{owner}/{name}/pulls/{pr_number}/comments",
            "-X",
            "POST",
            "-f",
            f"body={body}",
            "-F",
            f"in_reply_to={review_comment_id}",
        ],
        input_payload,
        "failed to post review-thread reply",
    )


def create_pr_conversation_comment(
    request, input_payload, owner, name, pr_number, body
):
    return run_required_command(
        request,
        [
            "gh",
            "api",
            f"repos/{owner}/{name}/issues/{pr_number}/comments",
            "-X",
            "POST",
            "-f",
            f"body={body}",
        ],
        input_payload,
        "failed to post PR conversation comment",
    )


def update_issue_comment(request, input_payload, owner, name, comment_id, body):
    return run_required_command(
        request,
        [
            "gh",
            "api",
            f"repos/{owner}/{name}/issues/comments/{comment_id}",
            "-X",
            "PATCH",
            "-f",
            f"body={body}",
        ],
        input_payload,
        "failed to update PR conversation comment",
    )


def list_pr_conversation_comments(request, input_payload, owner, name, pr_number):
    all_comments = []
    outputs = []
    errors = []
    page = 1
    while True:
        if page > input_payload.get("max_comment_pages", 100):
            raise RuntimeError(
                "comment page budget exhausted; search incomplete; "
                "no mutation performed"
            )
        completed = run_command(
            request,
            [
                "gh",
                "api",
                "--method",
                "GET",
                f"repos/{owner}/{name}/issues/{pr_number}/comments",
                "-f",
                "per_page=100",
                "-f",
                f"page={page}",
            ],
            input_payload,
        )
        outputs.append(completed.stdout)
        errors.append(completed.stderr)
        if completed.returncode != 0:
            message = (
                completed.stderr.strip() or completed.stdout.strip() or "command failed"
            )
            raise RuntimeError(f"failed to list PR conversation comments: {message}")
        payload = parse_json(completed.stdout, [])
        if not isinstance(payload, list):
            raise RuntimeError(
                "failed to list PR conversation comments: malformed comments payload"
            )
        all_comments.extend(payload)
        if len(all_comments) > input_payload.get("max_comments", 10000):
            raise RuntimeError(
                "comment count budget exhausted; search incomplete; "
                "no mutation performed"
            )
        if len(payload) < 100:
            break
        page += 1
    return all_comments, "".join(outputs), "".join(errors)


def require_complete_connection(connection: dict) -> None:
    """Fail rather than advance a snapshot checkpoint past unfetched comments."""
    page_info = connection.get("pageInfo")
    if (page_info or {}).get("hasNextPage") or (
        page_info is None and len(connection.get("nodes", [])) >= 100
    ):
        raise RuntimeError(
            "GitHub comment connection incomplete; snapshot not advanced"
        )


def marker_in_body(marker, body):
    return str(marker or "").strip() and str(marker) in str(body or "")


def append_marker(marker, body):
    cleaned_marker = str(marker or "").strip()
    cleaned_body = str(body or "").strip()
    if not cleaned_marker:
        return cleaned_body
    if cleaned_body.startswith(cleaned_marker):
        return cleaned_body
    if not cleaned_body:
        return cleaned_marker
    return f"{cleaned_marker}\n{cleaned_body}"


def handle_pr_list(request, input_payload):
    fields = "number,title,state,url,headRefName,baseRefName,isDraft,author"
    command = ["gh", "pr", "list", "--json", fields]
    if input_payload.get("repo"):
        command.extend(["-R", str(input_payload["repo"])])
    if input_payload.get("state"):
        command.extend(["--state", str(input_payload["state"])])
    if input_payload.get("base"):
        command.extend(["--base", str(input_payload["base"])])
    if input_payload.get("head"):
        command.extend(["--head", str(input_payload["head"])])
    command.extend(["--limit", str(input_payload.get("limit", 20))])
    completed = run_command(request, command, input_payload)
    pull_requests = []
    if completed.returncode == 0:
        pull_requests = parse_json(completed.stdout, [])
    payload = {
        "ok": completed.returncode == 0,
        "pull_requests": pull_requests,
        "count": len(pull_requests),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
    }
    return command_result(
        request,
        completed,
        payload,
        persist_optional_output_file(request, input_payload, payload),
    )


def handle_pr_view(request, input_payload):
    fields = list(PR_VIEW_FIELDS)
    if input_payload.get("comments"):
        fields.append("comments")
    command = ["gh", "pr", "view"]
    pull_request = input_payload.get("pull_request")
    if pull_request:
        command.append(str(pull_request))
    if input_payload.get("repo"):
        command.extend(["-R", str(input_payload["repo"])])
    command.extend(["--json", ",".join(fields)])
    completed = run_command(request, command, input_payload)
    pull_request_json = {}
    if completed.returncode == 0:
        pull_request_json = parse_json(completed.stdout, {})
    payload = {
        "ok": completed.returncode == 0,
        "pull_request": pull_request_json,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
    }
    return command_result(
        request,
        completed,
        payload,
        persist_optional_output_file(request, input_payload, payload),
    )


def handle_pr_checkout(request, input_payload):
    pull_request = input_payload.get("pull_request")
    if not pull_request:
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            "pull_request is required",
        )
    command = ["gh", "pr", "checkout", str(pull_request)]
    if input_payload.get("repo"):
        command.extend(["-R", str(input_payload["repo"])])
    if input_payload.get("branch_name"):
        command.extend(["--branch", str(input_payload["branch_name"])])
    if input_payload.get("detach"):
        command.append("--detach")
    if input_payload.get("force"):
        command.append("--force")
    completed = run_command(request, command, input_payload)
    payload = {
        "ok": completed.returncode == 0,
        "pull_request": str(pull_request),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
    }
    return command_result(request, completed, payload)


def handle_pr_create(request, input_payload):
    fill = bool(input_payload.get("fill"))
    title = input_payload.get("title")
    body = input_payload.get("body")
    try:
        if not title and input_payload.get("jira_issue_file"):
            title = pr_title_from_jira_issue(
                request, input_payload["jira_issue_file"], input_payload
            )
        if not title and input_payload.get("title_file"):
            title = read_text_file(
                request,
                input_payload["title_file"],
                input_payload,
                first_line_only=True,
            )
        if not body and input_payload.get("body_file"):
            body = read_text_file(
                request,
                input_payload["body_file"],
                input_payload,
                first_line_only=False,
            )
    except (OSError, ValueError, json.JSONDecodeError) as err:
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            str(err),
        )
    try:
        labels = parse_pr_labels(input_payload)
    except ValueError as err:
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            str(err),
        )
    body = pr_body_with_prefix(body, input_payload.get("body_prefix"))
    if not fill and not (title and body):
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            "set fill=true or provide title/body or title_file/body_file",
        )
    command = ["gh", "pr", "create"]
    if input_payload.get("repo"):
        command.extend(["-R", str(input_payload["repo"])])
    if title:
        command.extend(["--title", str(title)])
    if body:
        command.extend(["--body", str(body)])
    if input_payload.get("base"):
        command.extend(["--base", str(input_payload["base"])])
    if input_payload.get("head"):
        command.extend(["--head", str(input_payload["head"])])
    if input_payload.get("draft"):
        command.append("--draft")
    if fill:
        command.append("--fill")
    completed = run_command(request, command, input_payload)
    label_probe_completed = None
    label_create_completed = None
    label_apply_completed = None
    if completed.returncode == 0 and labels:
        try:
            owner, repo = repo_parts(request, input_payload, input_payload.get("repo"))
        except CleanupUnconfirmedError:
            raise
        except (RuntimeError, ValueError) as err:
            return failed_result(
                request["invoke_id"],
                "tool_execution_error",
                "tool",
                f"pull request created, but label setup failed: {err}",
                "safe",
            )
        for label in labels:
            label_completed, label_error = ensure_repo_label(
                request, input_payload, owner, repo, label
            )
            if label_probe_completed is None:
                label_probe_completed = label_completed
            label_create_completed = label_completed
            if label_error is not None:
                return failed_result(
                    request["invoke_id"],
                    "tool_execution_error",
                    "tool",
                    f"pull request created, but label setup failed: {label_error}",
                    "safe",
                )
        pr_ref = first_url(completed.stdout)
        if not pr_ref:
            pr_ref = str(
                resolve_pr_number(request, input_payload, input_payload.get("repo"))
            )
        edit_command = ["gh", "pr", "edit", pr_ref]
        if input_payload.get("repo"):
            edit_command.extend(["-R", str(input_payload["repo"])])
        for label in labels:
            edit_command.extend(["--add-label", label])
        label_apply_completed = run_command(request, edit_command, input_payload)
        if label_apply_completed.returncode != 0:
            return failed_result(
                request["invoke_id"],
                "tool_execution_error",
                "tool",
                "pull request created, but applying labels failed: "
                + (
                    label_apply_completed.stderr.strip()
                    or label_apply_completed.stdout.strip()
                    or "unknown error"
                ),
                "safe",
            )
    payload = {
        "ok": completed.returncode == 0,
        "url": first_url(completed.stdout),
        "labels": labels,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
    }
    stdout, stderr, exit_code = normalize_command_texts(
        completed,
        label_probe_completed,
        label_create_completed,
        label_apply_completed,
    )
    aggregate_completed = subprocess.CompletedProcess(
        command, exit_code, stdout=stdout, stderr=stderr
    )
    return command_result(
        request,
        aggregate_completed,
        payload,
        persist_optional_output_file(request, input_payload, payload),
    )


def handle_pr_unresolved_comments(request, input_payload):
    try:
        include_conversation_comments = include_pr_conversation_comments(input_payload)
        owner, name = repo_parts(request, input_payload, input_payload.get("repo"))
        number = resolve_pr_number(
            request,
            input_payload,
            input_payload.get("repo"),
            input_payload.get("pull_request"),
        )
    except CleanupUnconfirmedError:
        raise
    except (RuntimeError, ValueError) as err:
        return failed_result(
            request["invoke_id"],
            (
                "validation_error"
                if isinstance(err, ValueError)
                else "tool_execution_error"
            ),
            "validation" if isinstance(err, ValueError) else "tool",
            str(err),
            "safe" if isinstance(err, RuntimeError) else "none",
        )
    previous_payload = {}
    previous_cutoff = ""
    if include_conversation_comments and input_payload.get("output_file"):
        previous_payload = maybe_read_json_file(
            request, input_payload.get("output_file"), input_payload
        )
        previous_cutoff = str(
            previous_payload.get("last_successful_fetch_completed_at") or ""
        ).strip()
    previous_cutoff_dt = parse_iso8601_timestamp(previous_cutoff)
    command = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={UNRESOLVED_THREADS_QUERY}",
        "-f",
        f"owner={owner}",
        "-f",
        f"name={name}",
        "-F",
        f"number={number}",
    ]
    completed = run_command(request, command, input_payload)
    comments = []
    review_thread_comment_count = 0
    pr_conversation_comment_count = 0
    fetch_completed_at = previous_cutoff if include_conversation_comments else ""
    if completed.returncode == 0:
        payload = parse_json(completed.stdout, {})
        pull_request = (
            payload.get("data", {}).get("repository", {}).get("pullRequest", {})
        )
        if payload.get("errors") or not pull_request:
            raise RuntimeError(
                "GitHub GraphQL retrieval incomplete; snapshot not advanced"
            )
        require_complete_connection(pull_request.get("reviewThreads", {}))
        if include_conversation_comments:
            require_complete_connection(pull_request.get("conversationComments", {}))
        threads = pull_request.get("reviewThreads", {}).get("nodes", [])
        for thread in threads:
            if thread.get("isResolved"):
                continue
            require_complete_connection(thread.get("comments", {}))
            thread_node_id = str(thread.get("id") or "")
            for comment in thread.get("comments", {}).get("nodes", []):
                database_id = comment.get("databaseId")
                review_comment_id = str(database_id) if database_id is not None else ""
                comments.append(
                    comment_record(
                        comment_id=comment.get("id", ""),
                        author=(comment.get("author") or {}).get("login", ""),
                        body=comment.get("body", ""),
                        path=comment.get("path", ""),
                        outdated=thread.get("isOutdated", False),
                        diff_hunk=comment.get("diffHunk", ""),
                        created_at=comment.get("createdAt", ""),
                        url=comment.get("url", ""),
                        source="review_thread",
                        thread_id=thread_node_id,
                        review_comment_id=review_comment_id,
                    )
                )
                review_thread_comment_count += 1
        if include_conversation_comments:
            for comment in pull_request.get("conversationComments", {}).get(
                "nodes", []
            ):
                created_at = str(comment.get("createdAt") or "")
                created_at_dt = parse_iso8601_timestamp(created_at)
                if (
                    previous_cutoff_dt is not None
                    and created_at_dt is not None
                    and created_at_dt <= previous_cutoff_dt
                ):
                    continue
                comments.append(
                    comment_record(
                        comment_id=comment.get("id", ""),
                        author=(comment.get("author") or {}).get("login", ""),
                        body=comment.get("body", ""),
                        path="",
                        outdated=False,
                        diff_hunk="",
                        created_at=created_at,
                        url=comment.get("url", ""),
                        source="pr_conversation",
                    )
                )
                pr_conversation_comment_count += 1
        comments = sort_comments(comments)
        if len(comments) > input_payload.get("max_comments", 10000):
            raise RuntimeError("comment count budget exhausted; snapshot not advanced")
        if include_conversation_comments:
            fetch_completed_at = utc_now_rfc3339()
    payload = {
        "ok": completed.returncode == 0,
        "comments": comments,
        "count": len(comments),
        "review_thread_comment_count": review_thread_comment_count,
        "pr_conversation_comment_count": pr_conversation_comment_count,
        "pr_conversation_comments_after": (
            previous_cutoff if include_conversation_comments else ""
        ),
        "last_successful_fetch_completed_at": fetch_completed_at,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
    }
    return command_result(
        request,
        completed,
        payload,
        persist_optional_output_file(request, input_payload, payload),
    )


def handle_pr_check_approvals(request, input_payload):
    try:
        required_approvals = parse_required_approvals(input_payload)
    except ValueError as err:
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            str(err),
        )
    completed, pull_request_json = fetch_pr_view(
        request, input_payload, include_latest_reviews=True
    )
    if completed.returncode != 0:
        payload = {
            "ok": False,
            "pull_request": {},
            "approval_count": 0,
            "approval_ready": False,
            "approved_reviewers": [],
            "required_approvals": required_approvals,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "exit_code": completed.returncode,
        }
        return command_result(request, completed, payload)
    approved_reviewers = approved_reviewers_from_latest_reviews(pull_request_json)
    payload = {
        "ok": (
            pr_is_merged(pull_request_json)
            or len(approved_reviewers) >= required_approvals
        ),
        "pull_request": pull_request_json,
        "approval_count": len(approved_reviewers),
        "approved_reviewers": approved_reviewers,
        "required_approvals": required_approvals,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "exit_code": completed.returncode,
    }
    payload["approval_ready"] = payload["ok"]
    extra_artifacts = persist_optional_output_file(request, input_payload, payload)
    if payload["ok"] or input_payload.get("soft_fail", False):
        return invoke_result(
            request,
            "success",
            payload,
            command_artifacts(payload["stdout"], payload["stderr"], extra_artifacts),
            None,
        )
    return invoke_result(
        request,
        "failed",
        payload,
        command_artifacts(payload["stdout"], payload["stderr"], extra_artifacts),
        {
            "code": "tool_execution_error",
            "class": "tool",
            "message": approval_denied_message(
                request,
                required_approvals,
                payload["approval_count"],
            ),
            "retryability": "safe",
        },
    )


def handle_pr_check_mergeability(request: dict, input_payload: dict) -> dict:
    """Fetch bounded PR state and expose helper-equivalent merge readiness as data."""
    allowed = mergeability.normalize_statuses(
        input_payload.get("allowed_merge_state_statuses", [])
    )
    command = ["gh", "pr", "view"]
    pull_request = str(input_payload.get("pull_request") or "").strip()
    if pull_request:
        command.append(pull_request)
    if input_payload.get("repo"):
        command.extend(["-R", input_payload["repo"]])
    command.extend(["--json", ",".join(mergeability.PR_VIEW_FIELDS)])
    pr, error = None, ""
    for attempt in range(mergeability.MAX_PR_VIEW_ATTEMPTS):
        completed = run_command(request, command, input_payload)
        if completed.returncode == 0:
            try:
                pr = json.loads(completed.stdout)
                if not isinstance(pr, dict):
                    raise ValueError
                break
            except ValueError:
                pr, error, retryable = None, "invalid gh pr view JSON object", True
        else:
            error = "gh pr view failed"
            combined = f"{completed.stderr}\n{completed.stdout}".lower()
            retryable = any(
                m in combined for m in mergeability.TRANSIENT_GH_FAILURE_MARKERS
            )
        if not retryable or attempt == mergeability.MAX_PR_VIEW_ATTEMPTS - 1:
            break
        time.sleep(mergeability.RETRY_DELAY_SECONDS * (attempt + 1))
    payload = mergeability.evaluate(pr or {}, allowed)
    payload.update(
        stdout=completed.stdout, stderr=completed.stderr, exit_code=completed.returncode
    )
    if pr is None:
        payload.update(
            ok=False,
            merge_ready=False,
            reasons=[error],
            exit_code=completed.returncode or 2,
        )
    artifacts = persist_optional_output_file(request, input_payload, payload)
    # A completed negative decision is data so workflows can branch on a Boolean.
    # Operational failures remain failed invocations, even with an output checkpoint.
    return invoke_result(
        request,
        "success" if pr is not None else "failed",
        payload,
        command_artifacts(payload["stdout"], payload["stderr"], artifacts),
        (
            None
            if pr is not None
            else {
                "code": "tool_execution_error",
                "class": "tool",
                "message": error,
                "retryability": "safe",
            }
        ),
    )


def handle_pr_prepare_review_context(request: dict, input_payload: dict) -> dict:
    """Project/merge staged review files atomically; preserve exact pending requests."""
    try:

        def read(name: str) -> dict:
            """Read resolved regular JSON data within the helper's byte bound."""
            return review_context.read(
                Path(workspace_path(request, input_payload[name]))
            )

        current, pr = read("input_file"), read("pr_file")
        pending = read("pending_file") if input_payload.get("pending_file") else {}
        context = review_context.project(current, pending, pr)
        review_context.write(
            Path(workspace_path(request, input_payload["output_file"])), context
        )
    except (OSError, ValueError, KeyError):
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            "Review projection failed; pending requests were not discarded",
        )
    return invoke_result(request, "success", {"ok": True, **context}, [])


def handle_pr_merge(request, input_payload):
    try:
        method = parse_merge_method(input_payload)
    except ValueError as err:
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            str(err),
        )
    view_completed, initial_pull_request = fetch_pr_view(
        request, input_payload, include_latest_reviews=True
    )
    if view_completed.returncode != 0:
        payload = {
            "ok": False,
            "pull_request": {},
            "merged": False,
            "already_merged": False,
            "remote_branch_deleted": False,
            "method": method,
            "stdout": view_completed.stdout,
            "stderr": view_completed.stderr,
            "exit_code": view_completed.returncode,
        }
        return command_result(request, view_completed, payload)
    if pr_is_merged(initial_pull_request):
        payload = {
            "ok": True,
            "pull_request": initial_pull_request,
            "merged": True,
            "already_merged": True,
            "remote_branch_deleted": False,
            "method": method,
            "stdout": view_completed.stdout,
            "stderr": view_completed.stderr,
            "exit_code": 0,
        }
        return invoke_result(
            request,
            "success",
            payload,
            command_artifacts(
                payload["stdout"],
                payload["stderr"],
                persist_optional_output_file(request, input_payload, payload),
            ),
            None,
        )
    merge_command = ["gh", "pr", "merge", str(initial_pull_request.get("number") or "")]
    if input_payload.get("repo"):
        merge_command.extend(["-R", str(input_payload["repo"])])
    merge_command.append(SUPPORTED_MERGE_METHODS[method])
    merge_completed = run_command(request, merge_command, input_payload)
    if merge_completed.returncode != 0:
        stdout, stderr, exit_code = normalize_command_texts(
            view_completed, merge_completed
        )
        payload = {
            "ok": False,
            "pull_request": initial_pull_request,
            "merged": False,
            "already_merged": False,
            "remote_branch_deleted": False,
            "method": method,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
        }
        return invoke_result(
            request,
            "failed",
            payload,
            command_artifacts(stdout, stderr),
            {
                "code": "tool_execution_error",
                "class": "tool",
                "message": (
                    merge_completed.stderr.strip()
                    or merge_completed.stdout.strip()
                    or f"command exited with code {merge_completed.returncode}"
                ),
                "retryability": "safe",
            },
        )
    refresh_completed, refreshed_pull_request = fetch_pr_view(
        request,
        input_payload,
        include_latest_reviews=True,
    )
    if refresh_completed.returncode != 0:
        stdout, stderr, exit_code = normalize_command_texts(
            view_completed,
            merge_completed,
            refresh_completed,
        )
        payload = {
            "ok": False,
            "pull_request": initial_pull_request,
            "merged": True,
            "already_merged": False,
            "remote_branch_deleted": False,
            "method": method,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
        }
        return invoke_result(
            request,
            "failed",
            payload,
            command_artifacts(stdout, stderr),
            {
                "code": "tool_execution_error",
                "class": "tool",
                "message": (
                    refresh_completed.stderr.strip()
                    or refresh_completed.stdout.strip()
                    or f"command exited with code {refresh_completed.returncode}"
                ),
                "retryability": "safe",
            },
        )
    remote_branch_deleted = False
    delete_completed = None
    if input_payload.get("delete_remote_branch"):
        head_ref_name = str(refreshed_pull_request.get("headRefName") or "").strip()
        if not head_ref_name:
            remote_branch_deleted = True
        else:
            owner, name = repo_parts(request, input_payload, input_payload.get("repo"))
            branch_ref = urllib.parse.quote(head_ref_name, safe="")
            delete_command = [
                "gh",
                "api",
                "-X",
                "DELETE",
                f"repos/{owner}/{name}/git/refs/heads/{branch_ref}",
            ]
            delete_completed = run_command(request, delete_command, input_payload)
            if delete_completed.returncode == 0 or delete_missing_branch_response(
                delete_completed
            ):
                remote_branch_deleted = True
            else:
                stdout, stderr, exit_code = normalize_command_texts(
                    view_completed,
                    merge_completed,
                    refresh_completed,
                    delete_completed,
                )
                payload = {
                    "ok": False,
                    "pull_request": refreshed_pull_request,
                    "merged": pr_is_merged(refreshed_pull_request),
                    "already_merged": False,
                    "remote_branch_deleted": False,
                    "method": method,
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": exit_code,
                }
                return invoke_result(
                    request,
                    "failed",
                    payload,
                    command_artifacts(stdout, stderr),
                    {
                        "code": "tool_execution_error",
                        "class": "tool",
                        "message": (
                            delete_completed.stderr.strip()
                            or delete_completed.stdout.strip()
                            or f"command exited with code {delete_completed.returncode}"
                        ),
                        "retryability": "safe",
                    },
                )
    stdout, stderr, exit_code = normalize_command_texts(
        view_completed,
        merge_completed,
        refresh_completed,
        delete_completed,
    )
    payload = {
        "ok": pr_is_merged(refreshed_pull_request),
        "pull_request": refreshed_pull_request,
        "merged": pr_is_merged(refreshed_pull_request),
        "already_merged": False,
        "remote_branch_deleted": remote_branch_deleted,
        "method": method,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
    }
    return invoke_result(
        request,
        "success",
        payload,
        command_artifacts(
            stdout,
            stderr,
            persist_optional_output_file(request, input_payload, payload),
        ),
        None,
    )


def resolve_review_thread(request, input_payload, owner, name, thread_id):
    """Call the resolveReviewThread GraphQL mutation for a review thread node ID."""
    return run_required_command(
        request,
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={RESOLVE_REVIEW_THREAD_MUTATION}",
            "-f",
            f"threadId={thread_id}",
        ],
        input_payload,
        f"failed to resolve review thread {thread_id}",
    )


def parse_resolve_addressed_threads(input_payload):
    """Return True if resolve_addressed_threads is truthy (bool or string 'true')."""
    value = input_payload.get("resolve_addressed_threads")
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return bool(value)


def reply_json(text: str) -> object:
    """Parse publication data without duplicate keys or non-JSON constants."""

    def pairs(items: list) -> dict:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate publication JSON key")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise ValueError("invalid publication JSON constant")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def publication_plan(value: object, limit: int) -> list[dict]:
    """Validate the entire existing reply plan before any provider mutation."""
    if not isinstance(value, dict) or not isinstance(value.get("responses"), list):
        raise ValueError(
            "response_plan_file must contain a JSON object with responses[]"
        )
    responses = value["responses"]
    if len(responses) > limit:
        raise ValueError("response plan exceeds max_replies; no replies posted")
    threads = value.get("addressed_thread_ids", [])
    if (
        not isinstance(threads, list)
        or any(not isinstance(t, str) or not t.strip() for t in threads)
        or len(threads) > limit
    ):
        raise ValueError("invalid or excessive addressed_thread_ids")
    seen = set()
    planned = []
    for entry in responses:
        if not isinstance(entry, dict):
            raise ValueError("response entry must be an object")
        for field in (
            "comment_id",
            "source",
            "reply",
            "url",
            "review_comment_id",
            "thread_id",
        ):
            if field in entry and not isinstance(entry[field], str):
                raise ValueError(f"response {field} must be a string")
        comment_id = entry.get("comment_id", "")
        source = entry.get("source", "")
        reply = compact_text(entry.get("reply"))
        if not comment_id.strip() or comment_id != comment_id.strip() or not reply:
            raise ValueError("response requires exact comment_id and nonempty reply")
        if comment_id in seen:
            raise ValueError("duplicate response comment_id")
        seen.add(comment_id)
        if source not in ("review_thread", "pr_conversation"):
            raise ValueError("response source must be review_thread or pr_conversation")
        url = compact_text(entry.get("url"))
        rc_id = entry.get("review_comment_id", "")
        if rc_id and (not re.fullmatch(r"[1-9][0-9]*", rc_id)):
            raise ValueError("review_comment_id must be a positive decimal string")
        if source == "review_thread" and not rc_id:
            rc_id = review_thread_comment_id_from_url(url)
        if rc_id and not re.fullmatch(r"[1-9][0-9]*", rc_id):
            raise ValueError("review-comment URL must identify a positive decimal ID")
        threaded = source == "review_thread" and bool(rc_id)
        planned.append(
            {
                "comment_id": comment_id,
                "source": source,
                "reply_hash": sha256_text(reply),
                "target": rc_id if threaded else "",
                "surface": "pulls" if threaded else "issues",
                "delivery_mode": (
                    "review_thread_reply"
                    if threaded
                    else (
                        "pr_conversation_fallback"
                        if source == "review_thread"
                        else "pr_conversation_comment"
                    )
                ),
                "legacy_body": (
                    review_thread_reply_body(reply)
                    if threaded
                    else pr_conversation_reply_body(url, reply)
                ),
            }
        )
    return planned


def reply_publication_identity(owner: str, name: str, number: int, item: dict) -> str:
    """Bind an exact request ID to repo/PR/source/target, never paths or time/body."""
    identity = [
        owner.lower(),
        name.lower(),
        number,
        item["source"],
        item["comment_id"],
        item["surface"],
        item["target"],
    ]
    return sha256_text(json.dumps(identity, ensure_ascii=False, separators=(",", ":")))


def fetch_publication_comments(
    request: dict, payload: dict, owner: str, name: str, number: int
) -> list[dict]:
    """Read both REST collections completely within fixed page/count and byte bounds.

    The PR review-comment collection includes replies in resolved/outdated threads;
    unlike the unresolved-context query it has no nested connection truncation.
    No provider retries or polling: cancellation remains owned by process_runner.
    """
    comments = []
    for surface in ("issues", "pulls"):
        seen = set()
        for page in range(1, 101):
            completed = run_required_command(
                request,
                [
                    "gh",
                    "api",
                    "--method",
                    "GET",
                    f"repos/{owner}/{name}/{surface}/{number}/comments",
                    "-f",
                    "per_page=100",
                    "-f",
                    f"page={page}",
                ],
                payload,
                "reply reconciliation retrieval incomplete",
            )
            try:
                batch = reply_json(completed.stdout)
            except ValueError as err:
                raise RuntimeError(
                    "reply reconciliation JSON malformed; retrieval incomplete"
                ) from err
            if not isinstance(batch, list) or len(batch) > 100:
                raise RuntimeError(
                    "reply reconciliation page malformed; retrieval incomplete"
                )
            for comment in batch:
                if (
                    not isinstance(comment, dict)
                    or type(comment.get("id")) is not int
                    or comment["id"] <= 0
                    or comment["id"] in seen
                    or not isinstance(comment.get("body"), str)
                    or not isinstance(comment.get("html_url"), str)
                    or not comment["html_url"].startswith("https://")
                    or (
                        "in_reply_to_id" in comment
                        and (
                            type(comment["in_reply_to_id"]) is not int
                            or comment["in_reply_to_id"] <= 0
                        )
                    )
                ):
                    raise RuntimeError(
                        "reply reconciliation comment malformed; retrieval incomplete"
                    )
                seen.add(comment["id"])
                comments.append({**comment, "surface": surface})
            if len(comments) > 10000:
                raise RuntimeError(
                    "reply reconciliation count budget exhausted; retrieval incomplete"
                )
            if len(batch) < 100:
                break
        else:
            raise RuntimeError(
                "reply reconciliation page budget exhausted; retrieval incomplete"
            )
    return comments


def reconcile_publication(item: dict, comments: list[dict]) -> dict | None:
    """Require an exact marker line, full body and destination; reject conflicts."""
    matches = []
    for comment in comments:
        body = comment["body"]
        lines = body.splitlines()
        if lines and lines[0] == REVIEW_REPLY_MARKER:
            for line in lines[1:]:
                if line.startswith(
                    "<!-- transist-review-publication:"
                ) and not re.fullmatch(
                    r"<!-- transist-review-publication:[0-9a-f]{64} -->", line
                ):
                    raise RuntimeError("remote reply publication marker malformed")
        target_matches = comment["surface"] == item["surface"] and (
            item["surface"] == "issues"
            or str(comment.get("in_reply_to_id", "")) == item["target"]
        )
        if item["marker"] in lines:
            if body != item["body"] or not target_matches:
                raise RuntimeError(
                    "reply publication identity conflicts with remote body or target"
                )
            matches.append(comment)
        elif target_matches and body == item["legacy_body"]:
            raise RuntimeError(
                "legacy reply has no exact publication identity; "
                "manual reconciliation required"
            )
    if len(matches) > 1:
        raise RuntimeError("multiple remote replies have the same publication identity")
    return matches[0] if matches else None


def persist_publication_files(
    request: dict, payload: dict, state: dict, output: dict
) -> list[dict]:
    """Stage/serialize all destinations first, then atomically replace each file.

    State is replaced last so output preparation failures preserve local state.
    The runtime commits managed document revisions together only after success.
    """
    projected, _ = project_output(
        output, payload, request.get("_diagnostic_commands", [])
    )
    pending = []
    artifacts = []
    try:
        for field, data in (("output_file", projected), ("state_file", state)):
            if not payload.get(field):
                continue
            path = Path(workspace_path(request, payload[field], payload))
            encoded = (
                json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n"
            ).encode("utf-8")
            os.makedirs(path.parent, exist_ok=True)
            parent = review_context.parent_fd(path)
            temporary = ".reply-publication-" + os.urandom(16).hex()
            pending.append((parent, temporary, path.name))
            try:
                mode = os.stat(path.name, dir_fd=parent, follow_symlinks=False).st_mode
            except FileNotFoundError:
                mode = stat.S_IFREG
            if not stat.S_ISREG(mode):
                raise ValueError("publication output must be a regular file")
            fd = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if (
                field == "output_file"
                and payload.get("output_mode", "legacy") == "legacy"
            ):
                artifacts.append(
                    artifact_hint(os.path.basename(path), encoded.decode().rstrip("\n"))
                )
        for parent, temporary, name in pending:
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    except OSError as err:
        raise RuntimeError("reply publication files could not be published") from err
    finally:
        for parent, temporary, _ in pending:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            finally:
                os.close(parent)
    return artifacts


def handle_pr_publish_review_replies(request, input_payload):
    response_plan_file = str(input_payload.get("response_plan_file") or "").strip()
    if not response_plan_file:
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            "response_plan_file is required",
        )
    try:
        response_plan = reply_json(
            read_text_file(request, response_plan_file, input_payload)
        )
        planned_responses = publication_plan(
            response_plan, input_payload.get("max_replies", 1000)
        )
        state_file = input_payload.get("state_file", "")
        state_path = workspace_path(request, state_file, input_payload)
        state_payload = {"responses": {}}
        if state_path and os.path.exists(state_path):
            state_payload = reply_json(
                read_text_file(request, state_file, input_payload)
            )
        if not isinstance(state_payload, dict) or not isinstance(
            state_payload.get("responses"), dict
        ):
            raise ValueError("state_file must contain an object with responses{}")
        if any(not isinstance(v, dict) for v in state_payload["responses"].values()):
            raise ValueError("state response records must be objects")
        for record in state_payload["responses"].values():
            for field in ("reply_hash", "publication_id", "body_hash"):
                if field in record and (
                    not isinstance(record[field], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", record[field])
                ):
                    raise ValueError("state publication hashes must be SHA-256 strings")
            for field in ("posted_url", "delivery_mode", "posted_at"):
                if field in record and not isinstance(record[field], str):
                    raise ValueError("state publication metadata must be strings")
        # Resolve all output data paths before any provider mutation.
        output_path = workspace_path(
            request, input_payload.get("output_file"), input_payload
        )
        if state_path and output_path and state_path == output_path:
            raise ValueError("state_file and output_file must be distinct")
        if (
            output_path
            and os.path.exists(output_path)
            and not stat.S_ISREG(os.stat(output_path, follow_symlinks=False).st_mode)
        ):
            raise ValueError("publication output must be a regular file")
    except (OSError, json.JSONDecodeError, ValueError) as err:
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            str(err),
        )

    try:
        owner, name = repo_parts(request, input_payload, input_payload.get("repo"))
        pr_number = resolve_pr_number(
            request,
            input_payload,
            input_payload.get("repo"),
            input_payload.get("pull_request"),
        )
    except (KeyError, TypeError, OverflowError) as err:
        raise RuntimeError("repository or PR metadata incomplete") from err

    if (
        any(
            not isinstance(part, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
            for part in (owner, name)
        )
        or pr_number <= 0
    ):
        raise ValueError("invalid repository or pull request identity")

    tracked_responses = state_payload.setdefault("responses", {})

    pending = []
    for item in planned_responses:
        identity = reply_publication_identity(owner, name, pr_number, item)
        item["identity"] = identity
        item["marker"] = f"<!-- transist-review-publication:{identity} -->"
        item["body"] = item["legacy_body"] + "\n" + item["marker"]
        previous = tracked_responses.get(item["comment_id"], {})
        if previous.get("publication_id") == identity and previous.get(
            "body_hash"
        ) != sha256_text(item["body"]):
            raise RuntimeError("reply publication identity conflicts with local body")
        item["local_duplicate"] = (
            bool(previous.get("posted_url"))
            and previous.get("reply_hash") == item["reply_hash"]
            and (
                not previous.get("publication_id")
                or previous["publication_id"] == identity
            )
        )
        if not item["local_duplicate"]:
            pending.append(item)
    if pending:
        comments = fetch_publication_comments(
            request, input_payload, owner, name, pr_number
        )
        # Check every pending identity/conflict before posting even the first reply.
        for item in pending:
            item["remote"] = reconcile_publication(item, comments)

    results = []
    posted_count = 0
    skipped_count = 0
    stdout_parts = []
    stderr_parts = []
    last_exit_code = 0

    for item in planned_responses:
        comment_id, source = item["comment_id"], item["source"]
        previous = tracked_responses.get(comment_id, {})
        if item["local_duplicate"]:
            skipped_count += 1
            results.append(
                {
                    "comment_id": comment_id,
                    "source": source,
                    "status": "skipped",
                    "reason": "duplicate_reply",
                    "posted_url": compact_text(previous.get("posted_url")),
                }
            )
            continue

        delivery_mode = item["delivery_mode"]
        posted_payload = item.get("remote")
        if posted_payload is not None:
            skipped_count += 1
        elif item["surface"] == "pulls":
            posted_completed = create_review_thread_reply(
                request,
                input_payload,
                owner,
                name,
                pr_number,
                item["target"],
                item["body"],
            )
        else:
            posted_completed = create_pr_conversation_comment(
                request, input_payload, owner, name, pr_number, item["body"]
            )
        if posted_payload is None:
            try:
                posted_payload = reply_json(posted_completed.stdout)
            except ValueError as err:
                raise RuntimeError(
                    "reply POST response malformed; reconcile on re-entry"
                ) from err
            if (
                not isinstance(posted_payload, dict)
                or type(posted_payload.get("id")) is not int
                or posted_payload["id"] <= 0
                or not isinstance(posted_payload.get("html_url"), str)
                or not posted_payload["html_url"].startswith("https://")
                or posted_payload.get("body") != item["body"]
                or (
                    item["surface"] == "pulls"
                    and (
                        type(posted_payload.get("in_reply_to_id")) is not int
                        or str(posted_payload["in_reply_to_id"]) != item["target"]
                    )
                )
            ):
                raise RuntimeError(
                    "reply POST response incomplete; reconcile on re-entry"
                )
            posted_count += 1
            stdout_parts.append(posted_completed.stdout)
            stderr_parts.append(posted_completed.stderr)
            last_exit_code = posted_completed.returncode
        posted_url = compact_text(
            posted_payload.get("html_url") or posted_payload.get("url")
        )
        tracked_responses[comment_id] = {
            "reply_hash": item["reply_hash"],
            "publication_id": item["identity"],
            "body_hash": sha256_text(item["body"]),
            "posted_at": utc_now_rfc3339(),
            "posted_url": posted_url,
            "delivery_mode": delivery_mode,
        }
        results.append(
            {
                "comment_id": comment_id,
                "source": source,
                "status": "skipped" if item.get("remote") is not None else "posted",
                **(
                    {"reason": "remote_duplicate_reply"}
                    if item.get("remote") is not None
                    else {}
                ),
                "delivery_mode": delivery_mode,
                "posted_url": posted_url,
            }
        )

    state_payload["updated_at"] = utc_now_rfc3339()

    # Resolve addressed threads when the caller signals code changes were committed.
    resolved_count = 0
    resolution_errors = []
    if parse_resolve_addressed_threads(input_payload):
        addressed_thread_ids = response_plan.get("addressed_thread_ids") or []
        for thread_id in addressed_thread_ids:
            thread_id = compact_text(thread_id)
            if not thread_id:
                continue
            try:
                resolve_completed = resolve_review_thread(
                    request, input_payload, owner, name, thread_id
                )
                stdout_parts.append(resolve_completed.stdout)
                stderr_parts.append(resolve_completed.stderr)
                resolved_count += 1
            except CleanupUnconfirmedError:
                raise
            except RuntimeError as err:
                resolution_errors.append(str(err))

    payload = {
        "ok": True,
        "generated_at": utc_now_rfc3339(),
        "count": len(results),
        "posted_count": posted_count,
        "skipped_count": skipped_count,
        "resolved_count": resolved_count,
        "resolution_errors": resolution_errors,
        "responses": results,
        "stdout": "".join(stdout_parts),
        "stderr": "".join(stderr_parts),
        "exit_code": last_exit_code,
    }
    result = invoke_result(
        request,
        "success",
        payload,
        command_artifacts(
            payload["stdout"],
            payload["stderr"],
        ),
        None,
    )
    # Serialization/projection is checked before changing either destination.
    json.dumps(compact_result(request, dict(result)), allow_nan=False)
    result["artifact_hints"].extend(
        persist_publication_files(request, input_payload, state_payload, payload)
    )
    return result


def handle_pr_comment_upsert(request, input_payload):
    marker = str(input_payload.get("marker") or "").strip()
    if not marker:
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            "marker is required",
        )

    body = str(input_payload.get("body") or "").strip()
    if not body and input_payload.get("body_file"):
        try:
            body = read_text_file(
                request,
                input_payload["body_file"],
                input_payload,
                first_line_only=False,
            )
        except (OSError, ValueError, json.JSONDecodeError) as err:
            return failed_result(
                request["invoke_id"],
                "validation_error",
                "validation",
                str(err),
            )
    if not body:
        return failed_result(
            request["invoke_id"],
            "validation_error",
            "validation",
            "body or body_file is required",
        )

    create_if_missing = bool(input_payload.get("create_if_missing", True))
    owner, name = repo_parts(request, input_payload, input_payload.get("repo"))
    pr_number = resolve_pr_number(
        request,
        input_payload,
        input_payload.get("repo"),
        input_payload.get("pull_request"),
    )

    comments, list_stdout, list_stderr = list_pr_conversation_comments(
        request,
        input_payload,
        owner,
        name,
        pr_number,
    )
    matching_comment = None
    for comment in comments:
        if marker_in_body(marker, comment.get("body")):
            matching_comment = comment

    target_body = append_marker(marker, body)
    operation = "unchanged"
    comment_id = ""
    comment_url = ""
    command_stdout = [list_stdout]
    command_stderr = [list_stderr]
    command_exit_code = 0

    if matching_comment is not None:
        comment_id = str(matching_comment.get("id") or "")
        comment_url = str(
            matching_comment.get("html_url") or matching_comment.get("url") or ""
        )
        existing_body = str(matching_comment.get("body") or "").strip()
        if existing_body != target_body:
            updated = update_issue_comment(
                request,
                input_payload,
                owner,
                name,
                comment_id,
                target_body,
            )
            updated_payload = parse_json(updated.stdout, {})
            comment_id = str(updated_payload.get("id") or comment_id)
            comment_url = str(
                updated_payload.get("html_url")
                or updated_payload.get("url")
                or comment_url
            )
            operation = "updated"
            command_stdout.append(updated.stdout)
            command_stderr.append(updated.stderr)
            command_exit_code = updated.returncode
    elif create_if_missing:
        created = create_pr_conversation_comment(
            request,
            input_payload,
            owner,
            name,
            pr_number,
            target_body,
        )
        created_payload = parse_json(created.stdout, {})
        comment_id = str(created_payload.get("id") or "")
        comment_url = str(
            created_payload.get("html_url") or created_payload.get("url") or ""
        )
        operation = "created"
        command_stdout.append(created.stdout)
        command_stderr.append(created.stderr)
        command_exit_code = created.returncode

    payload = {
        "ok": True,
        "operation": operation,
        "created": operation == "created",
        "updated": operation == "updated",
        "unchanged": operation == "unchanged",
        "comment_id": comment_id,
        "comment_url": comment_url,
        "marker": marker,
        "stdout": "".join(command_stdout),
        "stderr": "".join(command_stderr),
        "exit_code": command_exit_code,
    }
    return invoke_result(
        request,
        "success",
        payload,
        command_artifacts(
            payload["stdout"],
            payload["stderr"],
            persist_optional_output_file(request, input_payload, payload),
        ),
        None,
    )


def handle_request(request: dict) -> dict:
    """Validate budgets, isolate diagnostics, and preserve legacy output by default."""
    local = dict(request)
    local["_diagnostic_commands"] = []
    local["_response_bytes"] = 0
    try:
        validate_options(local.get("input") or {})
    except ValueError as err:
        return failed_result(
            request["invoke_id"], "validation_error", "validation", str(err)
        )
    result = handle_action(local)
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
    """Execute a GitHub action with original controls, reply state and checkpoints."""
    try:
        action = request.get("action")
        input_payload = request.get("input") or {}

        if action == "pr_list":
            return handle_pr_list(request, input_payload)
        if action == "pr_view":
            return handle_pr_view(request, input_payload)
        if action == "pr_checkout":
            return handle_pr_checkout(request, input_payload)
        if action == "pr_create":
            return handle_pr_create(request, input_payload)
        if action == "pr_unresolved_comments":
            return handle_pr_unresolved_comments(request, input_payload)
        if action == "pr_check_approvals":
            return handle_pr_check_approvals(request, input_payload)
        if action == "pr_check_mergeability":
            return handle_pr_check_mergeability(request, input_payload)
        if action == "pr_prepare_review_context":
            return handle_pr_prepare_review_context(request, input_payload)
        if action == "pr_merge":
            return handle_pr_merge(request, input_payload)
        if action == "pr_publish_review_replies":
            return handle_pr_publish_review_replies(request, input_payload)
        if action == "pr_comment_upsert":
            return handle_pr_comment_upsert(request, input_payload)
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
    except RuntimeError as err:
        return failed_result(
            request["invoke_id"],
            "tool_execution_error",
            "tool",
            str(err),
            "safe",
        )
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
