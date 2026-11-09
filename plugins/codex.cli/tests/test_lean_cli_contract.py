"""Offline opt-in contract matrix for the four canonical CLI packages.

Each case loads the exact package-local selected helper, never a sibling helper.
Providers, usage-state readers and session export are fixtures, not live accounts.
"""

from __future__ import annotations

import importlib.util
import builtins
import io
import json
import os
from pathlib import Path
import struct
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, ExitStack
from typing import Any, Iterator
from unittest import mock

from jsonschema import Draft202012Validator, ValidationError
import yaml

PACKAGES = Path(__file__).resolve().parents[2]
IDS = ("codex.cli", "cursor.cli", "github.copilot.cli", "opencode.cli")
sys.path.insert(0, str(PACKAGES.parent / "tests"))
from support import (  # noqa: E402
    app_invoke_request,
    load_plugin_module,
    contract_validator,
    package_schema_validator,
    run_process_plugin_subprocess,
    assert_app_frames,
)

ANSWER = "Change title\n\n## What Changed\n\nUseful answer é🙂"
RAW = "provider tool narration\n\n" + ANSWER + "\n"
GATE = {
    "schema": "transist.gate_decision.v1",
    "run_id": "root",
    "gate_id": "review",
    "step_id": "review",
    "decision": "request_changes",
    "reason": "tighten é🙂",
    "decided_at_ms": 1,
}


@contextmanager
def staging_only_writes(staging: Path) -> Iterator[list[Path]]:
    """Reject test-process file/directory writes outside invocation staging."""
    staging = staging.resolve()
    writes: list[Path] = []
    original_open, original_io_open, original_mkdir = builtins.open, io.open, os.mkdir

    def check(path: str | Path) -> None:
        """Check the resolved write boundary and record authorized destinations."""
        target = Path(path).resolve()
        if target != staging and staging not in target.parents:
            raise PermissionError("write outside invocation staging")
        writes.append(target)

    def guarded_open(
        path: str | Path, mode: str = "r", *args: Any, **kwargs: Any
    ) -> Any:
        """Guard legacy open-based writes while preserving normal reads."""
        if any(flag in mode for flag in "wax+"):
            check(path)
        return original_open(path, mode, *args, **kwargs)

    def guarded_io_open(
        path: str | Path, mode: str = "r", *args: Any, **kwargs: Any
    ) -> Any:
        """Guard pathlib/io-based writes while preserving normal reads."""
        if any(flag in mode for flag in "wax+"):
            check(path)
        return original_io_open(path, mode, *args, **kwargs)

    def guarded_mkdir(path: str | Path, *args: Any, **kwargs: Any) -> None:
        """Guard directory creation, including recursive os.makedirs calls."""
        check(path)
        original_mkdir(path, *args, **kwargs)

    with mock.patch("builtins.open", side_effect=guarded_open), mock.patch(
        "io.open", side_effect=guarded_io_open
    ), mock.patch("os.mkdir", side_effect=guarded_mkdir):
        yield writes


class LeanCliContractTests(unittest.TestCase):
    """Exercise actual handlers and closed schemas at all four offline boundaries."""

    @contextmanager
    def fixture(
        self,
        plugin_id: str,
        payload: dict | None = None,
        export_fixture: bool = True,
        help_fixture: bool = True,
    ) -> Iterator[tuple]:
        """Create a disjoint workspace/staging fixture with an isolated local helper."""
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            workspace, artifacts = root / "workspace", root / "staging"
            workspace.mkdir()
            artifacts.mkdir()
            package = PACKAGES / plugin_id
            module = load_plugin_module(package)
            spec = importlib.util.spec_from_file_location(
                "local_lean_" + plugin_id, package / "lean_policy.py"
            )
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
            module.lean_policy = helper
            action = "exec_task_cli" if plugin_id == "cursor.cli" else "exec_task"
            request = app_invoke_request(
                action, payload or {"prompt": "task"}, str(artifacts)
            )
            request["paths"]["workspace_dir"] = str(workspace)
            environment = {
                name: str(root / name)
                for name in (
                    "CURSOR_CONFIG_DIR",
                    "CURSOR_DATA_DIR",
                    "TMPDIR",
                    "XDG_DATA_HOME",
                    "XDG_STATE_HOME",
                    "XDG_CACHE_HOME",
                    "COPILOT_CONFIG_DIR",
                    "COPILOT_SESSION_STATE_DIR",
                )
            }
            stack.enter_context(mock.patch.dict(os.environ, environment, clear=True))
            stack.enter_context(mock.patch.object(module, "emit"))
            for name, value in (
                ("extract_codex_usage", None),
                ("snapshot_copilot_sessions", {}),
                (
                    "extract_copilot_session_usage",
                    {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
                ),
                ("export_session_data", None),
                ("require_json_transport", None),
            ):
                if (
                    hasattr(module, name)
                    and (name != "export_session_data" or export_fixture)
                    and (name != "require_json_transport" or help_fixture)
                ):
                    stack.enter_context(
                        mock.patch.object(module, name, return_value=value)
                    )
            yield module, helper, request, workspace, artifacts

    def invoke(
        self,
        module: object,
        request: dict,
        exit_code: int = 0,
        stdout: str = RAW,
        stderr: str = "diagnostic é🙂\n",
        error: Exception | None = None,
        answer: str = ANSWER,
    ) -> tuple[dict, mock.Mock]:
        """Execute the real handler with one controlled provider outcome."""
        if module.PLUGIN_ID == "github.copilot.cli" and (
            request["input"].get("output_mode") == "compact"
            or request["input"].get("history_mode") == "invocation"
        ):
            stdout = (
                json.dumps(
                    {"type": "tool.execution_complete", "data": {"output": stdout}}
                )
                + "\n"
                + json.dumps({"type": "assistant.message", "data": {"content": answer}})
                + "\n"
            )
        completed = subprocess.CompletedProcess(["fixture"], exit_code, stdout, stderr)
        result = (
            completed
            if module.PLUGIN_ID in {"codex.cli", "github.copilot.cli"}
            else {
                "completed": completed,
                "response_text": answer,
                "session_id": "fixture-session",
                "share_url": "",
                "result_event": None,
                "last_error": None,
                "event_error": None,
                "usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
                "env": {},
            }
        )

        def provider_result(*args: object, **kwargs: object) -> object:
            """Model the authoritative Codex message separately from its log."""
            if error is not None:
                raise error
            if module.PLUGIN_ID == "codex.cli":
                Path(request["paths"]["artifacts_dir"], "last_message.txt").write_text(
                    answer, encoding="utf-8"
                )
            return result

        with mock.patch.object(
            module, "run_command", side_effect=provider_result
        ) as provider:
            frame = module.handle_request(request)
        contract_validator("app/invoke.result.v1.schema.json").validate(frame)
        return frame, provider

    def validator(self, plugin_id: str, kind: str = "output") -> Draft202012Validator:
        """Compile the canonical closed action schema."""
        return package_schema_validator(
            PACKAGES / plugin_id / f"schemas/exec_task.{kind}.schema.json"
        )

    def test_selected_payload_versions_and_copied_policy(self) -> None:
        """Finite packages independently ship identical policy and normalized pins."""
        reference = (PACKAGES / IDS[0] / "lean_policy.py").read_bytes()
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id):
                package = PACKAGES / plugin_id
                self.assertEqual((package / "lean_policy.py").read_bytes(), reference)
                manifest = yaml.safe_load((package / "plugin.yaml").read_text())
                self.assertEqual(manifest["version"], "0.1.0")
                self.assertIn("lean_policy.py", manifest["package"]["files"])
                workflow = yaml.safe_load((package / "workflow.yaml").read_text())
                self.assertEqual(workflow["version"], "0.1.0")
                self.assertEqual(workflow["plugins"][plugin_id], "0.1.0")
                controls = workflow["steps"][0]["with"]
                self.assertEqual(controls["max_context_file_bytes"], 65536)
                self.assertEqual(controls["max_prompt_bytes"], 98304)
                self.assertEqual(controls["output_mode"], "compact")
                self.assertEqual(controls["history_mode"], "invocation")

    def test_input_options_are_closed_positive_and_optional(self) -> None:
        """Invalid enums, bool/int confusion, nulls and unknown inputs are rejected."""
        for plugin_id in IDS:
            validator = self.validator(plugin_id, "input")
            validator.validate({"prompt": "task"})
            validator.validate(
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                    "max_context_file_bytes": 1,
                    "max_prompt_bytes": 1,
                }
            )
            for key, value in (
                ("output_mode", "invalid"),
                ("history_mode", "invalid"),
                ("max_prompt_bytes", 0),
                ("max_context_file_bytes", True),
                ("max_context_file_bytes", None),
                ("unexpected", 1),
            ):
                with self.subTest(plugin=plugin_id, field=key), self.assertRaises(
                    ValidationError
                ):
                    validator.validate({"prompt": "task", key: value})

    def test_utf8_file_exact_limit_and_one_byte_over_no_launch(self) -> None:
        """Count bytes, not characters; accept exact limits and reject before launch."""
        text = "é🙂" * 4
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(plugin_id) as (
                module,
                _,
                request,
                workspace,
                _,
            ):
                context = workspace / "private-marker.txt"
                context.write_text(text, encoding="utf-8")
                request["input"].update(
                    context_files=[context.name],
                    max_context_file_bytes=len(text.encode()),
                    max_prompt_bytes=4096,
                )
                frame, provider = self.invoke(module, request)
                self.assertEqual(frame["status"], "success")
                provider.assert_called_once()
                request["input"]["max_context_file_bytes"] -= 1
                frame, provider = self.invoke(module, request)
                provider.assert_not_called()
                self.assertEqual(frame["error"]["code"], "validation_error")
                manifest = json.loads(frame["error"]["message"])
                self.assertEqual(manifest["bytes"], len(text.encode()))
                self.assertEqual(manifest["contributor"], "context[0]")
                self.assertNotIn("private-marker", json.dumps(frame))
                self.assertNotIn(text, json.dumps(frame, ensure_ascii=False))

    def test_utf8_composed_exact_limit_includes_final_headings(self) -> None:
        """The final joined prompt, including separators and headings, is budgeted."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(plugin_id) as (
                module,
                helper,
                request,
                _,
                _,
            ):
                request["input"].update(
                    prompt="é🙂 task",
                    skills_rendered="skill é🙂",
                    max_prompt_bytes=4096,
                )
                prompt, _, _ = helper.compose(
                    vars(module), request, request["input"], request["input"]["prompt"]
                )
                exact = len(prompt.encode())
                request["input"]["max_prompt_bytes"] = exact
                frame, provider = self.invoke(module, request)
                self.assertEqual(frame["status"], "success")
                provider.assert_called_once()
                request["input"]["max_prompt_bytes"] = exact - 1
                frame, provider = self.invoke(module, request)
                provider.assert_not_called()
                manifest = json.loads(frame["error"]["message"])
                self.assertEqual(manifest["bytes"], exact)
                self.assertEqual(
                    manifest["contributor"], "composed_prompt:task_and_headings"
                )

    def test_each_contributor_is_checked_and_missing_is_not_skipped(self) -> None:
        """Base, skills, gate, draft, legacy replay, and named contexts fail closed."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(plugin_id) as (
                module,
                _,
                request,
                workspace,
                _,
            ):
                draft = workspace / "draft.md"
                draft.write_text("x" * 2049)
                history, _ = module.interaction_paths(
                    request, {"response_file": "draft.md"}
                )
                Path(history).write_text(
                    json.dumps(
                        [
                            {
                                "root_run_id": module.root_run_id(request),
                                "response_text": "x" * 2049,
                            }
                        ]
                    )
                )
                cases = (
                    ({"prompt": "x" * 2049}, "base_prompt"),
                    ({"skills_rendered": "x" * 2049}, "skills"),
                    (
                        {"gate_decision": {**GATE, "reason": "x" * 2049}},
                        "gate_feedback",
                    ),
                    (
                        {
                            "gate_decision": GATE,
                            "response_file": "draft.md",
                            "feedback_context_mode": "focused",
                        },
                        "current_draft",
                    ),
                    ({"context_files": ["not-present-private-name"]}, "context[0]"),
                    ({"context_files": ["draft.md"]}, "context[0]"),
                )
                for extra, contributor in cases:
                    request["input"] = {
                        "prompt": "task",
                        "max_prompt_bytes": 2048,
                        **extra,
                    }
                    frame, provider = self.invoke(module, request)
                    provider.assert_not_called()
                    self.assertEqual(frame["error"]["code"], "validation_error")
                    self.assertIn(contributor, frame["error"]["message"])
                    self.assertNotIn(
                        "not-present-private-name", frame["error"]["message"]
                    )
                draft.write_text("small draft")
                request["input"] = {
                    "prompt": "task",
                    "max_prompt_bytes": 2048,
                    "gate_decision": GATE,
                    "response_file": "draft.md",
                    "feedback_context_mode": "full",
                }
                frame, provider = self.invoke(module, request)
                provider.assert_not_called()
                self.assertIn("legacy_history", frame["error"]["message"])

    def test_prompt_file_raw_bytes_and_invalid_utf8(self) -> None:
        """Budget the source before stripping and reject unreadable/invalid UTF-8."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(plugin_id) as (
                module,
                _,
                request,
                workspace,
                _,
            ):
                source = workspace / "secret-name.txt"
                for data in (b" " * 2048 + b"x", b"\xff"):
                    source.write_bytes(data)
                    request["input"] = {
                        "prompt_file": str(source),
                        "max_prompt_bytes": 2048,
                    }
                    frame, provider = self.invoke(module, request)
                    provider.assert_not_called()
                    self.assertEqual(frame["error"]["code"], "validation_error")
                    self.assertNotIn(str(source), json.dumps(frame))

    def test_read_is_bounded_even_when_file_grows(self) -> None:
        """A small fstat result cannot cause an unbounded read after file growth."""
        with self.fixture(IDS[0]) as (_, helper, _, _, _):
            reader = mock.MagicMock()
            reader.__enter__.return_value = reader
            reader.read.return_value = b"12345"
            with mock.patch.object(
                helper.os, "open", return_value=100
            ), mock.patch.object(
                helper.os, "fdopen", return_value=reader
            ), mock.patch.object(
                helper.os, "fstat", return_value=mock.Mock(st_size=0, st_mode=0o100600)
            ):
                with self.assertRaisesRegex(ValueError, "bytes_at_least"):
                    helper.read_bounded(
                        "unused", "context[0]", {"max_context_file_bytes": 4}
                    )
            reader.read.assert_called_once_with(5)

    def test_actual_argv_environment_and_pointer_bytes(self) -> None:
        """Include UTF-8, env, terminators and pointers without disclosing values."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(plugin_id) as (
                _,
                helper,
                _,
                _,
                _,
            ):
                command, env = ["provider", "é🙂"], {"KEY": "é🙂"}
                strings = [os.fsencode(value) for value in command] + [
                    os.fsencode("KEY=é🙂")
                ]
                exact = sum(len(value) + 1 for value in strings) + (
                    len(strings) + 2
                ) * struct.calcsize("P")
                helper.check_exec(command, env, exact + 4096)
                with self.assertRaisesRegex(
                    ValueError, "command-line length"
                ) as caught:
                    helper.check_exec(command, env, exact + 4095)
                self.assertNotIn("KEY", str(caught.exception))
                with mock.patch.object(
                    helper.sys, "platform", "linux"
                ), self.assertRaises(ValueError):
                    helper.check_exec(["provider", "x" * 131072], {}, 10_000_000)
                with self.assertRaisesRegex(ValueError, "NUL"):
                    helper.check_exec(["provider", "a\0b"], {})

    def test_exec_guard_prevents_all_argv_provider_launches(self) -> None:
        """The real run_command checks its final child env before Popen/run_process."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(plugin_id) as (
                module,
                helper,
                request,
                _,
                _,
            ):
                target = "Popen" if plugin_id == "cursor.cli" else "run_process"
                owner = module.subprocess if plugin_id == "cursor.cli" else module
                with mock.patch.object(
                    helper.os, "sysconf", return_value=4200
                ), mock.patch.object(owner, target) as launch:
                    with self.assertRaises(
                        (ValueError, getattr(module, "PromptTooLongError", ValueError))
                    ):
                        module.run_command(
                            request,
                            ["provider", "é" * 100],
                            input_payload={
                                "timeout_seconds": 5,
                                "output_mode": "compact",
                            },
                        )
                    launch.assert_not_called()

    def test_codex_stdin_prompt_budget_is_independent_of_argv(self) -> None:
        """A configured total budget covers stdin even though argv carries only '-'."""
        with self.fixture(
            "codex.cli", {"prompt": "é" * 500, "max_prompt_bytes": 1000}
        ) as (module, _, request, _, _):
            frame, provider = self.invoke(module, request)
            provider.assert_not_called()
            self.assertIn("task_and_headings", frame["error"]["message"])
            request["input"]["max_prompt_bytes"] = 2000
            frame, provider = self.invoke(module, request)
            self.assertEqual(frame["status"], "success")
            self.assertIn("é" * 500, provider.call_args.kwargs["stdin_text"])
            self.assertNotIn("é" * 500, provider.call_args.args[1])

    def test_compact_closed_schema_success_nonzero_timeout_cleanup(self) -> None:
        """Each terminal branch keeps authoritative controls and validates honestly."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                },
            ) as (module, _, request, _, _):
                cleanup = module.CleanupUnconfirmedError.__new__(
                    module.CleanupUnconfirmedError
                )
                Exception.__init__(cleanup, "private native details")
                cleanup.stdout, cleanup.stderr, cleanup.response = (
                    RAW,
                    "warning",
                    ANSWER,
                )
                timeout = subprocess.TimeoutExpired(
                    ["fixture"], 5, output="partial answer", stderr="warning"
                )
                for exit_code, error in (
                    (0, None),
                    (7, None),
                    (0, timeout),
                    (0, cleanup),
                ):
                    with self.subTest(
                        outcome=type(error).__name__ if error else exit_code
                    ):
                        frame, _ = self.invoke(
                            module, request, exit_code=exit_code, error=error
                        )
                        output = frame["output"]
                        self.validator(plugin_id).validate(output)
                        self.assertEqual(output["output_mode"], "compact")
                        self.assertNotIn("stdout", output)
                        self.assertNotIn("stderr", output)
                        self.assertEqual(
                            output["exit_code"], None if error else exit_code
                        )
                        self.assertEqual(output["ok"], exit_code == 0 and error is None)
                        self.assertNotIn("private native details", json.dumps(frame))
                        if error is None:
                            self.assertEqual(
                                frame["status"],
                                "success" if exit_code == 0 else "failed",
                            )
                            if plugin_id != "codex.cli":
                                self.assertEqual(output["response_text"], ANSWER)
                            if plugin_id in {"cursor.cli", "opencode.cli"}:
                                self.assertEqual(
                                    output["session_id"], "fixture-session"
                                )
                                self.assertEqual(frame["usage"]["total_tokens"], 8)
                        for bad in (
                            {**output, "stdout": ""},
                            {**output, "unexpected": True},
                            {**output, "diagnostics": {"unknown": True}},
                        ):
                            with self.assertRaises(ValidationError):
                                self.validator(plugin_id).validate(bad)
                        if error:
                            self.assertEqual(
                                frame["error"]["code"],
                                (
                                    "timeout_error"
                                    if error is timeout
                                    else "protocol_error"
                                ),
                            )

    def test_compact_handoff_hints_do_not_duplicate_raw_logs(self) -> None:
        """Answer aliases retain canonical content without copying narration."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                    "response_file": "draft.md",
                    "summary_title_file": "title.txt",
                    "pr_body_file": "body.md",
                },
            ) as (module, _, request, workspace, _):
                # Codex intentionally has no summary/PR-body controls.
                if plugin_id == "codex.cli":
                    request["input"].pop("summary_title_file")
                    request["input"].pop("pr_body_file")
                frame, _ = self.invoke(module, request)
                self.assertEqual(frame["status"], "success")
                self.assertNotIn("provider tool narration", json.dumps(frame["output"]))
                self.assertEqual(
                    sum(
                        "provider tool narration" in hint["contents"]
                        for hint in frame["artifact_hints"]
                    ),
                    1,
                )
                self.assertEqual((workspace / "draft.md").read_text().strip(), ANSWER)
                if plugin_id != "codex.cli":
                    self.assertEqual(frame["output"]["summary_title"], "Change title")
                    self.assertEqual(
                        frame["output"]["pr_body_text"],
                        "## What Changed\n\nUseful answer é🙂",
                    )

    def test_partial_structured_timeout_retains_only_authoritative_answer(self) -> None:
        """Timeout output excludes tool events but retains their raw stdout artifact."""
        for plugin_id in ("cursor.cli", "github.copilot.cli", "opencode.cli"):
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                },
            ) as (module, _, request, _, _):
                if plugin_id == "cursor.cli":
                    event = {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": ANSWER}]},
                    }
                elif plugin_id == "github.copilot.cli":
                    event = {"type": "assistant.message", "data": {"content": ANSWER}}
                else:
                    event = {"type": "text", "part": {"text": ANSWER}}
                log = (
                    json.dumps(
                        {
                            "type": "tool_use",
                            "private_tool_diagnostic": "RAW_LOG_SENTINEL",
                        }
                    )
                    + "\n"
                    + json.dumps(event)
                    + "\n"
                )
                if plugin_id == "cursor.cli":
                    log = (
                        json.dumps({"type": "result", "result": "obsolete answer"})
                        + "\n"
                        + log
                    )
                    log += json.dumps({"type": "result", "result": ANSWER}) + "\n"
                timeout = subprocess.TimeoutExpired(
                    ["fixture"], 5, output=log, stderr="warning"
                )
                frame, _ = self.invoke(module, request, error=timeout)
                self.validator(plugin_id).validate(frame["output"])
                self.assertEqual(frame["output"]["response_text"], ANSWER)
                self.assertEqual(frame["error"]["code"], "timeout_error")
                self.assertNotIn("RAW_LOG_SENTINEL", json.dumps(frame["output"]))
                self.assertEqual(
                    next(
                        h["contents"]
                        for h in frame["artifact_hints"]
                        if h["name"] == "stdout.txt"
                    ),
                    log,
                )

    def test_missing_authoritative_answer_does_not_promote_raw_logs(self) -> None:
        """Opt-in Codex/OpenCode fail honestly when only diagnostic logs exist."""
        for plugin_id in ("codex.cli", "opencode.cli"):
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                    "response_file": "draft.md",
                },
            ) as (module, helper, request, workspace, _):
                completed = subprocess.CompletedProcess(
                    ["fixture"], 0, "RAW_LOG_SENTINEL\n", ""
                )
                provider_result = (
                    completed
                    if plugin_id == "codex.cli"
                    else {
                        "completed": completed,
                        "response_text": "",
                        "session_id": "",
                        "share_url": "",
                        "usage": None,
                        "event_error": None,
                    }
                )
                with mock.patch.object(
                    module, "run_command", return_value=provider_result
                ):
                    frame = module.handle_request(request)
                self.assertEqual(frame["status"], "failed")
                self.assertEqual(frame["error"]["code"], "protocol_error")
                self.validator(plugin_id).validate(frame["output"])
                self.assertEqual(frame["output"]["response_text"], "")
                for path in workspace.rglob("*"):
                    if path.is_file():
                        self.assertNotIn("RAW_LOG_SENTINEL", path.read_text())
                records = helper.record_paths(
                    helper.history_dir(vars(module), request, request["input"])
                )
                self.assertEqual(json.loads(records[0].read_text())["status"], "failed")

    def test_envelope_shortens_only_diagnostics_and_keeps_answer(self) -> None:
        """Escaped multibyte logs cannot discard a fitting authoritative answer."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                },
            ) as (module, helper, request, _, _):
                with mock.patch.object(helper, "RESULT_BYTES", 200000):
                    frame, _ = self.invoke(module, request, stderr="🙂" * 60000)
                contract_validator("app/invoke.result.v1.schema.json").validate(frame)
                self.validator(plugin_id).validate(frame["output"])
                self.assertEqual(frame["status"], "success")
                self.assertEqual(frame["output"]["response_text"], ANSWER)
                self.assertLessEqual(helper.size(json.dumps(frame) + "\n"), 200000)
                item = next(
                    d
                    for d in frame["output"]["diagnostics"]["artifacts"]
                    if d["artifact"] == "stderr.txt"
                )
                self.assertEqual(item["total_bytes"], 240000)
                self.assertEqual(item["retention"], "diagnostic_preview")
                self.assertFalse(item["full_log_available"])
                metadata = json.loads(
                    next(
                        h["contents"]
                        for h in frame["artifact_hints"]
                        if h["name"] == "invocation.json"
                    )
                )
                self.assertEqual(metadata["status"], "success")

    def test_megabyte_escaped_logs_preserve_backend_outcome_and_required_hints(
        self,
    ) -> None:
        """Optional multi-megabyte diagnostics never turn success into failure."""
        log = '🙂é\n"\0' * 200000
        for plugin_id in IDS:
            for exit_code in (0, 7):
                with self.subTest(plugin=plugin_id, exit=exit_code), self.fixture(
                    plugin_id,
                    {
                        "prompt": "task",
                        "output_mode": "compact",
                        "history_mode": "invocation",
                    },
                ) as (module, helper, request, _, _):
                    frame, _ = self.invoke(
                        module, request, exit_code=exit_code, stdout=log, stderr=log
                    )
                    self.assertEqual(
                        frame["status"], "success" if exit_code == 0 else "failed"
                    )
                    self.assertEqual(frame["output"]["ok"], exit_code == 0)
                    self.validator(plugin_id).validate(frame["output"])
                    if exit_code == 0:
                        self.assertNotIn("error", frame)
                    else:
                        self.assertEqual(frame["error"]["code"], "tool_execution_error")
                    hints = {
                        hint["name"]: hint["contents"]
                        for hint in frame["artifact_hints"]
                    }
                    self.assertEqual(hints["response.txt"], ANSWER)
                    metadata = json.loads(hints["invocation.json"])
                    self.assertEqual(metadata["status"], frame["status"])
                    self.assertEqual(metadata["exit_code"], exit_code)
                    for item in frame["output"]["diagnostics"]["artifacts"]:
                        if item["artifact"] in {"stdout.txt", "stderr.txt"}:
                            self.assertGreaterEqual(
                                item["total_bytes"], helper.FAMILY_BYTES
                            )
                            self.assertFalse(item["full_log_available"])
                            self.assertEqual(
                                item["retained_bytes"],
                                helper.size(hints.get(item["artifact"], "")),
                            )
                        else:
                            self.assertTrue(item["full_log_available"])
                    self.assertLessEqual(
                        helper.size(json.dumps(frame) + "\n"), helper.RESULT_BYTES
                    )
                    self.assertLess(
                        sum(helper.size(text) for text in hints.values()),
                        helper.FAMILY_BYTES,
                    )
                    record = helper.record_paths(
                        helper.history_dir(vars(module), request, request["input"])
                    )[0]
                    self.assertEqual(
                        json.loads(record.read_text())["status"], frame["status"]
                    )

    def test_family_budget_reserves_metadata_before_optional_logs(self) -> None:
        """Aggregate pressure cannot starve required metadata behind log hints."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                },
            ) as (module, helper, request, _, _):
                with mock.patch.object(helper, "FAMILY_BYTES", 4096):
                    frame, _ = self.invoke(
                        module, request, stdout="x" * 3000, stderr="x" * 3000
                    )
                self.assertEqual(frame["status"], "success")
                self.assertTrue(frame["output"]["ok"])
                hints = {
                    hint["name"]: hint["contents"] for hint in frame["artifact_hints"]
                }
                self.assertEqual(hints["response.txt"], ANSWER)
                self.assertEqual(
                    json.loads(hints["invocation.json"])["status"], "success"
                )
                self.assertLess(sum(helper.size(text) for text in hints.values()), 4096)

    def test_empty_optional_context_entries_skip_but_nonempty_missing_fails(
        self,
    ) -> None:
        """Unmaterialized optional report paths are absent; named missing files fail."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "initial plan",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                    "context_files": ["", None, "   "],
                    "max_prompt_bytes": 98304,
                },
            ) as (module, _, request, workspace, _):
                self.validator(plugin_id, "input").validate(request["input"])
                frame, provider = self.invoke(module, request)
                self.assertEqual(frame["status"], "success")
                provider.assert_called_once()
                self.assertFalse((workspace / "validation_report.md").exists())
                request["input"]["context_files"].append("validation_report.md")
                frame, provider = self.invoke(module, request)
                provider.assert_not_called()
                self.assertEqual(frame["error"]["code"], "validation_error")
                self.assertIn("context[3]", frame["error"]["message"])

    def test_modes_are_independent_and_invocation_timeout_hints_are_answers(
        self,
    ) -> None:
        """Invocation/legacy output keeps raw fields without aliasing log events."""
        for plugin_id in IDS:
            for output_mode, history_mode in (
                ("compact", "legacy"),
                ("legacy", "invocation"),
            ):
                with self.subTest(plugin=plugin_id, mode=output_mode), self.fixture(
                    plugin_id,
                    {
                        "prompt": "task",
                        "output_mode": output_mode,
                        "history_mode": history_mode,
                    },
                ) as (module, _, request, _, _):
                    frame, _ = self.invoke(module, request)
                    self.validator(plugin_id).validate(frame["output"])
                    self.assertEqual(
                        "stdout" in frame["output"], output_mode == "legacy"
                    )
                    self.assertEqual(
                        any(
                            h["name"] == "invocation.json"
                            for h in frame["artifact_hints"]
                        ),
                        history_mode == "invocation",
                    )
                    if (
                        plugin_id
                        in {"cursor.cli", "opencode.cli", "github.copilot.cli"}
                        and history_mode == "invocation"
                    ):
                        event = {"type": "text", "part": {"text": ANSWER}}
                        if plugin_id == "cursor.cli":
                            event = {"type": "result", "result": ANSWER}
                        elif plugin_id == "github.copilot.cli":
                            event = {
                                "type": "assistant.message",
                                "data": {"content": ANSWER},
                            }
                        log = json.dumps(event) + "\n"
                        frame, _ = self.invoke(
                            module,
                            request,
                            error=subprocess.TimeoutExpired(
                                [], 5, output=log, stderr=""
                            ),
                        )
                        self.validator(plugin_id).validate(frame["output"])
                        self.assertEqual(frame["output"]["response_text"], ANSWER)
                        self.assertEqual(
                            next(
                                h["contents"]
                                for h in frame["artifact_hints"]
                                if h["name"] == "response.txt"
                            ),
                            ANSWER,
                        )

    def test_budget_manifest_is_sanitized_and_counts_all_composed_sections(
        self,
    ) -> None:
        """All section labels account exact joined UTF-8 bytes."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task é🙂",
                    "history_mode": "invocation",
                    "output_mode": "compact",
                    "max_prompt_bytes": 98304,
                    "skills_rendered": "skill",
                    "gate_decision": GATE,
                    "context_files": ["PRIVATE_NAME.txt"],
                },
            ) as (module, _, request, workspace, _):
                (workspace / "PRIVATE_NAME.txt").write_text(
                    "context é🙂", encoding="utf-8"
                )
                frame, _ = self.invoke(module, request)
                metadata = json.loads(
                    next(
                        h["contents"]
                        for h in frame["artifact_hints"]
                        if h["name"] == "invocation.json"
                    )
                )
                budget = metadata["prompt_budget"]
                self.assertEqual(
                    budget["total_bytes"], len(metadata["composed_prompt"].encode())
                )
                self.assertEqual(
                    budget["total_bytes"],
                    sum(item["bytes"] for item in budget["contributors"])
                    + 2 * (len(budget["contributors"]) - 1),
                )
                self.assertNotIn("PRIVATE_NAME", json.dumps(budget))
                self.assertNotIn("context é🙂", json.dumps(budget, ensure_ascii=False))

    def test_selected_manifest_assets_and_schema_references_are_self_contained(
        self,
    ) -> None:
        """Action schemas, entrypoint, and local refs exist in each finite payload."""
        for plugin_id in IDS:
            package = PACKAGES / plugin_id
            manifest = yaml.safe_load((package / "plugin.yaml").read_text())
            selected = set(manifest["package"]["files"])
            self.assertIn(manifest["entrypoint"]["command"], selected)
            for relative in selected:
                path = package / relative
                self.assertTrue(path.is_file())
                self.assertFalse(path.is_symlink())
                if path.suffix == ".json":
                    schema = json.loads(path.read_text())
                    Draft202012Validator.check_schema(schema)

                    def inspect(value: object) -> None:
                        """Verify selected contained local schema refs recursively."""
                        if isinstance(value, dict):
                            if "$ref" in value and not value["$ref"].startswith("#"):
                                target = (
                                    path.parent / value["$ref"].split("#", 1)[0]
                                ).resolve()
                                self.assertIn(
                                    target.relative_to(package.resolve()).as_posix(),
                                    selected,
                                )
                            for child in value.values():
                                inspect(child)
                        elif isinstance(value, list):
                            for child in value:
                                inspect(child)

                    inspect(schema)
            for action in manifest["actions"]:
                self.assertIn(action["input_schema"], selected)
                self.assertIn(action["output_schema"], selected)

    def test_authoritative_transport_overrides_fail_before_provider_launch(
        self,
    ) -> None:
        """Raw CLI pass-through cannot change the answer source in opt-in modes."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "extra_args": ["--output-format=text"],
                },
            ) as (module, _, request, _, _):
                frame, provider = self.invoke(module, request)
                provider.assert_not_called()
                self.assertEqual(frame["error"]["code"], "validation_error")

    def test_opencode_export_preserves_full_final_message_parts(self) -> None:
        """The final assistant export is a complete report, not its last text part."""
        with self.fixture("opencode.cli") as (module, _, _, _, _):
            export = {
                "info": {"id": "session"},
                "messages": [
                    {
                        "info": {"role": "assistant"},
                        "parts": [{"type": "text", "text": "obsolete"}],
                    },
                    {
                        "info": {"role": "assistant"},
                        "parts": [
                            {"type": "text", "text": "# Findings\n\nP1: full finding"},
                            {"type": "tool", "text": "RAW_LOG_SENTINEL"},
                            {"type": "text", "text": "## Review Response\n\nAddressed"},
                        ],
                    },
                ],
            }
            self.assertEqual(
                module.extract_export_details(export, whole_answer=True)[
                    "response_text"
                ],
                "# Findings\n\nP1: full finding\n\n## Review Response\n\nAddressed",
            )
            self.assertEqual(
                module.extract_export_details(export)["response_text"],
                "## Review Response\n\nAddressed",
            )

    def test_hint_count_and_family_contracts_are_enforced(self) -> None:
        """More than 32 hints or 1 MiB cannot escape the terminal envelope guard."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                },
            ) as (module, helper, request, _, _):
                source, _ = self.invoke(module, request)
                source["artifact_hints"] = [
                    {"name": f"hint-{index}.txt", "contents": ""} for index in range(33)
                ]
                with mock.patch.object(module, "_handle_request", return_value=source):
                    failed = module.handle_request(request)
                self.assertEqual(failed["error"]["code"], "protocol_error")
                self.assertEqual(failed["output"], {})
                manifest = json.loads(failed["artifact_hints"][0]["contents"])
                self.assertEqual(manifest["hint_count"], 34)  # response.txt is added.
                self.assertEqual(manifest["limit_count"], 32)
                source, _ = self.invoke(module, request)
                source["artifact_hints"] = [
                    {"name": f"hint-{index}.txt", "contents": "x" * helper.HINT_BYTES}
                    for index in range(5)
                ]
                with mock.patch.object(module, "_handle_request", return_value=source):
                    failed = module.handle_request(request)
                self.assertEqual(failed["status"], "failed")
                self.assertLessEqual(
                    helper.size(json.dumps(failed) + "\n"), helper.RESULT_BYTES
                )
                self.assertLessEqual(
                    sum(helper.size(h["contents"]) for h in failed["artifact_hints"]),
                    helper.FAMILY_BYTES,
                )

    def test_serialized_result_exact_package_limit_and_one_byte_over(self) -> None:
        """The envelope counts escaped Unicode and the exact terminal newline."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                },
            ) as (module, helper, request, _, _):
                baseline, _ = self.invoke(module, request)
                exact = helper.size(json.dumps(baseline) + "\n")
                with mock.patch.object(helper, "RESULT_BYTES", exact):
                    frame, _ = self.invoke(module, request)
                    self.assertEqual(frame["status"], "success")
                with mock.patch.object(helper, "RESULT_BYTES", exact - 1):
                    frame, _ = self.invoke(module, request)
                    self.assertLessEqual(
                        helper.size(json.dumps(frame) + "\n"), exact - 1
                    )
                    if frame["output"]:
                        self.validator(plugin_id).validate(frame["output"])
                        self.assertEqual(frame["output"]["response_text"], ANSWER)
                        self.assertTrue(frame["output"]["ok"])
                        self.assertEqual(frame["status"], "success")
                        self.assertNotIn("error", frame)
                    else:
                        self.assertEqual(frame["error"]["code"], "protocol_error")
                        self.assertEqual(
                            json.loads(frame["artifact_hints"][0]["contents"])[
                                "result_bytes"
                            ],
                            exact,
                        )

    def test_oversized_metadata_json_is_unavailable_not_truncated(self) -> None:
        """Oversized metadata is unavailable rather than invalid partial JSON."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "x" * 300000,
                    "output_mode": "compact",
                    "history_mode": "invocation",
                    "max_prompt_bytes": 400000,
                },
            ) as (module, _, request, _, _):
                frame, _ = self.invoke(module, request)
                self.assertEqual(frame["status"], "failed")
                self.assertEqual(frame["error"]["code"], "protocol_error")
                self.validator(plugin_id).validate(frame["output"])
                manifest = next(
                    item
                    for item in frame["output"]["diagnostics"]["artifacts"]
                    if item["artifact"] == "invocation.json"
                )
                self.assertEqual(manifest["retained_bytes"], 0)
                self.assertFalse(manifest["full_log_available"])
                self.assertFalse(
                    any(h["name"] == "invocation.json" for h in frame["artifact_hints"])
                )

    def test_workflow_multibyte_budget_keeps_prompt_once_in_metadata(self) -> None:
        """A fitting UTF-8 task is stored once, without an escaped duplicate copy."""
        task = "🙂" * 22000
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": task,
                    "output_mode": "compact",
                    "history_mode": "invocation",
                    "max_prompt_bytes": 98304,
                    "max_context_file_bytes": 65536,
                },
            ) as (module, helper, request, _, _):
                frame, provider = self.invoke(module, request)
                self.assertEqual(frame["status"], "success")
                provider.assert_called_once()
                hint = next(
                    h for h in frame["artifact_hints"] if h["name"] == "invocation.json"
                )
                self.assertEqual(hint["contents"].count(task), 1)
                self.assertLessEqual(helper.size(hint["contents"]), helper.HINT_BYTES)
                self.assertLessEqual(
                    helper.size(json.dumps(frame) + "\n"), helper.RESULT_BYTES
                )
                self.assertNotIn("task_prompt", json.loads(hint["contents"]))

    def test_selected_packages_native_app_compact_smoke(self) -> None:
        """Execute each finite payload in a fresh process with native fake providers."""
        for plugin_id in IDS:
            with self.subTest(
                plugin=plugin_id
            ), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                package, workspace, artifacts = (
                    root / "package",
                    root / "workspace",
                    root / "staging",
                )
                package.mkdir()
                workspace.mkdir()
                artifacts.mkdir()
                source = PACKAGES / plugin_id
                manifest = yaml.safe_load((source / "plugin.yaml").read_text())
                for relative in ["plugin.yaml", *manifest["package"]["files"]]:
                    target = package / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source / relative, target)
                binary = workspace / "provider-fixture"
                command = (
                    f"#!{sys.executable}\nimport json,sys,pathlib\n"
                    f"answer={ANSWER!r}\n"
                )
                if plugin_id == "codex.cli":
                    command += (
                        "index=sys.argv.index('--output-last-message')+1\n"
                        "pathlib.Path(sys.argv[index]).write_text(answer,"
                        "encoding='utf-8')\nprint('RAW_LOG_SENTINEL')\n"
                    )
                    binary = workspace / "codex"
                elif plugin_id == "github.copilot.cli":
                    command += (
                        "if sys.argv[1:] == ['copilot','--','--help']:\n"
                        " print('--output-format <format> (choices: text, json)\\n'"
                        "'--stream <mode> (choices: on, off)')\n"
                        " sys.exit(0)\n"
                        "assert sys.argv[1:3] == ['copilot','--']\n"
                        "assert sys.argv[3] == '-p'\n"
                        "assert sys.argv[5:-1] == ['--allow-all-tools',"
                        "'--allow-all-paths','--no-ask-user','--no-color',"
                        "'--output-format','json','--stream','on','--add-dir']\n"
                        "index=sys.argv.index('--output-format')+1\n"
                        "assert sys.argv[index] == 'json'\n"
                        "assert sys.argv[sys.argv.index('--stream')+1] == 'on'\n"
                        "print(json.dumps({'type':'tool.execution_complete',"
                        "'data':{'output':'RAW_LOG_SENTINEL'}}))\n"
                        "print(json.dumps({'type':'assistant.message',"
                        "'data':{'content':answer}}))\n"
                    )
                    binary = workspace / "gh"
                elif plugin_id == "cursor.cli":
                    command += (
                        "print(json.dumps({'type':'result','result':answer,"
                        "'session_id':'fixture','usage':{'inputTokens':3,"
                        "'outputTokens':5,'totalTokens':8}}))\n"
                    )
                else:
                    command += (
                        "print(json.dumps({'type':'text','sessionID':'fixture',"
                        "'part':{'text':answer}}))\n"
                    )
                    binary = workspace / "opencode"
                binary.write_text(command, encoding="utf-8")
                binary.chmod(0o755)
                action = "exec_task"
                request = app_invoke_request(
                    action,
                    {
                        "prompt": "task",
                        "context_files": ["", None],
                        "output_mode": "compact",
                        "history_mode": "invocation",
                        "max_context_file_bytes": 65536,
                        "max_prompt_bytes": 98304,
                        "response_file": "draft.md",
                        "timeout_seconds": 5,
                    },
                    str(artifacts),
                )
                request["paths"]["workspace_dir"] = str(workspace)
                environment = {
                    "PATH": str(workspace),
                    "CURSOR_AGENT_PATH": str(binary),
                    "CODEX_HOME": str(root / "empty-codex-home"),
                    "CURSOR_API_KEY": "fixture-only-key",
                }
                with mock.patch.dict(os.environ, environment, clear=True):
                    code, frames, stderr = run_process_plugin_subprocess(
                        package, request, timeout=20
                    )
                self.assertEqual((code, stderr), (0, ""))
                handshake, frame = assert_app_frames(frames, plugin_id)
                self.assertEqual(handshake["plugin"]["version"], "0.1.0")
                self.assertEqual(frame["status"], "success")
                self.validator(plugin_id).validate(frame["output"])
                self.assertEqual(frame["output"]["response_text"], ANSWER)
                self.assertEqual((workspace / "draft.md").read_text().strip(), ANSWER)
                self.assertEqual(
                    sum(h["name"] == "stdout.txt" for h in frame["artifact_hints"]), 1
                )
                self.assertEqual(
                    sum(
                        h["name"] == "invocation.json" for h in frame["artifact_hints"]
                    ),
                    1,
                )

    def test_replay_namespace_rejects_symlink_aliases_and_corrupt_records(self) -> None:
        """Durable replay cannot alias a foreign root or follow escaped records."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                    "response_file": "draft.md",
                    "gate_decision": GATE,
                },
            ) as (module, helper, request, workspace, _):
                foreign = workspace.parent / "foreign-history"
                foreign.mkdir()
                base = workspace / "draft.invocations"
                base.symlink_to(foreign, target_is_directory=True)
                frame, provider = self.invoke(module, request)
                provider.assert_not_called()
                self.assertEqual(frame["error"]["code"], "validation_error")
                self.assertNotIn(str(foreign), frame["error"]["message"])
                base.unlink()
                directory = helper.history_dir(vars(module), request, request["input"])
                directory.mkdir(parents=True)
                record = directory / "record.json"
                record.write_text("{}")
                frame, provider = self.invoke(module, request)
                provider.assert_not_called()
                self.assertEqual(frame["error"]["code"], "validation_error")
                record.unlink()
                external = foreign / "record.json"
                external.write_text("{}")
                record.symlink_to(external)
                frame, provider = self.invoke(module, request)
                provider.assert_not_called()
                self.assertIn("symlink", frame["error"]["message"])

    def test_opencode_export_exec_guard_prevents_secondary_launch(self) -> None:
        """Session export checks argv/environment before its secondary child."""
        with self.fixture("opencode.cli", export_fixture=False) as (
            module,
            helper,
            _,
            _,
            _,
        ):
            with mock.patch.object(
                helper.os, "sysconf", return_value=4200
            ), mock.patch.object(module, "run_process") as launch:
                with self.assertRaises(ValueError):
                    module.export_session_data(
                        "é" * 100, "unused", {}, bounded_exec=True
                    )
                launch.assert_not_called()

    def test_legacy_whitespace_and_default_history_unchanged(self) -> None:
        """Omitted controls retain the old prompt/draft stripping and sidecars."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(plugin_id) as (
                module,
                _,
                request,
                workspace,
                _,
            ):
                (workspace / "draft.md").write_text("  Existing draft\n\n")
                request["input"].update(
                    prompt="  task é\n\n",
                    response_file="draft.md",
                    gate_decision=GATE,
                    feedback_context_mode="focused",
                )
                compose_args = (request, request["input"], request["input"]["prompt"])
                if plugin_id == "github.copilot.cli":
                    compose_args += ("focused",)
                composed, _, _ = module.compose_prompt(*compose_args)
                self.assertIn("# Current draft: draft.md\n\nExisting draft", composed)
                self.assertTrue(
                    composed.endswith(
                        "  task é\n\n"
                        if plugin_id == "github.copilot.cli"
                        else "task é"
                    )
                )
                frame, _ = self.invoke(module, request)
                self.validator(plugin_id).validate(frame["output"])
                self.assertEqual(frame["output"]["stdout"], RAW)
                self.assertNotIn("output_mode", frame["output"])
                history, markdown = module.interaction_paths(request, request["input"])
                self.assertTrue(Path(history).is_file())
                self.assertTrue(Path(markdown).is_file())
                self.assertEqual(
                    json.loads(Path(history).read_text())[0]["stdout"], RAW.strip()
                )

    def test_invocation_storage_linear_and_logs_not_in_workspace(self) -> None:
        """Repeated focused turns add one metadata hint and fixed-size replay files."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                    "feedback_context_mode": "focused",
                    "response_file": "draft.md",
                },
            ) as (module, helper, request, workspace, _):
                previous = 0
                increments = []
                for index in range(6):
                    request["invoke_id"] = f"invoke-{index}"
                    frame, _ = self.invoke(
                        module, request, stdout="RAW_LOG_SENTINEL\n\n" + ANSWER
                    )
                    self.assertEqual(frame["status"], "success")
                    hints = frame["artifact_hints"]
                    self.assertEqual(sum(h["name"] == "stdout.txt" for h in hints), 1)
                    self.assertEqual(sum(h["name"] == "stderr.txt" for h in hints), 1)
                    self.assertEqual(
                        sum(h["name"] == "invocation.json" for h in hints), 1
                    )
                    metadata = json.loads(
                        next(
                            h["contents"]
                            for h in hints
                            if h["name"] == "invocation.json"
                        )
                    )
                    self.assertEqual(metadata["invoke_id"], request["invoke_id"])
                    self.assertNotIn("task_prompt", metadata)
                    self.assertIn("# Task\n\ntask", metadata["composed_prompt"])
                    self.assertIn("composed_prompt", metadata)
                    self.assertEqual(metadata["artifacts"]["stdout"], "stdout.txt")
                    self.assertNotIn("RAW_LOG_SENTINEL", json.dumps(metadata))
                    self.assertFalse(any("interaction." in h["name"] for h in hints))
                    files = list(
                        helper.history_dir(
                            vars(module), request, request["input"]
                        ).iterdir()
                    )
                    self.assertEqual(len(files), 2 * (index + 1))
                    current = sum(path.stat().st_size for path in files)
                    increments.append(current - previous)
                    previous = current
                    for path in files:
                        self.assertNotIn("RAW_LOG_SENTINEL", path.read_text())
                        self.assertNotIn("composed_prompt", path.read_text())
                self.assertEqual(len(set(increments)), 1)
                self.assertFalse(list(workspace.glob("*.interaction.*")))

    def test_full_focused_and_crossroot_replay(self) -> None:
        """Read durable answers only in the same root; focused never renders history."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                    "response_file": "draft.md",
                    "max_prompt_bytes": 98304,
                },
            ) as (module, helper, request, _, _):
                frame, _ = self.invoke(module, request, stdout=ANSWER)
                self.assertEqual(frame["status"], "success")
                request["input"].update(
                    gate_decision=GATE, feedback_context_mode="focused"
                )
                with mock.patch.object(
                    helper,
                    "render_history",
                    side_effect=AssertionError("full history read"),
                ):
                    composed, _, _ = helper.compose(
                        vars(module), request, request["input"], "task"
                    )
                self.assertIn(ANSWER, composed)
                self.assertIn("# Operator feedback", composed)
                self.assertNotIn("# Prior interaction history", composed)
                request["input"]["feedback_context_mode"] = "full"
                composed, _, _ = helper.compose(
                    vars(module), request, request["input"], "task"
                )
                self.assertIn("# Prior interaction history", composed)
                self.assertIn("## Execution 1", composed)
                request["run_id"] = "fresh-root__child"
                composed, _, _ = helper.compose(
                    vars(module), request, request["input"], "task"
                )
                self.assertNotIn(ANSWER, composed)
                self.assertNotIn("# Current draft", composed)
                self.assertNotIn("# Prior interaction history", composed)

    def test_replay_overflow_and_unavailable_history_no_launch(self) -> None:
        """Reject oversized replay and full-feedback scratch replay before dispatch."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                    "response_file": "draft.md",
                },
            ) as (module, helper, request, _, _):
                frame, _ = self.invoke(module, request, stdout=ANSWER)
                self.assertEqual(frame["status"], "success")
                directory = helper.history_dir(vars(module), request, request["input"])
                next(directory.glob("*.md")).write_text("é" * 2048)
                request["input"].update(
                    gate_decision=GATE,
                    feedback_context_mode="full",
                    max_prompt_bytes=2048,
                )
                frame, provider = self.invoke(module, request)
                provider.assert_not_called()
                self.assertIn("replay_response", frame["error"]["message"])
                for path in (
                    "run://draft.md",
                    "runtime://temp/draft.md",
                    "/outside/workspace.md",
                ):
                    request["input"]["response_file"] = path
                    frame, provider = self.invoke(module, request)
                    provider.assert_not_called()
                    self.assertEqual(frame["error"]["code"], "validation_error")

    def test_scratch_history_with_read_only_workspace_is_artifact_only(self) -> None:
        """Scratch responses cause no replay reads or workspace writes."""
        for plugin_id in IDS:
            for feedback_mode in (None, "full", "focused", "feedback-only"):
                with self.subTest(plugin=plugin_id, mode=feedback_mode), self.fixture(
                    plugin_id,
                    {
                        "prompt": "scratch canary task",
                        "output_mode": "compact",
                        "history_mode": "invocation",
                        "response_file": "run://canary-response.md",
                        "max_prompt_bytes": 98304,
                        "max_context_file_bytes": 65536,
                    },
                ) as (module, helper, request, workspace, staging):
                    artifact_dir = staging / "artifacts"
                    artifact_dir.mkdir()
                    request["paths"]["artifacts_dir"] = str(artifact_dir)
                    request["plugin"] = {"id": plugin_id, "version": "0.1.0"}
                    request["policy"] = {"workspace": {"mode": "read_only"}}
                    request["requested_capabilities"] = [
                        {
                            "capability": "filesystem",
                            "resource": str(workspace),
                            "operations": ["read"],
                        },
                        {
                            "capability": "filesystem",
                            "resource": "run://artifacts",
                            "operations": ["write"],
                        },
                    ]
                    if feedback_mode is not None:
                        request["input"]["feedback_context_mode"] = feedback_mode
                    if feedback_mode in {"focused", "feedback-only"}:
                        request["input"]["gate_decision"] = GATE
                    contract_validator("app/invoke.request.v1.schema.json").validate(
                        request
                    )
                    self.validator(plugin_id, "input").validate(request["input"])
                    self.assertIsNone(
                        helper.history_dir(vars(module), request, request["input"])
                    )
                    (staging / "canary-response.md").write_text(
                        "OBSOLETE_SCRATCH_DRAFT", encoding="utf-8"
                    )
                    original_policy = json.dumps(request["policy"])
                    with staging_only_writes(staging) as writes, ExitStack() as stack:
                        # Deny writes even when host OS permissions allow them.
                        with self.assertRaises(PermissionError):
                            (workspace / "forbidden.md").write_text("must not persist")
                        with self.assertRaises(PermissionError):
                            (workspace / ".transist").mkdir()
                        for name in (
                            "record_paths",
                            "read_record",
                            "render_history",
                            "read_bounded",
                        ):
                            stack.enter_context(
                                mock.patch.object(
                                    helper,
                                    name,
                                    side_effect=AssertionError("scratch history read"),
                                )
                            )
                        frame, provider = self.invoke(module, request)
                    self.assertEqual(frame["status"], "success")
                    provider.assert_called_once()
                    self.validator(plugin_id).validate(frame["output"])
                    self.assertEqual(
                        (staging / "canary-response.md").read_text().strip(), ANSWER
                    )
                    self.assertTrue(writes)
                    self.assertEqual(list(workspace.iterdir()), [])
                    self.assertFalse(list(staging.rglob("*.interaction.*")))
                    self.assertFalse(list(staging.rglob("*.invocations")))
                    self.assertEqual(json.dumps(request["policy"]), original_policy)
                    hints = frame["artifact_hints"]
                    for name in ("stdout.txt", "stderr.txt", "invocation.json"):
                        self.assertEqual(sum(h["name"] == name for h in hints), 1)
                    self.assertFalse(any("interaction." in h["name"] for h in hints))
                    metadata = json.loads(
                        next(
                            h["contents"]
                            for h in hints
                            if h["name"] == "invocation.json"
                        )
                    )
                    self.assertEqual(
                        metadata["artifacts"],
                        {
                            "stdout": "stdout.txt",
                            "stderr": "stderr.txt",
                            "response": "response.txt",
                        },
                    )
                    self.assertEqual(
                        metadata["composed_prompt"].count("scratch canary task"), 1
                    )
                    self.assertNotIn(
                        "OBSOLETE_SCRATCH_DRAFT", metadata["composed_prompt"]
                    )
                    self.assertNotIn("# Current draft", metadata["composed_prompt"])
                    self.assertNotIn(
                        "# Prior interaction history", metadata["composed_prompt"]
                    )
                    self.assertNotIn(str(staging), json.dumps(metadata))

    def test_full_feedback_requires_durable_records_for_scratch_response(self) -> None:
        """Default/full revision replay fails explicitly before launching a provider."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                    "response_file": "run://canary-response.md",
                    "gate_decision": GATE,
                },
            ) as (module, helper, request, workspace, staging):
                with staging_only_writes(staging), mock.patch.object(
                    helper,
                    "record_paths",
                    side_effect=AssertionError("scratch history read"),
                ):
                    frame, provider = self.invoke(module, request)
                provider.assert_not_called()
                self.assertEqual(frame["error"]["code"], "validation_error")
                self.assertIn(
                    "full feedback replay requires durable", frame["error"]["message"]
                )
                self.assertEqual(list(workspace.iterdir()), [])
                self.assertFalse((staging / "canary-response.md").exists())

    def test_scratch_response_rejects_staging_escapes_and_unsupported_runtime_uri(
        self,
    ) -> None:
        """Metadata-only history cannot authorize traversal or symlink escape writes."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "history_mode": "invocation",
                    "output_mode": "compact",
                },
            ) as (module, _, request, workspace, staging):
                (staging / "alias").symlink_to(workspace, target_is_directory=True)
                for target in (
                    "run://../workspace/escape.md",
                    "run://alias/escape.md",
                    "run://",
                    "runtime://temp/reply.md",
                ):
                    request["input"]["response_file"] = target
                    with staging_only_writes(staging):
                        frame, provider = self.invoke(module, request)
                    provider.assert_not_called()
                    self.assertEqual(frame["error"]["code"], "validation_error")
                    self.assertEqual(list(workspace.iterdir()), [])

    def test_oversized_diagnostics_preserve_success_with_utf8_byte_manifest(
        self,
    ) -> None:
        """Log previews report retained/full bytes without claiming full logs."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                },
            ) as (module, helper, request, _, _):
                log = "é" * (helper.HINT_BYTES // 2 + 3)
                frame, _ = self.invoke(module, request, stdout=ANSWER, stderr=log)
                self.assertEqual(frame["status"], "success")
                self.assertTrue(frame["output"]["ok"])
                self.assertNotIn("error", frame)
                self.validator(plugin_id).validate(frame["output"])
                manifest = next(
                    item
                    for item in frame["output"]["diagnostics"]["artifacts"]
                    if item["artifact"] == "stderr.txt"
                )
                self.assertEqual(manifest["total_bytes"], len(log.encode()))
                self.assertLessEqual(manifest["retained_bytes"], helper.HINT_BYTES)
                self.assertEqual(manifest["retention"], "diagnostic_preview")
                self.assertFalse(manifest["full_log_available"])
                self.assertLessEqual(
                    len(json.dumps(frame).encode()) + 1, helper.RESULT_BYTES
                )
                self.assertLessEqual(len(frame["artifact_hints"]), helper.HINT_COUNT)
                self.assertLessEqual(
                    sum(len(h["contents"].encode()) for h in frame["artifact_hints"]),
                    helper.FAMILY_BYTES,
                )
                records = helper.record_paths(
                    helper.history_dir(vars(module), request, request["input"])
                )
                self.assertEqual(
                    json.loads(records[0].read_text())["status"], "success"
                )

    def test_full_review_report_is_authoritative_in_outputs_handoffs_and_replay(
        self,
    ) -> None:
        """Markdown headings inside a final answer cannot discard review findings."""
        report = (
            "# Review report\n\n## Findings\n\nP1: keep this finding.\n\n"
            "## Review Response\n\nAddressed.\n\n# Problem\n\nKeep this too."
        )
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "review",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                    "response_file": "review.md",
                },
            ) as (module, helper, request, workspace, _):
                frame, _ = self.invoke(module, request, answer=report)
                self.assertEqual(frame["status"], "success")
                self.validator(plugin_id).validate(frame["output"])
                self.assertEqual(frame["output"]["response_text"], report)
                self.assertEqual((workspace / "review.md").read_text().strip(), report)
                record = helper.record_paths(
                    helper.history_dir(vars(module), request, request["input"])
                )[0]
                self.assertEqual(
                    helper.read_record(record, request["input"])["response_text"],
                    report,
                )

    def test_diagnostic_overflow_metadata_outcome_and_preview_label(self) -> None:
        """Metadata matches the returned outcome and standalone previews are labeled."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                },
            ) as (module, helper, request, _, _):
                frame, _ = self.invoke(
                    module, request, stderr="x" * (helper.HINT_BYTES + 1)
                )
                metadata = json.loads(
                    next(
                        h["contents"]
                        for h in frame["artifact_hints"]
                        if h["name"] == "invocation.json"
                    )
                )
                self.assertEqual(metadata["status"], frame["status"])
                self.assertIn(
                    "diagnostic preview",
                    next(
                        h["contents"]
                        for h in frame["artifact_hints"]
                        if h["name"] == "stderr.txt"
                    ),
                )

    def test_copilot_missing_answer_event_never_promotes_narration(self) -> None:
        """Copilot JSON mode requires a completed answer, not tool output/deltas."""
        with self.fixture(
            "github.copilot.cli", {"prompt": "task", "output_mode": "compact"}
        ) as (module, _, request, _, _):
            for log in (
                "RAW_LOG_SENTINEL",
                json.dumps(
                    {
                        "type": "assistant.message_delta",
                        "data": {"deltaContent": "RAW_LOG_SENTINEL"},
                    }
                ),
            ):
                with mock.patch.object(
                    module,
                    "run_command",
                    return_value=subprocess.CompletedProcess([], 0, log, ""),
                ):
                    frame = module.handle_request(request)
                self.assertEqual(frame["error"]["code"], "protocol_error")
                self.assertEqual(frame["output"]["response_text"], "")

    def test_copilot_help_capability_guard_precedes_task_and_keeps_legacy(self) -> None:
        """Only advertised JSON/stream choices permit a task; legacy skips the probe."""
        supported = (
            '--output-format <format> (choices: "text", "json")\n'
            '--stream <mode> (choices: "on", "off")\n'
        )
        for help_text, help_code, allowed in (
            (supported, 0, True),
            (supported.replace('"json"', '"xml"'), 0, False),
            (supported.replace('"on"', '"auto"'), 0, False),
            (supported, 1, False),
            (
                "--output-format <format> text\n--other-json <flag> json\n"
                "--stream <mode> on\n",
                0,
                False,
            ),
        ):
            with self.subTest(help_code=help_code, allowed=allowed), self.fixture(
                "github.copilot.cli",
                {"prompt": "TASK_SENTINEL", "output_mode": "compact"},
                help_fixture=False,
            ) as (module, _, request, _, _):
                with mock.patch.object(
                    module,
                    "run_process",
                    return_value=subprocess.CompletedProcess(
                        [], help_code, help_text, ""
                    ),
                ) as probe:
                    frame, task = self.invoke(module, request)
                probe.assert_called_once()
                self.assertEqual(
                    probe.call_args.args[0], ["gh", "copilot", "--", "--help"]
                )
                self.assertNotIn("TASK_SENTINEL", json.dumps(probe.call_args.args))
                self.assertLessEqual(probe.call_args.kwargs["timeout"], 30)
                if allowed:
                    task.assert_called_once()
                    self.assertEqual(frame["status"], "success")
                    command = task.call_args.args[1]
                    self.assertEqual(
                        command[command.index("--output-format") + 1], "json"
                    )
                    for name in ("COPILOT_CONFIG_DIR", "COPILOT_HOME", "TMPDIR"):
                        self.assertEqual(
                            probe.call_args.kwargs["env"][name],
                            task.call_args.kwargs["env_overrides"][name],
                        )
                else:
                    task.assert_not_called()
                    self.assertEqual(frame["error"]["code"], "configuration_error")
        with self.fixture("github.copilot.cli", help_fixture=False) as (
            module,
            _,
            request,
            _,
            _,
        ):
            with mock.patch.object(module, "run_process") as probe:
                frame, task = self.invoke(module, request)
            probe.assert_not_called()
            task.assert_called_once()
            self.assertEqual(frame["status"], "success")

    def test_copilot_help_timeout_never_launches_task(self) -> None:
        """A stalled local transport probe reports configuration failure pre-task."""
        with self.fixture(
            "github.copilot.cli",
            {"prompt": "task", "output_mode": "compact"},
            help_fixture=False,
        ) as (module, _, request, _, _):
            with mock.patch.object(
                module, "run_process", side_effect=subprocess.TimeoutExpired([], 30)
            ):
                frame, task = self.invoke(module, request)
            task.assert_not_called()
            self.assertEqual(frame["error"]["code"], "configuration_error")
            self.assertIn("help probe timed out", frame["error"]["message"])

    def test_authoritative_hint_oversize_still_fails_without_shortening_answer(
        self,
    ) -> None:
        """The log exception cannot hide an oversized required answer artifact."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                },
            ) as (module, helper, request, _, _):
                answer = "x" * (helper.HINT_BYTES + 1)
                frame, _ = self.invoke(module, request, answer=answer)
                self.assertEqual(frame["status"], "failed")
                self.assertEqual(frame["error"]["code"], "protocol_error")
                if frame["output"]:
                    self.validator(plugin_id).validate(frame["output"])
                    self.assertFalse(frame["output"]["ok"])
                    self.assertEqual(frame["output"]["response_text"], answer)
                for hint in frame["artifact_hints"]:
                    if hint["name"] == "invocation.json":
                        self.assertEqual(
                            json.loads(hint["contents"])["status"], "failed"
                        )

    def test_copilot_final_completed_message_wins_and_tool_prose_is_not_final(
        self,
    ) -> None:
        """Completed messages supersede answers; pending tools clear them."""
        with self.fixture("github.copilot.cli") as (module, helper, _, _, _):

            def event(content: str, tools: list | None = None) -> str:
                """Serialize one completed Copilot message fixture."""
                return (
                    json.dumps(
                        {
                            "type": "assistant.message",
                            "data": {"content": content, "toolRequests": tools or []},
                        }
                    )
                    + "\n"
                )

            self.assertEqual(
                helper.partial_response(vars(module), event("old") + event(ANSWER)),
                ANSWER,
            )
            self.assertEqual(
                helper.partial_response(
                    vars(module), event("old") + event("tool narration", [{}])
                ),
                "",
            )

    def test_handoff_names_cannot_overwrite_log_or_metadata_artifacts(self) -> None:
        """Opt-in handoffs cannot alias runtime diagnostic artifact names."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "response_file": "stdout.txt",
                },
            ) as (module, _, request, _, _):
                frame, provider = self.invoke(module, request)
                provider.assert_not_called()
                self.assertEqual(frame["error"]["code"], "validation_error")

    def test_metadata_limit_failure_still_reports_final_failed_outcome(self) -> None:
        """A late family overflow never retains an invocation claiming success."""
        with self.fixture(
            "codex.cli",
            {"prompt": "task", "output_mode": "compact", "history_mode": "invocation"},
        ) as (module, helper, request, _, _):
            with mock.patch.object(helper, "FAMILY_BYTES", 700):
                frame, _ = self.invoke(module, request)
            self.assertEqual(frame["status"], "failed")
            for hint in frame["artifact_hints"]:
                if hint["name"] == "invocation.json":
                    self.assertEqual(json.loads(hint["contents"])["status"], "failed")

    def test_authoritative_json_and_escaping_envelope_are_never_silently_truncated(
        self,
    ) -> None:
        """Overflow is an APP failure rather than a shortened apparent JSON answer."""
        for plugin_id in IDS:
            with self.subTest(plugin=plugin_id), self.fixture(
                plugin_id,
                {
                    "prompt": "task",
                    "output_mode": "compact",
                    "history_mode": "invocation",
                },
            ) as (module, helper, request, _, _):
                answer = json.dumps({"answer": "🙂" * 100_000}, ensure_ascii=False)
                base = {
                    "ok": True,
                    "response_text": answer,
                    "response_file": "",
                    "stdout": "",
                    "stderr": "",
                    "exit_code": 0,
                }
                if plugin_id != "codex.cli":
                    base.update(
                        canonical_response_text=answer,
                        feedback="",
                        summary_title="",
                        summary_title_file="",
                        pr_body_file="",
                    )
                    if plugin_id != "github.copilot.cli":
                        base.update(session_id="", share_url="")
                source = module.invoke_result(
                    request["invoke_id"], "success", output=base
                )
                with mock.patch.object(module, "_handle_request", return_value=source):
                    frame = module.handle_request(request)
                self.assertEqual(frame["status"], "failed")
                self.assertEqual(frame["output"], {})
                self.assertIn("not truncated", frame["error"]["message"])
                self.assertLessEqual(
                    helper.size(json.dumps(frame) + "\n"), helper.RESULT_BYTES
                )
                manifest = json.loads(frame["artifact_hints"][0]["contents"])
                self.assertGreater(manifest["result_bytes"], helper.RESULT_BYTES)


if __name__ == "__main__":
    unittest.main()
