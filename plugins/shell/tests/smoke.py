#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import tempfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))

from support import (  # noqa: E402
    app_invoke_request as base_request,
    assert_app_frames,
    assert_canonical_error,
    completed_process,
    load_plugin_module,
    read_json,
    run_process_plugin_main,
    validate_json_schema,
)


def app_invoke_request(action: str, payload: dict, artifacts_dir=None) -> dict:
    """Reuse offline smoke cases with explicit --compact without changing defaults."""
    if sys.argv[1:] == ["--compact"]:
        payload = payload | {"output_mode": "compact"}
    return base_request(action, payload, artifacts_dir)


def manifest_version():
    for line in (ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version: "):
            version = line.split(": ", 1)[1].strip().strip("'\"")
            if version:
                return version
            break
    raise AssertionError("plugin.yaml is missing a non-empty version field")


def main():
    for required in ["plugin.yaml", "plugin.py", "workflow.yaml", "README.md"]:
        if not (ROOT / required).exists():
            raise AssertionError(f"missing file: {required}")

    output_schema = read_json(ROOT / "schemas/exec.output.schema.json")
    echo_output_schema = read_json(ROOT / "schemas/echo.output.schema.json")
    module = load_plugin_module(ROOT)
    expected_plugin_version = manifest_version()

    echo_request = app_invoke_request(
        "echo", {"args": ["hello", "from", "shell", True, 2]}
    )
    exit_code, frames, stderr = run_process_plugin_main(module, echo_request)
    if exit_code != 0 or stderr:
        raise AssertionError(
            "plugin should not emit stderr or fail, got"
            f" exit={exit_code} stderr={stderr}"
        )
    _, echo_result = assert_app_frames(frames, "shell")
    handshake_frame = frames[0]
    if handshake_frame["plugin"]["version"] != expected_plugin_version:
        raise AssertionError(
            "handshake plugin version does not match plugin.yaml: "
            f"{handshake_frame['plugin']['version']} != {expected_plugin_version}"
        )
    if echo_result["status"] != "success":
        raise AssertionError(f"expected echo success result, got: {echo_result}")
    validate_json_schema(echo_output_schema, echo_result["output"])
    if echo_result["output"]["stdout"] != "hello from shell true 2":
        raise AssertionError(f"unexpected echo output: {echo_result['output']}")

    gate_decision = {
        "schema": "transist.gate_decision.v1",
        "run_id": "run-1",
        "gate_id": "review",
        "step_id": "review",
        "decision": "request_changes",
        "actor": "reviewer@example.com",
        "actor_source": "test",
        "reason": "keep the boundary",
        "decided_at_ms": 42,
    }
    exec_request = app_invoke_request(
        "exec",
        {
            "command": "python3",
            "args": ["-V", True, 2, 1.5],
            "gate_decision": gate_decision,
        },
    )
    success_process = completed_process(
        ["python3", "-V", "true", "2", "1.5"],
        returncode=0,
        stdout="Python 3.12.0\n",
        stderr="",
    )
    run_mock = mock.Mock(return_value=success_process)
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        exec_request,
        [mock.patch.object(module, "run_process", run_mock)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "mocked exec success path should not fail, got"
            f" exit={exit_code} stderr={stderr}"
        )
    _, exec_result = assert_app_frames(frames, "shell")
    if exec_result["status"] != "success":
        raise AssertionError(f"expected exec success result, got: {exec_result}")
    validate_json_schema(output_schema, exec_result["output"])
    command = run_mock.call_args.args[0]
    if command != ["python3", "-V", "true", "2", "1.5"]:
        raise AssertionError(f"unexpected subprocess command: {command}")
    if run_mock.call_args.kwargs.get("cwd") != exec_request["paths"]["workspace_dir"]:
        raise AssertionError(
            f"unexpected subprocess cwd: {run_mock.call_args.kwargs.get('cwd')}"
        )
    decision_environment = run_mock.call_args.kwargs.get("env") or {}
    if (
        json.loads(decision_environment.get("TRANSIST_GATE_DECISION_JSON", "null"))
        != gate_decision
    ):
        raise AssertionError(
            "exec must expose the injected gate decision to the child command"
        )

    with mock.patch.dict(
        os.environ,
        {"TRANSIST_GATE_DECISION_JSON": json.dumps(gate_decision)},
    ):
        initial_environment = module.command_environment({})
    if "TRANSIST_GATE_DECISION_JSON" in initial_environment:
        raise AssertionError("initial iteration must not inherit a stale gate decision")

    with tempfile.TemporaryDirectory(prefix="transist-shell-smoke-") as tempdir:
        tempdir_path = pathlib.Path(tempdir)
        repo_root = tempdir_path / "repo"
        repo_root.mkdir()
        workflow_dir = tempdir_path / "workflow" / "demo_plan_delivery"
        workflow_dir.mkdir(parents=True)
        helper = workflow_dir / "scripts" / "check_plan_bundle.py"
        helper.parent.mkdir()
        helper.write_text("pass\n", encoding="utf-8")
        recipe_run_root = (
            tempdir_path / "run_run_1" / "contained_runs" / "run_run_1__plan_loop"
        )
        recipe_run_root.mkdir(parents=True)
        (recipe_run_root / "recipe.yaml").write_text(
            "schema: transist.recipe.v1\nsource_dir: " + str(workflow_dir) + "\n",
            encoding="utf-8",
        )
        uri_request = app_invoke_request(
            "exec",
            {
                "command": "python3",
                "args": [
                    "run://demo_plan_delivery/DEMO-186/plan.md",
                    "repo://demo_plan_delivery/DEMO-186/plan.md",
                    "source://scripts/check_plan_bundle.py",
                    "literal-value",
                ],
                "repo_path": str(repo_root),
            },
            artifacts_dir=tempdir_path / "artifacts",
        )
        uri_request["paths"]["run_root"] = str(recipe_run_root)
        uri_run_mock = mock.Mock(
            return_value=completed_process(
                ["python3"], returncode=0, stdout="", stderr=""
            )
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            uri_request,
            [mock.patch.object(module, "run_process", uri_run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "URI resolution path should not fail, got"
                f" exit={exit_code} stderr={stderr}"
            )
        _, uri_result = assert_app_frames(frames, "shell")
        if uri_result["status"] != "success":
            raise AssertionError(
                f"expected URI resolution exec success result, got: {uri_result}"
            )
        uri_command = uri_run_mock.call_args.args[0]
        expected_run_path = (
            tempdir_path / "run_run_1" / "demo_plan_delivery" / "DEMO-186" / "plan.md"
        )
        expected_repo_path = (
            repo_root / ".transist" / "demo_plan_delivery" / "DEMO-186" / "plan.md"
        )
        expected_source_path = helper.resolve()
        if uri_command != [
            "python3",
            str(expected_run_path),
            str(expected_repo_path),
            str(expected_source_path),
            "literal-value",
        ]:
            raise AssertionError(f"unexpected resolved URI args: {uri_command}")

        escape_request = app_invoke_request(
            "exec",
            {
                "command": "python3",
                "args": ["source://../scripts/check_plan_bundle.py"],
            },
        )
        escape_request["paths"]["run_root"] = str(recipe_run_root)
        _, frames, _ = run_process_plugin_main(module, escape_request)
        _, escape_result = assert_app_frames(frames, "shell")
        if escape_result["error"]["code"] != "validation_error":
            raise AssertionError("source:// traversal must be rejected")

    missing_repo_request = app_invoke_request(
        "exec",
        {
            "command": "python3",
            "args": ["repo://demo_plan_delivery/DEMO-186/plan.md"],
        },
    )
    exit_code, frames, stderr = run_process_plugin_main(module, missing_repo_request)
    if exit_code != 0 or stderr:
        raise AssertionError(
            "missing repo_path URI resolution should still return a normal APP result"
        )
    _, missing_repo_result = assert_app_frames(frames, "shell")
    if missing_repo_result["status"] != "failed":
        raise AssertionError(
            "expected validation failure for repo:// without repo_path, got:"
            f" {missing_repo_result}"
        )
    assert_canonical_error(
        missing_repo_result["error"],
        expected_code="validation_error",
        message_contains=["repo_path is required when using repo:// artifact paths"],
    )

    missing_source_request = app_invoke_request(
        "exec",
        {
            "command": "python3",
            "args": ["source://../scripts/check_plan_bundle.py"],
        },
    )
    exit_code, frames, stderr = run_process_plugin_main(module, missing_source_request)
    if exit_code != 0 or stderr:
        raise AssertionError(
            "missing source_dir URI resolution should still return a normal APP result"
        )
    _, missing_source_result = assert_app_frames(frames, "shell")
    if missing_source_result["status"] != "failed":
        raise AssertionError(
            "expected validation failure for source:// without recipe source_dir, got:"
            f" {missing_source_result}"
        )
    assert_canonical_error(
        missing_source_result["error"],
        expected_code="validation_error",
        message_contains=[
            "recipe source_dir is required when using source:// artifact paths"
        ],
    )

    failing_request = app_invoke_request("exec", {"command": "false", "args": []})
    failing_process = completed_process(
        ["false"],
        returncode=2,
        stdout="",
        stderr="boom\n",
    )
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        failing_request,
        [mock.patch.object(module, "run_process", return_value=failing_process)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "mocked exec failure path should not fail, got"
            f" exit={exit_code} stderr={stderr}"
        )
    _, failing_result = assert_app_frames(frames, "shell")
    if failing_result["status"] != "failed":
        raise AssertionError(f"expected failed exec result, got: {failing_result}")
    validate_json_schema(output_schema, failing_result["output"])
    assert_canonical_error(
        failing_result["error"],
        expected_code="tool_execution_error",
        message_contains=["code 2"],
    )

    missing_command_request = app_invoke_request("exec", {})
    exit_code, frames, stderr = run_process_plugin_main(module, missing_command_request)
    if exit_code != 0 or stderr:
        raise AssertionError(
            "missing-command path should still return a normal APP result"
        )
    _, missing_command_result = assert_app_frames(frames, "shell")
    if missing_command_result["status"] != "failed":
        raise AssertionError(
            "expected validation failure for missing command, got:"
            f" {missing_command_result}"
        )
    assert_canonical_error(
        missing_command_result["error"],
        expected_code="validation_error",
        message_contains=["command is required"],
    )

    unsupported_request = app_invoke_request("not_real", {})
    exit_code, frames, stderr = run_process_plugin_main(module, unsupported_request)
    if exit_code != 0 or stderr:
        raise AssertionError(
            "unsupported-action path should still return a normal APP result"
        )
    _, unsupported_result = assert_app_frames(frames, "shell")
    if unsupported_result["status"] != "failed":
        raise AssertionError(
            "expected validation failure for unsupported action, got:"
            f" {unsupported_result}"
        )
    assert_canonical_error(
        unsupported_result["error"],
        expected_code="validation_error",
        message_contains=["unsupported action not_real"],
    )

    for action in ("exec", "exec_with_secret_env"):
        request = app_invoke_request(action, {"command": "tool"})
        for error, code in (
            (module.CleanupUnconfirmedError("partial", "stderr"), "protocol_error"),
            (RuntimeError("private token/argv"), "adapter_internal_error"),
        ):
            _, frames, stderr = run_process_plugin_main(
                module,
                request,
                [mock.patch.object(module, "run_process", side_effect=error)],
            )
            _, result = assert_app_frames(frames, "shell")
            if stderr or result["error"]["code"] != code:
                raise AssertionError(f"unexpected runner failure mapping: {result}")
            if "private token/argv" in str(result):
                raise AssertionError("runner diagnostics must not escape into APP")
            if code == "protocol_error":
                if result["error"]["retryability"] != "none":
                    raise AssertionError("uncertain cleanup must not be retryable")
                if result["artifact_hints"][0]["contents"] != "partial":
                    raise AssertionError("cleanup failure lost captured stdout")

    print("shell offline smoke test passed")


if __name__ == "__main__":
    main()
