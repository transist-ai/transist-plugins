#!/usr/bin/env python3
import pathlib
import sys
import tempfile
from unittest import mock
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))

from support import (  # noqa: E402
    app_invoke_request as base_request,
    assert_app_frames,
    completed_process,
    load_plugin_module,
    read_json,
    run_process_plugin_main,
)


def app_invoke_request(action: str, payload: dict, artifacts_dir=None) -> dict:
    """Reuse offline smoke cases with explicit --compact without changing defaults."""
    if sys.argv[1:] == ["--compact"]:
        payload = payload | {"output_mode": "compact"}
    return base_request(action, payload, artifacts_dir)


def validate_json_schema(schema: dict, payload: object) -> None:
    """Validate action schemas using only the selected package-local compact ref."""
    compact = Resource.from_contents(read_json(ROOT / "schemas/compact.schema.json"))
    registry = Registry().with_resource("compact.schema.json", compact)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, registry=registry).validate(payload)


def main():
    for required in ["plugin.yaml", "plugin.py", "workflow.yaml", "README.md"]:
        if not (ROOT / required).exists():
            raise AssertionError(f"missing file: {required}")
    output_schema = read_json(ROOT / "schemas/current_branch.output.schema.json")
    sync_output_schema = read_json(
        ROOT / "schemas/sync_branch_from_pr.output.schema.json"
    )
    delete_output_schema = read_json(ROOT / "schemas/delete_branch.output.schema.json")
    worktree_add_output_schema = read_json(
        ROOT / "schemas/worktree_add.output.schema.json"
    )
    worktree_remove_output_schema = read_json(
        ROOT / "schemas/worktree_remove.output.schema.json"
    )
    pull_output_schema = read_json(
        ROOT / "schemas/pull_current_branch.output.schema.json"
    )
    module = load_plugin_module(ROOT)

    invoke_request = app_invoke_request("current_branch", {})
    success_process = completed_process(
        ["git", "branch", "--show-current"],
        returncode=0,
        stdout="main\n",
        stderr="",
    )
    exit_code, frames, stderr = run_process_plugin_main(
        module,
        invoke_request,
        [mock.patch.object(module, "run_process", return_value=success_process)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "plugin should not emit stderr or fail, got"
            f" exit={exit_code} stderr={stderr}"
        )
    _, result = assert_app_frames(frames, "git.cli")
    if result["status"] != "success":
        raise AssertionError("expected success result for mocked git invocation")
    validate_json_schema(output_schema, result["output"])

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        invoke_request,
        [mock.patch.object(module, "run_process", side_effect=FileNotFoundError())],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "missing binary path should still return a normal APP result"
        )
    _, missing_result = assert_app_frames(frames, "git.cli")
    if missing_result["status"] != "failed":
        raise AssertionError("missing binary should produce failed status")
    if missing_result["error"]["code"] != "configuration_error":
        raise AssertionError("missing binary should produce configuration_error")

    with tempfile.TemporaryDirectory() as tmpdir:
        request = {
            "paths": {
                "workspace_dir": tmpdir,
                "run_root": str(
                    pathlib.Path(tmpdir)
                    / "home"
                    / "runs"
                    / "run_demo"
                    / "contained_runs"
                    / "run_demo__implementation"
                ),
            }
        }
        run_path = module.workspace_path(
            request,
            "run://demo_ticket_delivery/DEMO-45/change_summary_title.txt",
        )
        if run_path != str(
            pathlib.Path(tmpdir)
            / "home"
            / "runs"
            / "run_demo"
            / "demo_ticket_delivery"
            / "DEMO-45"
            / "change_summary_title.txt"
        ):
            raise AssertionError(f"unexpected run:// path resolution: {run_path}")

        repo_workspace_path = module.workspace_path(
            request,
            "repo://demo_ticket_delivery/DEMO-45/change_summary_title.txt",
        )
        if repo_workspace_path != str(
            pathlib.Path(tmpdir)
            / ".transist"
            / "demo_ticket_delivery"
            / "DEMO-45"
            / "change_summary_title.txt"
        ):
            raise AssertionError(
                f"unexpected repo:// path resolution: {repo_workspace_path}"
            )

    with tempfile.TemporaryDirectory() as tmpdir:
        pr_file = pathlib.Path(tmpdir) / "pr.json"
        pr_file.write_text(
            '{"pull_request": {"baseRefName": "main"}}',
            encoding="utf-8",
        )
        invoke_request = app_invoke_request(
            "sync_branch_from_pr",
            {
                "pr_file": str(pr_file),
                "remote": "origin",
            },
        )
        run_mock = mock.Mock(
            side_effect=[
                completed_process(
                    ["git", "switch", "main"],
                    returncode=0,
                    stdout="Switched to branch 'main'\n",
                    stderr="",
                ),
                completed_process(
                    ["git", "pull", "origin", "main"],
                    returncode=0,
                    stdout="Already up to date.\n",
                    stderr="",
                ),
            ]
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "offline sync_branch_from_pr smoke path should not fail the plugin"
                " process"
            )
        _, result = assert_app_frames(frames, "git.cli")
        if result["status"] != "success":
            raise AssertionError(
                "expected success result for mocked sync_branch_from_pr flow, got:"
                f" {result}"
            )
        validate_json_schema(sync_output_schema, result["output"])
        if (
            result["output"]["branch"] != "main"
            or result["output"]["remote"] != "origin"
        ):
            raise AssertionError(f"unexpected sync output: {result['output']}")
        pull_command = run_mock.call_args_list[1].args[0]
        if pull_command != ["git", "pull", "origin", "main"]:
            raise AssertionError(f"unexpected git pull command: {pull_command}")

    with tempfile.TemporaryDirectory() as tmpdir:
        invoke_request = app_invoke_request(
            "delete_branch",
            {
                "name": "feature/example",
                "force": True,
                "allow_missing": False,
            },
        )
        run_mock = mock.Mock(
            return_value=completed_process(
                ["git", "branch", "-D", "feature/example"],
                returncode=0,
                stdout="Deleted branch feature/example (was deadbeef).\n",
                stderr="",
            )
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "offline delete_branch smoke path should not fail the plugin process"
            )
        _, result = assert_app_frames(frames, "git.cli")
        if result["status"] != "success":
            raise AssertionError(
                f"expected success result for mocked delete_branch flow, got: {result}"
            )
        validate_json_schema(delete_output_schema, result["output"])
        if not result["output"]["deleted"] or result["output"]["missing"]:
            raise AssertionError(f"unexpected delete output: {result['output']}")
        command = run_mock.call_args.args[0]
        if command != ["git", "branch", "-D", "feature/example"]:
            raise AssertionError(f"unexpected delete command: {command}")

    with tempfile.TemporaryDirectory() as tmpdir:
        invoke_request = app_invoke_request(
            "delete_branch",
            {
                "default_name": "feature/missing",
                "force": True,
                "allow_missing": True,
            },
        )
        run_mock = mock.Mock(
            return_value=completed_process(
                ["git", "rev-parse", "--verify", "refs/heads/feature/missing"],
                returncode=1,
                stdout="",
                stderr="fatal: Needed a single revision\n",
            )
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "offline missing-branch smoke path should not fail the plugin process"
            )
        _, result = assert_app_frames(frames, "git.cli")
        if result["status"] != "success":
            raise AssertionError(
                f"expected allow_missing path to succeed, got: {result}"
            )
        validate_json_schema(delete_output_schema, result["output"])
        if result["output"]["deleted"] or not result["output"]["missing"]:
            raise AssertionError(
                f"unexpected missing-branch output: {result['output']}"
            )
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_path = str(pathlib.Path(tmpdir) / "worktree-feature")
        invoke_request = app_invoke_request(
            "worktree_add",
            {
                "path": worktree_path,
                "new_branch": "feature/wt-test",
            },
        )
        run_mock = mock.Mock(
            return_value=completed_process(
                ["git", "worktree", "add", "-b", "feature/wt-test", worktree_path],
                returncode=0,
                stdout=(
                    "Preparing worktree (new branch 'feature/wt-test')\nHEAD is now at"
                    " abc1234\n"
                ),
                stderr="",
            )
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "offline worktree_add smoke path should not fail the plugin process"
            )
        _, result = assert_app_frames(frames, "git.cli")
        if result["status"] != "success":
            raise AssertionError(
                f"expected success result for mocked worktree_add, got: {result}"
            )
        validate_json_schema(worktree_add_output_schema, result["output"])
        if result["output"]["branch"] != "feature/wt-test":
            raise AssertionError(
                f"unexpected worktree_add branch in output: {result['output']}"
            )
        command = run_mock.call_args.args[0]
        if command != [
            "git",
            "worktree",
            "add",
            "-b",
            "feature/wt-test",
            worktree_path,
        ]:
            raise AssertionError(f"unexpected worktree_add command: {command}")

    with tempfile.TemporaryDirectory() as tmpdir:
        worktree_path = str(pathlib.Path(tmpdir) / "worktree-feature")
        invoke_request = app_invoke_request(
            "worktree_remove",
            {
                "path": worktree_path,
                "force": True,
            },
        )
        run_mock = mock.Mock(
            return_value=completed_process(
                ["git", "worktree", "remove", "--force", worktree_path],
                returncode=0,
                stdout="",
                stderr="",
            )
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "offline worktree_remove smoke path should not fail the plugin process"
            )
        _, result = assert_app_frames(frames, "git.cli")
        if result["status"] != "success":
            raise AssertionError(
                f"expected success result for mocked worktree_remove, got: {result}"
            )
        validate_json_schema(worktree_remove_output_schema, result["output"])
        if result["output"]["path"] != worktree_path:
            raise AssertionError(
                f"unexpected worktree_remove path in output: {result['output']}"
            )
        command = run_mock.call_args.args[0]
        if command != ["git", "worktree", "remove", "--force", worktree_path]:
            raise AssertionError(f"unexpected worktree_remove command: {command}")

    with tempfile.TemporaryDirectory() as tmpdir:
        invoke_request = app_invoke_request(
            "pull_current_branch",
            {
                "remote": "origin",
            },
        )
        run_mock = mock.Mock(
            side_effect=[
                completed_process(
                    ["git", "branch", "--show-current"],
                    returncode=0,
                    stdout="feature/example\n",
                    stderr="",
                ),
                completed_process(
                    [
                        "git",
                        "pull",
                        "--rebase",
                        "--autostash",
                        "origin",
                        "feature/example",
                    ],
                    returncode=0,
                    stdout="Already up to date.\n",
                    stderr="",
                ),
            ]
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "offline pull_current_branch smoke path should not fail the plugin"
                " process"
            )
        _, result = assert_app_frames(frames, "git.cli")
        if result["status"] != "success":
            raise AssertionError(
                f"expected success result for mocked pull_current_branch, got: {result}"
            )
        validate_json_schema(pull_output_schema, result["output"])
        if (
            result["output"]["branch"] != "feature/example"
            or result["output"]["remote"] != "origin"
        ):
            raise AssertionError(f"unexpected pull output: {result['output']}")
        pull_command = run_mock.call_args_list[1].args[0]
        if pull_command != [
            "git",
            "pull",
            "--rebase",
            "--autostash",
            "origin",
            "feature/example",
        ]:
            raise AssertionError(f"unexpected git pull command: {pull_command}")

    with tempfile.TemporaryDirectory() as tmpdir:
        invoke_request = app_invoke_request(
            "pull_current_branch",
            {
                "remote": "origin",
                "branch": "main",
                "rebase": False,
                "autostash": False,
                "ff_only": True,
            },
        )
        run_mock = mock.Mock(
            return_value=completed_process(
                ["git", "pull", "--ff-only", "origin", "main"],
                returncode=0,
                stdout="Already up to date.\n",
                stderr="",
            )
        )
        exit_code, frames, stderr = run_process_plugin_main(
            module,
            invoke_request,
            [mock.patch.object(module, "run_process", run_mock)],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "offline pull_current_branch ff-only path should not fail the plugin"
                " process"
            )
        _, result = assert_app_frames(frames, "git.cli")
        if result["status"] != "success":
            raise AssertionError(
                f"expected success for mocked ff-only pull, got: {result}"
            )
        validate_json_schema(pull_output_schema, result["output"])
        command = run_mock.call_args.args[0]
        if command != ["git", "pull", "--ff-only", "origin", "main"]:
            raise AssertionError(f"unexpected ff-only pull command: {command}")

    print("git.cli offline smoke test passed")


if __name__ == "__main__":
    main()
