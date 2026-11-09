#!/usr/bin/env python3
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


class CaptureStdin:
    def __init__(self):
        self.parts = []

    def write(self, data):
        self.parts.append(data)
        return len(data)

    def close(self):
        return None

    def getvalue(self):
        return "".join(self.parts)


def json_event(event_type, session_id, part=None, error=None, timestamp=1000):
    payload = {"type": event_type, "timestamp": timestamp, "sessionID": session_id}
    if part is not None:
        payload["part"] = part
    if error is not None:
        payload["error"] = error
    return json.dumps(payload)


def export_payload(
    session_id, share_url, response_text, provider="openai", model="gpt-5.1"
):
    return {
        "info": {"id": session_id, "shareURL": share_url},
        "messages": [
            {
                "info": {
                    "role": "assistant",
                    "providerID": provider,
                    "modelID": model,
                },
                "parts": [
                    {"type": "text", "text": response_text},
                ],
            }
        ],
    }


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
    validate_json_schema(
        input_schema,
        {
            "prompt": "Use the configured OpenRouter secret.",
            "secret_refs": ["OPENROUTER_KEY_PERSONAL"],
        },
    )
    runtime_root = pathlib.Path(tempfile.mkdtemp(prefix="opencode-runtime-"))
    runtime_env = {
        "XDG_DATA_HOME": str(runtime_root / "data"),
        "XDG_STATE_HOME": str(runtime_root / "state"),
        "XDG_CACHE_HOME": str(runtime_root / "cache"),
        "TMPDIR": str(runtime_root / "tmp"),
    }
    for value in runtime_env.values():
        pathlib.Path(value).mkdir(parents=True, exist_ok=True)
    os.environ.update(runtime_env)
    os.environ["OPENROUTER_API_KEY"] = "test-openrouter-key"
    manifest_text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    for declaration in (
        "XDG_DATA_HOME: runtime://temp/opencode/data",
        "XDG_STATE_HOME: runtime://temp/opencode/state",
        "XDG_CACHE_HOME: runtime://temp/opencode/cache",
        "TMPDIR: runtime://temp/opencode/tmp",
    ):
        if declaration not in manifest_text:
            raise AssertionError(f"missing runtime declaration: {declaration}")
    for declaration in (
        "name: OPENROUTER_KEY_PERSONAL",
        "required: false",
        "type: env",
        "name: OPENROUTER_API_KEY",
    ):
        if declaration not in manifest_text:
            raise AssertionError(
                f"missing OpenRouter secret declaration: {declaration}"
            )
    with tempfile.TemporaryDirectory(prefix="opencode-run-path-") as tmpdir:
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

    artifacts_dir = tempfile.mkdtemp(prefix="opencode-skills-artifacts-")
    workspace_dir = pathlib.Path(tempfile.mkdtemp(prefix="opencode-plugin-workspace-"))
    response_root = workspace_dir / ".transist" / "tmp"
    response_root.mkdir(parents=True, exist_ok=True)
    response_path = response_root / "opencode-response.md"
    title_path = response_root / "opencode-title.txt"
    pr_body_path = response_root / "opencode-pr-body.md"
    history_json_path = response_root / "opencode-response.interaction.json"
    history_markdown_path = response_root / "opencode-response.interaction.md"
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
            "response_file": ".transist/tmp/opencode-response.md",
            "summary_title_file": ".transist/tmp/opencode-title.txt",
            "pr_body_file": ".transist/tmp/opencode-pr-body.md",
            "allowed_tools": ["shell(git status:*)"],
            "denied_tools": ["bash(rm *)"],
            "permission": {"edit": "deny"},
            "model": "openai/gpt-5.1",
            "reasoning_effort": "xhigh",
            "agent": "build",
            "session_title": "Transist OpenCode Run",
            "share": True,
            "dangerously_skip_permissions": True,
            "timeout_ms": 42000,
        },
        artifacts_dir=artifacts_dir,
    )
    invoke_request["paths"]["workspace_dir"] = str(workspace_dir)
    stdout_text = "\n".join(
        [
            "~ https://opencode.ai/s/demo123",
            json_event(
                "step_start",
                "ses_demo123",
                {"id": "prt_start", "type": "step-start"},
                timestamp=1000,
            ),
            json_event(
                "tool_use",
                "ses_demo123",
                {
                    "id": "prt_tool",
                    "type": "tool",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "git status"},
                        "title": "git status",
                        "metadata": {},
                        "output": "On branch main",
                        "time": {"start": 1001, "end": 1002},
                    },
                },
                timestamp=1002,
            ),
            json_event(
                "text",
                "ses_demo123",
                {
                    "id": "prt_text",
                    "type": "text",
                    "text": (
                        "OpenCode checked the repo.\n\n"
                        "Short title\n\n"
                        "## What Changed\n\n"
                        "Body text"
                    ),
                    "time": {"start": 1003, "end": 1004},
                },
                timestamp=1004,
            ),
            json_event(
                "step_finish",
                "ses_demo123",
                {
                    "id": "prt_finish",
                    "type": "step-finish",
                    "reason": "stop",
                    "tokens": {
                        "input": 120,
                        "output": 34,
                        "reasoning": 8,
                        "cache": {"read": 56, "write": 7},
                        "total": 218,
                    },
                },
                timestamp=1005,
            ),
            "",
        ]
    )
    popen_calls = []

    class SuccessPopen:
        def __init__(self, command, **kwargs):
            stdin_capture = CaptureStdin() if kwargs.get("input") is not None else None
            popen_calls.append(
                {"command": command, "kwargs": kwargs, "stdin": stdin_capture}
            )
            self.command = command
            self.args = command
            self.stdin = stdin_capture
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
        [
            mock.patch.object(
                module, "run_process", side_effect=fixture_runner(SuccessPopen)
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("offline smoke path should not fail the plugin process")
    _, result = assert_app_frames(frames, "opencode.cli")
    if result["status"] != "success":
        raise AssertionError("expected success result for mocked OpenCode invocation")
    validate_json_schema(output_schema, result["output"])
    if not (pathlib.Path(artifacts_dir) / "workflow_skills.md").exists():
        raise AssertionError("expected workflow_skills.md artifact")
    if not (pathlib.Path(artifacts_dir) / "workflow_skills.json").exists():
        raise AssertionError("expected workflow_skills.json artifact")
    command = popen_calls[0]["command"]
    kwargs = popen_calls[0]["kwargs"]
    if popen_calls[0]["stdin"] is not None:
        raise AssertionError(
            "expected the prompt to be passed as an OpenCode message argument"
        )
    prompt_argument = command[-1]
    if command[:4] != ["opencode", "run", "--format", "json"]:
        raise AssertionError("expected OpenCode JSON-mode invocation")
    if "--dir" not in command or command[command.index("--dir") + 1] != str(
        workspace_dir
    ):
        raise AssertionError("expected the runtime workspace to flow through --dir")
    if (
        "--model" not in command
        or command[command.index("--model") + 1] != "openai/gpt-5.1"
    ):
        raise AssertionError("expected model flag")
    if "--agent" not in command or command[command.index("--agent") + 1] != "build":
        raise AssertionError("expected agent flag")
    if "--variant" not in command or command[command.index("--variant") + 1] != "max":
        raise AssertionError("expected reasoning_effort alias to map to --variant max")
    if (
        "--title" not in command
        or command[command.index("--title") + 1] != "Transist OpenCode Run"
    ):
        raise AssertionError("expected explicit session title")
    if "--share" not in command:
        raise AssertionError("expected share flag")
    if "--dangerously-skip-permissions" not in command:
        raise AssertionError("expected dangerously-skip-permissions flag")
    if kwargs["cwd"] != str(workspace_dir):
        raise AssertionError("expected the runtime workspace to set the subprocess cwd")
    if not callable(kwargs["on_line"]):
        raise AssertionError("expected caller-thread stream callback")
    for name, value in runtime_env.items():
        if kwargs["env"][name] != value:
            raise AssertionError(f"expected declared {name} runtime directory")
    if kwargs["env"]["OPENROUTER_API_KEY"] != "test-openrouter-key":
        raise AssertionError(
            "expected OpenRouter API key to reach the OpenCode subprocess"
        )
    config_content = json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
    if config_content["permission"] != {
        "bash": {"*": "deny", "git status *": "allow", "rm *": "deny"},
        "edit": "deny",
    }:
        raise AssertionError(
            f"unexpected shimmed permission config: {config_content['permission']}"
        )
    if "Workflow Skills" not in prompt_argument:
        raise AssertionError("expected workflow skills in the composed OpenCode prompt")
    if "# Operator feedback" not in prompt_argument:
        raise AssertionError("expected gate feedback in the composed prompt")
    if "# Current draft: opencode-response.md" not in prompt_argument:
        raise AssertionError("expected current draft to be replayed in focused mode")
    if "# Prior OpenCode interaction history" in prompt_argument:
        raise AssertionError(
            "focused mode should omit prior OpenCode interaction history"
        )
    if "# Context: README.md" not in prompt_argument:
        raise AssertionError("expected context file contents in the composed prompt")
    if "# Task" not in prompt_argument:
        raise AssertionError("expected task heading in the composed prompt")
    progress_frames = [
        frame for frame in frames if frame["type"] == "app.invoke.progress.v1"
    ]
    if len(progress_frames) < 6:
        raise AssertionError("expected OpenCode wrapper progress frames")
    if (
        progress_frames[0].get("line_preview")
        != "Prepared OpenCode task from 4 context item(s)."
    ):
        raise AssertionError("expected initial OpenCode status preview")
    if "Using repository context:" not in progress_frames[1].get("line_preview", ""):
        raise AssertionError("expected repository context status preview")
    if progress_frames[2].get("line_preview") != "Launching opencode run...":
        raise AssertionError("expected launch status preview")
    if progress_frames[3].get("line_preview") != "Waiting for OpenCode output...":
        raise AssertionError("expected waiting status preview")
    if progress_frames[4].get("line_preview") != "OpenCode started a step.":
        raise AssertionError("expected step-start preview")
    if "OpenCode tool: bash git status" not in progress_frames[5].get(
        "line_preview", ""
    ):
        raise AssertionError("expected tool-use preview")
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
        raise AssertionError("expected OpenCode interaction history sidecars")
    if "## Execution 1" not in history_markdown_path.read_text(encoding="utf-8"):
        raise AssertionError("expected first OpenCode execution in markdown sidecar")
    usage_json_path = pathlib.Path(artifacts_dir) / "usage.json"
    if not usage_json_path.exists():
        raise AssertionError("expected usage.json artifact")
    usage_payload = json.loads(usage_json_path.read_text(encoding="utf-8"))
    if usage_payload != {
        "model": "openai/gpt-5.1",
        "input_tokens": 120,
        "output_tokens": 42,
        "total_tokens": 218,
        "reasoning_tokens": 8,
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
    if result["output"]["session_id"] != "ses_demo123":
        raise AssertionError("expected session_id in output")
    if result["output"]["share_url"] != "https://opencode.ai/s/demo123":
        raise AssertionError("expected share_url from plain stdout line")

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
            json_event(
                "text",
                "ses_demo123",
                {
                    "id": "prt_text_2",
                    "type": "text",
                    "text": "Revised title\n\n## What Changed\n\nRevised body",
                    "time": {"start": 2001, "end": 2002},
                },
                timestamp=2002,
            ),
            json_event(
                "step_finish",
                "ses_demo123",
                {
                    "id": "prt_finish_2",
                    "type": "step-finish",
                    "reason": "stop",
                    "tokens": {
                        "input": 1,
                        "output": 1,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                },
                timestamp=2003,
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
    follow_up_request["input"]["share"] = False
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        follow_up_request,
        [
            mock.patch.object(
                module, "run_process", side_effect=fixture_runner(FollowUpPopen)
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "follow-up revision path should not fail the plugin process"
        )
    _, follow_up_result = assert_app_frames(frames, "opencode.cli")
    if follow_up_result["status"] != "success":
        raise AssertionError(
            "expected success result for follow-up OpenCode invocation"
        )
    follow_up_prompt = popen_calls[0]["command"][-1]
    if "# Prior OpenCode interaction history" in follow_up_prompt:
        raise AssertionError(
            "focused mode should still omit prior OpenCode interaction history"
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
    full_mode_request["input"]["share"] = False
    popen_calls.clear()
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        full_mode_request,
        [
            mock.patch.object(
                module, "run_process", side_effect=fixture_runner(FollowUpPopen)
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("full feedback mode should not fail the plugin process")
    _, full_mode_result = assert_app_frames(frames, "opencode.cli")
    if full_mode_result["status"] != "success":
        raise AssertionError("expected success result for full feedback mode")
    full_mode_prompt = popen_calls[0]["command"][-1]
    if "# Prior OpenCode interaction history" not in full_mode_prompt:
        raise AssertionError(
            "full mode should inject prior OpenCode interaction history"
        )
    if "### OpenCode response\n\nOpenCode checked the repo." not in full_mode_prompt:
        raise AssertionError(
            "expected prior OpenCode response text inside the replayed history"
        )

    export_request = app_invoke_request(
        "exec_task",
        {"prompt": "Summarize the exported result.", "share": True},
    )
    export_request["paths"]["workspace_dir"] = str(
        pathlib.Path(tempfile.mkdtemp(prefix="opencode-export-fallback-workspace-"))
    )
    export_stdout = "\n".join(
        [
            json_event(
                "step_start",
                "ses_export123",
                {"id": "prt_export_start", "type": "step-start"},
                timestamp=3000,
            ),
            json_event(
                "step_finish",
                "ses_export123",
                {
                    "id": "prt_export_finish",
                    "type": "step-finish",
                    "reason": "stop",
                    "tokens": {
                        "input": 0,
                        "output": 0,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                },
                timestamp=3001,
            ),
            "",
        ]
    )
    popen_calls.clear()

    class ExportFallbackPopen(SuccessPopen):
        def __init__(self, command, **kwargs):
            super().__init__(command, **kwargs)
            self.stdout = io.StringIO(export_stdout)
            self.stderr = io.StringIO("")
            self._returncode = 0

    export_completed = completed_process(
        ["opencode", "export", "ses_export123"],
        returncode=0,
        stdout=json.dumps(
            export_payload(
                "ses_export123",
                "https://opencode.ai/s/export123",
                "Exported title\n\n## What Changed\n\nExported body",
                provider="anthropic",
                model="claude-sonnet-4-5",
            )
        ),
        stderr="Exporting session: ses_export123\n",
    )
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        export_request,
        [
            mock.patch.object(
                module, "run_process", side_effect=fixture_runner(ExportFallbackPopen)
            ),
            mock.patch.object(
                module,
                "export_session_data",
                return_value=json.loads(export_completed.stdout),
            ),
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("export fallback path should not fail the plugin process")
    _, export_result = assert_app_frames(frames, "opencode.cli")
    if export_result["status"] != "success":
        raise AssertionError(
            "expected success result when export fallback fills response_text"
        )
    if (
        export_result["output"]["response_text"]
        != "Exported title\n\n## What Changed\n\nExported body"
    ):
        raise AssertionError("expected response_text from export fallback")
    if export_result["output"]["share_url"] != "https://opencode.ai/s/export123":
        raise AssertionError("expected share_url from export fallback")

    auth_request = app_invoke_request(
        "exec_task",
        {"prompt": "Trigger an auth error."},
    )
    auth_request["paths"]["workspace_dir"] = str(
        pathlib.Path(tempfile.mkdtemp(prefix="opencode-auth-workspace-"))
    )
    auth_stdout = "\n".join(
        [
            json_event(
                "error",
                "ses_auth123",
                error={
                    "name": "ProviderAuthError",
                    "data": {"message": "API key invalid", "statusCode": 401},
                },
                timestamp=4000,
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
        [
            mock.patch.object(
                module, "run_process", side_effect=fixture_runner(AuthPopen)
            ),
            mock.patch.object(module, "export_session_data", return_value=None),
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("auth failure should still produce a normal APP result")
    _, auth_result = assert_app_frames(frames, "opencode.cli")
    if auth_result["status"] != "failed":
        raise AssertionError("expected auth failure to produce failed status")
    assert_canonical_error(
        auth_result["error"],
        "auth_error",
        ["ProviderAuthError", "API key invalid"],
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
    _, validation_result = assert_app_frames(frames, "opencode.cli")
    assert_canonical_error(
        validation_result["error"],
        "validation_error",
        ["exactly one of prompt or prompt_file is required"],
    )

    invalid_tools_request = app_invoke_request(
        "exec_task",
        {"prompt": "test", "denied_tools": ["mcp(jira_*)"]},
    )
    exit_code, frames, stderr = run_process_plugin_main(module, invalid_tools_request)
    if exit_code != 0 or stderr:
        raise AssertionError(
            "tool shim validation should still produce a normal APP result"
        )
    _, invalid_tools_result = assert_app_frames(frames, "opencode.cli")
    assert_canonical_error(
        invalid_tools_result["error"],
        "validation_error",
        ["unsupported denied_tools descriptor"],
    )

    required_only_request = app_invoke_request(
        "exec_task",
        {
            "prompt": "Required-only demo.",
            "model": None,
            "reasoning_effort": None,
            "variant": None,
            "agent": None,
        },
    )
    validate_json_schema(
        read_json(ROOT / "schemas/exec_task.input.schema.json"),
        required_only_request["input"],
    )
    popen_calls.clear()
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        required_only_request,
        [
            mock.patch.object(
                module, "run_process", side_effect=fixture_runner(SuccessPopen)
            )
        ],
    )
    _, required_only_result = assert_app_frames(frames, "opencode.cli")
    if exit_code != 0 or stderr or required_only_result["status"] != "success":
        raise AssertionError(
            "required-only demo should succeed with null optional inputs"
        )
    if any(
        flag in popen_calls[0]["command"]
        for flag in ("--model", "--variant", "--agent")
    ):
        raise AssertionError("null demo options must not select CLI flags")

    variant_request = app_invoke_request(
        "exec_task",
        {
            "prompt": "Use variant precedence.",
            "variant": "minimal",
            "reasoning_effort": "xhigh",
        },
    )
    popen_calls.clear()
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        variant_request,
        [
            mock.patch.object(
                module, "run_process", side_effect=fixture_runner(SuccessPopen)
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "variant precedence path should not fail the plugin process"
        )
    if "--variant" not in popen_calls[0]["command"]:
        raise AssertionError("expected native variant flag")
    variant_index = popen_calls[0]["command"].index("--variant")
    if popen_calls[0]["command"][variant_index + 1] != "minimal":
        raise AssertionError("expected native variant to override reasoning_effort")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        auth_request,
        [mock.patch.object(module, "run_process", side_effect=FileNotFoundError())],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("missing binary should still produce a normal APP result")
    _, missing_result = assert_app_frames(frames, "opencode.cli")
    assert_canonical_error(
        missing_result["error"],
        "configuration_error",
        ["opencode CLI not found in PATH"],
    )

    class TimeoutPopen:
        def __init__(self, command, **kwargs):
            stdin_capture = CaptureStdin() if kwargs.get("stdin") is not None else None
            popen_calls.append(
                {"command": command, "kwargs": kwargs, "stdin": stdin_capture}
            )
            self.stdin = stdin_capture
            self.stdout = io.StringIO(
                json_event(
                    "step_start",
                    "ses_timeout",
                    {"id": "prt_timeout", "type": "step-start"},
                )
                + "\n"
            )
            self.stderr = io.StringIO("")
            self.command = command
            self._timed_out = False
            self._returncode = -9

        def wait(self, timeout=None):
            if not self._timed_out:
                self._timed_out = True
                raise module.subprocess.TimeoutExpired(self.command, timeout)
            return self._returncode

        def kill(self):
            self._returncode = -9
            return None

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        auth_request,
        [
            mock.patch.object(
                module, "run_process", side_effect=fixture_runner(TimeoutPopen)
            )
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("timeout should still produce a normal APP result")
    _, timeout_result = assert_app_frames(frames, "opencode.cli")
    assert_canonical_error(
        timeout_result["error"],
        "timeout_error",
        ["opencode timed out after"],
    )
    if timeout_result["output"]["exit_code"] is not None:
        raise AssertionError("expected timeout output exit_code to be null")

    print("opencode.cli offline smoke test passed")


if __name__ == "__main__":
    main()
