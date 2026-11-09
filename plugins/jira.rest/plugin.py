#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jira_output import compact_result, project_output  # noqa: E402

APP_PROTOCOL = "app.v1"
PLUGIN_ID = "jira.rest"
PLUGIN_VERSION = "0.1.0"
ACTIONS = [
    "get_issue",
    "search_issues",
    "create_issue",
    "transition_issue",
    "download_attachments",
]

DEFAULT_ALLOWED_EXTENSIONS = ["md", "txt", "log", "json", "yaml", "yml", "csv", "pdf"]
DEFAULT_ALLOWED_MIME_PREFIXES = [
    "text/",
    "application/json",
    "application/yaml",
    "application/pdf",
]
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class ResponseLimitError(RuntimeError):
    """A Jira JSON response was not retrieved completely within its byte budget."""


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def read_frame():
    line = sys.stdin.readline()
    if not line:
        return None
    return json.loads(line)


def invoke_result(invoke_id, status, output, error):
    return {
        "type": "app.invoke.result.v1",
        "protocol_version": APP_PROTOCOL,
        "invoke_id": invoke_id,
        "status": status,
        "output": output,
        "artifact_hints": [],
        "evidence_hints": [],
        "metrics": {},
        **({"error": error} if error is not None else {}),
    }


def failed_result(
    invoke_id,
    message,
    code="tool_execution_error",
    error_class="tool",
    retryability="safe",
):
    return invoke_result(
        invoke_id,
        "failed",
        {},
        {
            "code": code,
            "class": error_class,
            "message": message,
            "retryability": retryability,
        },
    )


def adf_paragraph(text):
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def adf_to_text(node):
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        parts = []
        for child in node.get("content", []) or []:
            parts.append(adf_to_text(child))
        if node.get("type") in {
            "paragraph",
            "heading",
            "bulletList",
            "orderedList",
            "listItem",
        }:
            parts.append("\n")
        return "".join(parts)
    if isinstance(node, list):
        return "".join(adf_to_text(item) for item in node)
    return ""


def select_transition(transitions, requested_name):
    wanted = str(requested_name or "").strip().lower()
    exact_name_matches = []
    target_status_matches = []
    for transition in transitions or []:
        transition_name = str(transition.get("name") or "").strip()
        target_status = str(((transition.get("to") or {}).get("name") or "")).strip()
        if transition_name.lower() == wanted:
            exact_name_matches.append(transition)
        if target_status.lower() == wanted:
            target_status_matches.append(transition)
    if exact_name_matches:
        return exact_name_matches[0], None
    if len(target_status_matches) == 1:
        return target_status_matches[0], None
    if len(target_status_matches) > 1:
        names = ", ".join(
            sorted(
                str(match.get("name") or "").strip() for match in target_status_matches
            )
        )
        return (
            None,
            (
                f"target status '{requested_name}' matches multiple Jira transitions:"
                f" {names}; use an exact transition name"
            ),
        )
    available = ", ".join(
        sorted(
            name
            for name in (
                str(transition.get("name") or "").strip()
                for transition in transitions or []
            )
            if name
        )
    )
    if available:
        return (
            None,
            (
                f"transition or target status '{requested_name}' not found "
                f"(available transitions: {available})"
            ),
        )
    return None, f"transition or target status '{requested_name}' not found"


def jira_request(
    method: str,
    url: str,
    auth_header: str,
    payload: dict | None = None,
    *,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    mutation_state: dict | None = None,
) -> dict:
    """Read bounded JSON; mark supplied invocation state before mutation dispatch."""
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": auth_header,
        },
    )
    if mutation_state is not None:
        mutation_state["_mutation_dispatched"] = True
    with urllib.request.urlopen(request) as response:
        # Jira returns 204 No Content (empty body) for successful POSTs such as
        # /transitions; treat any empty/whitespace body as an empty JSON object
        # so callers that ignore the return value do not blow up on json.loads.
        chunks = []
        total = 0
        while True:
            chunk = response.read(min(65536, max_response_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_response_bytes:
                raise ResponseLimitError(
                    "Jira response exceeds max_response_bytes; retrieval incomplete"
                )
            chunks.append(chunk)
        raw = b"".join(chunks).decode("utf-8")
        if not raw.strip():
            return {}
        return json.loads(raw)


def jira_site_url(input_payload):
    value = str(
        (input_payload or {}).get("site_url") or os.environ.get("JIRA_SITE_URL", "")
    ).strip()
    if not value:
        raise RuntimeError(
            "site_url is required or set JIRA_SITE_URL in the environment"
        )
    return value.rstrip("/")


def root_run_dir(request):
    run_root = (request.get("paths") or {}).get("run_root") or ""
    if not run_root:
        raise RuntimeError("run_root is required when using run:// paths")
    run_root = os.path.normpath(run_root)
    while os.path.basename(os.path.dirname(run_root)) == "contained_runs":
        run_root = os.path.dirname(os.path.dirname(run_root))
    run_name = os.path.basename(run_root)
    parent = os.path.dirname(run_root)
    if run_name.startswith("run_"):
        root_run_id = run_name[len("run_") :].split("__", 1)[0]
        return os.path.join(parent, f"run_{root_run_id}")
    return run_root


def workspace_path(request, path, _input_payload=None):
    candidate = str(path or "").strip()
    if (
        not candidate
        or "\0" in candidate
        or "\\" in candidate
        or ".." in candidate.split("/")
    ):
        raise ValueError("invalid file data path")
    if candidate.startswith("run://"):
        candidate = os.path.join(
            root_run_dir(request), candidate[len("run://") :].lstrip("/")
        )
    elif candidate.startswith("repo://"):
        candidate = os.path.join(".transist", candidate[len("repo://") :].lstrip("/"))
    elif "://" in candidate:
        raise ValueError("unsupported file path scheme")
    target = (
        Path(candidate)
        if os.path.isabs(candidate)
        else Path(request["paths"]["workspace_dir"]) / candidate
    )
    if any(p.is_symlink() for p in (target, *target.parents)):
        raise ValueError("file data paths must not traverse symlinks")
    if target.exists() and not (target.is_file() or target.is_dir()):
        raise ValueError("file data paths must be regular files or directories")
    return str(target)


def read_json_file(path: str, limit: int = DEFAULT_MAX_RESPONSE_BYTES) -> dict:
    """Read bounded regular UTF-8 JSON without following a final symlink."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("JSON file is nonregular or exceeds byte limit")
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("JSON file exceeds byte limit")
    return json.loads(raw.decode("utf-8"))


def map_attachment(attachment):
    author = attachment.get("author") or {}
    return {
        "id": str(attachment.get("id") or ""),
        "filename": str(attachment.get("filename") or ""),
        "mime_type": str(attachment.get("mimeType") or ""),
        "size": int(attachment.get("size") or 0),
        "created": str(attachment.get("created") or ""),
        "author_display_name": str(
            author.get("displayName") or author.get("emailAddress") or ""
        ),
        "content_url": str(attachment.get("content") or ""),
        "thumbnail_url": str(attachment.get("thumbnail") or ""),
    }


def extract_attachments(issue):
    items = (issue.get("fields") or {}).get("attachment") or []
    return [map_attachment(item) for item in items if isinstance(item, dict)]


def issue_payload(issue):
    fields = issue.get("fields") or {}
    description_text = adf_to_text(fields.get("description")).strip()
    return {
        "key": issue.get("key", ""),
        "summary": fields.get("summary", ""),
        "status": (fields.get("status") or {}).get("name") or "",
        "description_text": description_text,
        "url": "",
        "attachments": extract_attachments(issue),
        "raw": issue,
    }


def attachment_passes_policy(
    attachment, allowed_extensions, allowed_mime_prefixes, max_bytes
):
    size = int(attachment.get("size") or 0)
    if max_bytes and size > max_bytes:
        return False, "size_exceeds_max"
    filename = str(attachment.get("filename") or "").lower()
    mime_type = str(attachment.get("mime_type") or "").lower()
    ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
    ext_list = [str(e).lower().lstrip(".") for e in (allowed_extensions or [])]
    mime_list = [str(m).lower() for m in (allowed_mime_prefixes or [])]
    if not ext_list and not mime_list:
        return True, ""
    ext_pass = bool(ext_list) and ext in ext_list
    mime_pass = bool(mime_list) and any(mime_type.startswith(p) for p in mime_list)
    if ext_pass or mime_pass:
        return True, ""
    if not ext_list:
        return False, "mime_not_allowed"
    if not mime_list:
        return False, "extension_not_allowed"
    return False, "extension_and_mime_not_allowed"


def safe_filename(name, fallback):
    raw = str(name or "").strip() or fallback
    raw = raw.replace("/", "_").replace("\\", "_")
    allowed = []
    for ch in raw:
        if ch.isalnum() or ch in {".", "-", "_"}:
            allowed.append(ch)
        else:
            allowed.append("_")
    cleaned = "".join(allowed).strip("._") or fallback
    return cleaned


def unique_path(output_dir, filename, attachment_id):
    candidate = os.path.join(output_dir, filename)
    if not os.path.lexists(candidate):
        return candidate
    stem, ext = os.path.splitext(filename)
    prefix = safe_filename(attachment_id, "copy")
    index = 1
    while True:
        candidate = os.path.join(output_dir, f"{prefix}_{index}_{stem}{ext}")
        if not os.path.lexists(candidate):
            return candidate
        index += 1


def fetch_to_file(url, auth_header, dest, max_bytes):
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": auth_header, "Accept": "*/*"},
    )
    with urllib.request.urlopen(request) as response:
        total = 0
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes and total > max_bytes:
                    handle.close()
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
                    raise RuntimeError("size_exceeds_max")
                handle.write(chunk)
    return total


def skip_record(attachment, reason):
    return {
        "id": str(attachment.get("id") or ""),
        "filename": str(attachment.get("filename") or ""),
        "mime_type": str(attachment.get("mime_type") or ""),
        "size": int(attachment.get("size") or 0),
        "reason": reason,
    }


def download_attachments_from_issue(
    jira_issue,
    auth_header,
    output_dir,
    allowed_extensions,
    allowed_mime_prefixes,
    max_bytes,
):
    attachments = jira_issue.get("attachments") or []
    os.makedirs(output_dir, exist_ok=True)
    downloaded = []
    skipped = []
    for attachment in attachments:
        passes, reason = attachment_passes_policy(
            attachment, allowed_extensions, allowed_mime_prefixes, max_bytes
        )
        if not passes:
            skipped.append(skip_record(attachment, reason))
            continue
        attachment_id = str(attachment.get("id") or "")
        filename = safe_filename(
            attachment.get("filename"),
            fallback=f"attachment_{attachment_id or 'unknown'}",
        )
        dest = unique_path(output_dir, filename, attachment_id)
        content_url = str(attachment.get("content_url") or "")
        if not content_url:
            skipped.append(skip_record(attachment, "fetch_failed"))
            continue
        try:
            total = fetch_to_file(content_url, auth_header, dest, max_bytes)
        except RuntimeError as err:
            reason = (
                "size_exceeds_max" if "size_exceeds_max" in str(err) else "fetch_failed"
            )
            skipped.append(skip_record(attachment, reason))
            continue
        except Exception:
            skipped.append(skip_record(attachment, "fetch_failed"))
            continue
        downloaded.append(
            {
                "id": attachment_id,
                "filename": os.path.basename(dest),
                "local_path": os.path.basename(dest),
                "mime_type": str(attachment.get("mime_type") or ""),
                "size": total,
            }
        )
    return downloaded, skipped


def write_output_file(request, input_payload, payload, invoke_id):
    output_file = (input_payload or {}).get("output_file")
    if not output_file:
        return None
    try:
        payload, _ = project_output(payload, input_payload)
        target = workspace_path(request, output_file)
        target = os.path.expanduser(target)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except (OSError, RuntimeError) as exc:
        return failed_result(
            invoke_id,
            f"failed to write output_file '{output_file}': {exc}",
            "tool_execution_error",
            "tool",
            "safe",
        )
    return None


def fetch_issue(request, input_payload, auth_header, issue_key):
    include_attachments = (input_payload or {}).get("include_attachments")
    if include_attachments is None:
        include_attachments = True
    requested_fields = list(
        (input_payload or {}).get("fields") or ["summary", "description", "status"]
    )
    if include_attachments and "attachment" not in requested_fields:
        requested_fields.append("attachment")
    query = urllib.parse.urlencode({"fields": ",".join(requested_fields)})
    site_url = jira_site_url(input_payload)
    return jira_request(
        "GET",
        f"{site_url}/rest/api/3/issue/{issue_key}?{query}",
        auth_header,
        max_response_bytes=input_payload.get(
            "max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES
        ),
    )


def handle_request(request: dict) -> dict:
    """Validate opt-in mode and budgets, then finalize normalized compact output."""
    payload = request.get("input") or {}
    if payload.get("output_mode", "legacy") not in ("legacy", "compact"):
        return failed_result(
            request["invoke_id"],
            "output_mode must be legacy or compact",
            "validation_error",
            "validation",
            "none",
        )
    for name in ("max_response_bytes", "max_diagnostic_bytes"):
        value = payload.get(name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            return failed_result(
                request["invoke_id"],
                f"{name} must be a positive integer",
                "validation_error",
                "validation",
                "none",
            )
    local = dict(request)
    local["_mutation_dispatched"] = False
    local["_mutation_output"] = {}
    try:
        for name in ("output_file", "jira_issue_file", "output_dir", "manifest_file"):
            if payload.get(name):
                workspace_path(local, payload[name])
    except ValueError:
        return failed_result(
            request["invoke_id"],
            "invalid file data path",
            "validation_error",
            "validation",
            "none",
        )
    result = handle_action(local)
    if result["status"] == "failed" and local["_mutation_dispatched"]:
        if result.get("error", {}).get("retryability") == "safe":
            result["error"]["retryability"] = "operator"
            recovery = "; mutation dispatched; inspect effects before retrying"
            result["error"]["message"] += recovery
        if not result["output"] and local["_mutation_output"]:
            result["output"] = local["_mutation_output"]
    try:
        return compact_result(local, result)
    except (TypeError, ValueError, UnicodeError):
        return failed_result(
            request["invoke_id"],
            "Result could not be serialized completely",
            "adapter_internal_error",
            "internal",
            "operator",
        )


def handle_action(request: dict) -> dict:
    """Execute one Jira action without discarding normalized handoff fields."""
    invoke_id = request.get("invoke_id") or "generated-by-process-plugin"
    action = request.get("action")
    input_payload = request.get("input") or {}

    auth_header = os.environ.get("JIRA_AUTH_HEADER", "").strip()
    if not auth_header:
        return failed_result(
            invoke_id,
            "JIRA_AUTH_HEADER is required",
            "auth_error",
            "auth",
            "operator",
        )

    try:
        if action == "get_issue":
            issue_key = input_payload.get("issue_key")
            if not issue_key:
                return failed_result(
                    invoke_id,
                    "issue_key is required",
                    "validation_error",
                    "validation",
                    "none",
                )
            response = fetch_issue(request, input_payload, auth_header, issue_key)
            payload = issue_payload(response)
            if not payload["url"]:
                payload["url"] = f"{jira_site_url(input_payload)}/browse/{issue_key}"
            write_failure = write_output_file(
                request, input_payload, payload, invoke_id
            )
            if write_failure is not None:
                return write_failure
            return invoke_result(
                invoke_id,
                "success",
                payload,
                None,
            )

        if action == "search_issues":
            jql = input_payload.get("jql")
            if not jql:
                return failed_result(
                    invoke_id,
                    "jql is required",
                    "validation_error",
                    "validation",
                    "none",
                )
            requested_fields = list(
                input_payload.get("fields") or ["summary", "status"]
            )
            request_payload = {
                "jql": jql,
                "maxResults": int(input_payload.get("max_results", 10)),
                "fields": requested_fields,
            }
            next_page_token = input_payload.get("next_page_token")
            if next_page_token:
                request_payload["nextPageToken"] = str(next_page_token)
            response = jira_request(
                "POST",
                f"{jira_site_url(input_payload)}/rest/api/3/search/jql",
                auth_header,
                request_payload,
                max_response_bytes=input_payload.get(
                    "max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES
                ),
            )
            site_url = jira_site_url(input_payload)
            issues = []
            for issue in response.get("issues", []):
                issues.append(
                    {
                        "key": issue.get("key", ""),
                        "summary": (issue.get("fields") or {}).get("summary", ""),
                        "status": (
                            ((issue.get("fields") or {}).get("status") or {}).get(
                                "name", ""
                            )
                        ),
                        "url": f"{site_url}/browse/{issue.get('key', '')}",
                    }
                )
            output_file = input_payload.get("output_file")
            if output_file:
                try:
                    target = workspace_path(request, output_file)
                    target = os.path.expanduser(target)
                    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                    with open(target, "w", encoding="utf-8") as handle:
                        handoff = response
                        if input_payload.get("output_mode", "legacy") == "compact":
                            handoff, _ = project_output(
                                {
                                    "issues": issues,
                                    "count": len(issues),
                                    "next_page_token": str(
                                        response.get("nextPageToken") or ""
                                    ),
                                    "is_last": bool(response.get("isLast", True)),
                                    "raw": response,
                                },
                                input_payload,
                            )
                        json.dump(handoff, handle, indent=2)
                        handle.write("\n")
                except (OSError, RuntimeError) as exc:
                    return failed_result(
                        invoke_id,
                        f"failed to write output_file '{output_file}': {exc}",
                        "tool_execution_error",
                        "tool",
                        "safe",
                    )
            return invoke_result(
                invoke_id,
                "success",
                {
                    "issues": issues,
                    "count": len(issues),
                    "next_page_token": str(response.get("nextPageToken") or ""),
                    "is_last": bool(response.get("isLast", True)),
                    "raw": response,
                },
                None,
            )

        if action == "create_issue":
            project_key = input_payload.get("project_key")
            issue_type = input_payload.get("issue_type")
            summary = str(input_payload.get("summary") or "").strip()
            summary_file = str(input_payload.get("summary_file") or "").strip()
            if not summary and summary_file:
                try:
                    resolved_summary_file = workspace_path(request, summary_file)
                    with open(resolved_summary_file, "r", encoding="utf-8") as handle:
                        summary = handle.read().strip()
                except (OSError, RuntimeError) as err:
                    return failed_result(
                        invoke_id,
                        f"could not read summary_file '{summary_file}': {err}",
                        "validation_error",
                        "validation",
                        "none",
                    )
            if not project_key or not issue_type or not summary:
                return failed_result(
                    invoke_id,
                    "project_key, issue_type, and summary (or summary_file) are"
                    " required",
                    "validation_error",
                    "validation",
                    "none",
                )
            fields = {
                "project": {"key": str(project_key)},
                "issuetype": {"name": str(issue_type)},
                "summary": str(summary),
            }

            description = str(input_payload.get("description") or "").strip()
            description_file = str(input_payload.get("description_file") or "").strip()
            if not description and description_file:
                try:
                    resolved_description_file = workspace_path(
                        request, description_file
                    )
                    with open(
                        resolved_description_file, "r", encoding="utf-8"
                    ) as handle:
                        description = handle.read().strip()
                except (OSError, RuntimeError) as err:
                    return failed_result(
                        invoke_id,
                        f"could not read description_file '{description_file}': {err}",
                        "validation_error",
                        "validation",
                        "none",
                    )
            if description:
                fields["description"] = adf_paragraph(str(description))

            parent_key = str(input_payload.get("parent") or "").strip()
            if parent_key:
                fields["parent"] = {"key": parent_key}

            labels = input_payload.get("labels")
            if labels:
                if isinstance(labels, str):
                    labels = [
                        label.strip() for label in labels.split(",") if label.strip()
                    ]
                fields["labels"] = [str(label) for label in labels if label]

            assignee_account_id = str(
                input_payload.get("assignee_account_id") or ""
            ).strip()
            if assignee_account_id:
                fields["assignee"] = {"accountId": assignee_account_id}

            response = jira_request(
                "POST",
                f"{jira_site_url(input_payload)}/rest/api/3/issue",
                auth_header,
                {"fields": fields},
                max_response_bytes=input_payload.get(
                    "max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES
                ),
                mutation_state=request,
            )
            site_url = jira_site_url(input_payload)
            created_key = response.get("key", "")
            output = {
                "key": created_key,
                "id": str(response.get("id", "")),
                "url": f"{site_url}/browse/{created_key}",
                "raw": response,
            }
            request["_mutation_output"] = output
            write_failure = write_output_file(request, input_payload, output, invoke_id)
            if write_failure is not None:
                return write_failure
            return invoke_result(
                invoke_id,
                "success",
                output,
                None,
            )

        if action == "transition_issue":
            issue_key = input_payload.get("issue_key")
            status_name = input_payload.get("status_name")
            if not issue_key or not status_name:
                return failed_result(
                    invoke_id,
                    "issue_key and status_name are required",
                    "validation_error",
                    "validation",
                    "none",
                )
            current = fetch_issue(request, input_payload, auth_header, issue_key)
            current_status = ((current.get("fields") or {}).get("status") or {}).get(
                "name"
            ) or ""
            if current_status.strip().lower() != str(status_name).strip().lower():
                transitions_response = jira_request(
                    "GET",
                    f"{jira_site_url(input_payload)}/rest/api/3/issue/"
                    f"{issue_key}/transitions",
                    auth_header,
                    max_response_bytes=input_payload.get(
                        "max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES
                    ),
                )
                selected, selection_error = select_transition(
                    transitions_response.get("transitions", []) or [], status_name
                )
                if selected is None:
                    return failed_result(
                        invoke_id,
                        f"{selection_error} for issue '{issue_key}'",
                        "validation_error",
                        "validation",
                        "none",
                    )
                checkpoint = issue_payload(current)
                checkpoint["previous_status"] = current_status
                checkpoint["url"] = f"{jira_site_url(input_payload)}/browse/{issue_key}"
                request["_mutation_output"] = checkpoint
                jira_request(
                    "POST",
                    f"{jira_site_url(input_payload)}/rest/api/3/issue/"
                    f"{issue_key}/transitions",
                    auth_header,
                    {"transition": {"id": selected.get("id")}},
                    max_response_bytes=input_payload.get(
                        "max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES
                    ),
                    mutation_state=request,
                )
                current = fetch_issue(request, input_payload, auth_header, issue_key)
            payload = issue_payload(current)
            payload["previous_status"] = current_status
            if not payload["url"]:
                payload["url"] = f"{jira_site_url(input_payload)}/browse/{issue_key}"
            request["_mutation_output"] = payload
            write_failure = write_output_file(
                request, input_payload, payload, invoke_id
            )
            if write_failure is not None:
                return write_failure
            return invoke_result(invoke_id, "success", payload, None)

        if action == "download_attachments":
            jira_issue_file = input_payload.get("jira_issue_file")
            output_dir = input_payload.get("output_dir")
            if not jira_issue_file or not output_dir:
                return failed_result(
                    invoke_id,
                    "jira_issue_file and output_dir are required",
                    "validation_error",
                    "validation",
                    "none",
                )
            allowed_extensions = input_payload.get("allowed_extensions")
            if allowed_extensions is None:
                allowed_extensions = DEFAULT_ALLOWED_EXTENSIONS
            allowed_mime_prefixes = input_payload.get("allowed_mime_prefixes")
            if allowed_mime_prefixes is None:
                allowed_mime_prefixes = DEFAULT_ALLOWED_MIME_PREFIXES
            max_bytes = input_payload.get("max_bytes", DEFAULT_MAX_BYTES)
            if type(max_bytes) is not int or max_bytes < 0:
                raise ValueError("max_bytes must be a nonnegative integer")
            # Keep the action's existing zero-as-default behavior. The low-level
            # downloader also supports zero, but the public action stays bounded.
            max_bytes = max_bytes or DEFAULT_MAX_BYTES
            try:
                resolved_issue_file = workspace_path(request, jira_issue_file)
                resolved_output_dir = workspace_path(request, output_dir)
                manifest_file = input_payload.get("manifest_file") or os.path.join(
                    output_dir, "manifest.json"
                )
                resolved_manifest_file = workspace_path(request, manifest_file)
                if os.path.dirname(resolved_manifest_file) != resolved_output_dir:
                    raise ValueError("manifest_file must be directly inside output_dir")
            except RuntimeError as err:
                return failed_result(
                    invoke_id, str(err), "validation_error", "validation", "none"
                )
            try:
                jira_issue = read_json_file(resolved_issue_file)
            except (OSError, ValueError) as err:
                return failed_result(
                    invoke_id,
                    f"could not read jira_issue_file '{jira_issue_file}': {err}",
                    "validation_error",
                    "validation",
                    "none",
                )
            downloaded, skipped = download_attachments_from_issue(
                jira_issue,
                auth_header,
                resolved_output_dir,
                allowed_extensions,
                allowed_mime_prefixes,
                max_bytes,
            )
            # Each manifest member is relative to THIS manifest, not to an old
            # invocation or a caller's repository. Keep nested manifests portable.
            for item in downloaded:
                member = os.path.join(resolved_output_dir, item["local_path"])
                relative = os.path.relpath(
                    member, os.path.dirname(resolved_manifest_file)
                )
                if ".." in Path(relative).parts:
                    raise ValueError(
                        "manifest members must remain below manifest parent"
                    )
                item["local_path"] = relative
            manifest_parent = os.path.dirname(resolved_manifest_file)
            if manifest_parent:
                os.makedirs(manifest_parent, exist_ok=True)
            manifest_payload = {
                "downloaded": downloaded,
                "skipped": skipped,
                "total_downloaded": len(downloaded),
                "total_skipped": len(skipped),
            }
            with open(resolved_manifest_file, "w", encoding="utf-8") as handle:
                json.dump(manifest_payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            output = dict(manifest_payload)
            output["manifest_path"] = resolved_manifest_file
            return invoke_result(invoke_id, "success", output, None)

        return failed_result(
            invoke_id,
            f"unsupported action {action}",
            "validation_error",
            "validation",
            "none",
        )
    except ResponseLimitError as err:
        return failed_result(
            invoke_id, str(err), "tool_execution_error", "tool", "safe"
        )
    except (ValueError, UnicodeError):
        return failed_result(
            invoke_id,
            "invalid bounded file input or output",
            "validation_error",
            "validation",
            "none",
        )
    except urllib.error.HTTPError as err:
        if err.code in {401, 403}:
            return failed_result(
                invoke_id,
                f"Jira authentication failed with HTTP {err.code}",
                "auth_error",
                "auth",
                "operator",
            )
        return failed_result(
            invoke_id,
            f"Jira request failed with HTTP {err.code}",
            "transport_error",
            "transport",
            "safe",
        )
    except urllib.error.URLError as err:
        return failed_result(
            invoke_id,
            f"Jira request transport failed: {err}",
            "transport_error",
            "transport",
            "safe",
        )
    except Exception as err:
        return failed_result(
            invoke_id,
            f"Jira plugin failed: {err}",
            "adapter_internal_error",
            "internal",
            "operator",
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
