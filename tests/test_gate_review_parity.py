"""Differential schema/reader/action acceptance for optional review metadata."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Iterator
import unittest
from unittest import mock

from e2e_document_actions import ANSWER, PLAN
from e2e_gate_decision_contract import PACKAGES as ACTIONS
from gate_contract_fixture import decision
from support import (
    app_invoke_request,
    assert_app_frames,
    contract_validator,
    load_plugin_module,
    package_schema_validator,
    resolved_runtime_temp_environment,
    run_process_plugin_main,
)
from test_gate_decision_contract import input_path

ROOT = Path(__file__).resolve().parents[1]
FEEDBACK_ACTIONS = tuple(case for case in ACTIONS if case[0] != "shell")
HELPERS = ("gate_review", "lean_policy", "process_runner", "process_lifecycle")


@contextmanager
def selected_module(package: str) -> Iterator[object]:
    """Load a package's independent selected helpers without sibling import caches."""
    with mock.patch.dict(sys.modules), mock.patch.object(sys, "path", list(sys.path)):
        for name in HELPERS:
            sys.modules.pop(name, None)
        yield load_plugin_module(ROOT / "plugins" / package)


def set_value(value: dict, path: tuple, replacement: object) -> dict:
    """Return an independent decision with one nested field replaced."""
    value = deepcopy(value)
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = replacement
    return value


def produced(kind: str = "file") -> dict:
    """Return a valid post-initialization producer revision for numeric comparisons."""
    value = decision()
    value["review_request"]["review_documents"][0].update(
        step_id="producer", execution=1, commit_sequence=1, kind=kind
    )
    return value


def semantic_invalid_decisions() -> Iterator[tuple[str, dict]]:
    """Yield published-regex edge cases that require explicit semantic rejection."""
    request = ("review_request",)
    revision = (*request, "review_documents", 0)
    for path in (
        (*request, "run_id"),
        (*request, "gate_id"),
        (*request, "step_id"),
        (*revision, "root_run_id"),
        (*revision, "document"),
        (*revision, "run_id"),
        (*revision, "step_id"),
        (*revision, "revision_id"),
        (*revision, "blob", "sha256"),
    ):
        original = produced()
        target = original
        for key in path:
            target = target[key]
        yield str(path), set_value(original, path, target + "\n")


class GateReviewParityTests(unittest.TestCase):
    """Require accepted nested numbers or explicit failure, never missing feedback."""

    def test_nested_numeric_differential(self) -> None:
        """All six schemas/five readers agree on finite integral floats and bounds."""
        request = ("review_request",)
        revision = (*request, "review_documents", 0)
        paths = (
            (*request, "execution"),
            (*revision, "execution"),
            (*revision, "commit_sequence"),
            (*revision, "size_bytes"),
            (*revision, "file_count"),
            (*revision, "blob", "size_bytes"),
        )
        numbers = (
            0,
            0.0,
            -0.0,
            1,
            1.0,
            7.0,
            1.5,
            -1,
            False,
            True,
            2**32 - 1,
            float(2**32 - 1),
            2**32,
            2**53 + 1,
            float(2**53 + 1),
            2**64 - 1,
            float(2**64 - 1),
            math.nextafter(float(2**64), -math.inf),
            2**64,
            math.nan,
            math.inf,
            -math.inf,
        )
        canonical = contract_validator("runtime/gate-review.v1.schema.json")
        for package in dict.fromkeys(package for package, _ in ACTIONS):
            path = input_path(package)
            schema = json.loads(path.read_text())["properties"]["gate_decision"]
            action_schema = package_schema_validator(path, schema)
            with selected_module(package) as module:
                for base in (decision(), produced(), produced("tree")):
                    for field in paths:
                        for number in numbers:
                            value = set_value(base, field, number)
                            with self.subTest(
                                package=package,
                                field=field,
                                number=repr(number),
                                producer=base["review_request"]["review_documents"][0],
                            ):
                                accepted = canonical.is_valid(value)
                                self.assertEqual(
                                    action_schema.is_valid(value), accepted
                                )
                                if package == "shell":
                                    continue
                                self.assertEqual(
                                    module.valid_gate_decision(value), accepted
                                )
                                if accepted:
                                    feedback = module.gate_decision_feedback(
                                        {"gate_decision": value}
                                    )
                                    text = (
                                        " ".join(feedback)
                                        if isinstance(feedback, tuple)
                                        else feedback
                                    )
                                    self.assertIn(value["reason"], text)
                                    self.assertIn(value["actor"], text)
                                else:
                                    with self.assertRaisesRegex(
                                        ValueError, "gate_decision"
                                    ):
                                        module.gate_decision_feedback(
                                            {"gate_decision": value}
                                        )

    def test_semantic_regex_edges_fail_explicitly(self) -> None:
        """Canonical '$' permits a final newline; strict semantic readers must fail."""
        canonical = contract_validator("runtime/gate-review.v1.schema.json")
        for package in dict.fromkeys(package for package, _ in ACTIONS):
            path = input_path(package)
            action_schema = package_schema_validator(
                path, json.loads(path.read_text())["properties"]["gate_decision"]
            )
            with selected_module(package) as module:
                for label, value in semantic_invalid_decisions():
                    with self.subTest(package=package, field=label):
                        self.assertTrue(canonical.is_valid(value))
                        self.assertTrue(action_schema.is_valid(value))
                        if package == "shell":
                            continue
                        self.assertFalse(module.valid_gate_decision(value))
                        with self.assertRaisesRegex(ValueError, "gate_decision"):
                            module.gate_decision_feedback({"gate_decision": value})

    def test_legacy_timestamp_and_feedback_distinction(self) -> None:
        """Nested numeric parity never loosens the pre-existing timestamp reader."""
        for package, action in FEEDBACK_ACTIONS:
            with (
                self.subTest(package=package, action=action),
                selected_module(package) as module,
            ):
                value = decision(metadata=False)
                expected = (
                    ("request_changes", value["reason"], "Fixture Reviewer (cli)")
                    if package == "openspec"
                    else "request_changes by Fixture Reviewer (cli): "
                    "Tighten the wording café"
                )
                self.assertEqual(
                    module.gate_decision_feedback({"gate_decision": value}), expected
                )
                path = input_path(package)
                action_schema = package_schema_validator(
                    path, json.loads(path.read_text())["properties"]["gate_decision"]
                )
                for timestamp in (1.0, True, False, None):
                    legacy = {**value, "decided_at_ms": timestamp}
                    self.assertEqual(
                        action_schema.is_valid(legacy), type(timestamp) is float
                    )
                    self.assertFalse(module.valid_gate_decision(legacy))
                    self.assertIn(
                        module.gate_decision_feedback({"gate_decision": legacy}),
                        ("", ("", "", "")),
                    )
                    with_metadata = {**decision(), "decided_at_ms": timestamp}
                    with self.assertRaisesRegex(ValueError, "gate_decision"):
                        module.gate_decision_feedback({"gate_decision": with_metadata})
                del value["decided_at_ms"]
                self.assertFalse(action_schema.is_valid(value))
                self.assertIn(
                    module.gate_decision_feedback({"gate_decision": value}),
                    ("", ("", "", "")),
                )

    def test_invalid_provided_review_decision_never_reaches_transport(self) -> None:
        """All six feedback bindings return validation_error before provider work."""
        cases = list(semantic_invalid_decisions())
        invalid = decision()
        invalid["review_request"]["review_documents"][0]["blob"][
            "unknown"
        ] = "private metadata"
        cases.append(("unknown nested field", invalid))
        for number in (False, math.nan, math.inf, -math.inf, float(2**64)):
            cases.append(
                (
                    repr(number),
                    set_value(
                        decision(),
                        ("review_request", "review_documents", 0, "blob", "size_bytes"),
                        number,
                    ),
                )
            )
        for timestamp in (1.0, False, True, None):
            cases.append(
                (
                    "timestamp " + repr(timestamp),
                    {**decision(), "decided_at_ms": timestamp},
                )
            )
        for outcome in ("approved", "rejected"):
            cases.append((outcome, {**cases[0][1], "decision": outcome}))
        for package, action in FEEDBACK_ACTIONS:
            with selected_module(package) as module:
                for label, value in cases:
                    with (
                        self.subTest(package=package, action=action, case=label),
                        ExitStack() as stack,
                    ):
                        providers = [
                            stack.enter_context(mock.patch.object(module, name))
                            for name in (
                                "run_command",
                                "generate_backend_response",
                                "run_openspec_validate",
                            )
                            if hasattr(module, name)
                        ]
                        providers.append(
                            stack.enter_context(mock.patch("subprocess.Popen"))
                        )
                        payload = {
                            "prompt": "Perform fixture task",
                            "gate_decision": value,
                        }
                        if package == "openspec":
                            payload["change_name"] = "fixture"
                        request = app_invoke_request(action, payload)
                        code, frames, stderr = run_process_plugin_main(module, request)
                        self.assertEqual((code, stderr), (0, ""))
                        _, result = assert_app_frames(frames, package)
                        self.assertEqual(result["status"], "failed")
                        self.assertEqual(result["error"]["code"], "validation_error")
                        self.assertIn("gate_decision", result["error"]["message"])
                        self.assertNotIn(value["reason"], result["error"]["message"])
                        for provider in providers:
                            provider.assert_not_called()

    def test_integral_metadata_reaches_actual_feedback_actions(self) -> None:
        """All feedback actions succeed with integral floats and complete feedback."""
        cases = [
            set_value(decision(), ("review_request", "execution"), 1.0),
            set_value(
                decision(),
                ("review_request", "review_documents", 0, "blob", "size_bytes"),
                7.0,
            ),
            produced("tree"),
        ]
        revision = cases[-1]["review_request"]["review_documents"][0]
        revision.update(
            execution=1.0, commit_sequence=1.0, size_bytes=7.0, file_count=7.0
        )
        for package, action in FEEDBACK_ACTIONS:
            for value in cases:
                with (
                    self.subTest(package=package, action=action, value=value),
                    selected_module(package) as module,
                    tempfile.TemporaryDirectory() as temporary,
                    ExitStack() as stack,
                ):
                    # Canonicalize the fixture root, not paths supplied to actions:
                    # macOS /tmp is a symlink and production rejects traversal.
                    root = Path(temporary).resolve()
                    (root / "openspec/changes").mkdir(parents=True)
                    artifacts = root / "artifacts"
                    artifacts.mkdir()
                    environment = stack.enter_context(
                        resolved_runtime_temp_environment(ROOT / "plugins" / package)
                    )
                    stack.enter_context(
                        mock.patch.dict(
                            os.environ,
                            {**environment, "CURSOR_API_KEY": "fixture-only"},
                        )
                    )
                    payload = {
                        "prompt": "Perform fixture task",
                        "gate_decision": value,
                        "feedback_context_mode": "feedback-only",
                        "response_file": "answer.md",
                        "output_mode": "compact",
                    }
                    if package == "openspec":
                        payload["change_name"] = "fixture"
                    else:
                        payload["history_mode"] = "invocation"
                    path = input_path(package)
                    package_schema_validator(path).validate(payload)
                    request = app_invoke_request(action, payload, str(artifacts))
                    request["paths"]["workspace_dir"] = str(root)

                    def check_prompt(prompt: str) -> None:
                        """Require operator text, excluding opaque review metadata."""
                        for key in ("reason", "actor", "actor_source"):
                            self.assertIn(value[key], prompt)
                        self.assertNotIn("a" * 64, prompt)
                        self.assertNotIn("b" * 64, prompt)

                    def provider(
                        command_request: dict,
                        command: list,
                        *args: object,
                        **kwargs: object,
                    ) -> object:
                        """Check the prompt and return a supported provider result."""
                        prompt = kwargs.get("stdin_text") or next(
                            (
                                text
                                for text in command
                                if isinstance(text, str)
                                and "Perform fixture task" in text
                            ),
                            "",
                        )
                        check_prompt(prompt)
                        completed = subprocess.CompletedProcess(
                            command, 0, "diagnostic", ""
                        )
                        if package == "codex.cli":
                            (artifacts / "last_message.txt").write_text(ANSWER)
                            return completed
                        if package == "github.copilot.cli":
                            completed.stdout = json.dumps(
                                {
                                    "type": "assistant.message",
                                    "data": {"content": ANSWER},
                                }
                            )
                            return completed
                        return {
                            "completed": completed,
                            "response_text": ANSWER,
                            "session_id": "fixture",
                            "share_url": "",
                            "result_event": None,
                            "last_error": None,
                            "event_error": None,
                            "env": {},
                        }

                    def backend(
                        backend_request: dict,
                        backend_payload: dict,
                        working_dir: str,
                        prompt: str,
                        **kwargs: object,
                    ) -> tuple:
                        """Check feedback and return a typed OpenSpec plan."""
                        check_prompt(prompt)
                        text = json.dumps(PLAN)
                        return (
                            "gh-copilot",
                            subprocess.CompletedProcess([], 0, text, ""),
                            text,
                            None,
                        )

                    for name in (
                        "extract_codex_usage",
                        "snapshot_copilot_sessions",
                        "extract_copilot_session_usage",
                        "export_session_data",
                        "require_json_transport",
                    ):
                        if hasattr(module, name):
                            stack.enter_context(
                                mock.patch.object(
                                    module,
                                    name,
                                    return_value=(
                                        {}
                                        if name == "snapshot_copilot_sessions"
                                        else None
                                    ),
                                )
                            )
                    if package == "openspec":
                        transport = stack.enter_context(
                            mock.patch.object(
                                module, "generate_backend_response", side_effect=backend
                            )
                        )
                        stack.enter_context(
                            mock.patch.object(
                                module,
                                "run_openspec_validate",
                                return_value=subprocess.CompletedProcess(
                                    [], 0, "validated", ""
                                ),
                            )
                        )
                    else:
                        transport = stack.enter_context(
                            mock.patch.object(
                                module, "run_command", side_effect=provider
                            )
                        )
                    code, frames, stderr = run_process_plugin_main(module, request)
                    self.assertEqual((code, stderr), (0, ""))
                    _, result = assert_app_frames(frames, package)
                    self.assertEqual(result["status"], "success", result.get("error"))
                    self.assertTrue(transport.called)
                    output_path = path.with_name(
                        path.name.replace(".input.", ".output.")
                    )
                    package_schema_validator(output_path).validate(result["output"])
                    self.assertEqual(
                        (root / "answer.md").read_text().strip(),
                        "# Plan" if package == "openspec" else ANSWER,
                    )


if __name__ == "__main__":
    unittest.main()
