#!/usr/bin/env python3
import io
import json
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
    with tempfile.TemporaryDirectory(prefix="codex-run-path-") as tmpdir:
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
    artifacts_dir = tempfile.mkdtemp(prefix="codex-skills-artifacts-")
    workspace_dir = pathlib.Path(tempfile.mkdtemp(prefix="codex-plugin-workspace-"))
    response_root = workspace_dir / ".transist" / "tmp"
    response_root.mkdir(parents=True, exist_ok=True)
    response_path = response_root / "codex-response.md"
    history_json_path = response_root / "codex-response.interaction.json"
    history_markdown_path = response_root / "codex-response.interaction.md"
    response_path.unlink(missing_ok=True)
    history_json_path.unlink(missing_ok=True)
    history_markdown_path.unlink(missing_ok=True)
    message_path = pathlib.Path(artifacts_dir) / "last_message.txt"
    response_path.write_text("Existing draft body\n", encoding="utf-8")
    invoke_request = app_invoke_request(
        "exec_task",
        {
            "prompt": "Say hello in one short sentence.",
            "skills_bundle": {"version": "v1", "roots": []},
            "skills_rendered": "# Workflow Skills\n\nUse the repo skill.\n",
            "context_files": [str(ROOT / "README.md")],
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
            "response_file": ".transist/tmp/codex-response.md",
            "sandbox": "read-only",
            "timeout_ms": 42000,
        },
        artifacts_dir=artifacts_dir,
    )
    invoke_request["paths"]["workspace_dir"] = str(workspace_dir)
    success_process = completed_process(
        ["codex", "exec"], returncode=0, stdout="live stdout\n", stderr=""
    )
    popen_calls = []

    class FakePopen:
        def __init__(self, command, **kwargs):
            stdin_capture = CaptureStdin() if kwargs.get("input") is not None else None
            popen_calls.append(
                {"command": command, "kwargs": kwargs, "stdin": stdin_capture}
            )
            self.command = command
            self.stdin = stdin_capture
            self.stdout = io.StringIO(success_process.stdout)
            self.stderr = io.StringIO(success_process.stderr)
            self._returncode = success_process.returncode

        def wait(self, timeout=None):
            return self._returncode

        def kill(self):
            self._returncode = -9

    message_path.write_text("Hello from Codex.\n", encoding="utf-8")
    # Missing optional workflow inputs are materialized as null by 0.12.0.
    invoke_request["input"].update(model=None, reasoning_effort=None)
    validate_json_schema(
        read_json(ROOT / "schemas/exec_task.input.schema.json"),
        invoke_request["input"],
        schema_path=ROOT / "schemas/exec_task.input.schema.json",
    )
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
    _, result = assert_app_frames(frames, "codex.cli")
    if result["status"] != "success":
        raise AssertionError("expected success result for mocked codex invocation")
    validate_json_schema(output_schema, result["output"])
    if not (pathlib.Path(artifacts_dir) / "workflow_skills.md").exists():
        raise AssertionError("expected workflow_skills.md artifact")
    if not (pathlib.Path(artifacts_dir) / "workflow_skills.json").exists():
        raise AssertionError("expected workflow_skills.json artifact")
    if response_path.read_text(encoding="utf-8").strip() != "Hello from Codex.":
        raise AssertionError(
            "expected response_file to persist the final Codex message"
        )
    command = popen_calls[0]["command"]
    kwargs = popen_calls[0]["kwargs"]
    stdin_payload = popen_calls[0]["stdin"].getvalue()
    if command[:3] != ["codex", "exec", "-"]:
        raise AssertionError("expected Codex prompt input to be passed through stdin")
    if kwargs["cwd"] != str(workspace_dir):
        raise AssertionError("expected the runtime workspace to set the subprocess cwd")
    if "--cd" not in command or command[command.index("--cd") + 1] != str(
        workspace_dir
    ):
        raise AssertionError(
            "expected the runtime workspace to be forwarded through --cd"
        )
    if "Workflow skills" not in stdin_payload:
        raise AssertionError("expected workflow skills in the composed Codex prompt")
    if "# Operator feedback" not in stdin_payload:
        raise AssertionError("expected gate feedback in the composed Codex prompt")
    if "# Current draft: codex-response.md" not in stdin_payload:
        raise AssertionError("expected current draft to be replayed in focused mode")
    if "# Prior Codex interaction history" in stdin_payload:
        raise AssertionError("focused mode should omit prior interaction history")
    if "# Context: README.md" not in stdin_payload:
        raise AssertionError(
            "expected context file contents in the composed Codex prompt"
        )
    if "# Task" not in stdin_payload:
        raise AssertionError("expected task heading in the composed Codex prompt")
    if "--model" in command:
        raise AssertionError("model should not appear in command when not supplied")
    if any(
        "-c" in str(arg) and "model_reasoning_effort" in str(arg) for arg in command
    ):
        raise AssertionError(
            "reasoning_effort should not appear in command when not supplied"
        )
    progress_frames = [
        frame for frame in frames if frame["type"] == "app.invoke.progress.v1"
    ]
    if len(progress_frames) < 4:
        raise AssertionError("expected Codex wrapper progress frames")
    if (
        progress_frames[0].get("line_preview")
        != "Prepared Codex task from 4 context item(s)."
    ):
        raise AssertionError("expected initial Codex status preview")
    if "Using repository context:" not in progress_frames[1].get("line_preview", ""):
        raise AssertionError("expected repository context status preview")
    history_entries = json.loads(history_json_path.read_text(encoding="utf-8"))
    if len(history_entries) != 1:
        raise AssertionError(
            "expected the first Codex revision to create one history entry"
        )
    if "# Codex interaction history" not in history_markdown_path.read_text(
        encoding="utf-8"
    ):
        raise AssertionError("expected readable Codex interaction history sidecar")
    if "usage" in result:
        raise AssertionError(
            "expected usage to be None when no real Codex session files exist"
        )

    follow_up_process = completed_process(
        ["codex", "exec"], returncode=0, stdout="second stdout\n", stderr=""
    )
    message_path.write_text("Hello from revised Codex.\n", encoding="utf-8")
    popen_calls.clear()

    class FollowUpPopen(FakePopen):
        def __init__(self, command, **kwargs):
            super().__init__(command, **kwargs)
            self.stdout = io.StringIO(follow_up_process.stdout)
            self.stderr = io.StringIO(follow_up_process.stderr)
            self._returncode = follow_up_process.returncode

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
            "focused follow-up path should not fail the plugin process"
        )
    _, focused_follow_up_result = assert_app_frames(frames, "codex.cli")
    if focused_follow_up_result["status"] != "success":
        raise AssertionError("expected success result for focused Codex follow-up")
    follow_up_stdin_payload = popen_calls[0]["stdin"].getvalue()
    if "# Prior Codex interaction history" in follow_up_stdin_payload:
        raise AssertionError(
            "focused mode should still omit prior interaction history on later"
            " revisions"
        )
    history_entries = json.loads(history_json_path.read_text(encoding="utf-8"))
    if len(history_entries) != 2:
        raise AssertionError("expected focused Codex follow-up to append history")
    if "## Execution 2" not in history_markdown_path.read_text(encoding="utf-8"):
        raise AssertionError("expected second Codex execution in history markdown")

    full_mode_request = json.loads(json.dumps(invoke_request))
    full_mode_request["input"]["feedback_context_mode"] = "full"
    message_path.write_text("Hello from full-history Codex.\n", encoding="utf-8")
    popen_calls.clear()

    class FullModePopen(FakePopen):
        def __init__(self, command, **kwargs):
            super().__init__(command, **kwargs)
            self.stdout = io.StringIO("third stdout\n")
            self.stderr = io.StringIO("")
            self._returncode = 0

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
            "full-history Codex path should not fail the plugin process"
        )
    _, full_mode_result = assert_app_frames(frames, "codex.cli")
    if full_mode_result["status"] != "success":
        raise AssertionError("expected success result for full-history Codex run")
    full_mode_stdin_payload = popen_calls[0]["stdin"].getvalue()
    if "# Prior Codex interaction history" not in full_mode_stdin_payload:
        raise AssertionError("full mode should include prior Codex interaction history")
    if "### Codex response\n\nHello from revised Codex." not in full_mode_stdin_payload:
        raise AssertionError(
            "expected prior Codex response text inside the replayed history"
        )

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write("Say hello from a file.\n")
        prompt_path = handle.name
    file_artifacts_dir = tempfile.mkdtemp(prefix="codex-file-artifacts-")
    file_workspace_dir = pathlib.Path(tempfile.mkdtemp(prefix="codex-file-workspace-"))
    pathlib.Path(file_artifacts_dir, "last_message.txt").write_text(
        "Hello from file Codex.\n",
        encoding="utf-8",
    )
    invoke_request = app_invoke_request(
        "exec_task",
        {
            "prompt_file": prompt_path,
            "skills_rendered": "# Workflow Skills\n\nBe concise.\n",
        },
        artifacts_dir=file_artifacts_dir,
    )
    invoke_request["paths"]["workspace_dir"] = str(file_workspace_dir)
    popen_calls.clear()
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
        raise AssertionError(
            "prompt_file smoke path should not fail the plugin process"
        )
    _, file_result = assert_app_frames(frames, "codex.cli")
    if file_result["status"] != "success":
        raise AssertionError(
            "expected success result for mocked codex prompt_file invocation"
        )
    called_command = popen_calls[0]["command"]
    stdin_payload = popen_calls[0]["stdin"].getvalue()
    if called_command[2] != "-":
        raise AssertionError("prompt_file should execute Codex with '-' prompt input")
    if "Say hello from a file." not in stdin_payload:
        raise AssertionError("prompt_file should pass file contents through stdin")
    if "Workflow skills" not in stdin_payload:
        raise AssertionError(
            "skills should be merged into the prompt_file stdin payload"
        )

    failure_artifacts_dir = tempfile.mkdtemp(prefix="codex-failure-artifacts-")
    failure_request = app_invoke_request(
        "exec_task",
        {"prompt": "Please fail cleanly.", "timeout_ms": 42000},
        artifacts_dir=failure_artifacts_dir,
    )
    failure_request["paths"]["workspace_dir"] = str(
        pathlib.Path(tempfile.mkdtemp(prefix="codex-failure-workspace-"))
    )
    popen_calls.clear()
    failed_process = completed_process(
        ["codex", "exec"],
        returncode=17,
        stdout="partial codex stdout\n",
        stderr="codex command failed\n",
    )

    class NonZeroPopen(FakePopen):
        def __init__(self, command, **kwargs):
            super().__init__(command, **kwargs)
            self.stdout = io.StringIO(failed_process.stdout)
            self.stderr = io.StringIO(failed_process.stderr)
            self._returncode = failed_process.returncode

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
            "non-zero Codex exit should still produce a normal APP result"
        )
    _, nonzero_result = assert_app_frames(frames, "codex.cli")
    if nonzero_result["status"] != "failed":
        raise AssertionError("expected non-zero Codex exit to fail the APP result")
    assert_canonical_error(
        nonzero_result["error"],
        "tool_execution_error",
        ["command exited with code 17"],
    )
    if nonzero_result["output"]["exit_code"] != 17:
        raise AssertionError("expected non-zero Codex exit code in output")

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write("This prompt file conflicts with inline input.\n")
        invalid_prompt_path = handle.name
    invalid_request = app_invoke_request(
        "exec_task",
        {"prompt": "inline", "prompt_file": invalid_prompt_path},
    )
    exit_code, frames, stderr = run_process_plugin_main(module, invalid_request)
    if exit_code != 0 or stderr:
        raise AssertionError(
            "validation failure should still produce a normal APP result"
        )
    _, validation_result = assert_app_frames(frames, "codex.cli")
    if validation_result["status"] != "failed":
        raise AssertionError(
            "expected conflicting Codex prompt sources to fail validation"
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
    _, missing_result = assert_app_frames(frames, "codex.cli")
    assert_canonical_error(
        missing_result["error"],
        "configuration_error",
        ["codex CLI not found in PATH"],
    )

    class TimeoutPopen(FakePopen):
        def __init__(self, command, **kwargs):
            super().__init__(command, **kwargs)
            self.stdout = io.StringIO("streaming codex stdout\n")
            self.stderr = io.StringIO("streaming codex stderr\n")
            self._returncode = 0
            self._timed_out = False

        def wait(self, timeout=None):
            if not self._timed_out:
                self._timed_out = True
                raise module.subprocess.TimeoutExpired(self.command, timeout)
            return self._returncode

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
    _, timeout_result = assert_app_frames(frames, "codex.cli")
    assert_canonical_error(
        timeout_result["error"],
        "timeout_error",
        ["37 seconds"],
    )
    if timeout_result["output"]["stdout"] != "streaming codex stdout\n":
        raise AssertionError("expected timeout output to preserve partial Codex stdout")
    if timeout_result["output"]["stderr"] != "streaming codex stderr\n":
        raise AssertionError("expected timeout output to preserve partial Codex stderr")
    if timeout_result["output"]["exit_code"] is not None:
        raise AssertionError("expected timeout output to leave exit_code unset")

    model_artifacts_dir = tempfile.mkdtemp(prefix="codex-model-artifacts-")
    model_workspace_dir = pathlib.Path(
        tempfile.mkdtemp(prefix="codex-model-workspace-")
    )
    pathlib.Path(model_artifacts_dir, "last_message.txt").write_text(
        "Hello from model Codex.\n", encoding="utf-8"
    )
    model_request = app_invoke_request(
        "exec_task",
        {
            "prompt": "Say hello.",
            "model": "o4-mini",
            "reasoning_effort": "high",
        },
        artifacts_dir=model_artifacts_dir,
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
    _, model_result = assert_app_frames(frames, "codex.cli")
    if model_result["status"] != "success":
        raise AssertionError(
            "expected success result for model+reasoning_effort invocation"
        )
    model_command = popen_calls[0]["command"]
    if (
        "--model" not in model_command
        or model_command[model_command.index("--model") + 1] != "o4-mini"
    ):
        raise AssertionError("expected --model o4-mini to be forwarded to codex exec")
    reasoning_flag_index = next(
        (
            i
            for i, arg in enumerate(model_command)
            if arg == "-c" and i + 1 < len(model_command)
        ),
        None,
    )
    if (
        reasoning_flag_index is None
        or model_command[reasoning_flag_index + 1] != "model_reasoning_effort=high"
    ):
        raise AssertionError(
            "expected -c model_reasoning_effort=high to be forwarded to codex exec"
        )

    print("codex.cli offline smoke test passed")


if __name__ == "__main__":
    main()
