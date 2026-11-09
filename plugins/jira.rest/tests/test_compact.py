"""Offline bounded Jira reads and normalized compact cross-step handoffs."""

import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
import support  # noqa: E402


class Response(io.BytesIO):
    """Record requested read sizes and optionally simulate short HTTP reads."""

    def __init__(self, data: bytes, short: bool = False) -> None:
        super().__init__(data)
        self.sizes = []
        self.short = short

    def read(self, size: int = -1) -> bytes:
        """Return an honest finite chunk while recording the caller's bound."""
        self.sizes.append(size)
        return super().read(min(size, 3) if self.short else size)


def response(payload: object) -> Response:
    """Encode provider fixtures as actual multibyte UTF-8 JSON."""
    return Response(json.dumps(payload, ensure_ascii=False).encode())


def validator(action: str) -> Draft202012Validator:
    """Load output schemas and their selected reference without network access."""
    compact = support.read_json(ROOT / "schemas/compact.schema.json")
    registry = Registry().with_resource(
        "compact.schema.json", Resource.from_contents(compact)
    )
    schema = support.read_json(ROOT / f"schemas/{action}.output.schema.json")
    return Draft202012Validator(schema, registry=registry)


class CompactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = support.load_plugin_module(ROOT)
        self.issue = {
            "key": "DEMO-1",
            "fields": {
                "summary": "Title é",
                "status": {"name": "To Do"},
                "description": self.plugin.adf_paragraph('Description é"\\'),
                "attachment": [
                    {
                        "id": "123",
                        "filename": "notes.md",
                        "size": 10,
                        "mimeType": "text/markdown",
                        "content": "https://example.test/attachment/123",
                    }
                ],
            },
        }

    def invoke(self, action: str, payload: dict, responses: object) -> tuple:
        """Run the real handler with dummy auth and an offline HTTP boundary."""
        request = support.app_invoke_request(action, payload)
        with mock.patch.dict(
            self.plugin.os.environ,
            {
                "JIRA_AUTH_HEADER": "Basic fixture",
                "JIRA_SITE_URL": "https://example.test",
            },
        ), mock.patch.object(
            self.plugin.urllib.request, "urlopen", side_effect=responses
        ) as fetch:
            result = self.plugin.handle_request(request)
        support.contract_validator("app/invoke.result.v1.schema.json").validate(result)
        return result, fetch

    def test_read_bound_utf8_exact_and_one_sentinel(self) -> None:
        data = json.dumps(self.issue, ensure_ascii=False).encode()
        for cap, expected in ((len(data), "success"), (len(data) - 1, "failed")):
            reply = Response(data, short=True)
            payload = {
                "issue_key": "DEMO-1",
                "output_mode": "compact",
                "max_response_bytes": cap,
            }
            result, _ = self.invoke("get_issue", payload, [reply])
            self.assertEqual(result["status"], expected)
            self.assertTrue(all(0 < size <= 65536 for size in reply.sizes))
            if expected == "failed":
                self.assertIn("retrieval incomplete", result["error"]["message"])
                self.assertEqual(result["output"], {})
                self.assertEqual(result["error"]["retryability"], "safe")
            self.assertEqual(
                result["metrics"]["result_bytes"], len(json.dumps(result).encode())
            )

    def test_default_response_cap_and_empty_204(self) -> None:
        reply = Response(b" " * (self.plugin.DEFAULT_MAX_RESPONSE_BYTES + 10))
        result, _ = self.invoke("get_issue", {"issue_key": "DEMO-1"}, [reply])
        self.assertEqual(result["status"], "failed")
        self.assertTrue(all(size <= 65536 for size in reply.sizes))
        with mock.patch.object(
            self.plugin.urllib.request, "urlopen", return_value=Response(b"")
        ):
            self.assertEqual(
                self.plugin.jira_request("POST", "https://example.test", "fixture"), {}
            )

    def test_normalized_handoff_is_identical_and_consumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "issue.json"
            payload = {
                "issue_key": "DEMO-1",
                "output_mode": "compact",
                "output_file": str(path),
            }
            result, _ = self.invoke("get_issue", payload, [response(self.issue)])
            validator("get_issue").validate(result["output"])
            self.assertEqual(json.loads(path.read_text()), result["output"])
            self.assertEqual(result["output"]["description_text"], 'Description é"\\')
            self.assertEqual(result["output"]["attachments"][0]["id"], "123")
            self.assertNotIn("raw", result["output"])
            # Both downstream consumers need only normalized fields.
            self.assertTrue(result["output"]["key"] and result["output"]["summary"])
            downloaded, _ = self.invoke(
                "download_attachments",
                {
                    "jira_issue_file": str(path),
                    "output_dir": str(path.parent / "attachments"),
                    "output_mode": "compact",
                },
                [Response(b"notes\n")],
            )
            self.assertEqual(downloaded["output"]["total_downloaded"], 1)
            self.assertEqual(downloaded["output"]["downloaded"][0]["id"], "123")
            validator("download_attachments").validate(downloaded["output"])
            before = path.read_bytes()
            failed, _ = self.invoke(
                "get_issue", payload | {"max_response_bytes": 2}, [response(self.issue)]
            )
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(path.read_bytes(), before)

    def test_search_tokens_survive_compact_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "search.json"
            provider = {
                "issues": [self.issue],
                "nextPageToken": "next-é",
                "isLast": False,
            }
            result, _ = self.invoke(
                "search_issues",
                {
                    "jql": "project=DEMO",
                    "output_mode": "compact",
                    "output_file": str(path),
                },
                [response(provider)],
            )
            self.assertEqual(result["output"]["next_page_token"], "next-é")
            self.assertFalse(result["output"]["is_last"])
            self.assertEqual(json.loads(path.read_text()), result["output"])
            validator("search_issues").validate(result["output"])

    def test_oversized_transition_search_never_mutates(self) -> None:
        result, fetch = self.invoke(
            "transition_issue",
            {"issue_key": "DEMO-1", "status_name": "Done", "max_response_bytes": 900},
            [response(self.issue), Response(b" " * 901)],
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result["error"]["retryability"], "safe")
        self.assertTrue(
            all(c.args[0].get_method() == "GET" for c in fetch.call_args_list)
        )

    def test_legacy_and_closed_compact_diagnostics(self) -> None:
        legacy, _ = self.invoke(
            "get_issue", {"issue_key": "DEMO-1"}, [response(self.issue)]
        )
        self.assertEqual(legacy["output"]["raw"], self.issue)
        self.assertNotIn("output_mode", legacy["output"])
        self.assertEqual(legacy["artifact_hints"], [])
        self.assertEqual(legacy["metrics"], {})
        compact, _ = self.invoke(
            "get_issue",
            {"issue_key": "DEMO-1", "output_mode": "compact"},
            [response(self.issue)],
        )
        size = compact["output"]["diagnostics"]["bytes"]
        for cap, retained in ((size, True), (size - 1, False)):
            result, _ = self.invoke(
                "get_issue",
                {
                    "issue_key": "DEMO-1",
                    "output_mode": "compact",
                    "max_diagnostic_bytes": cap,
                },
                [response(self.issue)],
            )
            self.assertEqual(result["output"]["diagnostics"]["retained"], retained)
            self.assertEqual(bool(result["artifact_hints"]), retained)
            self.assertEqual(result["output"]["description_text"], 'Description é"\\')
        check = validator("get_issue")
        check.validate(legacy["output"])
        check.validate(compact["output"])
        for bad in (
            legacy["output"] | {"unknown": 1},
            compact["output"] | {"raw": {}},
            compact["output"]
            | {"diagnostics": compact["output"]["diagnostics"] | {"unknown": 1}},
        ):
            with self.assertRaises(ValidationError):
                check.validate(bad)

    def test_invalid_bounds_do_not_fetch(self) -> None:
        for value in (0, -1, True):
            result, fetch = self.invoke(
                "get_issue", {"issue_key": "DEMO-1", "max_response_bytes": value}, []
            )
            fetch.assert_not_called()
            self.assertEqual(result["error"]["code"], "validation_error")

    def test_all_jira_output_modes_are_closed_and_require_controls(self) -> None:
        for action in self.plugin.ACTIONS:
            schema = support.read_json(ROOT / f"schemas/{action}.output.schema.json")
            legacy = {}
            keys = schema["required"] + (
                [] if action == "download_attachments" else ["raw"]
            )
            for key in keys:
                kind = schema["properties"][key]["type"]
                legacy[key] = {"string": "", "integer": 0, "array": [], "object": {}}[
                    kind
                ]
            compact = {k: v for k, v in legacy.items() if k != "raw"}
            compact.update(
                output_mode="compact",
                diagnostics={"artifact": "", "bytes": 0, "retained": False},
            )
            if action == "search_issues":
                compact.update(next_page_token="", is_last=True)
            check = validator(action)
            with self.subTest(action=action):
                check.validate(legacy)
                check.validate(compact)
                for bad in (
                    compact | {"raw": {}},
                    compact | {"unknown": 1},
                    compact | {"output_mode": "legacy"},
                    {k: v for k, v in compact.items() if k != "diagnostics"},
                ):
                    with self.assertRaises(ValidationError):
                        check.validate(bad)

    def test_serialization_failure_is_explicit(self) -> None:
        request = support.app_invoke_request("get_issue", {"output_mode": "compact"})
        fixture = self.plugin.invoke_result(
            request["invoke_id"], "success", {"description_text": float("nan")}, None
        )
        with mock.patch.object(self.plugin, "handle_action", return_value=fixture):
            result = self.plugin.handle_request(request)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "adapter_internal_error")
        self.assertEqual(result["output"], {})

    def test_created_issue_response_overflow_requires_operator(self) -> None:
        """The fake provider creates once before its reply exceeds the read budget."""
        for mode in ("legacy", "compact"):
            created = []

            def server(request: object) -> Response:
                """Complete one fake issue creation before returning its JSON reply."""
                self.assertEqual(request.get_method(), "POST")
                self.assertTrue(request.full_url.endswith("/rest/api/3/issue"))
                created.append("DEMO-2")
                return response({"key": "DEMO-2", "id": "10000"})

            payload = {
                "project_key": "DEMO",
                "issue_type": "Task",
                "summary": "fixture",
                "output_mode": mode,
                "max_response_bytes": 1,
            }
            result, fetch = self.invoke("create_issue", payload, server)
            fetch.assert_called_once()
            self.assertEqual(created, ["DEMO-2"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["retryability"], "operator")
            self.assertIn("inspect effects", result["error"]["message"])
            self.assertEqual(result["output"], {})  # Truncated JSON supplies no key.
            if mode == "compact":
                self.assertEqual(
                    result["metrics"]["result_bytes"], len(json.dumps(result).encode())
                )

    def test_transition_post_and_refresh_overflow_require_operator(self) -> None:
        """Overflow at POST or a later GET preserves the prior issue checkpoint."""
        initial = {"key": "DEMO-1", "fields": {"status": {"name": "To Do"}}}
        transitions = {
            "transitions": [{"id": "1", "name": "Done", "to": {"name": "Done"}}]
        }
        for mode in ("legacy", "compact"):
            for overflow_at_post in (True, False):
                changed = []
                get_count = 0

                def server(request: object) -> Response:
                    """Apply the transition once, then overflow its reply or refresh."""
                    nonlocal get_count
                    if request.get_method() == "POST":
                        changed.append("Done")
                        return Response(b"x" * 129 if overflow_at_post else b"")
                    get_count += 1
                    if get_count == 1:
                        return response(initial)
                    if get_count == 2:
                        return response(transitions)
                    return Response(b"x" * 129)

                with tempfile.TemporaryDirectory() as directory:
                    path = pathlib.Path(directory) / "issue.json"
                    path.write_text("previous handoff")
                    payload = {
                        "issue_key": "DEMO-1",
                        "status_name": "Done",
                        "max_response_bytes": 128,
                        "output_mode": mode,
                        "output_file": str(path),
                    }
                    result, fetch = self.invoke("transition_issue", payload, server)
                    self.assertEqual(path.read_text(), "previous handoff")
                self.assertEqual(fetch.call_count, 3 if overflow_at_post else 4)
                self.assertEqual(changed, ["Done"])
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"]["retryability"], "operator")
                self.assertEqual(result["output"]["key"], "DEMO-1")
                self.assertEqual(result["output"]["previous_status"], "To Do")
                self.assertEqual(result["output"]["status"], "To Do")
                validator("transition_issue").validate(result["output"])

    def test_search_post_response_overflow_remains_read_safe(self) -> None:
        """A Jira search POST is a read and must not cross the mutation boundary."""
        result, fetch = self.invoke(
            "search_issues",
            {"jql": "project=DEMO", "max_response_bytes": 1},
            [response({"issues": []})],
        )
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0].get_method(), "POST")
        self.assertEqual(result["error"]["retryability"], "safe")

    def test_created_identifiers_survive_output_policy_failure(self) -> None:
        """A post-create handoff failure retains the known key without safe replay."""
        for mode in ("legacy", "compact"):
            for raises in (False, True):
                failure = self.plugin.failed_result(
                    "smoke-invoke", "output policy rejected"
                )
                effect = (
                    {"side_effect": RuntimeError("output policy rejected")}
                    if raises
                    else {"return_value": failure}
                )
                with mock.patch.object(self.plugin, "write_output_file", **effect):
                    result, fetch = self.invoke(
                        "create_issue",
                        {
                            "project_key": "DEMO",
                            "issue_type": "Task",
                            "summary": "fixture",
                            "output_mode": mode,
                        },
                        [response({"key": "DEMO-2", "id": "10000"})],
                    )
                fetch.assert_called_once()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"]["retryability"], "operator")
                self.assertEqual(result["output"]["key"], "DEMO-2")
                self.assertEqual(result["output"]["id"], "10000")
                validator("create_issue").validate(result["output"])


if __name__ == "__main__":
    unittest.main()
