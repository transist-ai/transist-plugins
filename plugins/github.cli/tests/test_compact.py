"""Offline retrieval budgets, compact handoffs and review-state regressions."""

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


def validator(action: str) -> Draft202012Validator:
    """Resolve action output schemas locally and without remote retrieval."""
    compact = support.read_json(ROOT / "schemas/compact.schema.json")
    registry = Registry().with_resource(
        "compact.schema.json", Resource.from_contents(compact)
    )
    schema = support.read_json(ROOT / f"schemas/{action}.output.schema.json")
    return Draft202012Validator(schema, registry=registry)


def completed(value: object, stderr: str = "") -> object:
    """Build a completed fixture with real JSON encoding and optional diagnostics."""
    return support.completed_process(
        [], 0, json.dumps(value, ensure_ascii=False), stderr
    )


class CompactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = support.load_plugin_module(ROOT)

    def invoke(self, action: str, payload: dict, processes: list) -> tuple:
        """Exercise real request handling with offline subprocess replies."""
        request = support.app_invoke_request(action, payload)
        with mock.patch.object(
            self.plugin, "run_process", side_effect=processes
        ) as run:
            result = self.plugin.handle_request(request)
        support.contract_validator("app/invoke.result.v1.schema.json").validate(result)
        return result, run

    def test_all_closed_contracts_both_modes(self) -> None:
        for path in (ROOT / "schemas").glob("*.output.schema.json"):
            schema = support.read_json(path)
            legacy = {}
            for key in schema["required"] + [
                k for k in ("stdout", "stderr") if k in schema["properties"]
            ]:
                kind = schema["properties"][key].get("type", "null")
                legacy[key] = {
                    "string": "",
                    "boolean": True,
                    "integer": 0,
                    "array": [],
                    "object": {},
                    "null": None,
                }[kind]
                if "enum" in schema["properties"][key]:
                    legacy[key] = schema["properties"][key]["enum"][0]
            compact = {k: v for k, v in legacy.items() if k not in ("stdout", "stderr")}
            compact.update(
                output_mode="compact",
                diagnostics={
                    "artifact": "",
                    "bytes": 0,
                    "retained": False,
                    "stdout_bytes": 0,
                    "stderr_bytes": 0,
                },
            )
            check = validator(path.name.split(".output")[0])
            with self.subTest(action=path.name):
                check.validate(legacy)
                check.validate(compact)
                for bad in (
                    legacy | {"unknown": 1},
                    compact | {"stdout": "duplicate"},
                    compact | {"output_mode": "legacy"},
                    compact | {"diagnostics": compact["diagnostics"] | {"unknown": 1}},
                ):
                    with self.assertRaises(ValidationError):
                        check.validate(bad)

    def test_review_full_semantics_handoff_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "snapshot.json"
            comment = {
                "id": "node",
                "databaseId": 123,
                "body": 'é"\\\n' * 300,
                "diffHunk": "@@ full hunk @@\n" * 300,
                "path": "file.py",
                "createdAt": "2099-01-01T00:00:00Z",
                "author": {"login": "reviewer"},
            }
            pr = {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {
                            "id": "thread",
                            "isResolved": False,
                            "comments": {
                                "nodes": [comment],
                                "pageInfo": {"hasNextPage": False},
                            },
                        }
                    ],
                },
                "conversationComments": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": False},
                },
            }
            response = {"data": {"repository": {"pullRequest": pr}}}
            payload = {
                "repo": "example/repo",
                "output_file": str(path),
                "include_pr_conversation_comments": True,
                "output_mode": "compact",
            }
            with mock.patch.object(
                self.plugin, "utc_now_rfc3339", return_value="2026-01-01T00:00:00Z"
            ):
                result, _ = self.invoke(
                    "pr_unresolved_comments",
                    payload,
                    [completed({"number": 7}), completed(response)],
                )
            validator("pr_unresolved_comments").validate(result["output"])
            record = result["output"]["comments"][0]
            self.assertEqual(record["body"], comment["body"])
            self.assertEqual(record["diff_hunk"], comment["diffHunk"])
            self.assertEqual(record["thread_id"], "thread")
            self.assertEqual(record["review_comment_id"], "123")
            self.assertEqual(json.loads(path.read_text()), result["output"])
            self.assertEqual(
                [h["name"] for h in result["artifact_hints"]], ["diagnostics.json"]
            )
            self.assertEqual(
                result["metrics"]["result_bytes"], len(json.dumps(result).encode())
            )
            before = path.read_bytes()
            for connection in (
                pr["reviewThreads"],
                pr["reviewThreads"]["nodes"][0]["comments"],
                pr["conversationComments"],
            ):
                connection["pageInfo"]["hasNextPage"] = True
                failed, _ = self.invoke(
                    "pr_unresolved_comments",
                    payload,
                    [completed({"number": 7}), completed(response)],
                )
                self.assertEqual(failed["status"], "failed")
                self.assertIn("incomplete", failed["error"]["message"])
                self.assertEqual(path.read_bytes(), before)
                connection["pageInfo"]["hasNextPage"] = False

    def test_upsert_page_exhaustion_never_mutates_even_with_marker(self) -> None:
        comments = [
            {"id": index, "body": "marker" if index == 0 else "other"}
            for index in range(100)
        ]
        for mode in ("legacy", "compact"):
            payload = {
                "repo": "example/repo",
                "marker": "marker",
                "body": "updated",
                "max_comment_pages": 1,
                "output_mode": mode,
            }
            result, run = self.invoke(
                "pr_comment_upsert",
                payload,
                [completed({"number": 7}), completed(comments)],
            )
            self.assertEqual(result["status"], "failed")
            self.assertIn("search incomplete", result["error"]["message"])
            self.assertEqual(result["error"]["retryability"], "safe")
            self.assertEqual(run.call_count, 2)
            self.assertFalse(
                any(
                    "POST" in c.args[0] or "PATCH" in c.args[0]
                    for c in run.call_args_list
                )
            )

    def test_upsert_byte_and_count_exhaustion_never_mutates(self) -> None:
        comments = [{"id": 1, "body": "é" * 100}, {"id": 2, "body": "marker"}]
        for budget in (
            {"max_response_bytes": 50},
            {"max_total_response_bytes": 50},
            {"max_comments": 1},
        ):
            payload = {
                "repo": "example/repo",
                "marker": "marker",
                "body": "new",
                "output_mode": "compact",
            } | budget
            result, run = self.invoke(
                "pr_comment_upsert",
                payload,
                [completed({"number": 7}), completed(comments)],
            )
            self.assertEqual(result["status"], "failed")
            self.assertIn("incomplete", result["error"]["message"])
            self.assertEqual(result["error"]["retryability"], "safe")
            self.assertEqual(run.call_count, 2)

    def test_retrieval_and_diagnostic_utf8_escaping_boundaries(self) -> None:
        process = completed({"number": 7, "title": 'é"\\\n'})
        size = len(process.stdout.encode())
        for cap, status in ((size, "success"), (size - 1, "failed")):
            result, _ = self.invoke(
                "pr_view",
                {"output_mode": "compact", "max_response_bytes": cap},
                [process],
            )
            self.assertEqual(result["status"], status)
            self.assertEqual(
                result["metrics"]["result_bytes"], len(json.dumps(result).encode())
            )
        first, _ = self.invoke("pr_view", {"output_mode": "compact"}, [process])
        artifact_size = first["output"]["diagnostics"]["bytes"]
        for cap, retained in ((artifact_size, True), (artifact_size - 1, False)):
            result, _ = self.invoke(
                "pr_view",
                {"output_mode": "compact", "max_diagnostic_bytes": cap},
                [process],
            )
            self.assertEqual(result["output"]["diagnostics"]["retained"], retained)
            self.assertEqual(result["output"]["pull_request"]["title"], 'é"\\\n')

    def test_oversized_reply_plan_rejected_before_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "plan.json"
            data = json.dumps(
                {
                    "responses": [
                        {"comment_id": "one", "reply": "é"},
                        {"comment_id": "two", "reply": "fix"},
                    ]
                },
                ensure_ascii=False,
            ).encode()
            path.write_bytes(data)
            for budget in ({"max_plan_bytes": len(data) - 1}, {"max_replies": 1}):
                payload = {
                    "response_plan_file": str(path),
                    "output_mode": "compact",
                } | budget
                result, run = self.invoke("pr_publish_review_replies", payload, [])
                self.assertEqual(result["status"], "failed")
                run.assert_not_called()
            parsed = self.plugin.read_json_file(
                {}, str(path), {"max_plan_bytes": len(data)}
            )
            self.assertEqual(len(parsed["responses"]), 2)

    def test_reply_hash_state_and_duplicate_checkpoint_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            plan = path / "plan.json"
            state = path / "state.json"
            plan.write_text(
                json.dumps(
                    {
                        "responses": [
                            {
                                "comment_id": "node",
                                "source": "review_thread",
                                "review_comment_id": "123",
                                "reply": "Applied  the fix.",
                            }
                        ]
                    }
                )
            )
            payload = {
                "repo": "example/repo",
                "response_plan_file": str(plan),
                "state_file": str(state),
            }
            item = self.plugin.publication_plan(json.loads(plan.read_text()), 1000)[0]
            identity = self.plugin.reply_publication_identity(
                "example", "repo", 7, item
            )
            body = (
                item["legacy_body"]
                + f"\n<!-- transist-review-publication:{identity} -->"
            )
            with mock.patch.object(
                self.plugin, "utc_now_rfc3339", return_value="fixed-time"
            ):
                legacy, _ = self.invoke(
                    "pr_publish_review_replies",
                    payload,
                    [
                        completed({"number": 7}),
                        completed([]),
                        completed([]),
                        completed(
                            {
                                "id": 42,
                                "html_url": "https://example.test/42",
                                "body": body,
                                "in_reply_to_id": 123,
                            }
                        ),
                    ],
                )
                checkpoint = state.read_bytes()
                compact, run = self.invoke(
                    "pr_publish_review_replies",
                    payload | {"output_mode": "compact"},
                    [completed({"number": 7})],
                )
            self.assertEqual(state.read_bytes(), checkpoint)
            self.assertEqual(
                json.loads(checkpoint)["responses"]["node"]["reply_hash"],
                self.plugin.sha256_text("Applied the fix."),
            )
            self.assertEqual(legacy["output"]["posted_count"], 1)
            self.assertEqual(compact["output"]["skipped_count"], 1)
            self.assertEqual(compact["output"]["posted_count"], 0)
            self.assertEqual(
                compact["output"]["responses"][0]["posted_url"],
                "https://example.test/42",
            )
            self.assertEqual(run.call_count, 1)
            validator("pr_publish_review_replies").validate(compact["output"])

    def test_default_legacy_shape_and_approval_controls(self) -> None:
        pr = {
            "number": 7,
            "state": "OPEN",
            "author": {"login": "author"},
            "latestReviews": [{"author": {"login": "reviewer"}, "state": "APPROVED"}],
        }
        for mode in (None, "legacy", "compact"):
            payload = {"required_approvals": 2}
            if mode:
                payload["output_mode"] = mode
            result, _ = self.invoke("pr_check_approvals", payload, [completed(pr)])
            self.assertFalse(result["output"]["ok"])
            self.assertEqual(result["output"]["approval_count"], 1)
            self.assertEqual(result["output"]["required_approvals"], 2)
            self.assertEqual(result["output"]["approved_reviewers"], ["reviewer"])
            validator("pr_check_approvals").validate(result["output"])
            self.assertEqual("stdout" in result["output"], mode != "compact")

    def test_serialization_failure_is_explicit(self) -> None:
        request = support.app_invoke_request("pr_view", {"output_mode": "compact"})
        fixture = self.plugin.invoke_result(
            request, "success", {"pull_request": {"number": float("nan")}}
        )
        with mock.patch.object(self.plugin, "handle_action", return_value=fixture):
            result = self.plugin.handle_request(request)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "adapter_internal_error")
        self.assertEqual(result["output"], {})

    def test_default_artifact_cap_does_not_truncate_review_data(self) -> None:
        title = 'é"\\' * 70000
        result, _ = self.invoke(
            "pr_view", {"output_mode": "compact"}, [completed({"title": title})]
        )
        self.assertEqual(result["output"]["pull_request"]["title"], title)
        self.assertGreater(result["output"]["diagnostics"]["bytes"], 256 * 1024)
        self.assertFalse(result["output"]["diagnostics"]["retained"])
        self.assertEqual(result["artifact_hints"], [])
        self.assertEqual(result["output"]["diagnostics"]["artifact"], "")


if __name__ == "__main__":
    unittest.main()
