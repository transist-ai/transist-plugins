#!/usr/bin/env python3
import json
import pathlib
import sys
import tempfile
from unittest import mock
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))

from support import (  # noqa: E402
    app_invoke_request as base_request,
    assert_app_frames,
    completed_process,
    load_plugin_module,
    read_json,
    run_process_plugin_main,
)


def app_invoke_request(action: str, payload: dict, artifacts_dir=None) -> dict:
    """Reuse offline smoke cases with explicit --compact without changing defaults."""
    if sys.argv[1:] == ["--compact"]:
        payload = payload | {"output_mode": "compact"}
    return base_request(action, payload, artifacts_dir)


def validate_json_schema(schema: dict, payload: object) -> None:
    """Validate action schemas using only the selected package-local compact ref."""
    compact = Resource.from_contents(read_json(ROOT / "schemas/compact.schema.json"))
    registry = Registry().with_resource("compact.schema.json", compact)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, registry=registry).validate(payload)


def main():
    for required in ["plugin.yaml", "plugin.py", "workflow.yaml", "README.md"]:
        if not (ROOT / required).exists():
            raise AssertionError(f"missing file: {required}")
    output_schema = read_json(ROOT / "schemas/pr_list.output.schema.json")
    unresolved_comments_output_schema = read_json(
        ROOT / "schemas/pr_unresolved_comments.output.schema.json"
    )
    approval_output_schema = read_json(
        ROOT / "schemas/pr_check_approvals.output.schema.json"
    )
    merge_output_schema = read_json(ROOT / "schemas/pr_merge.output.schema.json")
    publish_review_replies_output_schema = read_json(
        ROOT / "schemas/pr_publish_review_replies.output.schema.json"
    )
    comment_upsert_output_schema = read_json(
        ROOT / "schemas/pr_comment_upsert.output.schema.json"
    )
    module = load_plugin_module(ROOT)
    pull_requests = [
        {
            "number": 7,
            "title": "Example PR",
            "state": "OPEN",
            "url": "https://github.com/example/repo/pull/7",
            "headRefName": "feature/example",
            "baseRefName": "main",
            "isDraft": False,
            "author": {"login": "octocat"},
        }
    ]
    invoke_request = app_invoke_request("pr_list", {"limit": 1})
    success_process = completed_process(
        ["gh", "pr", "list"],
        returncode=0,
        stdout=json.dumps(pull_requests),
        stderr="",
    )
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        invoke_request,
        [mock.patch.object(module, "run_process", return_value=success_process)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("offline smoke path should not fail the plugin process")
    _, result = assert_app_frames(frames, "github.cli")
    if result["status"] != "success":
        raise AssertionError("expected success result for mocked gh invocation")
    validate_json_schema(output_schema, result["output"])

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        invoke_request,
        [mock.patch.object(module, "run_process", side_effect=FileNotFoundError())],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("missing binary should still produce a normal APP result")
    _, missing_result = assert_app_frames(frames, "github.cli")
    if missing_result["error"]["code"] != "configuration_error":
        raise AssertionError("missing gh binary should produce configuration_error")

    with tempfile.TemporaryDirectory() as tmpdir:
        output_file = pathlib.Path(tmpdir) / "review_comments.json"
        invoke_request = app_invoke_request(
            "pr_unresolved_comments",
            {
                "repo": "example/repo",
                "output_file": str(output_file),
            },
        )
        graphql_payload = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "RT_kwDOA_thread1",
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "comments": {
                                        "nodes": [
                                            {
                                                "id": "thread-comment-1",
                                                "databaseId": 111,
                                                "body": "Please rename this helper.",
                                                "path": "src/main.py",
                                                "diffHunk": "@@ -1 +1 @@",
                                                "createdAt": "2026-04-11T08:00:00Z",
                                                "url": (
                                                    "https://github.com/"
                                                    "example/repo/pull/7#discussion_r1"
                                                ),
                                                "author": {"login": "reviewer-a"},
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                        "conversationComments": {
                            "nodes": [
                                {
                                    "id": "pr-comment-ignored",
                                    "body": "General note",
                                    "createdAt": "2026-04-11T08:05:00Z",
                                    "url": (
                                        "https://github.com/"
                                        "example/repo/pull/7#issuecomment-1"
                                    ),
                                    "author": {"login": "reviewer-b"},
                                }
                            ]
                        },
                    }
                }
            }
        }
        success_process = completed_process(
            ["gh", "api", "graphql"],
            returncode=0,
            stdout=json.dumps(graphql_payload),
            stderr="",
        )
        run_mock = mock.Mock(
            side_effect=[
                completed_process(
                    ["gh", "pr", "view", "-R", "example/repo", "--json", "number"],
                    returncode=0,
                    stdout=json.dumps({"number": 7}),
                    stderr="",
                ),
                success_process,
            ]
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "offline pr_unresolved_comments legacy path should not fail"
            )
        _, result = assert_app_frames(frames, "github.cli")
        if result["status"] != "success":
            raise AssertionError("expected success result for unresolved comments")
        validate_json_schema(unresolved_comments_output_schema, result["output"])
        if result["output"]["count"] != 1:
            raise AssertionError(f"unexpected comment count: {result['output']}")
        if result["output"]["review_thread_comment_count"] != 1:
            raise AssertionError(f"unexpected review thread count: {result['output']}")
        if result["output"]["pr_conversation_comment_count"] != 0:
            raise AssertionError(
                f"unexpected PR conversation count: {result['output']}"
            )
        if result["output"]["comments"][0]["source"] != "review_thread":
            raise AssertionError(
                f"unexpected comment source: {result['output']['comments']}"
            )
        if result["output"]["comments"][0]["thread_id"] != "RT_kwDOA_thread1":
            raise AssertionError(
                f"expected thread_id in comment: {result['output']['comments']}"
            )
        if result["output"]["comments"][0]["review_comment_id"] != "111":
            raise AssertionError(
                f"expected review_comment_id in comment: {result['output']['comments']}"
            )
        if result["output"]["pr_conversation_comments_after"] != "":
            raise AssertionError(f"unexpected conversation cutoff: {result['output']}")
        if result["output"]["last_successful_fetch_completed_at"] != "":
            raise AssertionError(
                "legacy unresolved comments fetch should not record a conversation"
                " cutoff timestamp"
            )
        persisted = json.loads(output_file.read_text(encoding="utf-8"))
        if (
            persisted["count"] != 1
            or persisted["comments"][0]["source"] != "review_thread"
        ):
            raise AssertionError(
                f"unexpected persisted unresolved comments payload: {persisted}"
            )

    with tempfile.TemporaryDirectory() as tmpdir:
        output_file = pathlib.Path(tmpdir) / "review_comments.json"
        invoke_request = app_invoke_request(
            "pr_unresolved_comments",
            {
                "repo": "example/repo",
                "output_file": str(output_file),
                "include_pr_conversation_comments": True,
            },
        )
        graphql_payload = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "RT_kwDOA_thread1",
                                    "isResolved": False,
                                    "isOutdated": True,
                                    "comments": {
                                        "nodes": [
                                            {
                                                "id": "thread-comment-1",
                                                "databaseId": 111,
                                                "body": "Please rename this helper.",
                                                "path": "src/main.py",
                                                "diffHunk": "@@ -1 +1 @@",
                                                "createdAt": "2026-04-11T08:00:00Z",
                                                "url": (
                                                    "https://github.com/"
                                                    "example/repo/pull/7#discussion_r1"
                                                ),
                                                "author": {"login": "reviewer-a"},
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                        "conversationComments": {
                            "nodes": [
                                {
                                    "id": "pr-comment-1",
                                    "body": "Please also update the docs.",
                                    "createdAt": "2026-04-11T08:05:00Z",
                                    "url": (
                                        "https://github.com/"
                                        "example/repo/pull/7#issuecomment-1"
                                    ),
                                    "author": {"login": "reviewer-b"},
                                }
                            ]
                        },
                    }
                }
            }
        }
        success_process = completed_process(
            ["gh", "api", "graphql"],
            returncode=0,
            stdout=json.dumps(graphql_payload),
            stderr="",
        )
        run_mock = mock.Mock(
            side_effect=[
                completed_process(
                    ["gh", "pr", "view", "-R", "example/repo", "--json", "number"],
                    returncode=0,
                    stdout=json.dumps({"number": 7}),
                    stderr="",
                ),
                success_process,
            ]
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "offline pr_unresolved_comments mixed path should not fail"
            )
        _, result = assert_app_frames(frames, "github.cli")
        if result["status"] != "success":
            raise AssertionError(
                "expected success result for mixed unresolved comments"
            )
        validate_json_schema(unresolved_comments_output_schema, result["output"])
        if result["output"]["count"] != 2:
            raise AssertionError(f"unexpected mixed comment count: {result['output']}")
        if result["output"]["review_thread_comment_count"] != 1:
            raise AssertionError(
                f"unexpected mixed review thread count: {result['output']}"
            )
        if result["output"]["pr_conversation_comment_count"] != 1:
            raise AssertionError(
                f"unexpected mixed PR conversation count: {result['output']}"
            )
        if result["output"]["pr_conversation_comments_after"] != "":
            raise AssertionError(f"unexpected first-cycle cutoff: {result['output']}")
        if [comment["source"] for comment in result["output"]["comments"]] != [
            "review_thread",
            "pr_conversation",
        ]:
            raise AssertionError(
                f"unexpected mixed comment ordering: {result['output']['comments']}"
            )
        persisted = json.loads(output_file.read_text(encoding="utf-8"))
        previous_cutoff = persisted["last_successful_fetch_completed_at"]
        if not previous_cutoff:
            raise AssertionError(
                "expected mixed run to persist a successful fetch timestamp"
            )

        next_payload = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "RT_kwDOA_thread2",
                                    "isResolved": False,
                                    "isOutdated": False,
                                    "comments": {
                                        "nodes": [
                                            {
                                                "id": "thread-comment-2",
                                                "databaseId": 222,
                                                "body": "Please add a test.",
                                                "path": "src/main.py",
                                                "diffHunk": "@@ -3 +3 @@",
                                                "createdAt": "2026-04-11T09:00:00Z",
                                                "url": (
                                                    "https://github.com/"
                                                    "example/repo/pull/7#discussion_r2"
                                                ),
                                                "author": {"login": "reviewer-a"},
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                        "conversationComments": {
                            "nodes": [
                                {
                                    "id": "pr-comment-equal-cutoff",
                                    "body": (
                                        "This should be skipped because it matches the"
                                        " cutoff."
                                    ),
                                    "createdAt": previous_cutoff,
                                    "url": (
                                        "https://github.com/"
                                        "example/repo/pull/7#issuecomment-2"
                                    ),
                                    "author": {"login": "reviewer-b"},
                                },
                                {
                                    "id": "pr-comment-new",
                                    "body": "This is a new conversation comment.",
                                    "createdAt": "2099-01-01T00:00:00Z",
                                    "url": (
                                        "https://github.com/"
                                        "example/repo/pull/7#issuecomment-3"
                                    ),
                                    "author": {"login": "reviewer-c"},
                                },
                            ]
                        },
                    }
                }
            }
        }
        next_process = completed_process(
            ["gh", "api", "graphql"],
            returncode=0,
            stdout=json.dumps(next_payload),
            stderr="",
        )
        next_run_mock = mock.Mock(
            side_effect=[
                completed_process(
                    ["gh", "pr", "view", "-R", "example/repo", "--json", "number"],
                    returncode=0,
                    stdout=json.dumps({"number": 7}),
                    stderr="",
                ),
                next_process,
            ]
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", next_run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "offline pr_unresolved_comments cutoff path should not fail"
            )
        _, result = assert_app_frames(frames, "github.cli")
        validate_json_schema(unresolved_comments_output_schema, result["output"])
        if result["output"]["count"] != 2:
            raise AssertionError(
                f"unexpected filtered mixed comment count: {result['output']}"
            )
        if result["output"]["review_thread_comment_count"] != 1:
            raise AssertionError(
                f"unexpected filtered review thread count: {result['output']}"
            )
        if result["output"]["pr_conversation_comment_count"] != 1:
            raise AssertionError(
                f"unexpected filtered PR conversation count: {result['output']}"
            )
        if result["output"]["pr_conversation_comments_after"] != previous_cutoff:
            raise AssertionError(f"unexpected filtered cutoff echo: {result['output']}")
        if [comment["id"] for comment in result["output"]["comments"]] != [
            "thread-comment-2",
            "pr-comment-new",
        ]:
            raise AssertionError(
                f"unexpected filtered mixed comments: {result['output']['comments']}"
            )
        if result["output"]["comments"][1]["source"] != "pr_conversation":
            raise AssertionError(
                "unexpected filtered conversation comment source:"
                f" {result['output']['comments']}"
            )
        filtered_persisted = json.loads(output_file.read_text(encoding="utf-8"))
        if filtered_persisted["pr_conversation_comments_after"] != previous_cutoff:
            raise AssertionError(
                f"unexpected filtered persisted cutoff: {filtered_persisted}"
            )
        if module.parse_iso8601_timestamp(
            filtered_persisted["last_successful_fetch_completed_at"]
        ) < module.parse_iso8601_timestamp(previous_cutoff):
            raise AssertionError(
                "expected successful filtered run to preserve or advance the fetch"
                " timestamp"
            )

        failure_process = completed_process(
            ["gh", "api", "graphql"],
            returncode=1,
            stdout="",
            stderr="boom\n",
        )
        last_successful_fetch_completed_at = filtered_persisted[
            "last_successful_fetch_completed_at"
        ]
        failure_run_mock = mock.Mock(
            side_effect=[
                completed_process(
                    ["gh", "pr", "view", "-R", "example/repo", "--json", "number"],
                    returncode=0,
                    stdout=json.dumps({"number": 7}),
                    stderr="",
                ),
                failure_process,
            ]
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", failure_run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "failed pr_unresolved_comments should still exit normally"
            )
        _, result = assert_app_frames(frames, "github.cli")
        if result["status"] != "failed":
            raise AssertionError(
                "expected failed result for unresolved comments command error"
            )
        validate_json_schema(unresolved_comments_output_schema, result["output"])
        if (
            result["output"]["last_successful_fetch_completed_at"]
            != last_successful_fetch_completed_at
        ):
            raise AssertionError(
                "expected failed fetch to preserve prior success cutoff:"
                f" {result['output']}"
            )
        if (
            result["output"]["pr_conversation_comments_after"]
            != last_successful_fetch_completed_at
        ):
            raise AssertionError(
                f"expected failed fetch to echo prior cutoff: {result['output']}"
            )
        failure_persisted = json.loads(output_file.read_text(encoding="utf-8"))
        if (
            failure_persisted["last_successful_fetch_completed_at"]
            != last_successful_fetch_completed_at
        ):
            raise AssertionError(
                f"failed fetch should preserve persisted cutoff: {failure_persisted}"
            )
        if failure_persisted["count"] != 0:
            raise AssertionError(
                "failed fetch should persist an empty comment snapshot:"
                f" {failure_persisted}"
            )

    with tempfile.TemporaryDirectory() as tmpdir:
        jira_issue = pathlib.Path(tmpdir) / "jira_issue.json"
        jira_issue.write_text(
            json.dumps(
                {"key": "DEMO-45", "summary": "Invalid payload: expected an object"}
            ),
            encoding="utf-8",
        )
        title = module.pr_title_from_jira_issue(
            {"paths": {"workspace_dir": tmpdir}},
            "jira_issue.json",
        )
        if title != "[DEMO-45] Invalid payload: expected an object":
            raise AssertionError(f"unexpected Jira-derived PR title: {title}")
        templated_title = module.pr_title_from_jira_issue(
            {"paths": {"workspace_dir": tmpdir}},
            "jira_issue.json",
            {"jira_title_template": "[{key}] (🤖) {summary}"},
        )
        if templated_title != "[DEMO-45] (🤖) Invalid payload: expected an object":
            raise AssertionError(
                f"unexpected templated Jira-derived PR title: {templated_title}"
            )

    with tempfile.TemporaryDirectory() as tmpdir:
        run_request = {
            "paths": {
                "workspace_dir": tmpdir,
                "run_root": str(
                    pathlib.Path(tmpdir)
                    / "home"
                    / "runs"
                    / "run_demo"
                    / "contained_runs"
                    / "run_demo__plan_loop"
                ),
            }
        }
        run_issue = (
            pathlib.Path(tmpdir)
            / "home"
            / "runs"
            / "run_demo"
            / "demo_ticket_delivery"
            / "DEMO-45"
            / "jira_issue.json"
        )
        run_issue.parent.mkdir(parents=True, exist_ok=True)
        run_issue.write_text(
            json.dumps({"key": "DEMO-45", "summary": "Stored under run root"}),
            encoding="utf-8",
        )
        title = module.pr_title_from_jira_issue(
            run_request,
            "run://demo_ticket_delivery/DEMO-45/jira_issue.json",
        )
        if title != "[DEMO-45] Stored under run root":
            raise AssertionError(f"unexpected run:// Jira-derived PR title: {title}")

        repo_issue = (
            pathlib.Path(tmpdir)
            / ".transist"
            / "demo_ticket_delivery"
            / "DEMO-45"
            / "jira_issue.json"
        )
        repo_issue.parent.mkdir(parents=True, exist_ok=True)
        repo_issue.write_text(
            json.dumps({"key": "DEMO-45", "summary": "Stored under target repo"}),
            encoding="utf-8",
        )
        title = module.pr_title_from_jira_issue(
            run_request,
            "repo://demo_ticket_delivery/DEMO-45/jira_issue.json",
        )
        if title != "[DEMO-45] Stored under target repo":
            raise AssertionError(f"unexpected repo:// Jira-derived PR title: {title}")

    with tempfile.TemporaryDirectory() as tmpdir:
        jira_issue = pathlib.Path(tmpdir) / "jira_issue.json"
        jira_issue.write_text(
            json.dumps({"key": "DEMO-45", "summary": "Add robot format"}),
            encoding="utf-8",
        )
        pr_body = pathlib.Path(tmpdir) / "pr_body.md"
        pr_body.write_text(
            "## What Changed\n\nUpdated the workflow.\n", encoding="utf-8"
        )
        invoke_request = app_invoke_request(
            "pr_create",
            {
                "jira_issue_file": str(jira_issue),
                "jira_title_template": "[{key}] (🤖) {summary}",
                "body_file": str(pr_body),
                "body_prefix": "Addresses: DEMO-45",
                "draft": True,
            },
        )
        success_process = completed_process(
            ["gh", "pr", "create"],
            returncode=0,
            stdout="https://github.com/example/repo/pull/7\n",
            stderr="",
        )
        run_mock = mock.Mock(return_value=success_process)
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "offline pr_create smoke path should not fail the plugin process"
            )
        _, result = assert_app_frames(frames, "github.cli")
        validate_json_schema(
            read_json(ROOT / "schemas/pr_create.output.schema.json"), result["output"]
        )
        command = run_mock.call_args.args[0]
        if "--title" not in command or "--body" not in command:
            raise AssertionError("expected pr_create to pass explicit title and body")
        title_index = command.index("--title")
        if command[title_index + 1] != "[DEMO-45] (🤖) Add robot format":
            raise AssertionError(
                f"unexpected templated pr_create title: {command[title_index + 1]}"
            )
        body_index = command.index("--body")
        if (
            command[body_index + 1]
            != "Addresses: DEMO-45\n\n## What Changed\n\nUpdated the workflow."
        ):
            raise AssertionError(
                f"unexpected prefixed pr_create body: {command[body_index + 1]}"
            )
        if "--draft" not in command:
            raise AssertionError("expected pr_create to preserve draft mode")

    with tempfile.TemporaryDirectory() as tmpdir:
        output_file = pathlib.Path(tmpdir) / "approval_pr.json"
        invoke_request = app_invoke_request(
            "pr_check_approvals",
            {
                "repo": "example/repo",
                "required_approvals": 2,
                "output_file": str(output_file),
            },
        )
        invoke_request["paths"]["run_root"] = str(
            pathlib.Path(tmpdir) / "home" / "runs" / "run_demo__review_cycle"
        )
        pr_payload = {
            "number": 7,
            "title": "Example PR",
            "state": "OPEN",
            "url": "https://github.com/example/repo/pull/7",
            "headRefName": "feature/example",
            "baseRefName": "main",
            "isDraft": False,
            "author": {"login": "octocat"},
            "reviewDecision": "REVIEW_REQUIRED",
            "mergedAt": None,
            "latestReviews": [
                {
                    "author": {"login": "reviewer-b"},
                    "state": "APPROVED",
                    "submittedAt": "2026-04-10T09:00:00Z",
                },
                {
                    "author": {"login": "reviewer-b"},
                    "state": "COMMENTED",
                    "submittedAt": "2026-04-11T09:00:00Z",
                },
                {
                    "author": {"login": "reviewer-a"},
                    "state": "APPROVED",
                    "submittedAt": "2026-04-11T10:00:00Z",
                },
                {
                    "author": {"login": "octocat"},
                    "state": "APPROVED",
                    "submittedAt": "2026-04-11T11:00:00Z",
                },
            ],
        }
        success_process = completed_process(
            ["gh", "pr", "view"],
            returncode=0,
            stdout=json.dumps(pr_payload),
            stderr="",
        )
        run_mock = mock.Mock(return_value=success_process)
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0:
            raise AssertionError(
                "pr_check_approvals threshold failures should still exit normally"
            )
        if stderr:
            raise AssertionError(
                "pr_check_approvals threshold failures should not crash the plugin"
                " process"
            )
        _, result = assert_app_frames(frames, "github.cli")
        if result["status"] != "failed":
            raise AssertionError("expected threshold miss to produce failed APP result")
        validate_json_schema(approval_output_schema, result["output"])
        if result["output"]["approval_count"] != 1:
            raise AssertionError(
                f"unexpected approval_count: {result['output']['approval_count']}"
            )
        if result["output"]["approved_reviewers"] != ["reviewer-a"]:
            raise AssertionError(
                "unexpected approved_reviewers:"
                f" {result['output']['approved_reviewers']}"
            )
        if result["error"]["code"] != "tool_execution_error":
            raise AssertionError("expected canonical tool_execution_error code")
        if "transist runs resume demo" not in result["error"]["message"]:
            raise AssertionError(
                f"unexpected approval failure message: {result['error']['message']}"
            )
        if "need 2 current approvals, found 1" not in result["error"]["message"]:
            raise AssertionError(
                "unexpected approval threshold explanation:"
                f" {result['error']['message']}"
            )
        persisted = json.loads(output_file.read_text(encoding="utf-8"))
        if persisted["approval_count"] != 1 or persisted["required_approvals"] != 2:
            raise AssertionError(f"unexpected persisted approval payload: {persisted}")
        command = run_mock.call_args.args[0]
        if "latestReviews" not in command[command.index("--json") + 1]:
            raise AssertionError("expected pr_check_approvals to request latestReviews")

    with tempfile.TemporaryDirectory() as tmpdir:
        output_file = pathlib.Path(tmpdir) / "merged_pr.json"
        invoke_request = app_invoke_request(
            "pr_merge",
            {
                "repo": "example/repo",
                "method": "squash",
                "delete_remote_branch": True,
                "output_file": str(output_file),
            },
        )
        initial_pr = {
            "number": 7,
            "title": "Example PR",
            "state": "OPEN",
            "url": "https://github.com/example/repo/pull/7",
            "headRefName": "feature/example",
            "baseRefName": "main",
            "isDraft": False,
            "author": {"login": "octocat"},
            "reviewDecision": "APPROVED",
            "mergedAt": None,
            "latestReviews": [],
        }
        merged_pr = dict(initial_pr)
        merged_pr["state"] = "MERGED"
        merged_pr["mergedAt"] = "2026-04-11T12:00:00Z"
        run_mock = mock.Mock(
            side_effect=[
                completed_process(
                    ["gh", "pr", "view"],
                    returncode=0,
                    stdout=json.dumps(initial_pr),
                    stderr="",
                ),
                completed_process(
                    ["gh", "pr", "merge", "7", "-R", "example/repo", "--squash"],
                    returncode=0,
                    stdout="merged pull request\n",
                    stderr="",
                ),
                completed_process(
                    ["gh", "pr", "view"],
                    returncode=0,
                    stdout=json.dumps(merged_pr),
                    stderr="",
                ),
                completed_process(
                    [
                        "gh",
                        "api",
                        "-X",
                        "DELETE",
                        "repos/example/repo/git/refs/heads/feature%2Fexample",
                    ],
                    returncode=1,
                    stdout="",
                    stderr="HTTP 422: Reference does not exist",
                ),
            ]
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "offline pr_merge smoke path should not fail the plugin process"
            )
        _, result = assert_app_frames(frames, "github.cli")
        if result["status"] != "success":
            raise AssertionError(
                f"expected success result for mocked pr_merge flow, got: {result}"
            )
        validate_json_schema(merge_output_schema, result["output"])
        if not result["output"]["merged"] or result["output"]["already_merged"]:
            raise AssertionError(f"unexpected merge flags: {result['output']}")
        if not result["output"]["remote_branch_deleted"]:
            raise AssertionError(
                "expected missing remote branch cleanup to be treated as success"
            )
        persisted = json.loads(output_file.read_text(encoding="utf-8"))
        if persisted["pull_request"]["state"] != "MERGED":
            raise AssertionError(f"unexpected persisted merge payload: {persisted}")
        delete_command = run_mock.call_args_list[3].args[0]
        if delete_command[-1] != "repos/example/repo/git/refs/heads/feature%2Fexample":
            raise AssertionError(f"unexpected remote delete command: {delete_command}")

    with tempfile.TemporaryDirectory() as tmpdir:
        response_plan_file = pathlib.Path(tmpdir) / "review_replies.json"
        state_file = pathlib.Path(tmpdir) / "review_replies_state.json"
        output_file = pathlib.Path(tmpdir) / "review_replies_publish.json"
        response_plan_file.write_text(
            json.dumps(
                {
                    "addressed_thread_ids": [],
                    "responses": [
                        {
                            "comment_id": "comment-1",
                            "source": "review_thread",
                            "url": (
                                "https://github.com/example/repo/pull/7#discussion_r123"
                            ),
                            "review_comment_id": "123",
                            "thread_id": "RT_kwDOA_thread1",
                            "reply": "Applied the requested fix.",
                        },
                        {
                            "comment_id": "comment-2",
                            "source": "pr_conversation",
                            "url": (
                                "https://github.com/example/repo/pull/7#issuecomment-99"
                            ),
                            "review_comment_id": "",
                            "thread_id": "",
                            "reply": "Clarified why no change is needed.",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        invoke_request = app_invoke_request(
            "pr_publish_review_replies",
            {
                "repo": "example/repo",
                "response_plan_file": str(response_plan_file),
                "state_file": str(state_file),
                "output_file": str(output_file),
            },
        )
        empty_comments = completed_process([], 0, "[]", "")
        planned = module.publication_plan(
            json.loads(response_plan_file.read_text()), 1000
        )
        reply_bodies = []
        for item in planned:
            identity = module.reply_publication_identity("example", "repo", 7, item)
            reply_bodies.append(
                item["legacy_body"]
                + f"\n<!-- transist-review-publication:{identity} -->"
            )
        run_mock = mock.Mock(
            side_effect=[
                completed_process(
                    ["gh", "pr", "view", "-R", "example/repo", "--json", "number"],
                    returncode=0,
                    stdout=json.dumps({"number": 7}),
                    stderr="",
                ),
                empty_comments,
                empty_comments,
                completed_process(
                    ["gh", "api", "repos/example/repo/pulls/7/comments"],
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "id": 501,
                            "body": reply_bodies[0],
                            "in_reply_to_id": 123,
                            "html_url": (
                                "https://github.com/example/repo/pull/7#discussion_r501"
                            ),
                        }
                    ),
                    stderr="",
                ),
                completed_process(
                    ["gh", "api", "repos/example/repo/issues/7/comments"],
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "id": 601,
                            "body": reply_bodies[1],
                            "html_url": (
                                "https://github.com/"
                                "example/repo/pull/7#issuecomment-601"
                            ),
                        }
                    ),
                    stderr="",
                ),
            ]
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "offline pr_publish_review_replies smoke path should not fail the"
                " plugin process"
            )
        _, result = assert_app_frames(frames, "github.cli")
        if result["status"] != "success":
            raise AssertionError(
                "expected success result for pr_publish_review_replies flow, got:"
                f" {result}"
            )
        validate_json_schema(publish_review_replies_output_schema, result["output"])
        if (
            result["output"]["posted_count"] != 2
            or result["output"]["skipped_count"] != 0
        ):
            raise AssertionError(
                f"unexpected review publish counts: {result['output']}"
            )
        if result["output"]["resolved_count"] != 0:
            raise AssertionError(
                f"expected 0 resolutions when flag is off: {result['output']}"
            )
        # comment-1 has review_comment_id: use in-thread delivery.
        posted_review_thread = next(
            (
                r
                for r in result["output"]["responses"]
                if r["comment_id"] == "comment-1"
            ),
            None,
        )
        if (
            posted_review_thread is None
            or posted_review_thread.get("delivery_mode") != "review_thread_reply"
        ):
            raise AssertionError(
                "expected review_thread_reply for comment-1:"
                f" {result['output']['responses']}"
            )
        persisted_state = json.loads(state_file.read_text(encoding="utf-8"))
        if "comment-1" not in persisted_state.get("responses", {}):
            raise AssertionError(
                f"expected tracked comment-1 state, got: {persisted_state}"
            )
        # Verify the in-thread call used review_comment_id=123 (not URL regex)
        pulls_call_args = run_mock.call_args_list[3].args[0]
        if "in_reply_to=123" not in " ".join(str(a) for a in pulls_call_args):
            raise AssertionError(
                f"expected in_reply_to=123 in pulls call, got: {pulls_call_args}"
            )

        run_mock = mock.Mock(
            side_effect=[
                completed_process(
                    ["gh", "pr", "view", "-R", "example/repo", "--json", "number"],
                    returncode=0,
                    stdout=json.dumps({"number": 7}),
                    stderr="",
                )
            ]
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "duplicate pr_publish_review_replies smoke path should not fail"
            )
        _, duplicate_result = assert_app_frames(frames, "github.cli")
        if (
            duplicate_result["output"]["posted_count"] != 0
            or duplicate_result["output"]["skipped_count"] != 2
        ):
            raise AssertionError(
                "expected duplicate replies to be skipped:"
                f" {duplicate_result['output']}"
            )

    # resolve_addressed_threads=true: verify GraphQL mutation per addressed thread.
    with tempfile.TemporaryDirectory() as tmpdir:
        response_plan_file = pathlib.Path(tmpdir) / "review_replies_resolve.json"
        state_file = pathlib.Path(tmpdir) / "review_replies_state.json"
        output_file = pathlib.Path(tmpdir) / "review_replies_publish.json"
        response_plan_file.write_text(
            json.dumps(
                {
                    "addressed_thread_ids": ["RT_kwDOA_threadA", "RT_kwDOA_threadB"],
                    "responses": [],
                }
            ),
            encoding="utf-8",
        )
        invoke_request_resolve = app_invoke_request(
            "pr_publish_review_replies",
            {
                "repo": "example/repo",
                "response_plan_file": str(response_plan_file),
                "state_file": str(state_file),
                "output_file": str(output_file),
                "resolve_addressed_threads": True,
            },
        )
        run_mock_resolve = mock.Mock(
            side_effect=[
                completed_process(
                    ["gh", "pr", "view", "-R", "example/repo", "--json", "number"],
                    returncode=0,
                    stdout=json.dumps({"number": 7}),
                    stderr="",
                ),
                completed_process(
                    ["gh", "api", "graphql"],
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "data": {
                                "resolveReviewThread": {
                                    "thread": {
                                        "id": "RT_kwDOA_threadA",
                                        "isResolved": True,
                                    }
                                }
                            }
                        }
                    ),
                    stderr="",
                ),
                completed_process(
                    ["gh", "api", "graphql"],
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "data": {
                                "resolveReviewThread": {
                                    "thread": {
                                        "id": "RT_kwDOA_threadB",
                                        "isResolved": True,
                                    }
                                }
                            }
                        }
                    ),
                    stderr="",
                ),
            ]
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request_resolve,
            [mock.patch.object(module, "run_process", run_mock_resolve)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError("resolve_addressed_threads smoke path should not fail")
        _, resolve_result = assert_app_frames(frames, "github.cli")
        if resolve_result["status"] != "success":
            raise AssertionError(
                f"expected success for resolve path, got: {resolve_result}"
            )
        validate_json_schema(
            publish_review_replies_output_schema, resolve_result["output"]
        )
        if resolve_result["output"]["resolved_count"] != 2:
            raise AssertionError(
                f"expected 2 resolved threads, got: {resolve_result['output']}"
            )
        if resolve_result["output"]["posted_count"] != 0:
            raise AssertionError(
                f"expected 0 posted (no replies), got: {resolve_result['output']}"
            )
        if resolve_result["output"]["resolution_errors"]:
            raise AssertionError(
                "expected no resolution errors, got:"
                f" {resolve_result['output']['resolution_errors']}"
            )

    with tempfile.TemporaryDirectory() as tmpdir:
        body_file = pathlib.Path(tmpdir) / "comment_body.md"
        body_file.write_text(
            "Validation failed: implementation diverges from the approved plan.",
            encoding="utf-8",
        )
        output_file = pathlib.Path(tmpdir) / "comment_upsert.json"
        invoke_request = app_invoke_request(
            "pr_comment_upsert",
            {
                "repo": "example/repo",
                "marker": "<!-- transist-validation-awareness -->",
                "body_file": str(body_file),
                "create_if_missing": True,
                "output_file": str(output_file),
            },
        )
        run_mock = mock.Mock(
            side_effect=[
                completed_process(
                    ["gh", "pr", "view", "-R", "example/repo", "--json", "number"],
                    returncode=0,
                    stdout=json.dumps({"number": 7}),
                    stderr="",
                ),
                completed_process(
                    [
                        "gh",
                        "api",
                        "repos/example/repo/issues/7/comments",
                        "-f",
                        "per_page=100",
                        "-f",
                        "page=1",
                    ],
                    returncode=0,
                    stdout=json.dumps([]),
                    stderr="",
                ),
                completed_process(
                    ["gh", "api", "repos/example/repo/issues/7/comments", "-X", "POST"],
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "id": 701,
                            "html_url": (
                                "https://github.com/"
                                "example/repo/pull/7#issuecomment-701"
                            ),
                        }
                    ),
                    stderr="",
                ),
            ]
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "offline pr_comment_upsert create path should not fail"
            )
        _, create_result = assert_app_frames(frames, "github.cli")
        validate_json_schema(comment_upsert_output_schema, create_result["output"])
        if create_result["output"]["operation"] != "created":
            raise AssertionError(
                f"expected created operation, got: {create_result['output']}"
            )

    with tempfile.TemporaryDirectory() as tmpdir:
        body_file = pathlib.Path(tmpdir) / "comment_body.md"
        body_file.write_text(
            "Validation now passes against the approved plan.", encoding="utf-8"
        )
        invoke_request = app_invoke_request(
            "pr_comment_upsert",
            {
                "repo": "example/repo",
                "marker": "<!-- transist-validation-awareness -->",
                "body_file": str(body_file),
                "create_if_missing": False,
            },
        )
        existing_comment = {
            "id": 701,
            "body": (
                "<!-- transist-validation-awareness -->\nValidation failed:"
                " implementation diverges from the approved plan."
            ),
            "html_url": "https://github.com/example/repo/pull/7#issuecomment-701",
        }
        run_mock = mock.Mock(
            side_effect=[
                completed_process(
                    ["gh", "pr", "view", "-R", "example/repo", "--json", "number"],
                    returncode=0,
                    stdout=json.dumps({"number": 7}),
                    stderr="",
                ),
                completed_process(
                    [
                        "gh",
                        "api",
                        "repos/example/repo/issues/7/comments",
                        "-f",
                        "per_page=100",
                        "-f",
                        "page=1",
                    ],
                    returncode=0,
                    stdout=json.dumps([existing_comment]),
                    stderr="",
                ),
                completed_process(
                    [
                        "gh",
                        "api",
                        "repos/example/repo/issues/comments/701",
                        "-X",
                        "PATCH",
                    ],
                    returncode=0,
                    stdout=json.dumps(
                        {"id": 701, "html_url": existing_comment["html_url"]}
                    ),
                    stderr="",
                ),
            ]
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "offline pr_comment_upsert update path should not fail"
            )
        _, update_result = assert_app_frames(frames, "github.cli")
        if update_result["output"]["operation"] != "updated":
            raise AssertionError(
                f"expected updated operation, got: {update_result['output']}"
            )

        body_file.write_text(
            "Validation now passes against the approved plan.", encoding="utf-8"
        )
        run_mock = mock.Mock(
            side_effect=[
                completed_process(
                    ["gh", "pr", "view", "-R", "example/repo", "--json", "number"],
                    returncode=0,
                    stdout=json.dumps({"number": 7}),
                    stderr="",
                ),
                completed_process(
                    [
                        "gh",
                        "api",
                        "repos/example/repo/issues/7/comments",
                        "-f",
                        "per_page=100",
                        "-f",
                        "page=1",
                    ],
                    returncode=0,
                    stdout=json.dumps(
                        [
                            {
                                "id": 701,
                                "body": (
                                    "<!-- transist-validation-awareness -->\nValidation"
                                    " now passes against the approved plan."
                                ),
                                "html_url": existing_comment["html_url"],
                            }
                        ]
                    ),
                    stderr="",
                ),
            ]
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "offline pr_comment_upsert unchanged path should not fail"
            )
        _, unchanged_result = assert_app_frames(frames, "github.cli")
        if unchanged_result["output"]["operation"] != "unchanged":
            raise AssertionError(
                f"expected unchanged operation, got: {unchanged_result['output']}"
            )
    print("github.cli offline smoke test passed")


if __name__ == "__main__":
    main()
