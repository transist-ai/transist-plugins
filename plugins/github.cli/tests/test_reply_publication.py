"""Exact remote reply reconciliation, bounded reads and all-or-nothing local state."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
import support  # noqa: E402
from test_compact import validator  # noqa: E402


class ReplyPublicationTests(unittest.TestCase):
    """Use the actual APP handler and a finite offline REST transport."""

    def setUp(self) -> None:
        self.plugin = support.load_plugin_module(ROOT)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plan = self.root / "plan.json"
        self.state = self.root / "state.json"
        self.output = self.root / "out.json"
        self.state.write_text('{"responses":{"retained":{"note":"preserve"}}}\n')
        self.output.write_text("previous output")
        self.before = self.state.read_bytes()
        self.remote = {"issues": [], "pulls": []}
        self.calls = []
        self.lose_at = 0
        self.posts = 0
        self.read_error = None
        self.post_response = None
        self.endless_pages = False

    def response(self, source: str = "pr_conversation", **extra: object) -> dict:
        """Return existing workflow plan fields, never a new schema-side identity."""
        return {
            "comment_id": "request-exact",
            "source": source,
            "reply": "Exact  café clarification.",
            "url": "https://github.com/fixture/repo/pull/7#discussion_r42",
            **({"review_comment_id": "42"} if source == "review_thread" else {}),
            **extra,
        }

    def transport(self, args: list, **kwargs: object) -> object:
        """Accept remote writes independently of their returned transport status."""
        self.calls.append(args)
        self.assertEqual(kwargs["cwd"], str(self.root))
        self.assertIsNone(kwargs["timeout"])
        if args[:3] == ["gh", "pr", "view"]:
            return support.completed_process(args, 0, '{"number":7}', "")
        surface = "issues" if any("/issues/" in a for a in args) else "pulls"
        if "GET" in args:
            if self.read_error is not None:
                return self.read_error
            page = int(next(a[5:] for a in args if a.startswith("page=")))
            batch = self.remote[surface][(page - 1) * 100 : page * 100]
            if self.endless_pages:
                batch = [
                    {
                        "id": page * 100 + i,
                        "body": "other",
                        "html_url": "https://example.test/1",
                    }
                    for i in range(100)
                ]
            return support.completed_process(args, 0, json.dumps(batch), "")
        self.assertIn("POST", args)
        self.posts += 1
        comment = {
            "id": self.posts,
            "body": next(a[5:] for a in args if a.startswith("body=")),
            "html_url": f"https://github.com/fixture/repo/pull/7#{self.posts}",
        }
        if surface == "pulls":
            comment["in_reply_to_id"] = int(
                next(a[12:] for a in args if a.startswith("in_reply_to="))
            )
        self.remote[surface].append(comment)
        return support.completed_process(
            args,
            int(self.posts == self.lose_at),
            (
                ""
                if self.posts == self.lose_at
                else json.dumps(
                    comment if self.post_response is None else self.post_response
                )
            ),
            "response lost" if self.posts == self.lose_at else "",
        )

    def invoke(self, responses: list, **options: object) -> dict:
        """Execute the bounded public request and validate successful output/files."""
        self.plan.write_text(json.dumps({"responses": responses}))
        payload = {
            "repo": "fixture/repo",
            "response_plan_file": str(self.plan),
            "state_file": str(self.state),
            "output_file": str(self.output),
            "output_mode": "compact",
            **options,
        }
        request = support.app_invoke_request("pr_publish_review_replies", payload)
        request["paths"]["workspace_dir"] = str(self.root)
        with mock.patch.object(self.plugin, "run_process", side_effect=self.transport):
            result = self.plugin.handle_request(request)
        support.contract_validator("app/invoke.result.v1.schema.json").validate(result)
        if result["status"] == "success":
            validator("pr_publish_review_replies").validate(result["output"])
            self.assertEqual(json.loads(self.output.read_text()), result["output"])
        return result

    def assert_preserved(self) -> None:
        """A failed invocation must not replace either previous local file."""
        self.assertEqual(self.state.read_bytes(), self.before)
        self.assertEqual(self.output.read_text(), "previous output")

    def test_accepted_lost_response_both_surfaces_missing_or_old_state(self) -> None:
        for source in ("pr_conversation", "review_thread"):
            with self.subTest(source=source):
                self.setUp()
                response = self.response(source)
                self.lose_at = 1
                self.assertEqual(self.invoke([response])["status"], "failed")
                self.assert_preserved()
                first = copy.deepcopy(self.remote)
                if source == "review_thread":
                    self.state.unlink()
                boundary = len(self.calls)
                result = self.invoke([response])
                self.assertEqual(result["status"], "success", result)
                self.assertEqual(self.remote, first)
                self.assertEqual(self.posts, 1)
                self.assertEqual(result["output"]["posted_count"], 0)
                self.assertEqual(result["output"]["skipped_count"], 1)
                self.assertEqual(
                    result["output"]["responses"][0]["comment_id"], "request-exact"
                )
                self.assertTrue(any("GET" in c for c in self.calls[boundary:]))
                self.assertFalse(any("POST" in c for c in self.calls[boundary:]))

    def test_partial_batch_failure_reconciles_every_accepted_reply(self) -> None:
        responses = [
            self.response(),
            self.response("review_thread", comment_id="second"),
        ]
        self.lose_at = 2
        self.assertEqual(self.invoke(responses)["status"], "failed")
        self.assert_preserved()
        result = self.invoke(responses)
        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["output"]["skipped_count"], 2)
        self.assertEqual(self.posts, 2)
        self.assertEqual(
            json.loads(self.state.read_text())["responses"]["retained"],
            {"note": "preserve"},
        )

    def test_complete_later_pages_both_surfaces(self) -> None:
        responses = [
            self.response(),
            self.response("review_thread", comment_id="thread"),
        ]
        self.assertEqual(self.invoke(responses)["status"], "success")
        for surface in self.remote:
            self.remote[surface][:0] = [
                {
                    "id": i + 1000,
                    "body": "unrelated",
                    "html_url": "https://example.test/comment",
                }
                for i in range(100)
            ]
        self.state.write_bytes(self.before)
        self.calls.clear()
        result = self.invoke(responses)
        self.assertEqual(result["output"]["skipped_count"], 2)
        self.assertEqual(self.posts, 2)
        self.assertEqual(sum("page=2" in c for c in self.calls), 2)

    def test_completed_local_state_preserves_records_without_remote_fetch(self) -> None:
        response = self.response()
        self.assertEqual(self.invoke([response])["status"], "success")
        saved = json.loads(self.state.read_text())["responses"]
        self.calls.clear()
        result = self.invoke([response])
        self.assertEqual(result["output"]["skipped_count"], 1)
        self.assertFalse(any("api" in c for c in self.calls))
        self.assertEqual(json.loads(self.state.read_text())["responses"], saved)

    def test_exact_identity_not_substring_or_reply_text(self) -> None:
        first = self.response(comment_id="request-exact-suffix")
        self.assertEqual(self.invoke([first])["status"], "success")
        self.state.write_bytes(self.before)
        self.assertEqual(self.invoke([self.response()])["output"]["posted_count"], 1)
        self.assertEqual(self.posts, 2)
        bodies = [c["body"] for c in self.remote["issues"]]
        self.assertNotEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[0].splitlines()[:-1], bodies[1].splitlines()[:-1])

    def test_same_identity_changed_body_or_target_fails_before_any_post(self) -> None:
        response = self.response("review_thread")
        self.assertEqual(self.invoke([response])["status"], "success")
        self.state.write_bytes(self.before)
        self.output.write_text("previous output")
        for change in (
            {"body": self.remote["pulls"][0]["body"].replace("café", "changed")},
            {"in_reply_to_id": 43},
        ):
            with self.subTest(change=change):
                original = copy.deepcopy(self.remote["pulls"][0])
                self.remote["pulls"][0].update(change)
                result = self.invoke([self.response(comment_id="new"), response])
                self.assertEqual(result["status"], "failed")
                self.assertIn("conflicts", result["error"]["message"])
                self.assertEqual(self.posts, 1)
                self.assert_preserved()
                self.remote["pulls"][0] = original

    def test_legacy_generic_marker_is_not_identity_proof(self) -> None:
        response = self.response()
        self.remote["issues"] = [
            {
                "id": 9,
                "body": self.plugin.pr_conversation_reply_body(
                    response["url"], "Exact café clarification."
                ),
                "html_url": "https://example.test/9",
            }
        ]
        result = self.invoke([response])
        self.assertEqual(result["status"], "failed")
        self.assertIn("no exact publication identity", result["error"]["message"])
        self.assertEqual(self.posts, 0)
        self.assert_preserved()

    def test_incomplete_error_malformed_and_byte_bounds_never_post(self) -> None:
        for process in (
            support.completed_process([], 1, "", "transient timeout"),
            support.completed_process([], 0, "", ""),
            support.completed_process([], 0, '{"errors":[]}', ""),
            support.completed_process([], 0, '[{"id":1,"body":null}]', ""),
            support.completed_process([], 0, '[{"id":1,"body":"a","body":"b"}]', ""),
        ):
            self.read_error = process
            result = self.invoke([self.response()])
            self.assertEqual(result["status"], "failed", result)
            self.assertEqual(self.posts, 0)
            self.assert_preserved()
        self.read_error = None
        for option in ({"max_response_bytes": 1}, {"max_total_response_bytes": 15}):
            self.assertEqual(
                self.invoke([self.response()], **option)["status"], "failed"
            )
            self.assertEqual(self.posts, 0)
            self.assert_preserved()

    def test_fixed_page_bound_fails_closed(self) -> None:
        self.endless_pages = True
        result = self.invoke([self.response()])
        self.assertEqual(result["status"], "failed")
        self.assertIn("page budget", result["error"]["message"])
        self.assertEqual(sum("GET" in c for c in self.calls), 100)
        self.assertEqual(self.posts, 0)
        self.assert_preserved()

    def test_combined_comment_count_bound_fails_closed(self) -> None:
        self.remote = {
            surface: [
                {"id": i + 1, "body": "other", "html_url": "https://example.test/1"}
                for i in range(count)
            ]
            for surface, count in (("issues", 9999), ("pulls", 2))
        }
        result = self.invoke([self.response()])
        self.assertEqual(result["status"], "failed")
        self.assertIn("count budget", result["error"]["message"])
        self.assertEqual(self.posts, 0)
        self.assert_preserved()

    def test_match_does_not_bypass_later_page_error(self) -> None:
        response = self.response()
        self.assertEqual(self.invoke([response])["status"], "success")
        self.state.write_bytes(self.before)
        self.output.write_text("previous output")
        self.remote["pulls"] = [None]
        result = self.invoke([response])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.posts, 1)
        self.assert_preserved()

    def test_read_byte_budget_exact_boundary_and_one_byte_under(self) -> None:
        total = len('{"number":7}'.encode()) + len(b"[]") * 2
        result = self.invoke([self.response()], max_total_response_bytes=total - 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.posts, 0)
        result = self.invoke([self.response()], max_total_response_bytes=total)
        self.assertEqual(result["status"], "success", result)

    def test_duplicate_page_ids_and_malformed_marker_fail_closed(self) -> None:
        response = self.response()
        self.assertEqual(self.invoke([response])["status"], "success")
        self.state.write_bytes(self.before)
        self.output.write_text("previous output")
        accepted = copy.deepcopy(self.remote["issues"][0])
        for comments in (
            [accepted, accepted],
            [{**accepted, "body": accepted["body"][:-4]}],
        ):
            self.remote["issues"] = comments
            self.assertEqual(self.invoke([response])["status"], "failed")
            self.assertEqual(self.posts, 1)
            self.assert_preserved()

    def test_invalid_state_hashes_are_rejected_before_provider(self) -> None:
        self.state.write_text('{"responses":{"request-exact":{"reply_hash":true}}}')
        before = self.state.read_bytes()
        result = self.invoke([self.response()])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.state.read_bytes(), before)

    def test_malformed_post_acknowledgment_never_becomes_local_identity_proof(
        self,
    ) -> None:
        for index, acknowledgment in enumerate(
            ({"html_url": "https://example.test/1"}, {"id": True}, {"body": "changed"})
        ):
            response = self.response(comment_id=f"request-{index}")
            self.post_response = acknowledgment
            self.assertEqual(self.invoke([response])["status"], "failed")
            self.assert_preserved()
            self.post_response = None
            result = self.invoke([response])
            self.assertEqual(result["status"], "success", result)
            self.assertEqual(result["output"]["skipped_count"], 1)
            self.assertEqual(self.posts, index + 1)
            self.state.write_bytes(self.before)
            self.output.write_text("previous output")

    def test_malformed_plan_entries_are_all_checked_before_provider(self) -> None:
        for malformed in (
            None,
            {},
            self.response(reply=3),
            self.response(source="unknown"),
            self.response(review_comment_id="42bad"),
            self.response(comment_id=" request "),
        ):
            self.calls.clear()
            result = self.invoke([self.response(), malformed])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(self.calls, [])
            self.assert_preserved()

    def test_output_publication_failure_preserves_prior_state_and_reentry_reconciles(
        self,
    ) -> None:
        with mock.patch.object(
            self.plugin.os, "replace", side_effect=OSError("fixture")
        ):
            result = self.invoke([self.response()])
        self.assertEqual(result["status"], "failed")
        self.assert_preserved()
        self.assertEqual(self.posts, 1)
        self.assertEqual(self.invoke([self.response()])["output"]["skipped_count"], 1)
        self.assertEqual(self.posts, 1)
        self.assertEqual(
            sorted(p.name for p in self.root.iterdir()),
            ["out.json", "plan.json", "state.json"],
        )


if __name__ == "__main__":
    unittest.main()
