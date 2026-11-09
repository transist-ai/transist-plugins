#!/usr/bin/env python3
import io
import json
import os
import pathlib
import sys
import tempfile
import time
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))

from support import (  # noqa: E402
    app_invoke_request,
    assert_app_frames,
    assert_canonical_error,
    load_plugin_module,
    read_json,
    run_process_plugin_main,
    validate_json_schema,
)


def cursor_event(event_type, session_id, payload=None, subtype=None):
    result = {"type": event_type, "session_id": session_id}
    if subtype is not None:
        result["subtype"] = subtype
    if payload is not None:
        result.update(payload)
    return json.dumps(result)


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
    runtime_root = pathlib.Path(tempfile.mkdtemp(prefix="cursor-runtime-"))
    runtime_env = {
        "CURSOR_CONFIG_DIR": str(runtime_root / "config"),
        "CURSOR_DATA_DIR": str(runtime_root / "data"),
        "HOME": str(runtime_root / "home"),
        "TMPDIR": str(runtime_root / "tmp"),
    }
    for value in runtime_env.values():
        pathlib.Path(value).mkdir(parents=True, exist_ok=True)
    os.environ.update(runtime_env)
    os.environ["CURSOR_API_KEY"] = "test-cursor-api-key"
    manifest_text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    for declaration in (
        "CURSOR_CONFIG_DIR: runtime://temp/cursor/config",
        "CURSOR_DATA_DIR: runtime://temp/cursor/data",
        "TMPDIR: runtime://temp/cursor/tmp",
    ):
        if declaration not in manifest_text:
            raise AssertionError(f"missing runtime declaration: {declaration}")
    for declaration in (
        "name: CURSOR_API_KEY",
        "required: true",
        "type: env",
    ):
        if declaration not in manifest_text:
            raise AssertionError(f"missing Cursor secret declaration: {declaration}")
    validate_json_schema(
        input_schema,
        {"prompt": "Use Cursor non-interactively.", "secret_refs": ["CURSOR_API_KEY"]},
    )
    validate_json_schema(input_schema, {"prompt": "Use the existing CLI login."})
    if "exec_task_cli" not in manifest_text or module.PLUGIN_VERSION != "0.1.0":
        raise AssertionError("expected versioned CLI-login action")

    artifacts_dir = tempfile.mkdtemp(prefix="cursor-skills-artifacts-")
    workspace_dir = pathlib.Path(tempfile.mkdtemp(prefix="cursor-plugin-workspace-"))
    response_root = workspace_dir / ".transist" / "tmp"
    response_root.mkdir(parents=True, exist_ok=True)
    response_path = response_root / "cursor-response.md"
    title_path = response_root / "cursor-title.txt"
    pr_body_path = response_root / "cursor-pr-body.md"
    history_json_path = response_root / "cursor-response.interaction.json"
    history_markdown_path = response_root / "cursor-response.interaction.md"
    response_path.write_text("Existing draft body\n", encoding="utf-8")

    invoke_request = app_invoke_request(
        "exec_task",
        {
            "prompt": "Write a concise change summary title on the first line.",
            "skills_bundle": {"version": "v1", "roots": []},
            "skills_rendered": "# Workflow Skills\n\nUse the repo skill.\n",
            "context_files": [str(ROOT.parents[1] / "README.md")],
            "gate_decision": {
                "schema": "transist.gate_decision.v1",
                "run_id": "demo",
                "gate_id": "review",
                "step_id": "review",
                "decision": "request_changes",
                "reason": "tighten the final wording",
                "decided_at_ms": 1,
            },
            "feedback_context_mode": "focused",
            "response_file": ".transist/tmp/cursor-response.md",
            "summary_title_file": ".transist/tmp/cursor-title.txt",
            "pr_body_file": ".transist/tmp/cursor-pr-body.md",
            "allowed_tools": ["shell(git status:*)"],
            "denied_tools": ["bash(rm *)"],
            "permission": {"edit": "deny"},
            "model": "composer-2.5",
            "reasoning_effort": "xhigh",
            "agent": "build",
            "session_title": "Transist Cursor Run",
            "share": True,
            "dangerously_skip_permissions": True,
            "approve_mcps": True,
            "sandbox": "disabled",
            "timeout_ms": 42000,
        },
        artifacts_dir=artifacts_dir,
    )
    invoke_request["paths"]["workspace_dir"] = str(workspace_dir)

    stdout_lines = [
        cursor_event(
            "system",
            "ses_cursor123",
            {"subtype": "init", "model": "Composer 2.5", "permissionMode": "default"},
        ),
        cursor_event(
            "tool_call",
            "ses_cursor123",
            {
                "subtype": "started",
                "call_id": "call_1",
                "tool_call": {
                    "shellToolCall": {
                        "args": {
                            "command": "git status",
                            "description": "Check repo status",
                            "toolCallId": "call_1",
                        }
                    }
                },
            },
        ),
        cursor_event(
            "tool_call",
            "ses_cursor123",
            {
                "subtype": "completed",
                "call_id": "call_1",
                "tool_call": {
                    "shellToolCall": {
                        "args": {"command": "git status"},
                        "result": {"success": {"stdout": "On branch main"}},
                    }
                },
            },
        ),
        cursor_event(
            "assistant",
            "ses_cursor123",
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "Short title\n\n## What Changed\n\nBody text",
                        }
                    ],
                }
            },
        ),
        cursor_event(
            "result",
            "ses_cursor123",
            {
                "subtype": "success",
                "is_error": False,
                "result": "Short title\n\n## What Changed\n\nBody text",
                "request_id": "req_cursor123",
                "usage": {
                    "inputTokens": 120,
                    "outputTokens": 42,
                    "cacheReadTokens": 56,
                    "cacheWriteTokens": 7,
                },
            },
        ),
        "",
    ]
    stdout_text = "\n".join(stdout_lines)
    popen_calls = []

    # These existing APP/argument fixtures do not create OS processes. Actual
    # native lifecycle ownership is exercised separately by test_lifecycle.py.
    lifecycle_patch = mock.patch.object(module, "ProcessLifecycle")
    lifecycle_patch.start().return_value.cleanup.return_value = True

    class SuccessPopen:
        def __init__(self, command, **kwargs):
            popen_calls.append({"command": command, "kwargs": kwargs})
            self.command = command
            self.args = command
            self.stdin = None
            self.stdout = io.StringIO(stdout_text)
            self.stderr = io.StringIO("")
            self._returncode = 0

        def wait(self, timeout=None):
            return self._returncode

        def poll(self):
            return self._returncode

        def communicate(self, input=None, timeout=None):
            return (self.stdout.getvalue(), self.stderr.getvalue())

        def kill(self):
            self._returncode = -9

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        invoke_request,
        [mock.patch.object(module.subprocess, "Popen", side_effect=SuccessPopen)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("offline smoke path should not fail the plugin process")
    _, result = assert_app_frames(frames, "cursor.cli")
    if result["status"] != "success":
        raise AssertionError("expected success result for mocked Cursor invocation")
    validate_json_schema(output_schema, result["output"])
    if not (pathlib.Path(artifacts_dir) / "workflow_skills.md").exists():
        raise AssertionError("expected workflow_skills.md artifact")
    if not (pathlib.Path(artifacts_dir) / "workflow_skills.json").exists():
        raise AssertionError("expected workflow_skills.json artifact")
    command = popen_calls[0]["command"]
    kwargs = popen_calls[0]["kwargs"]
    binary_name = command[0]
    if not (
        binary_name == "agent"
        or binary_name.endswith("/agent")
        or binary_name.endswith("\\agent")
        or binary_name.endswith("/cursor-agent")
        or binary_name.endswith("\\cursor-agent")
    ):
        raise AssertionError(f"expected Cursor Agent binary, got {binary_name}")
    if command[1:4] != ["--print", "--output-format", "stream-json"]:
        raise AssertionError(
            f"expected Cursor Agent stream-json invocation, got {command[1:4]}"
        )
    if "--workspace" not in command or command[command.index("--workspace") + 1] != str(
        workspace_dir
    ):
        raise AssertionError(
            "expected the runtime workspace to flow through --workspace"
        )
    if (
        "--model" not in command
        or command[command.index("--model") + 1] != "composer-2.5"
    ):
        raise AssertionError("expected model flag")
    if "--mode" in command:
        raise AssertionError("mode should not be passed by default")
    if "--force" not in command:
        raise AssertionError("expected force flag by default")
    if "--approve-mcps" not in command:
        raise AssertionError("expected approve-mcps flag")
    if (
        "--sandbox" not in command
        or command[command.index("--sandbox") + 1] != "disabled"
    ):
        raise AssertionError("expected sandbox disabled flag")
    if kwargs["cwd"] != str(workspace_dir):
        raise AssertionError("expected the runtime workspace to set the subprocess cwd")
    if kwargs["bufsize"] != 1 or not kwargs["text"]:
        raise AssertionError("expected line-buffered text subprocess mode")
    for name in ("CURSOR_CONFIG_DIR", "CURSOR_DATA_DIR", "TMPDIR"):
        if kwargs["env"][name] != runtime_env[name]:
            raise AssertionError(f"expected declared {name} runtime directory")
    if kwargs["env"]["HOME"] != runtime_env["CURSOR_CONFIG_DIR"]:
        raise AssertionError("expected API-key action to isolate Cursor's home")
    if (
        "CURSOR_CLIENT" not in kwargs["env"]
        or kwargs["env"]["CURSOR_CLIENT"] != "cursor.cli"
    ):
        raise AssertionError("expected CURSOR_CLIENT environment variable")
    if kwargs["env"].get("CURSOR_API_KEY") != "test-cursor-api-key":
        raise AssertionError("expected CURSOR_API_KEY to be forwarded to Cursor Agent")
    prompt_arg = command[-1]
    if "Workflow Skills" not in prompt_arg:
        raise AssertionError("expected workflow skills in the composed Cursor prompt")
    if "# Operator feedback" not in prompt_arg:
        raise AssertionError("expected gate feedback in the composed prompt")
    if "# Current draft: cursor-response.md" not in prompt_arg:
        raise AssertionError("expected current draft to be replayed in focused mode")
    if "# Prior Cursor interaction history" in prompt_arg:
        raise AssertionError(
            "focused mode should omit prior Cursor interaction history"
        )
    if "# Context: README.md" not in prompt_arg:
        raise AssertionError("expected context file contents in the composed prompt")
    if "# Task" not in prompt_arg:
        raise AssertionError("expected task heading in the composed prompt")
    progress_frames = [
        frame for frame in frames if frame["type"] == "app.invoke.progress.v1"
    ]
    if len(progress_frames) < 6:
        raise AssertionError("expected Cursor wrapper progress frames")
    if (
        progress_frames[0].get("line_preview")
        != "Prepared Cursor task from 4 context item(s)."
    ):
        raise AssertionError("expected initial Cursor status preview")
    if "Using repository context:" not in progress_frames[1].get("line_preview", ""):
        raise AssertionError("expected repository context status preview")
    if progress_frames[2].get("line_preview") != "Launching Cursor Agent...":
        raise AssertionError("expected launch status preview")
    if progress_frames[3].get("line_preview") != "Waiting for Cursor Agent output...":
        raise AssertionError("expected waiting status preview")
    if progress_frames[4].get("line_preview") != "Cursor Agent initialized":
        raise AssertionError("expected system-init preview")
    if "Cursor tool: Check repo status" not in progress_frames[5].get(
        "line_preview", ""
    ):
        raise AssertionError("expected tool-call preview")
    if (
        response_path.read_text(encoding="utf-8").strip()
        != "Short title\n\n## What Changed\n\nBody text"
    ):
        raise AssertionError(
            "expected response_file to keep only the canonical markdown artifact"
        )
    if title_path.read_text(encoding="utf-8").strip() != "Short title":
        raise AssertionError("expected summary_title_file to use the canonical title")
    if (
        pr_body_path.read_text(encoding="utf-8").strip()
        != "## What Changed\n\nBody text"
    ):
        raise AssertionError("expected pr_body_file to keep the canonical PR body")
    if not history_json_path.exists() or not history_markdown_path.exists():
        raise AssertionError("expected Cursor interaction history sidecars")
    if "## Execution 1" not in history_markdown_path.read_text(encoding="utf-8"):
        raise AssertionError("expected first Cursor execution in markdown sidecar")
    usage_json_path = pathlib.Path(artifacts_dir) / "usage.json"
    if not usage_json_path.exists():
        raise AssertionError("expected usage.json artifact")
    usage_payload = json.loads(usage_json_path.read_text(encoding="utf-8"))
    if usage_payload != {
        "model": "Composer 2.5",
        "input_tokens": 120,
        "output_tokens": 42,
        "total_tokens": 162,
        "cache_read_tokens": 56,
        "cache_write_tokens": 7,
    }:
        raise AssertionError(f"unexpected usage.json payload: {usage_payload}")
    if result["usage"] != {
        key: value
        for key, value in usage_payload.items()
        if key in {"provider", "model", "input_tokens", "output_tokens", "total_tokens"}
    }:
        raise AssertionError(f"unexpected APP usage payload: {result['usage']}")
    if result["output"]["session_id"] != "ses_cursor123":
        raise AssertionError("expected session_id in output")
    if result["output"]["share_url"] != "":
        raise AssertionError("expected empty share_url for Cursor Agent")

    # The OAuth action delegates to the CLI login without forwarding secrets
    # or provider directory overrides, even when the wrapper has an API key.
    cli_request = json.loads(json.dumps(invoke_request))
    cli_request["action"] = "exec_task_cli"
    popen_calls.clear()
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        cli_request,
        [mock.patch.object(module.subprocess, "Popen", side_effect=SuccessPopen)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("CLI-login path should produce normal APP frames")
    _, cli_result = assert_app_frames(frames, "cursor.cli")
    if cli_result["status"] != "success":
        raise AssertionError("expected mocked CLI-login invocation to succeed")
    cli_env = popen_calls[0]["kwargs"]["env"]
    if any(
        name in cli_env
        for name in ("CURSOR_API_KEY", "CURSOR_CONFIG_DIR", "CURSOR_DATA_DIR", "HOME")
    ):
        raise AssertionError(
            "CLI-login child must not receive API keys or private auth directory"
            " overrides"
        )
    if cli_env["TMPDIR"] != runtime_env["TMPDIR"]:
        raise AssertionError("CLI-login child must retain private temporary storage")
    cli_request["input"]["extra_args"] = ["--api-key=forbidden"]
    _, frames, _ = run_process_plugin_main(module, cli_request)
    _, invalid_cli_result = assert_app_frames(frames, "cursor.cli")
    assert_canonical_error(
        invalid_cli_result["error"], "validation_error", ["authentication arguments"]
    )

    invoke_request["input"]["gate_decision"] = {
        "schema": "transist.gate_decision.v1",
        "run_id": "demo",
        "gate_id": "review",
        "step_id": "review",
        "decision": "request_changes",
        "reason": "rewrite the draft from history",
        "decided_at_ms": 2,
    }

    follow_up_stdout = "\n".join(
        [
            cursor_event(
                "assistant",
                "ses_cursor123",
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Revised title\n\n## What Changed\n\nRevised body"
                                ),
                            }
                        ],
                    }
                },
            ),
            cursor_event(
                "result",
                "ses_cursor123",
                {
                    "subtype": "success",
                    "is_error": False,
                    "result": "Revised title\n\n## What Changed\n\nRevised body",
                },
            ),
            "",
        ]
    )
    popen_calls.clear()

    class FollowUpPopen(SuccessPopen):
        def __init__(self, command, **kwargs):
            super().__init__(command, **kwargs)
            self.stdout = io.StringIO(follow_up_stdout)
            self.stderr = io.StringIO("")
            self._returncode = 0

    follow_up_request = json.loads(json.dumps(invoke_request))
    follow_up_request["input"]["dangerously_skip_permissions"] = False
    follow_up_request["input"]["force"] = False
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        follow_up_request,
        [mock.patch.object(module.subprocess, "Popen", side_effect=FollowUpPopen)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "follow-up revision path should not fail the plugin process"
        )
    _, follow_up_result = assert_app_frames(frames, "cursor.cli")
    if follow_up_result["status"] != "success":
        raise AssertionError("expected success result for follow-up Cursor invocation")
    follow_up_command = popen_calls[0]["command"]
    if "--force" in follow_up_command:
        raise AssertionError("expected force flag to be omitted when force is false")
    follow_up_prompt = follow_up_command[-1]
    if "# Prior Cursor interaction history" in follow_up_prompt:
        raise AssertionError(
            "focused mode should still omit prior Cursor interaction history"
        )
    if "rewrite the draft from history" not in follow_up_prompt:
        raise AssertionError("expected latest history-backed operator feedback")
    if title_path.read_text(encoding="utf-8").strip() != "Revised title":
        raise AssertionError("expected revised title file contents")
    if (
        pr_body_path.read_text(encoding="utf-8").strip()
        != "## What Changed\n\nRevised body"
    ):
        raise AssertionError("expected revised PR body")

    full_mode_request = json.loads(json.dumps(invoke_request))
    full_mode_request["input"]["feedback_context_mode"] = "full"
    full_mode_request["input"]["force"] = True
    full_mode_request["input"]["mode"] = "ask"
    popen_calls.clear()
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        full_mode_request,
        [mock.patch.object(module.subprocess, "Popen", side_effect=FollowUpPopen)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("full feedback mode should not fail the plugin process")
    _, full_mode_result = assert_app_frames(frames, "cursor.cli")
    if full_mode_result["status"] != "success":
        raise AssertionError("expected success result for full feedback mode")
    full_mode_command = popen_calls[0]["command"]
    full_mode_prompt = full_mode_command[-1]
    if "# Prior Cursor interaction history" not in full_mode_prompt:
        raise AssertionError("full mode should inject prior Cursor interaction history")
    if "### Cursor response\n\nShort title" not in full_mode_prompt:
        raise AssertionError(
            "expected prior Cursor response text inside the replayed history"
        )
    if (
        "--mode" not in full_mode_command
        or full_mode_command[full_mode_command.index("--mode") + 1] != "ask"
    ):
        raise AssertionError("expected mode ask flag")

    auth_request = app_invoke_request(
        "exec_task",
        {"prompt": "Trigger an auth error."},
    )
    with mock.patch.dict(os.environ, {"CURSOR_API_KEY": ""}):
        _, frames, _ = run_process_plugin_main(module, auth_request)
    _, missing_key_result = assert_app_frames(frames, "cursor.cli")
    assert_canonical_error(
        missing_key_result["error"], "secret_resolution_error", ["CURSOR_API_KEY"]
    )
    auth_request["paths"]["workspace_dir"] = str(
        pathlib.Path(tempfile.mkdtemp(prefix="cursor-auth-workspace-"))
    )
    auth_stdout = "\n".join(
        [
            cursor_event(
                "error",
                "ses_auth123",
                {
                    "error": {"name": "AuthError", "message": "API key invalid"},
                    "subtype": "error",
                },
            ),
            "",
        ]
    )
    popen_calls.clear()

    class AuthPopen(SuccessPopen):
        def __init__(self, command, **kwargs):
            super().__init__(command, **kwargs)
            self.stdout = io.StringIO(auth_stdout)
            self.stderr = io.StringIO("")
            self._returncode = 1

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        auth_request,
        [mock.patch.object(module.subprocess, "Popen", side_effect=AuthPopen)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("auth failure should still produce a normal APP result")
    _, auth_result = assert_app_frames(frames, "cursor.cli")
    if auth_result["status"] != "failed":
        raise AssertionError("expected auth failure to produce failed status")
    assert_canonical_error(
        auth_result["error"],
        "auth_error",
        ["AuthError", "API key invalid"],
    )

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
    _, validation_result = assert_app_frames(frames, "cursor.cli")
    assert_canonical_error(
        validation_result["error"],
        "validation_error",
        ["exactly one of prompt or prompt_file is required"],
    )

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        auth_request,
        [
            mock.patch.object(
                module.subprocess, "Popen", side_effect=FileNotFoundError()
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("missing binary should still produce a normal APP result")
    _, missing_result = assert_app_frames(frames, "cursor.cli")
    assert_canonical_error(
        missing_result["error"],
        "configuration_error",
        ["agent CLI not found in PATH"],
    )

    class TimeoutPopen:
        def __init__(self, command, **kwargs):
            popen_calls.append({"command": command, "kwargs": kwargs})
            self.stdout = io.StringIO(
                cursor_event("system", "ses_timeout", {"subtype": "init"}) + "\n"
            )
            self.stderr = io.StringIO("")
            self.command = command
            self._timed_out = False
            self._returncode = -9

        def wait(self, timeout=None):
            time.sleep(min(timeout or 0, 0.01))
            raise module.subprocess.TimeoutExpired(self.command, timeout)

        def kill(self):
            self._returncode = -9
            return None

        def poll(self):
            return self._returncode

    auth_request["input"]["timeout_seconds"] = 1
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        auth_request,
        [mock.patch.object(module.subprocess, "Popen", side_effect=TimeoutPopen)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("timeout should still produce a normal APP result")
    _, timeout_result = assert_app_frames(frames, "cursor.cli")
    assert_canonical_error(
        timeout_result["error"],
        "timeout_error",
        ["Cursor Agent timed out after"],
    )
    if timeout_result["output"]["exit_code"] is not None:
        raise AssertionError("expected timeout output exit_code to be null")

    print("cursor.cli offline smoke test passed")
    lifecycle_patch.stop()


if __name__ == "__main__":
    main()
