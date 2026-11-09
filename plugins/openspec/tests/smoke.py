#!/usr/bin/env python3
import datetime
import json
import os
import pathlib
import sys
import tempfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))

from support import (  # noqa: E402
    app_invoke_request as build_invoke_request,
    assert_app_frames,
    completed_process,
    load_plugin_module,
    read_json,
    run_process_plugin_main,
    validate_json_schema,
)


def app_invoke_request(action, input_payload, artifacts_dir=None):
    input_payload = dict(input_payload)
    workspace_dir = input_payload.pop("_workspace_dir", ROOT.parents[1])
    request = build_invoke_request(action, input_payload, artifacts_dir=artifacts_dir)
    request["paths"]["workspace_dir"] = str(workspace_dir)
    return request


def write_copilot_session(
    session_state_dir, session_id, cwd, model, input_tokens, output_tokens
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
                            "reasoningTokens": 0,
                            "cacheReadTokens": 0,
                            "cacheWriteTokens": 0,
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


def create_repo(with_openspec=True):
    repo_dir = pathlib.Path(tempfile.mkdtemp(prefix="openspec-plugin-repo-"))
    if with_openspec:
        (repo_dir / "openspec" / "changes").mkdir(parents=True, exist_ok=True)
    (repo_dir / "services" / "billing").mkdir(parents=True, exist_ok=True)
    (repo_dir / "services" / "billing" / "handler.py").write_text(
        "def handle_invoice_summary():\n    return {'status': 'ok'}\n",
        encoding="utf-8",
    )
    (repo_dir / "docs").mkdir(parents=True, exist_ok=True)
    (repo_dir / "docs" / "planning-context.md").write_text(
        "# Planning Context\n\nFocus only on billing invoice summaries.\n",
        encoding="utf-8",
    )
    return repo_dir


def design_doc(summary):
    return f"# Design\n\n{summary}\n"


def write_canonical_bundle(bundle_dir):
    (bundle_dir / "specs" / "billing-api").mkdir(parents=True, exist_ok=True)
    (bundle_dir / "README.md").write_text(
        "1. Goal\n"
        "2. Constraints and assumptions\n"
        "3. Implementation plan\n"
        "4. Verification plan\n"
        "5. Risks and open questions\n",
        encoding="utf-8",
    )
    (bundle_dir / "proposal.md").write_text(
        "# Proposal\n\nApproved billing plan.\n", encoding="utf-8"
    )
    (bundle_dir / "tasks.md").write_text(
        "- [ ] Implement the approved billing plan\n", encoding="utf-8"
    )
    (bundle_dir / "design.md").write_text(
        design_doc("Keep the current billing queue."), encoding="utf-8"
    )
    (bundle_dir / "specs" / "billing-api" / "spec.md").write_text(
        "# Spec Delta\n\n"
        "## ADDED Requirements\n\n"
        "### Requirement: Billing API returns invoice summaries\n\n"
        "The billing API SHALL return invoice summary data for an existing account.\n\n"
        "#### Scenario: Invoice summary is requested for an existing account\n\n"
        "- WHEN a client requests invoice summary for an existing account\n"
        "- THEN the API returns the matching invoice summary data\n",
        encoding="utf-8",
    )
    return bundle_dir


def write_prose_bundle(bundle_dir):
    (bundle_dir / "specs").mkdir(parents=True, exist_ok=True)
    (bundle_dir / "README.md").write_text(
        "# Development Plan\n\n## Goal\n\nShip the approved implementation.\n",
        encoding="utf-8",
    )
    (bundle_dir / "proposal.md").write_text(
        "# Proposal\n\nApproved billing plan.\n", encoding="utf-8"
    )
    (bundle_dir / "tasks.md").write_text(
        "- [ ] Implement the approved billing plan\n", encoding="utf-8"
    )
    (bundle_dir / "design.md").write_text(
        design_doc("Keep the current billing queue."), encoding="utf-8"
    )
    (bundle_dir / "specs" / "01-billing-api.md").write_text(
        "# Spec 01: Billing API invoice summaries\n\n"
        "## Acceptance Criterion\n"
        "The API returns invoice summary data.\n\n"
        "## Requirement\n"
        "The API must return invoice summary data for existing accounts.\n\n"
        "## Verification\n"
        "- Tests cover invoice summary responses.\n",
        encoding="utf-8",
    )
    return bundle_dir


def main():
    for required in ["plugin.yaml", "plugin.py", "workflow.yaml", "README.md"]:
        if not (ROOT / required).exists():
            raise AssertionError(f"missing file: {required}")
    generate_output_schema = read_json(
        ROOT / "schemas/generate_plan.output.schema.json"
    )
    generate_input_schema = read_json(ROOT / "schemas/generate_plan.input.schema.json")
    verify_output_schema = read_json(ROOT / "schemas/verify.output.schema.json")
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
        if module.gate_decision_feedback(payload) != ("", "", ""):
            raise AssertionError("non-revision gate decisions must not create feedback")
    attributed_feedback = module.gate_decision_feedback(
        {
            "gate_decision": {
                "schema": "transist.gate_decision.v1",
                "run_id": "demo",
                "gate_id": "review",
                "step_id": "review",
                "decision": "request_changes",
                "actor": "alice",
                "actor_source": "flag",
                "reason": "tighten the plan",
                "decided_at_ms": 1,
            }
        }
    )
    if attributed_feedback != ("request_changes", "tighten the plan", "alice (flag)"):
        raise AssertionError("request-change feedback must preserve actor attribution")
    try:
        validate_json_schema(
            generate_input_schema["properties"]["gate_decision"],
            {"schema": "transist.gate_decision.v1", "decision": "request_changes"},
            schema_path=ROOT / "schemas/generate_plan.input.schema.json",
        )
    except AssertionError:
        pass
    else:
        raise AssertionError("schema must reject incomplete request-change decisions")

    repo_dir = create_repo()
    copilot_session_state_dir = pathlib.Path(
        tempfile.mkdtemp(prefix="openspec-copilot-session-state-")
    )
    runtime_root = pathlib.Path(tempfile.mkdtemp(prefix="openspec-runtime-"))
    runtime_env = {
        "COPILOT_HOME": str(runtime_root / "copilot"),
        "COPILOT_CONFIG_DIR": str(runtime_root / "copilot"),
        "COPILOT_SESSION_STATE_DIR": str(copilot_session_state_dir),
        "XDG_CACHE_HOME": str(runtime_root / "copilot" / "cache"),
        "TMPDIR": str(runtime_root / "tmp"),
    }
    for value in runtime_env.values():
        pathlib.Path(value).mkdir(parents=True, exist_ok=True)
    os.environ.update(runtime_env)
    manifest_text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    for declaration in (
        "COPILOT_HOME: runtime://temp/openspec/copilot",
        "COPILOT_CONFIG_DIR: runtime://temp/openspec/copilot",
        "COPILOT_SESSION_STATE_DIR: runtime://temp/openspec/copilot/session-state",
        "XDG_CACHE_HOME: runtime://temp/openspec/copilot/cache",
        "TMPDIR: runtime://temp/openspec/tmp",
    ):
        if declaration not in manifest_text:
            raise AssertionError(f"missing runtime declaration: {declaration}")
    plan_request = app_invoke_request(
        "generate_plan",
        {
            "change_name": "PROJ-123 Improve Billing",
            "prompt": "Create a development plan.",
            "context_files": [],
            "gate_decision": {
                "schema": "transist.gate_decision.v1",
                "run_id": "demo",
                "gate_id": "plan-review",
                "step_id": "review",
                "decision": "request_changes",
                "reason": "tighten the plan",
                "decided_at_ms": 1,
            },
            "feedback_context_mode": "focused",
            "response_file": "run://openspec-tests/plan.md",
            "change_metadata_file": "run://openspec-tests/openspec_change.json",
            "backend": "gh-copilot",
            "timeout_ms": 42000,
            "skills_rendered": (
                "# Workflow skills\n\nStay within the approved workflow surface.\n"
            ),
        },
    )
    plan_request["paths"]["workspace_dir"] = str(repo_dir)
    gh_prompts = []
    gh_commands = []
    gh_command_cwds = []
    gh_envs = []

    def generate_run(command, **kwargs):
        if command[0] == "gh":
            gh_commands.append(list(command))
            gh_command_cwds.append(kwargs["cwd"])
            gh_envs.append(kwargs["env"])
            gh_prompts.append(command[4])
            # Simulate Copilot persisting a session-state shutdown event for the
            # working directory the plugin invoked it in.
            write_copilot_session(
                pathlib.Path(kwargs["env"]["COPILOT_SESSION_STATE_DIR"]),
                session_id="openspec-session-1",
                cwd=kwargs["cwd"],
                model="openai/gpt-5.1",
                input_tokens=320,
                output_tokens=84,
            )
            payload = {
                "summary_markdown": (
                    "1. Goal\n"
                    "2. Constraints and assumptions\n"
                    "3. Implementation plan\n"
                    "4. Verification plan\n"
                    "5. Risks and open questions\n"
                ),
                "proposal_markdown": "# Proposal\n\nImprove billing flows.\n",
                "tasks_markdown": "- [ ] Update API\n- [ ] Add tests\n",
                "design_markdown": design_doc("Keep the current queue shape."),
                "specs": [
                    {
                        "capability": "billing-api",
                        "deltas": [
                            {
                                "type": "ADDED",
                                "requirements": [
                                    {
                                        "title": (
                                            "Billing API supports invoice summaries"
                                        ),
                                        "body_markdown": (
                                            "The billing API SHALL return invoice"
                                            " summary data for an existing"
                                            " account.\nThe response MUST preserve the"
                                            " current account scoping rules."
                                        ),
                                        "scenarios": [
                                            {
                                                "title": (
                                                    "Invoice summary is requested for"
                                                    " an existing account"
                                                ),
                                                "body_markdown": (
                                                    "- WHEN a client requests invoice"
                                                    " summary for an existing"
                                                    " account\n- THEN the API returns"
                                                    " the matching invoice summary data"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        plan_request,
        [
            mock.patch.object(module, "run_process", side_effect=generate_run),
            mock.patch.dict(
                module.os.environ,
                {"COPILOT_SESSION_STATE_DIR": str(copilot_session_state_dir)},
                clear=False,
            ),
        ],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "generate_plan smoke path should not fail the plugin process"
        )
    _, generate_result = assert_app_frames(frames, "openspec")
    if generate_result["status"] != "success":
        raise AssertionError("expected successful generate_plan result")
    if generate_result.get("usage") != {
        "input_tokens": 320,
        "output_tokens": 84,
        "total_tokens": 404,
        "model": "openai/gpt-5.1",
    }:
        raise AssertionError(
            "unexpected openspec generate_plan usage payload:"
            f" {generate_result.get('usage')}"
        )
    usage_hint_names = [
        hint.get("name") for hint in generate_result.get("artifact_hints", [])
    ]
    if "usage.json" not in usage_hint_names:
        raise AssertionError(
            "expected usage.json artifact hint when backend usage is available"
        )
    validate_json_schema(generate_output_schema, generate_result["output"])
    plan_path = (
        pathlib.Path(plan_request["paths"]["run_root"]) / "openspec-tests" / "plan.md"
    )
    metadata_path = (
        pathlib.Path(plan_request["paths"]["run_root"])
        / "openspec-tests"
        / "openspec_change.json"
    )
    if "Goal" not in plan_path.read_text(encoding="utf-8"):
        raise AssertionError("expected human-facing plan artifact")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata["backend"] != "gh-copilot":
        raise AssertionError("expected openspec_change.json metadata")
    change_dir = repo_dir / "openspec" / "changes" / "proj-123-improve-billing"
    if not (change_dir / "proposal.md").exists():
        raise AssertionError(
            "expected proposal.md written into repo-local OpenSpec change"
        )
    if not (change_dir / "design.md").exists():
        raise AssertionError(
            "expected design.md written into repo-local OpenSpec change"
        )
    old_spec_path = change_dir / "specs" / "billing-api" / "spec.md"
    if not old_spec_path.exists():
        raise AssertionError("expected spec delta file")
    if "--add-dir" in gh_commands[0]:
        raise AssertionError(
            "default generate_plan should not pass repo-wide --add-dir context"
        )
    if gh_command_cwds[0] == str(repo_dir):
        raise AssertionError(
            "default generate_plan should not run the backend inside the target repo"
        )
    default_context_dir = pathlib.Path(gh_command_cwds[0])
    if not default_context_dir.is_dir():
        raise AssertionError(
            "default generate_plan should create an accessible isolated context"
            " directory"
        )
    if not str(default_context_dir).startswith(
        str(pathlib.Path(plan_request["paths"]["artifacts_dir"]))
    ):
        raise AssertionError(
            "default generate_plan should keep isolated context inside the artifact"
            " directory"
        )
    child_copilot_dir = pathlib.Path(gh_envs[0]["COPILOT_CONFIG_DIR"])
    if child_copilot_dir == repo_dir or repo_dir in child_copilot_dir.parents:
        raise AssertionError(
            "expected the Copilot runtime directory outside the workspace"
        )
    if gh_envs[0]["TMPDIR"] != runtime_env["TMPDIR"]:
        raise AssertionError("expected declared private TMPDIR")
    if "# Workflow skills" not in gh_prompts[0]:
        raise AssertionError("expected rendered skills in composed generate prompt")
    hint_names = [
        hint.get("name") for hint in generate_result.get("artifact_hints", [])
    ]
    if "design.md" not in hint_names:
        raise AssertionError("expected design.md artifact hint")
    if module.artifact_hint("specs/billing-api/spec.md", "")["name"] not in hint_names:
        raise AssertionError("expected flattened spec artifact hint name")
    if any("/" in str(name) or "\\" in str(name) for name in hint_names):
        raise AssertionError("artifact hint names must be filesystem-safe")

    with tempfile.TemporaryDirectory(prefix="openspec-contained-run-") as tmpdir:
        tmpdir_path = pathlib.Path(tmpdir)
        root_run = tmpdir_path / "run_demo"
        child_run = root_run / "contained_runs" / "run_demo__plan_loop"
        planning_context = (
            root_run / "demo_plan_delivery" / "DEMO-229" / "planning_context.md"
        )
        planning_context.parent.mkdir(parents=True, exist_ok=True)
        planning_context.write_text(
            "# Ticket\n\n"
            "- key: DEMO-229\n"
            "- summary: Normalize retry labels in the sample billing handler\n",
            encoding="utf-8",
        )
        contained_run_request = app_invoke_request(
            "generate_plan",
            {
                "_workspace_dir": str(repo_dir),
                "change_name": "DEMO-229 Contained Context",
                "prompt": "Create a development plan.",
                "context_files": [
                    "run://demo_plan_delivery/DEMO-229/planning_context.md",
                ],
                "response_file": "run://demo_plan_delivery/DEMO-229/plan.md",
                "change_metadata_file": (
                    "run://demo_plan_delivery/DEMO-229/openspec_change.json"
                ),
                "backend": "gh-copilot",
            },
        )
        contained_run_request["paths"]["run_root"] = str(child_run)
        contained_prompts = []

        def contained_context_run(command, **kwargs):
            if command[0] == "gh":
                contained_prompts.append(command[4])
                payload = {
                    "summary_markdown": (
                        "1. Goal\n"
                        "2. Constraints and assumptions\n"
                        "3. Implementation plan\n"
                        "4. Verification plan\n"
                        "5. Risks and open questions\n"
                    ),
                    "proposal_markdown": "# Proposal\n\nContained-run billing plan.\n",
                    "tasks_markdown": "- [ ] Preserve contained-run planning context\n",
                    "design_markdown": design_doc(
                        "Contained-run context should resolve from the root run."
                    ),
                    "specs": [
                        {
                            "capability": "billing-contained-context",
                            "deltas": [
                                {
                                    "type": "ADDED",
                                    "requirements": [
                                        {
                                            "title": (
                                                "Contained runs keep root-run context"
                                                " files"
                                            ),
                                            "body_markdown": (
                                                "The plugin SHALL resolve run://"
                                                " context files from the root run even"
                                                " when generate_plan executes inside a"
                                                " contained run."
                                            ),
                                            "scenarios": [
                                                {
                                                    "title": (
                                                        "contained run uses a root-run"
                                                        " planning brief"
                                                    ),
                                                    "body_markdown": (
                                                        "- WHEN a contained plan loop"
                                                        " references a root-run"
                                                        " planning brief\n- THEN the"
                                                        " composed backend prompt"
                                                        " includes that brief"
                                                    ),
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
                return completed_process(
                    command, returncode=0, stdout=json.dumps(payload), stderr=""
                )
            if command[0] == "openspec":
                return completed_process(
                    command, returncode=0, stdout="validated\n", stderr=""
                )
            raise AssertionError(f"unexpected command: {command}")

        exit_code, frames, stderr = run_process_plugin_main(
            module,
            contained_run_request,
            [
                mock.patch.object(
                    module, "run_process", side_effect=contained_context_run
                )
            ],
        )
        if exit_code != 0 or stderr:
            raise AssertionError(
                "contained-run context resolution should not fail the plugin process"
            )
        _, contained_result = assert_app_frames(frames, "openspec")
        if contained_result["status"] != "success":
            raise AssertionError("expected successful contained-run context result")
        if "# Context: planning_context.md" not in contained_prompts[0]:
            raise AssertionError(
                "expected contained-run planning_context.md to be included in the"
                " backend prompt"
            )
        if (
            "Normalize retry labels in the sample billing handler"
            not in contained_prompts[0]
        ):
            raise AssertionError(
                "expected contained-run planning brief contents in the backend prompt"
            )
        if not (root_run / "demo_plan_delivery" / "DEMO-229" / "plan.md").exists():
            raise AssertionError(
                "expected run:// response_file to write under the root run from a"
                " contained run"
            )

    full_context_request = app_invoke_request(
        "generate_plan",
        {
            "_workspace_dir": str(repo_dir),
            "change_name": "PROJ-123 Improve Billing Full Context",
            "prompt": "Create a development plan.",
            "repo_context_mode": "full",
            "backend": "gh-copilot",
        },
    )
    full_context_commands = []
    full_context_cwds = []
    full_context_copilot_dirs = []
    full_context_temp_dirs = []
    full_context_session_state_dirs = []

    def full_context_run(command, **kwargs):
        if command[0] == "gh":
            full_context_commands.append(list(command))
            full_context_cwds.append(kwargs["cwd"])
            child_copilot_dir = pathlib.Path(kwargs["env"]["COPILOT_CONFIG_DIR"])
            if (
                pathlib.Path(kwargs["env"]["COPILOT_HOME"]) != child_copilot_dir
                or not child_copilot_dir.is_dir()
            ):
                raise AssertionError(
                    "gh copilot should receive an existing isolated config directory"
                )
            full_context_copilot_dirs.append(child_copilot_dir)
            child_temp_dir = pathlib.Path(kwargs["env"]["TMPDIR"])
            if (
                child_temp_dir != pathlib.Path(runtime_env["TMPDIR"])
                or not child_temp_dir.is_dir()
            ):
                raise AssertionError(
                    "gh copilot should receive the declared temporary directory"
                )
            full_context_temp_dirs.append(child_temp_dir)
            child_session_state_dir = pathlib.Path(
                kwargs["env"]["COPILOT_SESSION_STATE_DIR"]
            )
            if (
                child_session_state_dir
                != pathlib.Path(runtime_env["COPILOT_SESSION_STATE_DIR"])
                or not child_session_state_dir.is_dir()
            ):
                raise AssertionError(
                    "gh copilot should receive the declared session state directory"
                )
            full_context_session_state_dirs.append(child_session_state_dir)
            payload = {
                "summary_markdown": (
                    "1. Goal\n"
                    "2. Constraints and assumptions\n"
                    "3. Implementation plan\n"
                    "4. Verification plan\n"
                    "5. Risks and open questions\n"
                ),
                "proposal_markdown": "# Proposal\n\nFull-context billing plan.\n",
                "tasks_markdown": "- [ ] Reuse the existing repo-wide context path\n",
                "design_markdown": design_doc(
                    "Full-context planning keeps the existing repo-wide backend"
                    " visibility."
                ),
                "specs": [
                    {
                        "capability": "billing-full-context",
                        "deltas": [
                            {
                                "type": "ADDED",
                                "requirements": [
                                    {
                                        "title": "Full repo context remains opt-in",
                                        "body_markdown": (
                                            "The plugin SHALL preserve repo-wide"
                                            " context only when repo_context_mode is"
                                            " full."
                                        ),
                                        "scenarios": [
                                            {
                                                "title": "repo_context_mode is full",
                                                "body_markdown": (
                                                    "- WHEN full repo context is"
                                                    " explicitly requested\n- THEN the"
                                                    " backend receives the repo root as"
                                                    " ambient context"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        full_context_request,
        [mock.patch.object(module, "run_process", side_effect=full_context_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "full repo context path should not fail the plugin process"
        )
    _, full_context_result = assert_app_frames(frames, "openspec")
    if full_context_result["status"] != "success":
        raise AssertionError("expected successful full repo context result")
    full_context_cmd = full_context_commands[0]
    add_dir_index = (
        full_context_cmd.index("--add-dir") if "--add-dir" in full_context_cmd else None
    )
    if add_dir_index is None or full_context_cmd[add_dir_index + 1] != str(repo_dir):
        raise AssertionError("repo_context_mode=full should pass --add-dir repo_root")
    if full_context_cwds[0] != str(repo_dir):
        raise AssertionError(
            "repo_context_mode=full should run the backend from the repo root"
        )
    full_context_workspace_dir = pathlib.Path(
        full_context_request["paths"]["workspace_dir"]
    )
    if (
        full_context_copilot_dirs[0] == full_context_workspace_dir
        or full_context_workspace_dir in full_context_copilot_dirs[0].parents
    ):
        raise AssertionError(
            "gh copilot config directory should be outside the workspace"
        )
    for path in (
        full_context_copilot_dirs[0],
        full_context_temp_dirs[0],
        full_context_session_state_dirs[0],
    ):
        if not path.exists():
            raise AssertionError(
                "runtime-owned temporary directories remain available for the"
                " invocation"
            )

    paths_context_request = app_invoke_request(
        "generate_plan",
        {
            "_workspace_dir": str(repo_dir),
            "change_name": "PROJ-124 Scoped Repo Context",
            "prompt": "Create a development plan.",
            "repo_context_mode": "paths",
            "repo_context_paths": [
                "services/billing",
                "docs/planning-context.md",
            ],
            "backend": "gh-copilot",
        },
    )
    paths_context_commands = []
    paths_context_cwds = []

    def paths_context_run(command, **kwargs):
        if command[0] == "gh":
            paths_context_commands.append(list(command))
            paths_context_cwds.append(kwargs["cwd"])
            payload = {
                "summary_markdown": (
                    "1. Goal\n"
                    "2. Constraints and assumptions\n"
                    "3. Implementation plan\n"
                    "4. Verification plan\n"
                    "5. Risks and open questions\n"
                ),
                "proposal_markdown": "# Proposal\n\nScoped-context billing plan.\n",
                "tasks_markdown": (
                    "- [ ] Keep repo context limited to the requested paths\n"
                ),
                "design_markdown": design_doc(
                    "Scoped planning stages only the requested repo paths for backend"
                    " visibility."
                ),
                "specs": [
                    {
                        "capability": "billing-scoped-context",
                        "deltas": [
                            {
                                "type": "ADDED",
                                "requirements": [
                                    {
                                        "title": "Path-scoped repo context is isolated",
                                        "body_markdown": (
                                            "The plugin SHALL expose only the requested"
                                            " repo_context_paths to the planning"
                                            " backend."
                                        ),
                                        "scenarios": [
                                            {
                                                "title": "repo_context_mode is paths",
                                                "body_markdown": (
                                                    "- WHEN focused repo paths are"
                                                    " supplied\n- THEN unrelated repo"
                                                    " files stay outside the"
                                                    " backend-visible context"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        paths_context_request,
        [mock.patch.object(module, "run_process", side_effect=paths_context_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "paths repo context path should not fail the plugin process"
        )
    _, paths_context_result = assert_app_frames(frames, "openspec")
    if paths_context_result["status"] != "success":
        raise AssertionError("expected successful paths repo context result")
    paths_context_cmd = paths_context_commands[0]
    add_dir_indices = [
        index for index, value in enumerate(paths_context_cmd) if value == "--add-dir"
    ]
    if len(add_dir_indices) != 1:
        raise AssertionError(
            "repo_context_mode=paths should stage a single focused repo context root"
        )
    staged_repo_context = pathlib.Path(paths_context_cmd[add_dir_indices[0] + 1])
    if staged_repo_context == repo_dir:
        raise AssertionError(
            "repo_context_mode=paths should not expose the full repo root"
        )
    if paths_context_cwds[0] != str(staged_repo_context):
        raise AssertionError(
            "repo_context_mode=paths should run the backend from the staged repo"
            " context root"
        )
    if sorted(path.name for path in staged_repo_context.iterdir()) != [
        "docs",
        "services",
    ]:
        raise AssertionError(
            "repo_context_mode=paths should stage only the requested top-level repo"
            " paths"
        )
    if not (staged_repo_context / "services" / "billing" / "handler.py").exists():
        raise AssertionError("expected requested service path in staged repo context")
    if not (staged_repo_context / "docs" / "planning-context.md").exists():
        raise AssertionError("expected requested file path in staged repo context")
    if (staged_repo_context / "openspec").exists():
        raise AssertionError(
            "repo_context_mode=paths should exclude unrelated repo directories"
        )

    # ---- target_repo_path: gh-copilot exposes target as additional --add-dir
    target_repo_dir = pathlib.Path(tempfile.mkdtemp(prefix="openspec-target-repo-"))
    (target_repo_dir / "lib").mkdir()
    (target_repo_dir / "lib" / "billing.py").write_text(
        "def total(): return 0\n", encoding="utf-8"
    )
    target_repo_request = app_invoke_request(
        "generate_plan",
        {
            "_workspace_dir": str(repo_dir),
            "change_name": "PROJ-125 Target Repo Reference",
            "prompt": "Create a development plan.",
            "target_repo_path": str(target_repo_dir),
            "backend": "gh-copilot",
        },
    )
    target_repo_commands = []
    target_repo_prompts = []

    def target_repo_run(command, **kwargs):
        if command[0] == "gh":
            target_repo_commands.append(list(command))
            target_repo_prompts.append(command[4])
            payload = {
                "summary_markdown": (
                    "1. Goal\n"
                    "2. Constraints and assumptions\n"
                    "3. Implementation plan\n"
                    "4. Verification plan\n"
                    "5. Risks and open questions\n"
                ),
                "proposal_markdown": "# Proposal\n\nTarget-repo-aware billing plan.\n",
                "tasks_markdown": "- [ ] Reference target repo source layout\n",
                "design_markdown": design_doc(
                    "Plan grounds decisions in the supplied target reference repo."
                ),
                "specs": [
                    {
                        "capability": "billing-target-ref",
                        "deltas": [
                            {
                                "type": "ADDED",
                                "requirements": [
                                    {
                                        "title": (
                                            "Target reference repo is exposed read-only"
                                        ),
                                        "body_markdown": (
                                            "The plugin SHALL expose target_repo_path"
                                            " to the backend as read-only context."
                                        ),
                                        "scenarios": [
                                            {
                                                "title": "target_repo_path is set",
                                                "body_markdown": (
                                                    "- WHEN target_repo_path is"
                                                    " provided\n- THEN the backend can"
                                                    " read the target repo as"
                                                    " additional context"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        target_repo_request,
        [mock.patch.object(module, "run_process", side_effect=target_repo_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("target_repo_path path should not fail the plugin process")
    _, target_repo_result = assert_app_frames(frames, "openspec")
    if target_repo_result["status"] != "success":
        raise AssertionError("expected successful target_repo_path result")
    target_repo_cmd = target_repo_commands[0]
    add_dir_values = [
        target_repo_cmd[index + 1]
        for index, value in enumerate(target_repo_cmd)
        if value == "--add-dir"
    ]
    if str(target_repo_dir) not in add_dir_values:
        raise AssertionError(
            "target_repo_path should be passed as --add-dir to gh-copilot"
        )
    if "# Target reference repository" not in target_repo_prompts[0]:
        raise AssertionError(
            "backend prompt should mention the Target reference repository preamble"
        )
    if str(target_repo_dir) not in target_repo_prompts[0]:
        raise AssertionError(
            "backend prompt should include the resolved target_repo_path"
        )

    # ---- target_repo_path: invalid (non-absolute) is rejected
    invalid_target_request = app_invoke_request(
        "generate_plan",
        {
            "_workspace_dir": str(repo_dir),
            "change_name": "PROJ-126 Bad Target",
            "prompt": "Create a development plan.",
            "target_repo_path": "relative/path",
            "backend": "gh-copilot",
        },
    )

    def reject_run(command, **kwargs):
        raise AssertionError(
            "backend must not be invoked when target_repo_path is invalid"
        )

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        invalid_target_request,
        [mock.patch.object(module, "run_process", side_effect=reject_run)],
    )
    if exit_code != 0:
        raise AssertionError(
            "plugin process should not crash on invalid target_repo_path"
        )
    _, invalid_target_result = assert_app_frames(frames, "openspec")
    if invalid_target_result["status"] != "failed":
        raise AssertionError(
            "expected failed status when target_repo_path is non-absolute"
        )

    canonical_repo = create_repo(with_openspec=False)
    canonical_plan_dir = (
        canonical_repo
        / "backend-development-plans"
        / "plans"
        / "2026"
        / "04"
        / "PROJ-901"
    )
    canonical_request = app_invoke_request(
        "generate_plan",
        {
            "_workspace_dir": str(canonical_repo),
            "change_name": "PROJ-901 Canonical Plan",
            "prompt": "Create a development plan.",
            "canonical_plan_dir": str(canonical_plan_dir),
            "response_file": "run://openspec-tests/canonical-plan.md",
            "change_metadata_file": (
                "run://openspec-tests/canonical-openspec-change.json"
            ),
            "backend": "gh-copilot",
        },
    )

    def canonical_generate_run(command, **kwargs):
        if command[0] == "gh":
            payload = {
                "summary_markdown": (
                    "1. Goal\n"
                    "2. Constraints and assumptions\n"
                    "3. Implementation plan\n"
                    "4. Verification plan\n"
                    "5. Risks and open questions\n"
                ),
                "proposal_markdown": "# Proposal\n\nCanonical billing plan.\n",
                "tasks_markdown": "- [ ] Export the canonical plan bundle\n",
                "design_markdown": design_doc("Canonical queue handling."),
                "specs": [
                    {
                        "capability": "billing-api",
                        "deltas": [
                            {
                                "type": "ADDED",
                                "requirements": [
                                    {
                                        "title": (
                                            "Canonical bundle export stays"
                                            " validator-safe"
                                        ),
                                        "body_markdown": (
                                            "The canonical plan export SHALL preserve a"
                                            " valid OpenSpec change bundle."
                                        ),
                                        "scenarios": [
                                            {
                                                "title": (
                                                    "The wrapper exports a canonical"
                                                    " plan bundle"
                                                ),
                                                "body_markdown": (
                                                    "- WHEN canonical_plan_dir is"
                                                    " set\n- THEN the exported bundle"
                                                    " remains valid for OpenSpec"
                                                    " validation"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if command[0] == "openspec":
            staged_change_dir = (
                pathlib.Path(kwargs["cwd"])
                / "openspec"
                / "changes"
                / "proj-901-canonical-plan"
            )
            if kwargs["cwd"] == str(canonical_repo):
                raise AssertionError(
                    "canonical export should validate in a transient workspace, not the"
                    " repo root"
                )
            if not (staged_change_dir / "proposal.md").exists():
                raise AssertionError(
                    "expected transient OpenSpec change files before validation"
                )
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        canonical_request,
        [mock.patch.object(module, "run_process", side_effect=canonical_generate_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("canonical export path should not fail the plugin process")
    _, canonical_result = assert_app_frames(frames, "openspec")
    if canonical_result["status"] != "success":
        raise AssertionError("expected successful canonical export result")
    validate_json_schema(generate_output_schema, canonical_result["output"])
    if canonical_result["output"]["canonical_plan_dir"] != str(canonical_plan_dir):
        raise AssertionError("expected canonical plan dir in generate_plan output")
    if not (canonical_plan_dir / "README.md").exists():
        raise AssertionError(
            "expected README.md exported into the canonical plan bundle"
        )
    if not (canonical_plan_dir / "design.md").exists():
        raise AssertionError(
            "expected design.md exported into the canonical plan bundle"
        )
    if not (canonical_plan_dir / "specs" / "billing-api" / "spec.md").exists():
        raise AssertionError("expected specs exported into the canonical plan bundle")
    if (canonical_repo / "openspec").exists():
        raise AssertionError(
            "canonical export should not require repo-local openspec initialization"
        )

    plan_request["input"]["gate_decision"] = {
        "schema": "transist.gate_decision.v1",
        "run_id": "demo",
        "gate_id": "plan-review",
        "step_id": "review",
        "decision": "request_changes",
        "reason": "tighten verification guidance",
        "decided_at_ms": 1,
    }
    revision_prompts = []

    def revision_run(command, **kwargs):
        if command[0] == "gh":
            revision_prompts.append(command[4])
            payload = {
                "summary_markdown": (
                    "1. Goal\n"
                    "2. Constraints and assumptions\n"
                    "3. Implementation plan\n"
                    "4. Verification plan\n"
                    "5. Risks and open questions\n\n"
                    "Revised plan details.\n"
                ),
                "proposal_markdown": "# Proposal\n\nRevised billing plan.\n",
                "tasks_markdown": "- [ ] Tighten verification\n",
                "design_markdown": design_doc(
                    "Revised verification and rollout guidance remain simple but"
                    " explicit."
                ),
                "specs": [
                    {
                        "capability": "billing-api",
                        "deltas": [
                            {
                                "type": "MODIFIED",
                                "requirements": [
                                    {
                                        "title": (
                                            "Billing verification remains aligned with"
                                            " the approved scope"
                                        ),
                                        "body_markdown": (
                                            "The billing change SHALL include"
                                            " verification guidance that matches the"
                                            " approved plan."
                                        ),
                                        "scenarios": [
                                            {
                                                "title": (
                                                    "Verification guidance is revised"
                                                ),
                                                "body_markdown": (
                                                    "- WHEN the plan is regenerated"
                                                    " after review feedback\n- THEN the"
                                                    " verification section reflects the"
                                                    " requested tightening"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        plan_request,
        [mock.patch.object(module, "run_process", side_effect=revision_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("revision path should not fail the plugin process")
    _, revision_result = assert_app_frames(frames, "openspec")
    if revision_result["status"] != "success":
        raise AssertionError("expected successful revision result")
    if "# Operator feedback" not in revision_prompts[0]:
        raise AssertionError("expected operator feedback in revision prompt")
    if "# Current draft: plan.md" not in revision_prompts[0]:
        raise AssertionError("expected current draft in revision prompt")
    if "Existing OpenSpec file: proposal.md" in revision_prompts[0]:
        raise AssertionError(
            "focused feedback replay should omit prior OpenSpec change files"
        )

    full_feedback_request = json.loads(json.dumps(plan_request))
    full_feedback_request["input"]["feedback_context_mode"] = "full"
    full_feedback_prompts = []

    def full_feedback_run(command, **kwargs):
        if command[0] == "gh":
            full_feedback_prompts.append(command[4])
            payload = {
                "summary_markdown": (
                    "1. Goal\n"
                    "2. Constraints and assumptions\n"
                    "3. Implementation plan\n"
                    "4. Verification plan\n"
                    "5. Risks and open questions\n\n"
                    "Full replay revision details.\n"
                ),
                "proposal_markdown": "# Proposal\n\nFull replay billing plan.\n",
                "tasks_markdown": (
                    "- [ ] Reconcile feedback with the existing change tree\n"
                ),
                "design_markdown": design_doc(
                    "Full replay revisions can inspect the prior OpenSpec change files"
                    " when explicitly requested."
                ),
                "specs": [
                    {
                        "capability": "billing-full-replay",
                        "deltas": [
                            {
                                "type": "MODIFIED",
                                "requirements": [
                                    {
                                        "title": (
                                            "Full replay includes prior OpenSpec files"
                                        ),
                                        "body_markdown": (
                                            "The plugin SHALL replay existing OpenSpec"
                                            " change files only when"
                                            " feedback_context_mode is full."
                                        ),
                                        "scenarios": [
                                            {
                                                "title": (
                                                    "feedback_context_mode is full"
                                                ),
                                                "body_markdown": (
                                                    "- WHEN full feedback replay is"
                                                    " requested\n- THEN the prompt"
                                                    " includes the current draft and"
                                                    " existing OpenSpec files"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        full_feedback_request,
        [mock.patch.object(module, "run_process", side_effect=full_feedback_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "full feedback replay path should not fail the plugin process"
        )
    _, full_feedback_result = assert_app_frames(frames, "openspec")
    if full_feedback_result["status"] != "success":
        raise AssertionError("expected successful full feedback replay result")
    if "# Operator feedback" not in full_feedback_prompts[0]:
        raise AssertionError("expected operator feedback in full replay prompt")
    if "# Current draft: plan.md" not in full_feedback_prompts[0]:
        raise AssertionError("expected current draft in full replay prompt")
    if "Existing OpenSpec file: proposal.md" not in full_feedback_prompts[0]:
        raise AssertionError(
            "full feedback replay should include prior OpenSpec change files"
        )

    plan_request["input"]["gate_decision"] = {
        "schema": "transist.gate_decision.v1",
        "run_id": "demo",
        "gate_id": "plan-review",
        "step_id": "review",
        "decision": "request_changes",
        "reason": "tighten verification guidance from history",
        "decided_at_ms": 2,
    }
    history_prompts = []

    def history_run(command, **kwargs):
        if command[0] == "gh":
            history_prompts.append(command[4])
            payload = {
                "summary_markdown": (
                    "1. Goal\n"
                    "2. Constraints and assumptions\n"
                    "3. Implementation plan\n"
                    "4. Verification plan\n"
                    "5. Risks and open questions\n\n"
                    "History-backed revision.\n"
                ),
                "proposal_markdown": "# Proposal\n\nHistory-backed billing plan.\n",
                "tasks_markdown": "- [ ] Apply history-backed feedback\n",
                "design_markdown": design_doc(
                    "History-backed revisions keep the design rationale synchronized"
                    " with operator feedback."
                ),
                "specs": [
                    {
                        "capability": "billing-api",
                        "deltas": [
                            {
                                "type": "MODIFIED",
                                "requirements": [
                                    {
                                        "title": (
                                            "History-backed feedback is applied when"
                                            " the active gate file is absent"
                                        ),
                                        "body_markdown": (
                                            "The plugin SHALL load the latest"
                                            " request-changes feedback from gate"
                                            " history."
                                        ),
                                        "scenarios": [
                                            {
                                                "title": (
                                                    "Latest request-changes history"
                                                    " entry is used"
                                                ),
                                                "body_markdown": (
                                                    "- WHEN the active gate file is"
                                                    " missing\n- THEN the latest"
                                                    " request-changes history entry"
                                                    " drives the revision prompt"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        plan_request,
        [mock.patch.object(module, "run_process", side_effect=history_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "history-backed revision path should not fail the plugin process"
        )
    _, history_result = assert_app_frames(frames, "openspec")
    if history_result["status"] != "success":
        raise AssertionError("expected successful history-backed revision result")
    if "# Operator feedback" not in history_prompts[0]:
        raise AssertionError(
            "expected history-backed operator feedback in revision prompt"
        )
    if "tighten verification guidance from history" not in history_prompts[0]:
        raise AssertionError("expected latest history feedback text in revision prompt")
    if "# Current draft: plan.md" not in history_prompts[0]:
        raise AssertionError(
            "expected current draft replay when history-backed feedback exists"
        )
    if "Existing OpenSpec file: proposal.md" in history_prompts[0]:
        raise AssertionError(
            "focused history-backed replay should omit existing OpenSpec change files"
        )

    feedback_only_request = json.loads(json.dumps(plan_request))
    feedback_only_request["input"]["feedback_context_mode"] = "feedback-only"
    feedback_only_prompts = []

    def feedback_only_run(command, **kwargs):
        if command[0] == "gh":
            feedback_only_prompts.append(command[4])
            payload = {
                "summary_markdown": (
                    "1. Goal\n"
                    "2. Constraints and assumptions\n"
                    "3. Implementation plan\n"
                    "4. Verification plan\n"
                    "5. Risks and open questions\n\n"
                    "Feedback-only revision.\n"
                ),
                "proposal_markdown": "# Proposal\n\nFeedback-only billing plan.\n",
                "tasks_markdown": "- [ ] Apply feedback-only revision\n",
                "design_markdown": design_doc(
                    "Feedback-only revisions still emit the required design artifact."
                ),
                "specs": [
                    {
                        "capability": "billing-api",
                        "deltas": [
                            {
                                "type": "MODIFIED",
                                "requirements": [
                                    {
                                        "title": (
                                            "Feedback-only mode omits prior draft"
                                            " replay"
                                        ),
                                        "body_markdown": (
                                            "The plugin SHALL support a feedback-only"
                                            " revision mode for plan regeneration."
                                        ),
                                        "scenarios": [
                                            {
                                                "title": (
                                                    "Feedback-only mode is enabled"
                                                ),
                                                "body_markdown": (
                                                    "- WHEN the revision request opts"
                                                    " into feedback-only mode\n- THEN"
                                                    " the prompt omits prior draft and"
                                                    " existing change artifact replay"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        feedback_only_request,
        [mock.patch.object(module, "run_process", side_effect=feedback_only_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "feedback-only revision path should not fail the plugin process"
        )
    _, feedback_only_result = assert_app_frames(frames, "openspec")
    if feedback_only_result["status"] != "success":
        raise AssertionError("expected successful feedback-only revision result")
    if "# Current draft: plan.md" in feedback_only_prompts[0]:
        raise AssertionError("feedback-only prompt should omit current draft replay")
    if "Existing OpenSpec file: proposal.md" in feedback_only_prompts[0]:
        raise AssertionError(
            "feedback-only prompt should omit prior OpenSpec artifact replay"
        )

    plan_request["input"]["gate_decision"] = {
        "schema": "transist.gate_decision.v1",
        "run_id": "demo",
        "gate_id": "plan-review",
        "step_id": "review",
        "decision": "request_changes",
        "reason": "redo: regenerate from the ticket only",
        "decided_at_ms": 3,
    }
    redo_prompts = []

    def redo_run(command, **kwargs):
        if command[0] == "gh":
            redo_prompts.append(command[4])
            payload = {
                "summary_markdown": (
                    "1. Goal\n"
                    "2. Constraints and assumptions\n"
                    "3. Implementation plan\n"
                    "4. Verification plan\n"
                    "5. Risks and open questions\n\n"
                    "Fresh plan.\n"
                ),
                "proposal_markdown": "# Proposal\n\nFresh proposal.\n",
                "tasks_markdown": "- [ ] Fresh tasks\n",
                "design_markdown": design_doc(
                    "Fresh plan restarts still include a minimal design artifact."
                ),
                "specs": [
                    {
                        "capability": "fresh-billing-flow",
                        "deltas": [
                            {
                                "type": "ADDED",
                                "requirements": [
                                    {
                                        "title": (
                                            "Fresh plan artifacts replace the previous"
                                            " draft"
                                        ),
                                        "body_markdown": (
                                            "The regenerated OpenSpec change SHALL"
                                            " replace the stale plan artifacts from the"
                                            " previous draft."
                                        ),
                                        "scenarios": [
                                            {
                                                "title": (
                                                    "Redo feedback requests a clean"
                                                    " restart"
                                                ),
                                                "body_markdown": (
                                                    "- WHEN operator feedback starts"
                                                    " with redo:\n- THEN the"
                                                    " regenerated change contains only"
                                                    " the fresh artifact set"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        plan_request,
        [mock.patch.object(module, "run_process", side_effect=redo_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("redo path should not fail the plugin process")
    _, redo_result = assert_app_frames(frames, "openspec")
    if redo_result["status"] != "success":
        raise AssertionError("expected successful redo result")
    if "# Current draft: plan.md" in redo_prompts[0]:
        raise AssertionError("redo prompt should not include current draft")
    if "Existing OpenSpec file: proposal.md" in redo_prompts[0]:
        raise AssertionError("redo prompt should not include previous change artifacts")
    if old_spec_path.exists():
        raise AssertionError("redo output should replace stale spec files")
    new_spec_path = (
        repo_dir
        / "openspec"
        / "changes"
        / "proj-123-improve-billing"
        / "specs"
        / "fresh-billing-flow"
        / "spec.md"
    )
    if not new_spec_path.exists():
        raise AssertionError("redo output should write the replacement spec delta")

    missing_design_request = app_invoke_request(
        "generate_plan",
        {
            "_workspace_dir": str(repo_dir),
            "change_name": "PROJ-554 Missing Design",
            "prompt": "Create a plan.",
            "backend": "gh-copilot",
        },
    )
    missing_design_commands = []

    def missing_design_run(command, **kwargs):
        missing_design_commands.append(command[0])
        if command[0] == "gh":
            payload = {
                "summary_markdown": (
                    "1. Goal\n"
                    "2. Constraints and assumptions\n"
                    "3. Implementation plan\n"
                    "4. Verification plan\n"
                    "5. Risks and open questions\n"
                ),
                "proposal_markdown": "# Proposal\n\nMissing design proposal.\n",
                "tasks_markdown": "- [ ] Missing design task\n",
                "design_markdown": "",
                "specs": [
                    {
                        "capability": "missing-design",
                        "deltas": [
                            {
                                "type": "ADDED",
                                "requirements": [
                                    {
                                        "title": (
                                            "Successful plans always include a design"
                                            " artifact"
                                        ),
                                        "body_markdown": (
                                            "The plugin SHALL reject planning responses"
                                            " that omit design_markdown."
                                        ),
                                        "scenarios": [
                                            {
                                                "title": (
                                                    "Backend omits the design artifact"
                                                ),
                                                "body_markdown": (
                                                    "- WHEN design_markdown is empty\n-"
                                                    " THEN the plugin fails validation"
                                                    " before openspec validate runs"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if command[0] == "openspec":
            raise AssertionError(
                "openspec validate should not run when design_markdown is missing"
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        missing_design_request,
        [mock.patch.object(module, "run_process", side_effect=missing_design_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "missing design regression should still produce normal APP output"
        )
    _, missing_design_result = assert_app_frames(frames, "openspec")
    if missing_design_result["status"] != "failed":
        raise AssertionError("expected failure when backend omits design_markdown")
    if missing_design_result["error"]["code"] != "validation_error":
        raise AssertionError(
            "expected validation_error when backend omits design_markdown"
        )
    if "design_markdown" not in missing_design_result["error"]["message"]:
        raise AssertionError(
            "expected the missing design_markdown field in the validation error"
        )
    if missing_design_commands != ["gh"]:
        raise AssertionError("expected missing design to fail before openspec validate")

    empty_specs_request = app_invoke_request(
        "generate_plan",
        {
            "_workspace_dir": str(repo_dir),
            "change_name": "PROJ-555 Missing Delta",
            "prompt": "Create a plan.",
            "backend": "gh-copilot",
        },
    )
    empty_specs_commands = []

    def empty_specs_run(command, **kwargs):
        empty_specs_commands.append(command[0])
        if command[0] == "gh":
            payload = {
                "summary_markdown": (
                    "1. Goal\n"
                    "2. Constraints and assumptions\n"
                    "3. Implementation plan\n"
                    "4. Verification plan\n"
                    "5. Risks and open questions\n"
                ),
                "proposal_markdown": "# Proposal\n\nInvalid proposal.\n",
                "tasks_markdown": "- [ ] Invalid tasks\n",
                "design_markdown": design_doc("Invalid plan design placeholder."),
                "specs": [],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if command[0] == "openspec":
            raise AssertionError(
                "openspec validate should not run when spec deltas are missing"
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        empty_specs_request,
        [mock.patch.object(module, "run_process", side_effect=empty_specs_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "empty specs regression should still produce normal APP output"
        )
    _, empty_specs_result = assert_app_frames(frames, "openspec")
    if empty_specs_result["status"] != "failed":
        raise AssertionError("expected failure when backend omits spec deltas")
    if empty_specs_result["error"]["code"] != "validation_error":
        raise AssertionError("expected validation_error when backend omits spec deltas")
    if empty_specs_commands != ["gh"]:
        raise AssertionError(
            "expected backend generation to fail before openspec validate"
        )

    prose_prefixed_request = app_invoke_request(
        "generate_plan",
        {
            "_workspace_dir": str(repo_dir),
            "change_name": "PROJ-556 Prefixed Response",
            "prompt": "Create a plan.",
            "backend": "gh-copilot",
        },
    )
    prose_prefixed_commands = []

    def prose_prefixed_run(command, **kwargs):
        prose_prefixed_commands.append(command[0])
        if command[0] == "gh":
            payload = {
                "summary_markdown": (
                    "1. Goal\n"
                    "2. Constraints and assumptions\n"
                    "3. Implementation plan\n"
                    "4. Verification plan\n"
                    "5. Risks and open questions\n"
                ),
                "proposal_markdown": "# Proposal\n\nPrefixed proposal.\n",
                "tasks_markdown": "- [ ] Prefixed tasks\n",
                "design_markdown": design_doc(
                    "Prefixed responses still carry a valid design artifact."
                ),
                "specs": [
                    {
                        "capability": "prefixed-response",
                        "deltas": [
                            {
                                "type": "ADDED",
                                "requirements": [
                                    {
                                        "title": (
                                            "Backend responses stay in the JSON"
                                            " envelope"
                                        ),
                                        "body_markdown": (
                                            "The backend response MUST begin with the"
                                            " top-level JSON object."
                                        ),
                                        "scenarios": [
                                            {
                                                "title": (
                                                    "A planning response is emitted"
                                                ),
                                                "body_markdown": (
                                                    "- WHEN the backend returns a plan"
                                                    " response\n- THEN it starts with"
                                                    " the JSON object and contains no"
                                                    " prose prefix"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            response = json.dumps(payload)
            return completed_process(
                command,
                returncode=0,
                stdout=(
                    "Planning notes first.\n\n`{:ok, conn}` comes from the failing code"
                    " path.\n\n"
                )
                + response[:-2],
                stderr="",
            )
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        prose_prefixed_request,
        [mock.patch.object(module, "run_process", side_effect=prose_prefixed_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "prefixed prose path should still produce normal APP output"
        )
    _, prose_prefixed_result = assert_app_frames(frames, "openspec")
    if prose_prefixed_result["status"] != "success":
        raise AssertionError(
            "expected success when backend prefixes prose before an otherwise valid"
            " JSON object"
        )
    if prose_prefixed_commands != ["gh", "openspec"]:
        raise AssertionError(
            "expected prefixed prose path to continue through openspec validate"
        )

    malformed_prefixed_request = app_invoke_request(
        "generate_plan",
        {
            "_workspace_dir": str(repo_dir),
            "change_name": "PROJ-557 Malformed Response",
            "prompt": "Create a plan.",
            "backend": "gh-copilot",
        },
    )
    malformed_prefixed_commands = []

    def malformed_prefixed_run(command, **kwargs):
        malformed_prefixed_commands.append(command[0])
        if command[0] == "gh":
            malformed = (
                'Planning notes first.\n\n{"summary_markdown":"1. Goal\\n2. Constraints'
                " and assumptions\\n3. Implementation plan\\n4. Verification plan\\n5."
                ' Risks and open questions\\n","proposal_markdown":"#'
                ' Proposal\\n\\nMalformed proposal.\\n","tasks_markdown":"- [ ]'
                ' Malformed task\\n","design_markdown":"# Design\\n\\nMalformed'
                ' design.\\n""specs":[{"capability":"malformed-response",'
                '"deltas":[{"type":"ADDED","requirements":[{"title":"Nested'
                " object must not be parsed as the top-level"
                ' payload","body_markdown":"The backend response MUST parse as a'
                " complete top-level JSON object before any nested objects are"
                ' considered.","scenarios":[{"title":"Outer JSON has a syntax'
                ' error","body_markdown":"- WHEN the outer JSON object is malformed\\n-'
                " THEN the plugin rejects the response instead of parsing a nested"
                ' object"}]}]}]}]}'
            )
            return completed_process(command, returncode=0, stdout=malformed, stderr="")
        if command[0] == "openspec":
            raise AssertionError(
                "openspec validate should not run when the top-level JSON is malformed"
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        malformed_prefixed_request,
        [mock.patch.object(module, "run_process", side_effect=malformed_prefixed_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "malformed prefixed regression should still produce normal APP output"
        )
    _, malformed_prefixed_result = assert_app_frames(frames, "openspec")
    if malformed_prefixed_result["status"] != "failed":
        raise AssertionError(
            "expected failure when the top-level JSON remains malformed after a prose"
            " prefix"
        )
    if malformed_prefixed_result["error"]["code"] != "validation_error":
        raise AssertionError("expected validation_error for malformed prefixed JSON")
    if "malformed top-level JSON" not in malformed_prefixed_result["error"]["message"]:
        raise AssertionError(
            "expected a malformed top-level JSON error for malformed prefixed responses"
        )
    if malformed_prefixed_commands != ["gh"]:
        raise AssertionError(
            "expected malformed prefixed responses to fail before openspec validate"
        )

    quoted_title_request = app_invoke_request(
        "generate_plan",
        {
            "_workspace_dir": str(repo_dir),
            "change_name": "PROJ-558 Quoted Title",
            "prompt": "Create a plan.",
            "backend": "gh-copilot",
        },
    )
    quoted_title_commands = []

    def quoted_title_run(command, **kwargs):
        quoted_title_commands.append(command[0])
        if command[0] == "gh":
            payload = {
                "summary_markdown": (
                    "1. Goal\n"
                    "2. Constraints and assumptions\n"
                    "3. Implementation plan\n"
                    'Ticket context: DEMO-186 ("Test ticket").\n'
                    "4. Verification plan\n"
                    "5. Risks and open questions\n"
                ),
                "proposal_markdown": "# Proposal\n\nHandle quoted titles safely.\n",
                "tasks_markdown": "- [ ] Repair quoted titles\n",
                "design_markdown": design_doc(
                    "Quoted ticket titles do not change the required design artifact."
                ),
                "specs": [
                    {
                        "capability": "quoted-ticket-title",
                        "deltas": [
                            {
                                "type": "ADDED",
                                "requirements": [
                                    {
                                        "title": (
                                            "Quoted ticket titles remain parseable"
                                        ),
                                        "body_markdown": (
                                            "The plugin SHALL recover backend JSON when"
                                            " markdown string values contain unescaped"
                                            " quotes around ticket titles."
                                        ),
                                        "scenarios": [
                                            {
                                                "title": (
                                                    "Ticket title is wrapped in quotes"
                                                ),
                                                "body_markdown": (
                                                    "- WHEN the backend returns a"
                                                    " markdown field with a quoted"
                                                    " ticket title\n- THEN the plugin"
                                                    " repairs the JSON string and"
                                                    " continues validation"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            malformed = json.dumps(payload).replace(
                '\\"Test ticket\\"', '"Test ticket"'
            )
            return completed_process(command, returncode=0, stdout=malformed, stderr="")
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        quoted_title_request,
        [mock.patch.object(module, "run_process", side_effect=quoted_title_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "quoted title regression should still produce normal APP output"
        )
    _, quoted_title_result = assert_app_frames(frames, "openspec")
    if quoted_title_result["status"] != "success":
        raise AssertionError(
            "expected success when quoted ticket titles are the only malformed JSON"
            " fragment"
        )
    validate_json_schema(generate_output_schema, quoted_title_result["output"])
    if '("Test ticket")' not in quoted_title_result["output"]["response_text"]:
        raise AssertionError(
            "expected repaired output to preserve the quoted ticket title"
        )
    if quoted_title_commands != ["gh", "openspec"]:
        raise AssertionError(
            "expected quoted title repair path to continue through openspec validate"
        )

    missing_repo = create_repo(with_openspec=False)
    missing_request = app_invoke_request(
        "generate_plan",
        {
            "_workspace_dir": str(missing_repo),
            "change_name": "PROJ-404 Missing",
            "prompt": "Create a plan.",
            "backend": "gh-copilot",
        },
    )
    exit_code, frames, stderr = run_process_plugin_main(module, missing_request)
    if exit_code != 0 or stderr:
        raise AssertionError(
            "missing openspec repo should still return a normal APP frame"
        )
    _, missing_result = assert_app_frames(frames, "openspec")
    if missing_result["error"]["code"] != "validation_error":
        raise AssertionError("expected validation_error when openspec/ is missing")

    approved_bundle_dir = write_canonical_bundle(
        pathlib.Path(tempfile.mkdtemp(prefix="openspec-approved-bundle-")) / "PROJ-123"
    )
    implementation_repo = create_repo(with_openspec=False)
    approved_bundle_prompts = []
    approved_bundle_request = app_invoke_request(
        "verify",
        {
            "_workspace_dir": str(implementation_repo),
            "change_name": "PROJ-123 Improve Billing",
            "prompt": "Validate the implementation.",
            "approved_plan_dir": str(approved_bundle_dir),
            "report_file": "run://openspec-tests/approved-bundle-validation-report.md",
            "backend": "gh-copilot",
            "fail_on_mismatch": True,
        },
        artifacts_dir=plan_request["paths"]["run_root"],
    )

    def approved_bundle_run(command, **kwargs):
        if command[0] == "openspec":
            staged_change_dir = (
                pathlib.Path(kwargs["cwd"])
                / "openspec"
                / "changes"
                / "proj-123-improve-billing"
            )
            if kwargs["cwd"] == str(implementation_repo):
                raise AssertionError(
                    "approved bundle verify should validate in a transient workspace"
                )
            if not (staged_change_dir / "README.md").exists():
                raise AssertionError(
                    "expected approved bundle staged into the transient workspace"
                )
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        if command[0] == "gh":
            approved_bundle_prompts.append(command[4])
            payload = {
                "passed": True,
                "report_markdown": (
                    "# Validation Result\n\n## Verdict\n\nPASS\n\n## Findings\n\n-"
                    " None.\n\n## Evidence\n\n- Checked the approved bundle and"
                    " implementation repo.\n\n## Next Steps\n\n- Proceed to commit.\n"
                ),
                "findings": [],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        approved_bundle_request,
        [mock.patch.object(module, "run_process", side_effect=approved_bundle_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "approved bundle verify path should not fail the plugin process"
        )
    _, approved_bundle_result = assert_app_frames(frames, "openspec")
    if approved_bundle_result["status"] != "success":
        raise AssertionError("expected successful approved bundle verify result")
    validate_json_schema(verify_output_schema, approved_bundle_result["output"])
    if approved_bundle_result["output"]["approved_plan_dir"] != str(
        approved_bundle_dir
    ):
        raise AssertionError("expected approved plan dir in verify output")
    if "# Approved plan bundle" not in approved_bundle_prompts[0]:
        raise AssertionError("expected approved bundle location in verify prompt")
    if "Approved OpenSpec file: README.md" not in approved_bundle_prompts[0]:
        raise AssertionError(
            "expected README.md from the approved plan bundle in verify prompt"
        )

    prose_bundle_dir = write_prose_bundle(
        pathlib.Path(tempfile.mkdtemp(prefix="openspec-approved-prose-bundle-"))
        / "PROJ-124"
    )
    prose_bundle_prompts = []
    prose_bundle_request = app_invoke_request(
        "verify",
        {
            "_workspace_dir": str(implementation_repo),
            "change_name": "PROJ-124 Prose Bundle",
            "prompt": "Validate the implementation against the approved prose plan.",
            "approved_plan_dir": str(prose_bundle_dir),
            "approved_plan_mode": "prose_bundle",
            "report_file": "run://openspec-tests/prose-bundle-validation-report.md",
            "backend": "gh-copilot",
            "fail_on_mismatch": True,
        },
        artifacts_dir=plan_request["paths"]["run_root"],
    )

    def prose_bundle_run(command, **kwargs):
        if command[0] == "openspec":
            raise AssertionError("prose bundle verify should skip openspec validate")
        if command[0] == "gh":
            prose_bundle_prompts.append(command[4])
            payload = {
                "passed": True,
                "report_markdown": (
                    "# Validation Result\n\n## Verdict\n\nPASS\n\n## Findings\n\n-"
                    " None.\n\n## Evidence\n\n- Checked the approved prose plan bundle"
                    " and implementation repo.\n\n## Next Steps\n\n- Proceed to"
                    " commit.\n"
                ),
                "findings": [],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        prose_bundle_request,
        [mock.patch.object(module, "run_process", side_effect=prose_bundle_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "prose bundle verify path should not fail the plugin process"
        )
    _, prose_bundle_result = assert_app_frames(frames, "openspec")
    if prose_bundle_result["status"] != "success":
        raise AssertionError("expected successful prose bundle verify result")
    validate_json_schema(verify_output_schema, prose_bundle_result["output"])
    if "# Approved plan interpretation" not in prose_bundle_prompts[0]:
        raise AssertionError(
            "expected prose bundle interpretation guidance in verify prompt"
        )
    if "Approved plan file: README.md" not in prose_bundle_prompts[0]:
        raise AssertionError(
            "expected README.md from the prose bundle in verify prompt"
        )
    if "Approved OpenSpec file:" in prose_bundle_prompts[0]:
        raise AssertionError(
            "expected prose bundle prompt labels instead of OpenSpec file labels"
        )

    verify_request = app_invoke_request(
        "verify",
        {
            "_workspace_dir": str(repo_dir),
            "change_name": "PROJ-123 Improve Billing",
            "prompt": "Validate the implementation.",
            "context_files": [
                "run://openspec-tests/plan.md",
                "run://openspec-tests/openspec_change.json",
            ],
            "report_file": "run://openspec-tests/validation_report.md",
            "backend": "gh-copilot",
            "fail_on_mismatch": True,
        },
        artifacts_dir=plan_request["paths"]["run_root"],
    )

    def verify_pass_run(command, **kwargs):
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        if command[0] == "gh":
            payload = {
                "passed": True,
                "report_markdown": (
                    "# Validation Result\n\n"
                    "## Verdict\n\nPASS\n\n"
                    "## Findings\n\n- None.\n\n"
                    "## Evidence\n\n- Checked the approved plan and change files.\n\n"
                    "## Next Steps\n\n- Proceed to commit.\n"
                ),
                "findings": [],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        verify_request,
        [mock.patch.object(module, "run_process", side_effect=verify_pass_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("verify pass path should not fail the plugin process")
    _, verify_pass_result = assert_app_frames(frames, "openspec")
    if verify_pass_result["status"] != "success":
        raise AssertionError("expected successful verify result")
    validate_json_schema(verify_output_schema, verify_pass_result["output"])
    report_path = (
        pathlib.Path(verify_request["paths"]["run_root"])
        / "openspec-tests"
        / "validation_report.md"
    )
    if "PASS" not in report_path.read_text(encoding="utf-8"):
        raise AssertionError("expected persisted validation report")

    def verify_fail_run(command, **kwargs):
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        if command[0] == "gh":
            payload = {
                "passed": False,
                "report_markdown": (
                    "# Validation Result\n\n"
                    "## Verdict\n\nFAIL\n\n"
                    "## Findings\n\n- Missing test coverage.\n\n"
                    "## Evidence\n\n- No regression test updated.\n\n"
                    "## Next Steps\n\n- Add the missing test.\n"
                ),
                "findings": [
                    {
                        "title": "Missing regression test",
                        "severity": "high",
                        "details": "No billing regression test changed.",
                    }
                ],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        verify_request,
        [mock.patch.object(module, "run_process", side_effect=verify_fail_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "verify failure path should still produce normal APP output"
        )
    _, verify_fail_result = assert_app_frames(frames, "openspec")
    validate_json_schema(verify_output_schema, verify_fail_result["output"])
    if verify_fail_result["status"] != "failed":
        raise AssertionError("expected failed status when verification does not pass")
    if verify_fail_result["error"]["code"] != "validation_error":
        raise AssertionError("expected validation_error error code")
    if "FAIL" not in report_path.read_text(encoding="utf-8"):
        raise AssertionError("expected failure report to be persisted")

    def verify_malformed_run(command, **kwargs):
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        if command[0] == "gh":
            malformed = (
                '{"passed": true, "report_markdown": "# Validation Result\\n\\n##'
                " Verdict\\n\\nPASS\\n\\n## Findings\\n\\n- None.\\n\\n##"
                " Evidence\\n\\n- Checked the approved plan.\\n\\n## Next Steps\\n\\n-"
                ' Proceed to commit.\\n" "findings": [{"title": "Nested finding",'
                ' "severity": "high", "details": "Should not be parsed alone"}]'
            )
            return completed_process(command, returncode=0, stdout=malformed, stderr="")
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        verify_request,
        [mock.patch.object(module, "run_process", side_effect=verify_malformed_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "verify malformed regression should still produce normal APP output"
        )
    _, verify_malformed_result = assert_app_frames(frames, "openspec")
    if verify_malformed_result["status"] != "failed":
        raise AssertionError("expected failed status for malformed verify JSON")
    if verify_malformed_result["error"]["code"] != "validation_error":
        raise AssertionError("expected validation_error for malformed verify JSON")
    if "malformed top-level JSON" not in verify_malformed_result["error"]["message"]:
        raise AssertionError(
            "expected malformed top-level JSON error for verify responses"
        )

    codex_request = app_invoke_request(
        "generate_plan",
        {
            "_workspace_dir": str(repo_dir),
            "change_name": "PROJ-777 Codex",
            "prompt": "Create a plan with Codex.",
            "backend": "codex",
            "response_file": "run://openspec-tests/codex-plan.md",
        },
    )
    codex_commands = []
    codex_command_cwds = []

    def codex_run(command, **kwargs):
        if command[0] == "codex":
            codex_commands.append(list(command))
            codex_command_cwds.append(kwargs["cwd"])
            message_path = pathlib.Path(
                command[command.index("--output-last-message") + 1]
            )
            message_path.write_text(
                json.dumps(
                    {
                        "summary_markdown": (
                            "1. Goal\n2. Constraints and assumptions\n3. Implementation"
                            " plan\n4. Verification plan\n5. Risks and open questions\n"
                        ),
                        "proposal_markdown": "# Proposal\n\nCodex proposal.\n",
                        "tasks_markdown": "- [ ] Codex task\n",
                        "design_markdown": design_doc(
                            "Codex-generated plans still include a concise design"
                            " artifact."
                        ),
                        "specs": [
                            {
                                "capability": "codex-billing",
                                "deltas": [
                                    {
                                        "type": "ADDED",
                                        "requirements": [
                                            {
                                                "title": (
                                                    "Codex plan includes a valid spec"
                                                    " delta"
                                                ),
                                                "body_markdown": (
                                                    "The generated plan SHALL include"
                                                    " at least one OpenSpec requirement"
                                                    " delta."
                                                ),
                                                "scenarios": [
                                                    {
                                                        "title": (
                                                            "Codex backend returns"
                                                            " planning data"
                                                        ),
                                                        "body_markdown": (
                                                            "- WHEN the plugin uses the"
                                                            " Codex backend\n- THEN it"
                                                            " still produces"
                                                            " validator-safe OpenSpec"
                                                            " specs"
                                                        ),
                                                    }
                                                ],
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            return completed_process(command, returncode=0, stdout="", stderr="")
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        codex_request,
        [mock.patch.object(module, "run_process", side_effect=codex_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("codex backend path should not fail the plugin process")
    _, codex_result = assert_app_frames(frames, "openspec")
    if codex_result["status"] != "success":
        raise AssertionError("expected successful codex generate_plan result")
    if codex_result["output"]["backend"] != "codex":
        raise AssertionError("expected codex backend in output")
    codex_workdir = codex_commands[0][codex_commands[0].index("--cd") + 1]
    if codex_workdir == str(repo_dir):
        raise AssertionError(
            "default codex generate_plan should not run inside the target repo"
        )
    if codex_command_cwds[0] != codex_workdir:
        raise AssertionError(
            "codex generate_plan should execute from its declared --cd directory"
        )

    gh_model_commands = []
    gh_model_request = app_invoke_request(
        "generate_plan",
        {
            "_workspace_dir": str(repo_dir),
            "change_name": "PROJ-800 Model Controls",
            "prompt": "Plan with model controls.",
            "backend": "gh-copilot",
            "model": "claude-opus-4",
            "reasoning_effort": "high",
        },
    )

    def gh_model_run(command, **kwargs):
        gh_model_commands.append(list(command))
        if command[0] == "gh":
            payload = {
                "summary_markdown": (
                    "1. Goal\n2. Constraints and assumptions\n3. Implementation"
                    " plan\n4. Verification plan\n5. Risks and open questions\n"
                ),
                "proposal_markdown": "# Proposal\n\nModel-controlled plan.\n",
                "tasks_markdown": "- [ ] Model task\n",
                "design_markdown": design_doc(
                    "Model-controlled plans still produce the required design artifact."
                ),
                "specs": [
                    {
                        "capability": "model-control",
                        "deltas": [
                            {
                                "type": "ADDED",
                                "requirements": [
                                    {
                                        "title": (
                                            "Model and reasoning controls are forwarded"
                                            " to the backend"
                                        ),
                                        "body_markdown": (
                                            "The plugin SHALL pass model and"
                                            " reasoning_effort to the backend CLI."
                                        ),
                                        "scenarios": [
                                            {
                                                "title": (
                                                    "Model and reasoning_effort are set"
                                                ),
                                                "body_markdown": (
                                                    "- WHEN model and reasoning_effort"
                                                    " are set\n- THEN the backend"
                                                    " receives the corresponding CLI"
                                                    " flags"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        gh_model_request,
        [mock.patch.object(module, "run_process", side_effect=gh_model_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "gh-copilot model+reasoning_effort path should not fail the plugin process"
        )
    _, gh_model_result = assert_app_frames(frames, "openspec")
    if gh_model_result["status"] != "success":
        raise AssertionError(
            "expected success for gh-copilot with model+reasoning_effort"
        )
    gh_cmd = gh_model_commands[0]
    if (
        "--model" not in gh_cmd
        or gh_cmd[gh_cmd.index("--model") + 1] != "claude-opus-4"
    ):
        raise AssertionError("expected --model claude-opus-4 forwarded to gh copilot")
    if (
        "--reasoning-effort" not in gh_cmd
        or gh_cmd[gh_cmd.index("--reasoning-effort") + 1] != "high"
    ):
        raise AssertionError("expected --reasoning-effort high forwarded to gh copilot")

    codex_model_commands = []
    codex_model_request = app_invoke_request(
        "generate_plan",
        {
            "_workspace_dir": str(repo_dir),
            "change_name": "PROJ-801 Codex Model Controls",
            "prompt": "Plan with codex model controls.",
            "backend": "codex",
            "model": "o4-mini",
            "reasoning_effort": "xhigh",
        },
    )

    def codex_model_run(command, **kwargs):
        codex_model_commands.append(list(command))
        if command[0] == "codex":
            message_path = pathlib.Path(
                command[command.index("--output-last-message") + 1]
            )
            payload = {
                "summary_markdown": (
                    "1. Goal\n2. Constraints and assumptions\n3. Implementation"
                    " plan\n4. Verification plan\n5. Risks and open questions\n"
                ),
                "proposal_markdown": "# Proposal\n\nCodex model-controlled plan.\n",
                "tasks_markdown": "- [ ] Codex model task\n",
                "design_markdown": design_doc(
                    "Codex model-controlled plans still produce the required design"
                    " artifact."
                ),
                "specs": [
                    {
                        "capability": "codex-model-control",
                        "deltas": [
                            {
                                "type": "ADDED",
                                "requirements": [
                                    {
                                        "title": (
                                            "Codex model and reasoning controls are"
                                            " forwarded"
                                        ),
                                        "body_markdown": (
                                            "The plugin SHALL pass model and"
                                            " reasoning_effort to codex exec."
                                        ),
                                        "scenarios": [
                                            {
                                                "title": (
                                                    "Codex model and reasoning_effort"
                                                    " are set"
                                                ),
                                                "body_markdown": (
                                                    "- WHEN model and reasoning_effort"
                                                    " are set for codex backend\n- THEN"
                                                    " the codex exec command receives"
                                                    " the corresponding flags"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            message_path.write_text(json.dumps(payload), encoding="utf-8")
            return completed_process(command, returncode=0, stdout="", stderr="")
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        codex_model_request,
        [mock.patch.object(module, "run_process", side_effect=codex_model_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "codex model+reasoning_effort path should not fail the plugin process"
        )
    _, codex_model_result = assert_app_frames(frames, "openspec")
    if codex_model_result["status"] != "success":
        raise AssertionError("expected success for codex with model+reasoning_effort")
    codex_cmd = codex_model_commands[0]
    if (
        "--model" not in codex_cmd
        or codex_cmd[codex_cmd.index("--model") + 1] != "o4-mini"
    ):
        raise AssertionError("expected --model o4-mini forwarded to codex exec")
    reasoning_flag_index = next(
        (
            i
            for i, arg in enumerate(codex_cmd)
            if arg == "-c" and i + 1 < len(codex_cmd)
        ),
        None,
    )
    if (
        reasoning_flag_index is None
        or codex_cmd[reasoning_flag_index + 1] != "model_reasoning_effort=xhigh"
    ):
        raise AssertionError(
            "expected -c model_reasoning_effort=xhigh forwarded to codex exec"
        )

    gh_no_controls_commands = []
    gh_no_controls_request = app_invoke_request(
        "generate_plan",
        {
            "_workspace_dir": str(repo_dir),
            "change_name": "PROJ-802 No Controls",
            "prompt": "Plan without model controls.",
            "backend": "gh-copilot",
        },
    )

    def gh_no_controls_run(command, **kwargs):
        gh_no_controls_commands.append(list(command))
        if command[0] == "gh":
            payload = {
                "summary_markdown": (
                    "1. Goal\n2. Constraints and assumptions\n3. Implementation"
                    " plan\n4. Verification plan\n5. Risks and open questions\n"
                ),
                "proposal_markdown": "# Proposal\n\nNo-controls plan.\n",
                "tasks_markdown": "- [ ] No-controls task\n",
                "design_markdown": design_doc(
                    "Plans without model controls still include the required design"
                    " artifact."
                ),
                "specs": [
                    {
                        "capability": "no-controls",
                        "deltas": [
                            {
                                "type": "ADDED",
                                "requirements": [
                                    {
                                        "title": (
                                            "Omitting model controls preserves the"
                                            " existing command line"
                                        ),
                                        "body_markdown": (
                                            "The plugin SHALL not add model or"
                                            " reasoning flags when they are not"
                                            " supplied."
                                        ),
                                        "scenarios": [
                                            {
                                                "title": (
                                                    "Neither model nor reasoning_effort"
                                                    " is set"
                                                ),
                                                "body_markdown": (
                                                    "- WHEN neither model nor"
                                                    " reasoning_effort is supplied\n-"
                                                    " THEN the command line remains"
                                                    " unchanged"
                                                ),
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
            return completed_process(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if command[0] == "openspec":
            return completed_process(
                command, returncode=0, stdout="validated\n", stderr=""
            )
        raise AssertionError(f"unexpected command: {command}")

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        gh_no_controls_request,
        [mock.patch.object(module, "run_process", side_effect=gh_no_controls_run)],
    )
    if exit_code != 0 or stderr:
        raise AssertionError(
            "omitting model/reasoning_effort should not fail the plugin process"
        )
    _, gh_no_controls_result = assert_app_frames(frames, "openspec")
    if gh_no_controls_result["status"] != "success":
        raise AssertionError("expected success when model/reasoning_effort are omitted")
    no_controls_gh_cmd = gh_no_controls_commands[0]
    if "--model" in no_controls_gh_cmd:
        raise AssertionError(
            "--model should not appear in gh copilot command when not supplied"
        )
    if "--reasoning-effort" in no_controls_gh_cmd:
        raise AssertionError(
            "--reasoning-effort should not appear in gh copilot command when not"
            " supplied"
        )

    exit_code, frames, stderr = run_process_plugin_main(
        module,
        plan_request,
        [mock.patch.object(module, "run_process", side_effect=FileNotFoundError())],
    )
    if exit_code != 0 or stderr:
        raise AssertionError("missing binary should still produce a normal APP result")
    _, missing_binary_result = assert_app_frames(frames, "openspec")
    if missing_binary_result["error"]["code"] != "tool_execution_error":
        raise AssertionError(
            "expected tool_execution_error when required CLI is unavailable"
        )

    print("openspec offline smoke test passed")


if __name__ == "__main__":
    main()
