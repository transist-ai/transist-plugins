#!/usr/bin/env python3
import datetime
import io
import json
import os
import pathlib
import sys
import tempfile
from unittest import mock
from runner_fixture import fixture_runner

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))

from support import (  # noqa: E402
    app_invoke_request,
    assert_app_frames,
    assert_canonical_error,
    completed_process,
    load_plugin_module,
    read_json,
    run_process_plugin_main,
    validate_json_schema,
)


def write_copilot_session(
    session_state_dir,
    session_id,
    cwd,
    model,
    input_tokens,
    output_tokens,
    reasoning_tokens=0,
    cache_read_tokens=0,
    cache_write_tokens=0,
):
    session_dir = pathlib.Path(session_state_dir) / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    start_time = datetime.datetime.now(datetime.timezone.utc)
    shutdown_time = start_time + datetime.timedelta(seconds=1)
    events = [
        {
            "type": "session.start",
            "timestamp": start_time.isoformat().replace("+00:00", "Z"),
            "data": {
                "sessionId": session_id,
                "startTime": start_time.isoformat().replace("+00:00", "Z"),
                "context": {"cwd": cwd, "gitRoot": cwd},
            },
        },
        {
            "type": "session.shutdown",
            "timestamp": shutdown_time.isoformat().replace("+00:00", "Z"),
            "data": {
                "currentModel": model,
                "modelMetrics": {
                    model: {
                        "usage": {
                            "inputTokens": input_tokens,
                            "outputTokens": output_tokens,
                            "reasoningTokens": reasoning_tokens,
                            "cacheReadTokens": cache_read_tokens,
                            "cacheWriteTokens": cache_write_tokens,
                        }
                    }
                },
            },
        },
    ]
    events_path = session_dir / "events.jsonl"
    events_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def main():
    for required in ["plugin.yaml", "plugin.py", "workflow.yaml", "README.md"]:
        if not (ROOT / required).exists():
            raise AssertionError(f"missing file: {required}")
    output_schema = read_json(ROOT / "schemas/exec_task.output.schema.json")
    input_schema = read_json(ROOT / "schemas/exec_task.input.schema.json")
    module = load_plugin_module(ROOT)
    for payload in (
        {},
        {
            "gate_decision": {
                "schema": "transist.gate_decision.v1",
                "decision": "approved",
            }
        },
        {
            "gate_decision": {
                "schema": "transist.gate_decision.v1",
                "decision": "request_changes",
            }
        },
        {"gate_decision": {"schema": "transist.gate_decision.v1", "decision": []}},
        {"gate_decision": {"schema": "invalid", "decision": "request_changes"}},
        {"gate_decision": "malformed"},
    ):
        if module.gate_decision_feedback(payload):
            raise AssertionError("non-revision gate decisions must not create feedback")
    if module.root_run_id({"run_id": "root-run__nested-child"}) != "root-run":
        raise AssertionError(
            "interaction identity must derive root lineage from request.run_id"
        )
    try:
        validate_json_schema(
            input_schema["properties"]["gate_decision"],
            {"schema": "transist.gate_decision.v1", "decision": "request_changes"},
            schema_path=ROOT / "schemas/exec_task.input.schema.json",
        )
    except AssertionError:
        pass
    else:
        raise AssertionError("schema must reject incomplete request-change decisions")
    runtime_root = pathlib.Path(tempfile.mkdtemp(prefix="github-copilot-runtime-"))
    runtime_env = {
        "COPILOT_HOME": str(runtime_root / "copilot"),
        "COPILOT_CONFIG_DIR": str(runtime_root / "copilot"),
        "COPILOT_SESSION_STATE_DIR": str(runtime_root / "copilot" / "session-state"),
        "XDG_CACHE_HOME": str(runtime_root / "copilot" / "cache"),
        "TMPDIR": str(runtime_root / "copilot" / "tmp"),
    }
    for value in runtime_env.values():
        pathlib.Path(value).mkdir(parents=True, exist_ok=True)
    os.environ.update(runtime_env)
    manifest_text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    for declaration in (
        "COPILOT_HOME: runtime://temp/copilot",
        "COPILOT_CONFIG_DIR: runtime://temp/copilot",
        "COPILOT_SESSION_STATE_DIR: runtime://temp/copilot/session-state",
        "XDG_CACHE_HOME: runtime://temp/copilot/cache",
        "TMPDIR: runtime://temp/copilot/tmp",
    ):
        if declaration not in manifest_text:
            raise AssertionError(f"missing runtime declaration: {declaration}")
    with tempfile.TemporaryDirectory(prefix="github-copilot-run-path-") as tmpdir:
        run_request = {
            "paths": {
                "workspace_dir": tmpdir,
                "run_root": str(
                    pathlib.Path(tmpdir)
                    / "runs"
                    / "run_demo"
                    / "contained_runs"
                    / "run_demo__plan_loop"
                ),
            }
        }
        run_path = module.workspace_path(
            run_request,
            "run://demo_plan_delivery/DEMO-229/plan.md",
        )
        expected_run_path = (
            pathlib.Path(tmpdir)
            / "runs"
            / "run_demo"
            / "demo_plan_delivery"
            / "DEMO-229"
            / "plan.md"
        )
        if pathlib.Path(run_path) != expected_run_path:
            raise AssertionError(
                f"unexpected contained-run run:// path resolution: {run_path}"
            )
    invoke_request = app_invoke_request(
        "exec_task",
        {
            "prompt": "Write a concise change summary title on the first line.",
            "response_file": ".transist/tmp/copilot-response.md",
            "summary_title_file": ".transist/tmp/copilot-title.txt",
            "pr_body_file": ".transist/tmp/copilot-pr-body.md",
            "gate_decision": {
                "schema": "transist.gate_decision.v1",
                "run_id": "demo",
                "gate_id": "plan-review",
                "step_id": "review",
                "decision": "request_changes",
                "reason": "redo: focus the plan",
                "decided_at_ms": 1,
            },
            "feedback_context_mode": "focused",
            "denied_tools": ["shell(git commit:*)", "shell(git push:*)"],
            "timeout_ms": 42000,
        },
    )
    workspace_dir = pathlib.Path(
        tempfile.mkdtemp(prefix="transist-github-copilot-smoke-")
    )
    invoke_request["paths"]["workspace_dir"] = str(workspace_dir)
    (workspace_dir / ".transist" / "tmp").mkdir(parents=True, exist_ok=True)
    for artifact_name in [
        "copilot-response.md",
        "copilot-title.txt",
        "copilot-pr-body.md",
        "copilot-response.interaction.json",
        "copilot-response.interaction.md",
    ]:
        artifact_path = workspace_dir / ".transist" / "tmp" / artifact_name
        artifact_path.unlink(missing_ok=True)
    (workspace_dir / ".transist" / "tmp" / "copilot-response.md").write_text(
        "Existing draft body\n",
        encoding="utf-8",
    )
    success_process = completed_process(
        ["gh", "copilot"],
        returncode=0,
        stdout=(
            "Good — the implementation is already in the working tree.\n\n"
            "Let me verify code quality.\n\n"
            "---\n\n"
            "Short title\n\n"
            "## What Changed\n\n"
            "Body text\n"
        ),
        stderr="",
    )
    popen_calls = []

    class FakePopen:
        def __init__(self, command, **kwargs):
            popen_calls.append((command, kwargs))
            self.command = command
            self.stdout = io.StringIO(success_process.stdout)
            self.stderr = io.StringIO(success_process.stderr)
            self._returncode = success_process.returncode

        def wait(self, timeout=None):
            return self._returncode

        def kill(self):
            self._returncode = -9

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        invoke_request,
        [
            mock.patch.object(
                module, "run_process", side_effect=fixture_runner(FakePopen)
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("offline smoke path should not fail the plugin process")
    _, result = assert_app_frames(frames, "github.copilot.cli")
    if result["status"] != "success":
        raise AssertionError("expected success result for mocked Copilot invocation")
    if not pathlib.Path(runtime_env["COPILOT_CONFIG_DIR"]).exists():
        raise AssertionError(
            "plugin must not delete the runtime-owned Copilot config directory"
        )
    validate_json_schema(output_schema, result["output"])
    command = popen_calls[0][0]
    if command[:3] != ["gh", "copilot", "--"]:
        raise AssertionError("expected gh copilot wrapper invocation")
    if "--allow-all-tools" not in command or "--allow-all-paths" not in command:
        raise AssertionError("expected explicit Copilot tool and path permission flags")
    if "--allow-all" in command:
        raise AssertionError(
            "--allow-all must be absent because it broadens Copilot URL permissions"
        )
    if "--no-ask-user" not in command:
        raise AssertionError("expected non-interactive Copilot permission flag")
    if "--silent" in command:
        raise AssertionError(
            "--silent must be absent so gh copilot stays in the wrapper's streamed text"
            " shape"
        )
    if "--output-format" not in command or "--stream" not in command:
        raise AssertionError("expected deterministic output flags")
    stream_index = command.index("--stream")
    if command[stream_index + 1] != "on":
        raise AssertionError(
            "expected streamed stdout so Transist can drive live progress"
        )
    if "--deny-tool" not in command:
        raise AssertionError("expected denied tool flags to be passed through")
    deny_index = command.index("--deny-tool")
    if command[deny_index + 1] != "shell(git commit:*),shell(git push:*)":
        raise AssertionError("expected denied tool list to be comma-joined")
    if not callable(popen_calls[0][1]["on_line"]):
        raise AssertionError("expected caller-thread stream callback")
    copilot_env = popen_calls[0][1]["env"]
    copilot_config_dir = pathlib.Path(copilot_env["COPILOT_CONFIG_DIR"])
    if (
        copilot_config_dir == workspace_dir
        or workspace_dir in copilot_config_dir.parents
    ):
        raise AssertionError(
            "expected the Copilot runtime directory outside the workspace"
        )
    if pathlib.Path(copilot_env["XDG_CACHE_HOME"]) != pathlib.Path(
        runtime_env["XDG_CACHE_HOME"]
    ):
        raise AssertionError(
            "expected XDG_CACHE_HOME beneath the private Copilot runtime directory"
        )
    if copilot_env["TMPDIR"] != runtime_env["TMPDIR"]:
        raise AssertionError(
            "expected Copilot TMPDIR to use the declared private directory"
        )
    if "# Current draft: copilot-response.md" not in command[4]:
        raise AssertionError(
            "expected existing response_file to be injected when feedback exists"
        )
    if "# Prior Copilot interaction history" in command[4]:
        raise AssertionError(
            "first revision execution should not inject a nonexistent history yet"
        )
    progress_frames = [
        frame for frame in frames if frame["type"] == "app.invoke.progress.v1"
    ]
    if len(progress_frames) < 5:
        raise AssertionError(
            "expected synthetic status updates plus streamed output, got"
            f" {len(progress_frames)}"
        )
    if (
        progress_frames[0].get("line_preview")
        != "Prepared Copilot task from 2 context item(s)."
    ):
        raise AssertionError("expected initial wrapper status preview")
    if "Using repository context:" not in progress_frames[1].get("line_preview", ""):
        raise AssertionError("expected repository-context status preview")
    if progress_frames[2].get("line_preview") != "Launching gh copilot...":
        raise AssertionError("expected launch status preview")
    if progress_frames[3].get("line_preview") != "Waiting for Copilot output...":
        raise AssertionError("expected waiting status preview before streamed output")
    if (
        progress_frames[4].get("line_preview")
        != "Good — the implementation is already in the working tree."
    ):
        raise AssertionError(
            "expected stdout preview text after wrapper status previews"
        )
    response_root = workspace_dir / ".transist" / "tmp"
    response_path = response_root / "copilot-response.md"
    title_path = response_root / "copilot-title.txt"
    pr_body_path = response_root / "copilot-pr-body.md"
    history_json_path = response_root / "copilot-response.interaction.json"
    history_markdown_path = response_root / "copilot-response.interaction.md"
    if (
        response_path.read_text(encoding="utf-8").strip()
        != "Short title\n\n## What Changed\n\nBody text"
    ):
        raise AssertionError(
            "expected response_file to keep only the canonical markdown artifact"
        )
    if title_path.read_text(encoding="utf-8").strip() != "Short title":
        raise AssertionError(
            "expected summary_title_file to use the canonical title instead of chatter"
        )
    if (
        pr_body_path.read_text(encoding="utf-8").strip()
        != "## What Changed\n\nBody text"
    ):
        raise AssertionError(
            "expected pr_body_file to keep the canonical PR body from ## What Changed"
            " onward"
        )
    if not history_json_path.exists():
        raise AssertionError("expected interaction history json sidecar")
    if not history_markdown_path.exists():
        raise AssertionError("expected interaction history markdown sidecar")
    history_entries = json.loads(history_json_path.read_text(encoding="utf-8"))
    if len(history_entries) != 1:
        raise AssertionError(
            "expected first successful run to create one history entry"
        )
    if (
        history_entries[0].get("response_text")
        != "Good — the implementation is already in the working tree.\n\n"
        "Let me verify code quality.\n\n"
        "---\n\n"
        "Short title\n\n"
        "## What Changed\n\n"
        "Body text"
    ):
        raise AssertionError(
            "expected history entry to persist the previous Copilot response"
        )
    if "# Copilot interaction history" not in history_markdown_path.read_text(
        encoding="utf-8"
    ):
        raise AssertionError("expected readable markdown interaction history sidecar")
    if "usage" in result:
        raise AssertionError(
            "expected usage to be None when mocked stdout and session state lack token"
            " data"
        )

    usage_workspace_dir = pathlib.Path(
        tempfile.mkdtemp(prefix="transist-github-copilot-usage-")
    )
    usage_request = app_invoke_request(
        "exec_task",
        {
            "prompt": "Say hello.",
            "timeout_ms": 42000,
        },
    )
    usage_request["paths"]["workspace_dir"] = str(usage_workspace_dir)
    session_state_dir = pathlib.Path(
        tempfile.mkdtemp(prefix="transist-github-copilot-session-state-")
    )
    popen_calls.clear()

    class UsagePopen:
        def __init__(self, command, **kwargs):
            popen_calls.append((command, kwargs))
            self.command = command
            self.stdout = io.StringIO("Usage-backed reply.\n")
            self.stderr = io.StringIO("")
            self._returncode = 0
            write_copilot_session(
                session_state_dir,
                "copilot-usage-session",
                kwargs["cwd"],
                model="claude-sonnet-4.5",
                input_tokens=120,
                output_tokens=34,
                reasoning_tokens=8,
                cache_read_tokens=56,
                cache_write_tokens=7,
            )

        def wait(self, timeout=None):
            return self._returncode

        def kill(self):
            self._returncode = -9

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        usage_request,
        [
            mock.patch.object(
                module, "run_process", side_effect=fixture_runner(UsagePopen)
            ),
            mock.patch.dict(
                module.os.environ,
                {"COPILOT_SESSION_STATE_DIR": str(session_state_dir)},
                clear=False,
            ),
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "session-state usage smoke path should not fail the plugin process"
        )
    _, usage_result = assert_app_frames(frames, "github.copilot.cli")
    if usage_result["status"] != "success":
        raise AssertionError(
            "expected success result for session-state usage extraction"
        )
    if usage_result["usage"] != {
        "model": "claude-sonnet-4.5",
        "input_tokens": 120,
        "output_tokens": 42,
        "total_tokens": 162,
    }:
        raise AssertionError(
            f"unexpected session-state usage payload: {usage_result['usage']}"
        )
    usage_hint = next(
        hint for hint in usage_result["artifact_hints"] if hint["name"] == "usage.json"
    )
    if json.loads(usage_hint["contents"]) != usage_result["usage"] | {
        "reasoning_tokens": 8,
        "cache_read_tokens": 56,
        "cache_write_tokens": 7,
    }:
        raise AssertionError("usage.json must preserve provider-specific token details")

    invoke_request["input"]["gate_decision"] = {
        "schema": "transist.gate_decision.v1",
        "run_id": "demo",
        "gate_id": "plan-review",
        "step_id": "review",
        "decision": "request_changes",
        "reason": "tighten the plan from history",
        "decided_at_ms": 2,
    }

    follow_up_process = completed_process(
        ["gh", "copilot"],
        returncode=0,
        stdout="Revised title\n\n## What Changed\n\nRevised body\n",
        stderr="",
    )
    popen_calls.clear()

    class FollowUpPopen:
        def __init__(self, command, **kwargs):
            popen_calls.append((command, kwargs))
            self.command = command
            self.stdout = io.StringIO(follow_up_process.stdout)
            self.stderr = io.StringIO(follow_up_process.stderr)
            self._returncode = follow_up_process.returncode

        def wait(self, timeout=None):
            return self._returncode

        def kill(self):
            self._returncode = -9

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        invoke_request,
        [
            mock.patch.object(
                module, "run_process", side_effect=fixture_runner(FollowUpPopen)
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "follow-up revision smoke path should not fail the plugin process"
        )
    _, second_result = assert_app_frames(frames, "github.copilot.cli")
    if second_result["status"] != "success":
        raise AssertionError(
            "expected success result for mocked follow-up Copilot invocation"
        )
    follow_up_command = popen_calls[0][0]
    if "# Prior Copilot interaction history" in follow_up_command[4]:
        raise AssertionError(
            "focused feedback mode should omit prior interaction history on revisions"
        )
    if "# Operator feedback" not in follow_up_command[4]:
        raise AssertionError(
            "expected history-backed operator feedback in follow-up prompt"
        )
    if "tighten the plan from history" not in follow_up_command[4]:
        raise AssertionError(
            "expected latest history feedback text in follow-up prompt"
        )
    history_entries = json.loads(history_json_path.read_text(encoding="utf-8"))
    if len(history_entries) != 2:
        raise AssertionError(
            "expected follow-up revision to append another history entry"
        )
    if title_path.read_text(encoding="utf-8").strip() != "Revised title":
        raise AssertionError(
            "expected canonical revised title to replace the previous commit summary"
            " title"
        )
    if (
        pr_body_path.read_text(encoding="utf-8").strip()
        != "## What Changed\n\nRevised body"
    ):
        raise AssertionError(
            "expected pr_body_file to contain the revised canonical PR body"
        )
    history_markdown = history_markdown_path.read_text(encoding="utf-8")
    if "## Execution 2" not in history_markdown:
        raise AssertionError(
            "expected markdown sidecar to include the second execution"
        )

    full_mode_request = json.loads(json.dumps(invoke_request))
    full_mode_request["input"]["feedback_context_mode"] = "full"
    popen_calls.clear()

    class FullModePopen:
        def __init__(self, command, **kwargs):
            popen_calls.append((command, kwargs))
            self.command = command
            self.stdout = io.StringIO(follow_up_process.stdout)
            self.stderr = io.StringIO(follow_up_process.stderr)
            self._returncode = follow_up_process.returncode

        def wait(self, timeout=None):
            return self._returncode

        def kill(self):
            self._returncode = -9

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        full_mode_request,
        [
            mock.patch.object(
                module, "run_process", side_effect=fixture_runner(FullModePopen)
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "full feedback mode smoke path should not fail the plugin process"
        )
    _, full_mode_result = assert_app_frames(frames, "github.copilot.cli")
    if full_mode_result["status"] != "success":
        raise AssertionError(
            "expected success result for full feedback mode invocation"
        )
    full_mode_command = popen_calls[0][0]
    if "# Prior Copilot interaction history" not in full_mode_command[4]:
        raise AssertionError(
            "full feedback mode should inject prior interaction history"
        )
    if (
        "### Copilot response\n\nGood — the implementation is already in the working"
        " tree." not in full_mode_command[4]
    ):
        raise AssertionError(
            "expected previous response text inside the injected interaction history"
        )

    failure_request = app_invoke_request(
        "exec_task",
        {
            "prompt": "Summarize the failure.",
            "timeout_ms": 42000,
        },
    )
    failure_request["paths"]["workspace_dir"] = str(
        pathlib.Path(tempfile.mkdtemp(prefix="transist-github-copilot-failure-"))
    )

    failed_process = completed_process(
        ["gh", "copilot"],
        returncode=23,
        stdout="Partial Copilot response\n",
        stderr="copilot execution failed\n",
    )
    popen_calls.clear()

    class NonZeroPopen:
        def __init__(self, command, **kwargs):
            popen_calls.append((command, kwargs))
            self.command = command
            self.stdout = io.StringIO(failed_process.stdout)
            self.stderr = io.StringIO(failed_process.stderr)
            self._returncode = failed_process.returncode

        def wait(self, timeout=None):
            return self._returncode

        def kill(self):
            self._returncode = -9

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        failure_request,
        [
            mock.patch.object(
                module, "run_process", side_effect=fixture_runner(NonZeroPopen)
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "non-zero tool exit should still produce a normal APP result"
        )
    _, nonzero_result = assert_app_frames(frames, "github.copilot.cli")
    if nonzero_result["status"] != "failed":
        raise AssertionError("expected non-zero Copilot exit to produce failed status")
    assert_canonical_error(
        nonzero_result["error"],
        "tool_execution_error",
        ["command exited with code 23"],
    )
    if nonzero_result["output"]["exit_code"] != 23:
        raise AssertionError("expected non-zero Copilot exit code in output")

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write("This file should conflict with inline prompt.\n")
        invalid_prompt_path = handle.name
    invalid_request = app_invoke_request(
        "exec_task",
        {"prompt": "inline prompt", "prompt_file": invalid_prompt_path},
    )
    exit_code, frames, stderr = run_process_plugin_main(module, invalid_request)
    if exit_code != 0 or stderr:
        raise AssertionError(
            "validation failure should still produce a normal APP result"
        )
    _, validation_result = assert_app_frames(frames, "github.copilot.cli")
    if validation_result["status"] != "failed":
        raise AssertionError(
            "expected conflicting Copilot prompt sources to fail validation"
        )
    assert_canonical_error(
        validation_result["error"],
        "validation_error",
        ["exactly one of prompt or prompt_file is required"],
    )

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        failure_request,
        [mock.patch.object(module, "run_process", side_effect=FileNotFoundError())],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("missing binary should still produce a normal APP result")
    _, missing_result = assert_app_frames(frames, "github.copilot.cli")
    assert_canonical_error(
        missing_result["error"],
        "configuration_error",
        ["gh CLI not found in PATH"],
    )

    popen_calls.clear()

    class TimeoutPopen:
        def __init__(self, command, **kwargs):
            popen_calls.append((command, kwargs))
            self.command = command
            self.stdout = io.StringIO("Waiting on Copilot...\n")
            self.stderr = io.StringIO("still indexing repository\n")
            self._returncode = 0
            self._timed_out = False

        def wait(self, timeout=None):
            if not self._timed_out:
                self._timed_out = True
                raise module.subprocess.TimeoutExpired(self.command, timeout)
            return self._returncode

        def kill(self):
            self._returncode = -9

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        failure_request,
        [
            mock.patch.object(
                module, "run_process", side_effect=fixture_runner(TimeoutPopen)
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("timeout should still produce a normal APP result")
    _, timeout_result = assert_app_frames(frames, "github.copilot.cli")
    assert_canonical_error(
        timeout_result["error"],
        "timeout_error",
        ["37 seconds"],
    )
    if timeout_result["output"]["stdout"] != "Waiting on Copilot...\n":
        raise AssertionError(
            "expected timeout output to preserve partial Copilot stdout"
        )
    if timeout_result["output"]["stderr"] != "still indexing repository\n":
        raise AssertionError(
            "expected timeout output to preserve partial Copilot stderr"
        )
    if timeout_result["output"]["exit_code"] is not None:
        raise AssertionError("expected timeout output to leave exit_code unset")

    model_workspace_dir = pathlib.Path(
        tempfile.mkdtemp(prefix="transist-github-copilot-model-")
    )
    model_request = app_invoke_request(
        "exec_task",
        {
            "prompt": "Say hello.",
            "model": "claude-sonnet-4-5",
            "reasoning_effort": "xhigh",
            "timeout_ms": 42000,
        },
    )
    model_request["paths"]["workspace_dir"] = str(model_workspace_dir)
    popen_calls.clear()
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        model_request,
        [
            mock.patch.object(
                module, "run_process", side_effect=fixture_runner(FakePopen)
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "model+reasoning_effort smoke path should not fail the plugin process"
        )
    _, model_result = assert_app_frames(frames, "github.copilot.cli")
    if model_result["status"] != "success":
        raise AssertionError(
            "expected success result for model+reasoning_effort invocation"
        )
    model_command = popen_calls[0][0]
    if (
        "--model" not in model_command
        or model_command[model_command.index("--model") + 1] != "claude-sonnet-4-5"
    ):
        raise AssertionError("expected --model to be forwarded to gh copilot")
    if (
        "--reasoning-effort" not in model_command
        or model_command[model_command.index("--reasoning-effort") + 1] != "xhigh"
    ):
        raise AssertionError(
            "expected --reasoning-effort xhigh to be forwarded to gh copilot"
        )

    no_controls_workspace_dir = pathlib.Path(
        tempfile.mkdtemp(prefix="transist-github-copilot-nocontrols-")
    )
    no_controls_request = app_invoke_request(
        "exec_task",
        {
            "prompt": "Say hello.",
            "timeout_ms": 42000,
        },
    )
    no_controls_request["paths"]["workspace_dir"] = str(no_controls_workspace_dir)
    # Required-only demo inputs materialize the optional mappings as null.
    no_controls_request["input"].update(model=None, reasoning_effort=None)
    validate_json_schema(
        read_json(ROOT / "schemas/exec_task.input.schema.json"),
        no_controls_request["input"],
    )
    popen_calls.clear()
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        no_controls_request,
        [
            mock.patch.object(
                module, "run_process", side_effect=fixture_runner(FakePopen)
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "omitting model/reasoning_effort should not fail the plugin process"
        )
    _, no_controls_result = assert_app_frames(frames, "github.copilot.cli")
    if no_controls_result["status"] != "success":
        raise AssertionError(
            "expected success result when model/reasoning_effort are omitted"
        )
    no_controls_command = popen_calls[0][0]
    if "--model" in no_controls_command:
        raise AssertionError("--model should not appear in command when not supplied")
    if "--reasoning-effort" in no_controls_command:
        raise AssertionError(
            "--reasoning-effort should not appear in command when not supplied"
        )

    print("github.copilot.cli offline smoke test passed")


if __name__ == "__main__":
    main()
