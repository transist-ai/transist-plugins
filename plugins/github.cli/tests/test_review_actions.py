"""Extracted pending-review behavior, typed decisions and bounded transport tests."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
import support  # noqa: E402
from review_action_fixtures import (  # noqa: E402
    comment,
    fetch,
    merge_cases,
    invalid_projected_locations,
    valid_projected_locations,
)
from test_compact import validator, completed  # noqa: E402


class ReviewActions(unittest.TestCase):
    """Exercise real APP handlers, schemas and file data in disposable storage."""

    def setUp(self) -> None:
        self.plugin = support.load_plugin_module(ROOT)
        self.review = self.plugin.review_context
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def invoke(self, action: str, payload: dict, replies: list | None = None) -> dict:
        """Validate actual APP results and successful action outputs."""
        request = support.app_invoke_request(action, payload)
        request["paths"]["workspace_dir"] = str(self.root)
        with patch.object(self.plugin, "run_process", side_effect=replies or []) as run:
            result = self.plugin.handle_request(request)
        self.calls = run.call_count
        support.contract_validator("app/invoke.result.v1.schema.json").validate(result)
        if result["output"]:
            validator(action).validate(result["output"])
        return result

    def projection(
        self, current: dict, pending: dict | None = None, mode: str = "legacy"
    ) -> dict:
        """Pass real staged absolute paths outside the selected workspace."""
        for name, value in (("raw", current), ("pr", {"state": "OPEN"})):
            (self.root / (name + ".json")).write_text(json.dumps(value))
        output = self.root / "pending.json"
        payload = {
            "input_file": str(self.root / "raw.json"),
            "pr_file": str(self.root / "pr.json"),
            "output_file": str(output),
            "output_mode": mode,
        }
        if pending is not None:
            output.write_bytes(self.review.encode(pending))
            payload["pending_file"] = str(output)
        return self.invoke("pr_prepare_review_context", payload)

    def test_pending_pre_gate_request_survives_new_cutoff_and_deduplication(
        self,
    ) -> None:
        initial = self.review.project(fetch([comment("pending")]), {}, {})
        for mode in ("legacy", "compact"):
            result = self.projection(
                fetch(
                    [
                        comment("old"),
                        comment("pending", body="!!! Edited exact request"),
                        comment("fresh", created_at="2026-01-01T12:00:00Z"),
                        comment("fresh", created_at="2026-01-01T12:00:00Z"),
                        comment(
                            "thread",
                            source="review_thread",
                            thread_id="T",
                            review_comment_id="23",
                            diff_hunk="@@ -5,2 +7,2 @@\nduplicate body",
                        ),
                        comment("bot", body="<!-- transist-review-reply -->\nhandled"),
                        comment("slash", body="\n/retry please"),
                        comment(
                            "mandatory",
                            body="!!! /fix this",
                            created_at="2026-01-01T12:00:00Z",
                        ),
                    ],
                    pr_conversation_comments_after="2026-01-01T11:00:00Z",
                ),
                initial,
                mode,
            )
            self.assertEqual(result["status"], "success")
            context = json.loads((self.root / "pending.json").read_bytes())
            self.assertEqual(
                [c["id"] for c in context["comments"]],
                ["pending", "fresh", "thread", "mandatory"],
            )
            self.assertEqual(context["comments"][0]["body"], "!!! Edited exact request")
            self.assertEqual(
                context["comments"][2]["diff_hunk_header"], "@@ -5,2 +7,2 @@"
            )
            self.assertNotIn("diff_hunk", context["comments"][2])
            self.assertEqual(self.calls, 0)

    def test_resolved_threads_retire_while_conversations_and_current_edits_survive(
        self,
    ) -> None:
        thread = comment("T", source="review_thread", thread_id="T")
        initial = self.review.project(fetch([thread, comment("C")]), {}, {})
        result = self.review.project(
            fetch([], pr_conversation_comments_after="2026-01-01T11:00:00Z"),
            initial,
            {},
        )
        self.assertEqual([c["id"] for c in result["comments"]], ["C"])
        result = self.review.project(
            fetch([{**thread, "body": "changed"}]), initial, {}
        )
        self.assertEqual(result["comments"][1]["body"], "changed")

    def test_incomplete_or_malformed_fetch_preserves_pending_bytes(self) -> None:
        initial = self.review.project(
            fetch([comment("T", source="review_thread")]), {}, {}
        )
        cases = [fetch([], ok=False), fetch([], exit_code=1), fetch([], count=True)]
        for field in fetch([]):
            value = fetch([])
            value.pop(field)
            cases.append(value)
        cases += [
            fetch([], review_thread_comment_count=1),
            fetch([], last_successful_fetch_completed_at="invalid"),
            fetch([comment("T", body=None)]),
            fetch([comment("T", source="unknown")]),
            fetch([comment("T", outdated="false")]),
        ]
        for value in cases:
            with self.subTest(value=value):
                result = self.projection(value, initial)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(
                    (self.root / "pending.json").read_bytes(),
                    self.review.encode(initial),
                )

    def test_malformed_and_over_budget_context_fail_not_drop_requests(self) -> None:
        for current in (
            fetch([comment("")]),
            fetch(["invalid"]),
            fetch([], pr_conversation_comments_after="bad"),
            fetch([comment("big", body="é" * 32768)]),
        ):
            with self.assertRaises(ValueError):
                self.review.project(current, {}, {})
        for pending in (
            {"comments": {}},
            {"count": True, "comments": []},
            {"other": []},
        ):
            with self.assertRaises(ValueError):
                self.review.project(fetch([]), pending, {})

    def test_invalid_projected_locations_never_overwrite_pending(self) -> None:
        """Reject surviving malformed fields before even calling the file writer."""
        initial = self.review.project(fetch([comment("pending")]), {}, {})
        for field, value in invalid_projected_locations():
            for origin in ("fresh", "pending"):
                for mode in ("legacy", "compact"):
                    bad = comment("fresh", source="review_thread", **{field: value})
                    current, pending = fetch([bad]), initial
                    if origin == "pending":
                        current = fetch([])
                        pending = {"comments": [comment("pending", **{field: value})]}
                    with self.subTest(
                        field=field, value=value, origin=origin, mode=mode
                    ):
                        before = self.review.encode(pending)
                        with patch.object(
                            self.review, "write", wraps=self.review.write
                        ) as write:
                            result = self.projection(current, pending, mode)
                        self.assertEqual(result["status"], "failed")
                        self.assertEqual(result["error"]["code"], "validation_error")
                        self.assertEqual(
                            result["error"]["message"],
                            "Review projection failed; "
                            "pending requests were not discarded",
                        )
                        write.assert_not_called()
                        self.assertEqual(
                            (self.root / "pending.json").read_bytes(), before
                        )

    def test_valid_projected_location_types_and_bounds_remain_unchanged(self) -> None:
        """Nulls/integral floats and unconstrained side/line ranges match the schema."""
        for locations in valid_projected_locations():
            for origin in ("fresh", "pending"):
                original = comment("located", **locations)
                current, pending = fetch([original]), {}
                if origin == "pending":
                    current, pending = fetch([]), {"comments": [original]}
                with self.subTest(locations=locations, origin=origin):
                    result = self.projection(current, pending)
                    self.assertEqual(result["status"], "success")
                    for field, value in locations.items():
                        self.assertEqual(result["output"]["comments"][0][field], value)
                    self.assertEqual(self.calls, 0)

    def test_explicit_projection_shape_matches_every_output_schema_field(self) -> None:
        """Keep the dependency-free guard aligned with all declared field types."""
        base = self.review.project(fetch([comment("located")]), {}, {})
        schema = support.read_json(
            ROOT / "schemas/pr_prepare_review_context.output.schema.json"
        )
        fields = schema["properties"]["comments"]["items"]["properties"]
        check = validator("pr_prepare_review_context")
        variants = []
        values = (None, "", "custom", True, False, -1, 0, 12.0, 12.5, [], {})
        for field in (*fields, "unexpected", "diff_hunk"):
            for value in values:
                context = deepcopy(base)
                context["comments"][0][field] = value
                variants.append(context)
        for field in ("id", "source", "body"):
            context = deepcopy(base)
            del context["comments"][0][field]
            variants.append(context)
        for field in ("comments", "count", "pull_request", "unexpected"):
            for value in values:
                variants.append({**base, field: value})
        variants += [
            {key: value for key, value in base.items() if key != field}
            for field in base
        ]
        for context in variants:
            with self.subTest(context=context):
                expected = check.is_valid({"ok": True, **context})
                if expected:
                    self.review.validate_projection(context)
                else:
                    with self.assertRaises(ValueError):
                        self.review.validate_projection(context)

    def test_direct_write_validates_before_opening_output(self) -> None:
        """Future callers cannot bypass shape validation or publish non-JSON numbers."""
        output = self.root / "pending.json"
        output.write_bytes(b"previous pending bytes\n")
        base = self.review.project(fetch([comment("located")]), {}, {})
        for field, value in invalid_projected_locations():
            context = deepcopy(base)
            context["comments"][0][field] = value
            with self.subTest(field=field, value=value), patch.object(
                self.review, "parent_fd"
            ) as opened:
                with self.assertRaises(ValueError):
                    self.review.write(output, context)
                opened.assert_not_called()
                self.assertEqual(output.read_bytes(), b"previous pending bytes\n")
        for value in (float("nan"), float("inf"), float("-inf")):
            with patch.object(self.review, "parent_fd") as opened:
                with self.assertRaises(ValueError):
                    self.review.write(
                        output, {**base, "pull_request": {"number": value}}
                    )
                opened.assert_not_called()

    def test_atomic_output_duplicate_keys_and_file_boundaries(self) -> None:
        output = self.root / "pending.json"
        output.write_text('{"comments": []}')
        before = output.read_bytes()
        with patch.object(self.review.os, "replace", side_effect=OSError):
            with self.assertRaises(OSError):
                self.review.write(output, self.review.project(fetch([]), {}, {}))
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".review-context-*")), [])
        for raw in (
            b'{"comments":[],"comments":[{}]}',
            b"[]",
            b"\xff",
            b" " * (self.review.MAX_RAW_BYTES + 1),
        ):
            output.write_bytes(raw)
            with self.assertRaises(ValueError):
                self.review.read(output)
        link = self.root / "linked.json"
        link.symlink_to(output)
        for operation in (
            lambda: self.review.read(link),
            lambda: self.review.write(link, {}),
        ):
            with self.assertRaises((OSError, ValueError)):
                operation()
        os.mkfifo(self.root / "fifo")
        with self.assertRaises(ValueError):
            self.review.read(self.root / "fifo")

    def test_merge_decisions_and_compact_output_preserve_all_controls(self) -> None:
        for pr, statuses in merge_cases():
            for mode in ("legacy", "compact"):
                with self.subTest(pr=pr, mode=mode):
                    result = self.invoke(
                        "pr_check_mergeability",
                        {"allowed_merge_state_statuses": statuses, "output_mode": mode},
                        [completed(pr)],
                    )
                    expected = self.plugin.mergeability.evaluate(
                        pr, self.plugin.mergeability.normalize_statuses(statuses)
                    )
                    self.assertEqual(result["status"], "success")
                    self.assertEqual(
                        {k: result["output"][k] for k in expected}, expected
                    )
                    self.assertIs(type(result["output"]["merge_ready"]), bool)

    def test_transient_retries_are_bounded_auth_and_unknown_flags_do_not_retry(
        self,
    ) -> None:
        transient = support.completed_process([], 1, "", "HTTP 503")
        with patch.object(self.plugin.time, "sleep") as sleep:
            result = self.invoke(
                "pr_check_mergeability", {}, [transient, completed({})]
            )
            self.assertEqual((result["status"], self.calls), ("success", 2))
            result = self.invoke("pr_check_mergeability", {}, [transient] * 3)
            self.assertEqual((result["status"], self.calls), ("failed", 3))
            for text in ("{", "[]", ""):
                result = self.invoke(
                    "pr_check_mergeability",
                    {},
                    [support.completed_process([], 0, text, "")] * 3,
                )
                self.assertEqual((result["status"], self.calls), ("failed", 3))
            self.assertGreater(sleep.call_count, 0)
        for error in ("HTTP 401 Unauthorized", "unknown flag: --json"):
            result = self.invoke(
                "pr_check_mergeability",
                {},
                [support.completed_process([], 1, "", error)],
            )
            self.assertEqual((result["status"], self.calls), ("failed", 1))
            self.assertIs(result["output"]["merge_ready"], False)

    def test_retrieval_overflow_fails_and_does_not_publish(self) -> None:
        output = self.root / "check.json"
        result = self.invoke(
            "pr_check_mergeability",
            {"max_response_bytes": 1, "output_file": str(output)},
            [completed({"state": "OPEN"})],
        )
        self.assertEqual(result["status"], "failed")
        self.assertFalse(output.exists())

    def test_approval_ready_preserves_prior_rules_and_failure_default(self) -> None:
        pr = {
            "author": {"login": "author"},
            "latestReviews": [
                {"author": {"login": "author"}, "state": "APPROVED"},
                {"author": {"login": "A"}, "state": "APPROVED", "submittedAt": "2"},
                {"author": {"login": "a"}, "state": "DISMISSED", "submittedAt": "3"},
                {"author": {"login": "B"}, "state": "APPROVED", "submittedAt": "1"},
            ],
        }
        for mode in ("legacy", "compact"):
            for threshold, soft, merged, ready, status in (
                (1, False, False, True, "success"),
                (2, False, False, False, "failed"),
                (2, True, False, False, "success"),
                (2, False, True, True, "success"),
            ):
                result = self.invoke(
                    "pr_check_approvals",
                    {
                        "required_approvals": threshold,
                        "soft_fail": soft,
                        "output_mode": mode,
                    },
                    [completed({**pr, "state": "MERGED" if merged else "OPEN"})],
                )
                self.assertEqual(result["status"], status)
                self.assertIs(result["output"]["approval_ready"], ready)
                self.assertEqual(result["output"]["approved_reviewers"], ["B"])
                self.assertEqual(result["output"]["approval_count"], 1)
        result = self.invoke(
            "pr_check_approvals",
            {"required_approvals": 0, "soft_fail": True},
            [support.completed_process([], 1, "", "unknown flag: --json")],
        )
        self.assertEqual(result["status"], "failed")
        self.assertIs(result["output"]["approval_ready"], False)


if __name__ == "__main__":
    unittest.main()
