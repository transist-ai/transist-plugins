"""Offline budget, evidence and complete authoritative-output regressions."""

from __future__ import annotations

import importlib.util
import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
import support  # noqa: E402

SPEC = importlib.util.spec_from_file_location("lean_openspec", ROOT / "plugin.py")
PLUGIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLUGIN)


def validator(action: str, direction: str) -> Draft202012Validator:
    """Compile selected schemas with package-local, offline-only references."""
    resources = [
        (path.as_uri(), Resource.from_contents(json.loads(path.read_text())))
        for path in (ROOT / "schemas").glob("*.json")
    ]
    return Draft202012Validator(
        {"$ref": (ROOT / f"schemas/{action}.{direction}.schema.json").as_uri()},
        registry=Registry().with_resources(resources),
    )


class LeanIOTests(unittest.TestCase):
    """Both actions must reject excessive input before any command launches."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.change = self.root / "openspec/changes/unit"
        self.change.mkdir(parents=True)
        (self.change / "proposal.md").write_text("approved é", encoding="utf-8")

    def request(self, action: str, **extra: object) -> dict:
        """Build a disposable action request with no operational runtime home."""
        req = support.app_invoke_request(
            action,
            {"change_name": "unit", "prompt": "task", **extra},
            artifacts_dir=self.root / "artifacts",
        )
        req["paths"]["workspace_dir"] = str(self.root)
        return req

    def assert_result(self, result: dict, action: str) -> None:
        """Validate APP, action schema when present, and all transport caps."""
        support.contract_validator("app/invoke.result.v1.schema.json").validate(result)
        if result["output"]:
            validator(action, "output").validate(result["output"])
        self.assertLessEqual(len(json.dumps(result).encode()), 1024 * 1024)
        self.assertLessEqual(len(result["artifact_hints"]), 32)
        self.assertLessEqual(
            sum(len(item["contents"].encode()) for item in result["artifact_hints"]),
            1024 * 1024,
        )
        for item in result["artifact_hints"]:
            self.assertLessEqual(len(item["contents"].encode()), 256 * 1024)

    def test_unicode_raw_prompt_file_bound(self) -> None:
        path = self.root / "prompt.txt"
        path.write_text("éé", encoding="utf-8")
        self.assertEqual(
            PLUGIN.resolve_prompt_input(
                {"prompt_file": str(path), "max_prompt_bytes": 4}
            ),
            "éé",
        )
        for payload in (
            {"prompt_file": str(path), "max_prompt_bytes": 3},
            {"prompt": "éé", "max_prompt_bytes": 3},
        ):
            with self.assertRaisesRegex(ValueError, "UTF-8 bytes"):
                PLUGIN.resolve_prompt_input(payload)
        path.write_bytes(b"\xff")
        with self.assertRaises(UnicodeDecodeError):
            PLUGIN.resolve_prompt_input({"prompt_file": str(path)})
        path.write_text("éé", encoding="utf-8")
        for action in PLUGIN.ACTIONS:
            req = self.request(action, prompt_file=str(path), max_prompt_bytes=3)
            req["input"].pop("prompt")
            with mock.patch.object(PLUGIN, "run_process") as child:
                self.assertEqual(PLUGIN.handle_request(req)["status"], "failed")
                child.assert_not_called()

    def test_closed_input_contracts(self) -> None:
        for action in PLUGIN.ACTIONS:
            valid = {
                "change_name": "unit",
                "output_mode": "compact",
                "max_prompt_bytes": 98304,
                "max_context_file_bytes": 65536,
            }
            validator(action, "input").validate(valid)
            for key, bad in (
                ("output_mode", ""),
                ("output_mode", None),
                ("max_prompt_bytes", True),
                ("max_prompt_bytes", 1.5),
                ("max_context_file_bytes", 0),
            ):
                with self.subTest(action=action, key=key, bad=bad), self.assertRaises(
                    ValidationError
                ):
                    validator(action, "input").validate({**valid, key: bad})
                result = PLUGIN.handle_request(self.request(action, **{key: bad}))
                self.assertEqual(result["error"]["code"], "validation_error")

    def gate(self, reason: str = "revise") -> dict:
        """Return a complete revision decision selecting the existing draft."""
        return {
            "schema": "transist.gate_decision.v1",
            "run_id": "run",
            "gate_id": "gate",
            "step_id": "step",
            "decision": "request_changes",
            "decided_at_ms": 0,
            "reason": reason,
        }

    def test_missing_declared_context_fails_before_child(self) -> None:
        missing = self.root / "private-source-name.md"
        for action in PLUGIN.ACTIONS:
            for controls in (
                {"max_prompt_bytes": 98304},
                {"max_context_file_bytes": 65536},
            ):
                req = self.request(
                    action, context_files=["", "  ", str(missing)], **controls
                )
                with self.subTest(action=action, controls=controls), mock.patch.object(
                    PLUGIN, "run_process"
                ) as child:
                    result = PLUGIN.handle_request(req)
                    child.assert_not_called()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(
                    result["error"]["message"], "context_files[2]: unavailable"
                )
                self.assertNotIn(str(missing), json.dumps(result))
                self.assertNotIn(missing.name, json.dumps(result))
                self.assert_result(result, action)

    def test_missing_selected_revision_draft_fails_before_child(self) -> None:
        missing = self.root / "private-draft-name.md"
        for mode in ("full", "focused"):
            for controls in (
                {"max_prompt_bytes": 98304},
                {"max_context_file_bytes": 65536},
            ):
                req = self.request(
                    "generate_plan",
                    response_file=str(missing),
                    gate_decision=self.gate(),
                    feedback_context_mode=mode,
                    **controls,
                )
                with self.subTest(mode=mode, controls=controls), mock.patch.object(
                    PLUGIN, "run_process"
                ) as child:
                    result = PLUGIN.handle_request(req)
                    child.assert_not_called()
                self.assertEqual(
                    result["error"]["message"], "current_draft: unavailable"
                )
                self.assertNotIn(str(missing), json.dumps(result))
                self.assert_result(result, "generate_plan")

    def test_unavailable_file_errors_are_sanitized(self) -> None:
        target = self.root / "private-denied-context.md"
        target.write_text("secret contributor contents")
        original_open = os.open

        def denied(path: str, *args: object, **kwargs: object) -> object:
            """Deny only the selected input, leaving artifact writes available."""
            if str(path) == str(target):
                raise PermissionError(f"denied secret host path {target}")
            return original_open(path, *args, **kwargs)

        io_module = sys.modules[PLUGIN.bounded_text.__module__]
        req = self.request(
            "verify", context_files=[str(target)], max_prompt_bytes=98304
        )
        with mock.patch.object(
            io_module.os, "open", side_effect=denied
        ), mock.patch.object(PLUGIN, "run_process") as child:
            result = PLUGIN.handle_request(req)
            child.assert_not_called()
        self.assertEqual(result["error"]["message"], "context_files[0]: unavailable")
        self.assertNotIn(target.name, json.dumps(result))
        self.assertNotIn("secret host", json.dumps(result))
        self.assert_result(result, "verify")

    def test_selected_verification_plan_unavailability_is_sanitized(self) -> None:
        for extra in (
            {"approved_plan_dir": str(self.root / "private-missing-plan")},
            {"change_name": "private-missing-change"},
            {"approved_plan_dir": str(self.change)},
        ):
            req = self.request("verify", max_prompt_bytes=98304, **extra)
            with mock.patch.object(PLUGIN, "run_process") as child, mock.patch.object(
                PLUGIN,
                "copy_tree",
                side_effect=PermissionError(f"private copy failed at {self.root}"),
            ):
                result = PLUGIN.handle_request(req)
                child.assert_not_called()
            self.assertEqual(
                result["error"]["message"], "approved_plan_files: unavailable"
            )
            self.assertNotIn(str(self.root), json.dumps(result))
            self.assert_result(result, "verify")

    def test_bounded_errors_report_raw_counts_without_paths_or_content(self) -> None:
        target = self.root / "private-encoding-context.md"
        for raw, limit, reason in (
            (b"\xc3\xa9\xff", 64, "invalid UTF-8"),
            ("éé".encode(), 3, "exceeds budget"),
        ):
            target.write_bytes(raw)
            for field in ("context_files", "prompt_file"):
                extra = {
                    field: [str(target)] if field == "context_files" else str(target),
                    "max_prompt_bytes": 98304,
                    "max_context_file_bytes": limit,
                }
                if field == "prompt_file":
                    extra["max_prompt_bytes"] = limit
                req = self.request("generate_plan", **extra)
                if field == "prompt_file":
                    req["input"].pop("prompt")
                with mock.patch.object(PLUGIN, "run_process") as child:
                    result = PLUGIN.handle_request(req)
                    child.assert_not_called()
                self.assertIn(reason, result["error"]["message"])
                self.assertNotIn(target.name, json.dumps(result))
                report = json.loads(
                    (self.root / "artifacts/prompt-composition.json").read_text()
                )
                failure = report["sections"][-1]
                self.assertEqual(failure["bytes"], len(raw))
                self.assertEqual(failure["read_bytes"], len(raw))
                self.assertEqual(failure["reason"], reason)
                self.assert_result(result, "generate_plan")

    def test_optional_fresh_paths_and_legacy_skipping(self) -> None:
        missing = self.root / "missing.md"
        fresh_plan = self.root / "fresh-plan"
        fresh_change = self.root / "fresh-change"
        for extra in (
            {},
            {"gate_decision": self.gate(), "feedback_context_mode": "feedback-only"},
            {"gate_decision": self.gate("redo: restart")},
        ):
            req = self.request(
                "generate_plan",
                response_file=str(missing),
                max_prompt_bytes=98304,
                **extra,
            )
            prompt = PLUGIN.compose_generate_prompt(
                req, req["input"], str(fresh_change), str(fresh_plan), "task"
            )
            self.assertIn("# Task", prompt)
        # Revision roots are required; only fresh transient staging is optional.
        req = self.request(
            "generate_plan",
            canonical_plan_dir=str(fresh_plan),
            gate_decision=self.gate(),
            max_prompt_bytes=98304,
        )
        with mock.patch.object(PLUGIN, "run_process") as child:
            result = PLUGIN.handle_request(req)
            child.assert_not_called()
        self.assertEqual(result["error"]["message"], "existing_plan_files: unavailable")
        req = self.request(
            "generate_plan",
            gate_decision=self.gate(),
            response_file=str(missing),
            context_files=["", "  ", str(missing)],
        )
        prompt = PLUGIN.compose_generate_prompt(
            req, req["input"], str(self.change), "", "task"
        )
        self.assertNotIn("# Current draft", prompt)
        self.assertNotIn("# Context:", prompt)
        # Empty optional entries remain harmless under strict controls.
        req = self.request(
            "generate_plan", context_files=["", "  "], max_prompt_bytes=98304
        )
        with mock.patch.object(PLUGIN, "read_text", wraps=PLUGIN.read_text) as reader:
            PLUGIN.compose_generate_prompt(
                req, req["input"], str(self.change), "", "task"
            )
        self.assertFalse(any(call.args == ("",) for call in reader.call_args_list))

    def test_diagnostic_truncation_remains_success_and_reports_storage(self) -> None:
        payload = {
            "passed": True,
            "report_markdown": "Complete report é",
            "findings": [{"message": "Complete finding é"}],
        }
        req = self.request("verify", output_mode="compact", backend="cursor")
        stderr = "\x01" * (256 * 1024)
        with mock.patch.object(
            PLUGIN,
            "run_process",
            side_effect=[
                support.completed_process([], 0, "validated", ""),
                support.completed_process([], 0, json.dumps(payload), stderr),
            ],
        ), mock.patch.object(PLUGIN, "emit"):
            result = PLUGIN.handle_request(req)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"]["report_text"], payload["report_markdown"])
        self.assertEqual(result["output"]["findings"], payload["findings"])
        log = next(
            item
            for item in result["output"]["diagnostics"]["logs"]
            if item["name"] == "backend-stderr.txt"
        )
        self.assertEqual(log["full_log_storage"], "invocation_private")
        self.assertFalse(log["full_log_hint_offered"])
        self.assertTrue(log["truncated"])
        self.assertEqual(
            (self.root / "artifacts/backend-stderr.txt").read_text(), stderr
        )
        self.assert_result(result, "verify")

    def test_legal_32_hint_frame_is_not_a_diagnostic_failure(self) -> None:
        req = self.request("verify", output_mode="compact")
        output = PLUGIN.build_verify_output(
            "cursor", "unit", "report", "", 0, True, "", "", "", "", 0
        )
        hints = [PLUGIN.artifact_hint(f"business{i}.md", "evidence") for i in range(31)]
        result = PLUGIN.finalize_result(
            req,
            PLUGIN.build_result(req["invoke_id"], "success", output, hints),
            PLUGIN.artifact_hint,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["artifact_hints"]), 32)
        self.assertTrue(
            all(
                not item["full_log_hint_offered"]
                for item in result["output"]["diagnostics"]["logs"]
            )
        )
        self.assert_result(result, "verify")

    def test_all_composition_sources_fail_before_child(self) -> None:
        context = self.root / "context.md"
        context.write_text("é" * 100, encoding="utf-8")
        gate = {
            "schema": "transist.gate_decision.v1",
            "run_id": "run",
            "gate_id": "gate",
            "step_id": "step",
            "decision": "request_changes",
            "decided_at_ms": 0,
            "reason": "é" * 100,
        }
        cases = [
            ("generate_plan", {"skills_rendered": "é" * 200, "max_prompt_bytes": 300}),
            ("generate_plan", {"gate_decision": gate, "max_prompt_bytes": 200}),
            (
                "generate_plan",
                {
                    "gate_decision": {**gate, "reason": "revise"},
                    "response_file": str(context),
                    "max_context_file_bytes": 199,
                },
            ),
            (
                "generate_plan",
                {"context_files": [str(context)], "max_context_file_bytes": 199},
            ),
            (
                "verify",
                {"context_files": [str(context)], "max_context_file_bytes": 199},
            ),
            ("verify", {"max_context_file_bytes": 1}),
            ("verify", {"max_prompt_bytes": 1}),
        ]
        (self.change / "proposal.md").write_text("é" * 100, encoding="utf-8")
        cases.append(
            (
                "generate_plan",
                {
                    "gate_decision": {**gate, "reason": "revise"},
                    "max_context_file_bytes": 199,
                },
            )
        )
        for action, controls in cases:
            with self.subTest(
                action=action, controls=list(controls)
            ), mock.patch.object(PLUGIN, "run_process") as child:
                result = PLUGIN.handle_request(self.request(action, **controls))
                child.assert_not_called()
                self.assertEqual(result["status"], "failed")
                self.assert_result(result, action)
                report = (self.root / "artifacts/prompt-composition.json").read_text()
                self.assertNotIn("éé", report)
                self.assertIn("bytes", report)

    def test_aggregate_and_template_boundary(self) -> None:
        req = self.request(
            "generate_plan", skills_rendered="é", max_context_file_bytes=4
        )
        prompt = PLUGIN.compose_generate_prompt(
            req, req["input"], str(self.change), "", "task"
        )
        size = len(prompt.encode())
        req["input"]["max_prompt_bytes"] = size
        self.assertEqual(
            PLUGIN.compose_generate_prompt(
                req, req["input"], str(self.change), "", "task"
            ),
            prompt,
        )
        req["input"]["max_prompt_bytes"] = size - 1
        with self.assertRaisesRegex(ValueError, "composed prompt"):
            PLUGIN.compose_generate_prompt(
                req, req["input"], str(self.change), "", "task"
            )
        for name in ("one.md", "two.md"):
            (self.root / name).write_text("é" * 50, encoding="utf-8")
        req = self.request(
            "generate_plan",
            context_files=["one.md", "two.md"],
            max_context_file_bytes=100,
            max_prompt_bytes=250,
        )
        with mock.patch.object(PLUGIN, "run_process") as child:
            self.assertEqual(PLUGIN.handle_request(req)["status"], "failed")
            child.assert_not_called()

    def test_canonical_plan_replay_is_bounded(self) -> None:
        bundle = self.root / "canonical"
        bundle.mkdir()
        (bundle / "README.md").write_text("é" * 100, encoding="utf-8")
        gate = {
            "schema": "transist.gate_decision.v1",
            "run_id": "run",
            "gate_id": "gate",
            "step_id": "step",
            "decision": "request_changes",
            "decided_at_ms": 0,
        }
        req = self.request(
            "generate_plan",
            canonical_plan_dir=str(bundle),
            gate_decision=gate,
            max_context_file_bytes=199,
        )
        with mock.patch.object(PLUGIN, "run_process") as child:
            result = PLUGIN.handle_request(req)
            child.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assert_result(result, "generate_plan")
        inventory = next(
            item
            for item in result["artifact_hints"]
            if item["name"] == "produced-evidence.json"
        )
        self.assertEqual(
            json.loads(inventory["contents"])["retention"], "not_guaranteed"
        )

    def test_compact_and_legacy_complete_authoritative_output(self) -> None:
        plan = {
            "summary_markdown": "summary é",
            "proposal_markdown": "proposal é",
            "tasks_markdown": "tasks é",
            "design_markdown": "design é",
            "specs": [
                {
                    "capability": "api",
                    "spec_markdown": (
                        "## ADDED Requirements\n### Requirement: API\nMust work\n"
                        "#### Scenario: Works\n- THEN works"
                    ),
                }
            ],
        }
        findings = [{"severity": "warning", "message": "complete é"}]
        for action, payload in (
            ("generate_plan", plan),
            (
                "verify",
                {"passed": True, "report_markdown": "report é", "findings": findings},
            ),
        ):
            for mode in ("legacy", "compact"):
                req = self.request(action, output_mode=mode, backend="cursor")
                # User-chosen business filenames must not overwrite command logs.
                req["input"][
                    "response_file" if action == "generate_plan" else "report_file"
                ] = "backend-stdout.txt"
                raw = json.dumps(payload, ensure_ascii=False)
                completed = support.completed_process([], 0, raw, "diagnostic é")
                with mock.patch.object(
                    PLUGIN,
                    "run_process",
                    side_effect=(
                        [completed, support.completed_process([], 0, "validated", "")]
                        if action == "generate_plan"
                        else [
                            support.completed_process([], 0, "validated", ""),
                            completed,
                        ]
                    ),
                ):
                    exit_code, frames, stderr = support.run_process_plugin_main(
                        PLUGIN, req
                    )
                    handshake, result = support.assert_app_frames(frames, "openspec")
                    self.assertEqual(handshake["plugin"]["version"], "0.1.0")
                    self.assertEqual(exit_code, 0)
                    self.assertEqual(stderr, "")
                self.assert_result(result, action)
                self.assertEqual(result["status"], "success")
                output = result["output"]
                self.assertEqual(
                    output[
                        "response_text" if action == "generate_plan" else "report_text"
                    ],
                    "summary é" if action == "generate_plan" else "report é",
                )
                if mode == "compact":
                    self.assertNotIn("stdout", output)
                    self.assertNotIn("stderr", output)
                    if action == "verify":
                        self.assertEqual(output["findings"], findings)
                    invalid = {**output, "stdout": ""}
                    with self.assertRaises(ValidationError):
                        validator(action, "output").validate(invalid)
                else:
                    self.assertIn(raw, output["stdout"])
                names = [item["name"] for item in result["artifact_hints"]]
                self.assertEqual(len(names), len(set(names)))
                self.assertEqual(
                    (self.root / "artifacts/backend-stdout.txt").read_text(), raw
                )

    def test_large_business_output_explicit_failure_preserves_plan(self) -> None:
        huge = "é" * (256 * 1024)
        payload = {
            "summary_markdown": "summary",
            "proposal_markdown": huge,
            "tasks_markdown": "tasks",
            "design_markdown": "design",
            "specs": [
                {
                    "capability": "api",
                    "spec_markdown": (
                        "## ADDED Requirements\n### Requirement: API\nMust work\n"
                        "#### Scenario: Works\n- THEN works"
                    ),
                }
            ],
        }
        req = self.request("generate_plan", output_mode="compact", backend="cursor")
        with mock.patch.object(
            PLUGIN,
            "run_process",
            side_effect=[
                support.completed_process([], 0, json.dumps(payload), ""),
                support.completed_process([], 0, "validated", ""),
            ],
        ), mock.patch.object(PLUGIN, "emit"):
            result = PLUGIN.handle_request(req)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "validation_error")
        self.assertIn("preserved", result["error"]["message"])
        self.assertEqual((self.change / "proposal.md").read_text().strip(), huge)
        self.assertEqual((self.root / "artifacts/proposal.md").read_text(), huge)
        self.assert_result(result, "generate_plan")

    def test_diagnostics_can_truncate_nonzero_and_timeout(self) -> None:
        huge = "é" * (256 * 1024)
        for failure in (
            support.completed_process([], 7, "produced", huge),
            subprocess.TimeoutExpired(
                ["cursor-agent"], 1, output="produced", stderr=huge
            ),
        ):
            req = self.request("generate_plan", output_mode="compact", backend="cursor")
            with mock.patch.object(
                PLUGIN,
                "run_process",
                side_effect=failure if isinstance(failure, Exception) else None,
                return_value=failure,
            ), mock.patch.object(PLUGIN, "emit"):
                result = PLUGIN.handle_request(req)
            self.assertEqual(result["status"], "failed")
            self.assert_result(result, "generate_plan")
            self.assertEqual(
                (self.root / "artifacts/backend-stderr.txt").read_text(), huge
            )
            self.assertTrue(
                any(
                    item["name"].endswith(".truncated.txt")
                    for item in result["artifact_hints"]
                )
            )


if __name__ == "__main__":
    unittest.main()
