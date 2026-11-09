#!/usr/bin/env python3
import json
import io
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
    assert_canonical_error,
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


def assert_attachment_mapping(module):
    issue = {
        "fields": {
            "attachment": [
                {
                    "id": "10001",
                    "filename": "design.md",
                    "mimeType": "text/markdown",
                    "size": 1234,
                    "created": "2024-01-02T03:04:05.000+0000",
                    "author": {"displayName": "Jane Doe"},
                    "content": (
                        "https://example.atlassian.net/"
                        "secure/attachment/10001/design.md"
                    ),
                    "thumbnail": "",
                }
            ]
        }
    }
    attachments = module.extract_attachments(issue)
    if len(attachments) != 1 or attachments[0]["author_display_name"] != "Jane Doe":
        raise AssertionError("attachment mapping failed")


def assert_policy(module):
    defaults_ext = module.DEFAULT_ALLOWED_EXTENSIONS
    defaults_mime = module.DEFAULT_ALLOWED_MIME_PREFIXES
    md_attach = {"filename": "notes.md", "mime_type": "text/markdown", "size": 100}
    ok, reason = module.attachment_passes_policy(
        md_attach, defaults_ext, defaults_mime, 10 * 1024
    )
    if not ok:
        raise AssertionError(f"markdown should pass default policy, got: {reason}")
    png_attach = {"filename": "shot.png", "mime_type": "image/png", "size": 100}
    ok, reason = module.attachment_passes_policy(
        png_attach, defaults_ext, defaults_mime, 10 * 1024
    )
    if ok or reason != "extension_and_mime_not_allowed":
        raise AssertionError(
            f"png should be blocked by default policy, got ok={ok} reason={reason}"
        )
    big_attach = {"filename": "notes.md", "mime_type": "text/markdown", "size": 99999}
    ok, reason = module.attachment_passes_policy(
        big_attach, defaults_ext, defaults_mime, 1024
    )
    if ok or reason != "size_exceeds_max":
        raise AssertionError("size-exceeds-max should take precedence")
    ok, reason = module.attachment_passes_policy(png_attach, [], [], 10 * 1024)
    if not ok:
        raise AssertionError("empty filters should allow everything")


def assert_transition_selection(module):
    selected, error = module.select_transition(
        [
            {"id": "1", "name": "Start Progress", "to": {"name": "In Progress"}},
            {"id": "2", "name": "Send To Review", "to": {"name": "Review"}},
        ],
        "In Progress",
    )
    if error is not None or selected["id"] != "1":
        raise AssertionError("target-status fallback matching failed")
    selected, error = module.select_transition(
        [
            {"id": "1", "name": "Start Progress", "to": {"name": "In Progress"}},
            {"id": "2", "name": "Resume Progress", "to": {"name": "In Progress"}},
        ],
        "In Progress",
    )
    if selected is not None or "multiple Jira transitions" not in (error or ""):
        raise AssertionError("ambiguous target-status matching should fail")


class FakeJiraResponse:
    def __init__(self, payload):
        self._body = io.BytesIO(json.dumps(payload).encode())

    def read(self, size=-1):
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def main():
    for required in ["plugin.yaml", "plugin.py", "workflow.yaml", "README.md"]:
        if not (ROOT / required).exists():
            raise AssertionError(f"missing file: {required}")
    for schema_path in sorted((ROOT / "schemas").glob("*.json")):
        read_json(schema_path)

    module = load_plugin_module(ROOT)
    assert_attachment_mapping(module)
    assert_policy(module)
    assert_transition_selection(module)

    # Without JIRA_AUTH_HEADER → auth_error.
    invoke_request = app_invoke_request("get_issue", {"issue_key": "DEMO-1"})
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        invoke_request,
        [mock.patch.dict(module.os.environ, {"JIRA_AUTH_HEADER": ""}, clear=False)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            f"plugin should not emit stderr, got exit={exit_code} stderr={stderr}"
        )
    _, result = assert_app_frames(frames, "jira.rest")
    if result["status"] != "failed":
        raise AssertionError("expected failed result without JIRA_AUTH_HEADER")
    assert_canonical_error(
        result["error"],
        expected_code="auth_error",
        message_contains=["JIRA_AUTH_HEADER"],
    )

    # Successful get_issue: mock urllib.request.urlopen and validate output schema.
    issue_payload = {
        "key": "DEMO-1",
        "fields": {
            "summary": "Demo issue",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Demo description"}],
                    }
                ],
            },
            "status": {"name": "To Do"},
            "attachment": [],
        },
    }
    output_schema = read_json(ROOT / "schemas/get_issue.output.schema.json")
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        invoke_request,
        [
            mock.patch.dict(
                module.os.environ,
                {
                    "JIRA_AUTH_HEADER": "Basic xxx",
                    "JIRA_SITE_URL": "https://example.atlassian.net",
                },
                clear=False,
            ),
            mock.patch.object(
                module.urllib.request,
                "urlopen",
                return_value=FakeJiraResponse(issue_payload),
            ),
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            f"plugin should not emit stderr, got exit={exit_code} stderr={stderr}"
        )
    _, result = assert_app_frames(frames, "jira.rest")
    if result["status"] != "success":
        raise AssertionError(f"expected success, got {result}")
    if result["output"]["key"] != "DEMO-1" or result["output"]["status"] != "To Do":
        raise AssertionError("unexpected get_issue output mapping")
    if result["output"].get("description_text") != "Demo description":
        raise AssertionError("description_text mapping failed")
    validate_json_schema(output_schema, result["output"])

    with tempfile.TemporaryDirectory(prefix="jira-rest-create-issue-") as tmpdir:
        summary_file = pathlib.Path(tmpdir) / "summary.txt"
        description_file = pathlib.Path(tmpdir) / "description.md"
        summary_file.write_text("Summary from file\n", encoding="utf-8")
        description_file.write_text("Description from file\n", encoding="utf-8")
        create_request = app_invoke_request(
            "create_issue",
            {
                "project_key": "DEMO",
                "issue_type": "Task",
                "summary_file": str(summary_file),
                "description_file": str(description_file),
                "parent": "DEMO-1",
                "labels": "hw-backend,automation",
                "assignee_account_id": "abc-123",
                "output_file": str(pathlib.Path(tmpdir) / "created_issue.json"),
            },
        )
        create_response_payload = {"id": "10000", "key": "DEMO-2"}
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            create_request,
            [
                mock.patch.dict(
                    module.os.environ,
                    {
                        "JIRA_AUTH_HEADER": "Basic xxx",
                        "JIRA_SITE_URL": "https://example.atlassian.net",
                    },
                    clear=False,
                ),
                mock.patch.object(
                    module.urllib.request,
                    "urlopen",
                    return_value=FakeJiraResponse(create_response_payload),
                ),
            ],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                f"plugin should not emit stderr, got exit={exit_code} stderr={stderr}"
            )
        _, create_result = assert_app_frames(frames, "jira.rest")
        if create_result["status"] != "success":
            raise AssertionError(f"expected create success, got {create_result}")
        if (
            create_result["output"]["url"]
            != "https://example.atlassian.net/browse/DEMO-2"
        ):
            raise AssertionError("create_issue URL mapping failed")
        created_issue_path = pathlib.Path(tmpdir) / "created_issue.json"
        if not created_issue_path.exists():
            raise AssertionError("create_issue output_file was not written")

    # download_attachments: empty manifest path round-trip without network.
    with tempfile.TemporaryDirectory(prefix="jira-rest-smoke-") as tmpdir:
        tmpdir_path = pathlib.Path(tmpdir)
        issue_file = tmpdir_path / "jira_issue.json"
        issue_file.write_text(json.dumps({"attachments": []}), encoding="utf-8")
        output_dir = tmpdir_path / "attachments"
        download_request = app_invoke_request(
            "download_attachments",
            {
                "jira_issue_file": str(issue_file),
                "output_dir": str(output_dir),
            },
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            download_request,
            [
                mock.patch.dict(
                    module.os.environ,
                    {
                        "JIRA_AUTH_HEADER": "Basic xxx",
                        "JIRA_SITE_URL": "https://example.atlassian.net",
                    },
                    clear=False,
                )
            ],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                f"plugin should not emit stderr, got exit={exit_code} stderr={stderr}"
            )
        _, result = assert_app_frames(frames, "jira.rest")
        if result["status"] != "success":
            raise AssertionError(f"download_attachments expected success, got {result}")
        if (
            result["output"]["total_downloaded"] != 0
            or result["output"]["total_skipped"] != 0
        ):
            raise AssertionError("empty attachments should yield empty manifest")

    print("jira.rest smoke test passed")


if __name__ == "__main__":
    main()
