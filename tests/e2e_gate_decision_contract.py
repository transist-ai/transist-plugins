#!/usr/bin/env python3
"""Real installed document gates through seven actions; fake providers, no skips."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Iterator
import unittest
from unittest import mock

import e2e_document_actions as documents
from gate_contract_fixture import decision
from installed_fixture import fixture_base, json_args, runtime_binary

PACKAGES = (
    ("codex.cli", "exec_task"),
    ("cursor.cli", "exec_task_cli"),
    ("cursor.cli", "exec_task"),
    ("github.copilot.cli", "exec_task"),
    ("opencode.cli", "exec_task"),
    ("openspec", "generate_plan"),
    ("shell", "exec"),
)
ACTOR = "Gate Fixture Reviewer"
REASON = "Tighten the wording café"
OBSERVE = r"""
def observe(request: dict, prompt: str) -> None:
    "Assert actual provider feedback and retain the runtime-injected decision."
    gate = request['input']['gate_decision']
    assert plugin.valid_gate_decision(gate), 'typed runtime decision was rejected'
    feedback = plugin.gate_decision_feedback(request['input'])
    if gate['decision'] == 'request_changes':
        for field in ('reason', 'actor', 'actor_source'):
            assert gate[field] and gate[field] in prompt, 'operator feedback lost'
    else:
        assert feedback in ('', ('', '', ''))
    for revision in gate['review_request']['review_documents']:
        assert revision['revision_id'] not in prompt, 'metadata leaked into prompt'
    with Path(WORKSPACE, 'gate-observations.jsonl').open('a') as handle:
        data = {'gate': gate, 'feedback': feedback, 'prompt': prompt}
        handle.write(json.dumps(data) + '\n')
"""
WRAPPER = (
    documents.WRAPPER.replace("def completed(", OBSERVE + "\ndef completed(", 1)
    .replace(
        "    Path(WORKSPACE, 'provider-called')",
        "    observe(request, prompt)\n    Path(WORKSPACE, 'provider-called')",
    )
    .replace("        data = (", "        observe(request, prompt)\n        data = (")
)


@contextmanager
def retained_directory(prefix: str) -> Iterator[str]:
    """Retain fresh disposable homes and numbered CLI transcripts for parent review."""
    path = tempfile.mkdtemp(prefix="gate-contract-", dir=fixture_base())
    print("FIXTURE " + path, flush=True)
    yield path


class InstalledGateContract(documents.InstalledDocuments):
    """Reuse installed document staging with the actual production input contract."""

    def cli(self, *args: str, expected_exit: int = 0) -> str:
        """Execute the current CLI, retaining complete bounded command transcripts."""
        result = subprocess.run(
            [runtime_binary(), "--home", str(self.home), *json_args(args)],
            cwd=self.workspace,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.command_sequence += 1
        (self.root / f"command-{self.command_sequence:04d}.json").write_text(
            json.dumps(
                {
                    "args": args,
                    "exit": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
                indent=2,
            )
        )
        self.assertEqual(
            result.returncode, expected_exit, result.stdout + result.stderr
        )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict) and isinstance(value.get("runner_pid"), int):
            self.runner_pids.add(value["runner_pid"])
        return result.stdout

    def settle_runners(self) -> None:
        """Wait for owned runners to release leases after durable checkpoints."""
        deadline = time.monotonic() + 30
        while self.runner_pids:
            for pid in tuple(self.runner_pids):
                result = subprocess.run(
                    ["/bin/ps", "-p", str(pid), "-o", "command="],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if not result.stdout.strip():
                    self.runner_pids.remove(pid)
            self.assertLess(time.monotonic(), deadline, "owned runner did not exit")
            if self.runner_pids:
                time.sleep(0.05)

    def wait(self, status: str) -> dict:
        """Wait for the precise state; fail immediately on another terminal state."""
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            report = json.loads(self.cli("runs", "show", self.run_id))
            if report["status"] == status:
                self.settle_runners()
                return report
            self.assertNotIn(report["status"], documents.TERMINAL, json.dumps(report))
            time.sleep(0.05)
        self.fail(f"run did not reach {status}")

    def launch(self, action: str, outcome: str, invalid: bool = False) -> dict:
        """Install a fresh authored caller and decide its real initial-document gate."""
        self.sequence += 1
        self.run_id = f"gate_{self.sequence}"
        self.run_root = self.home / "runs" / ("run_" + self.run_id)
        payload = {
            "prompt": "Perform the fixture task",
            "context_files": [documents.document("context", "read")],
            "response_file": documents.document("out", "write"),
            "output_mode": "compact",
            "max_context_file_bytes": 65536,
            "max_prompt_bytes": 98304,
            "feedback_context_mode": "feedback-only",
        }
        if self.package == "shell":
            payload = {
                "command": "python3",
                "args": [
                    "-c",
                    "import os; from pathlib import Path; "
                    "Path('provider-called').write_text('yes'); "
                    "print(os.environ['TRANSIST_GATE_DECISION_JSON'])",
                ],
            }
        elif self.package == "openspec":
            payload.update(
                change_name="fixture",
                change_metadata_file=documents.document("state", "write"),
            )
        else:
            payload["history_mode"] = "invocation"
            if action == "exec_task" and self.package == "cursor.cli":
                payload["secret_refs"] = ["CURSOR_API_KEY"]
        consume = {
            "id": "consume",
            "uses": f"process:{self.package}/{action}",
            "with": payload,
        }
        steps = [
            {
                "id": "review",
                "control": "control.gate.manual",
                "with": {"gate_id": "operator-review", "review_documents": ["context"]},
            },
            consume,
        ]
        if invalid:
            malformed = decision()
            malformed["review_request"]["review_documents"][0]["blob"][
                "unexpected"
            ] = "closed"
            payload["gate_decision"] = malformed
            steps = [consume]
        else:
            consume["gate_decision"] = {"step": "review"}
        flow = {
            "schema": "transist.flow.v1",
            "id": self.run_id,
            "version": "0.1.0",
            "plugins": {self.package: "0.1.0"},
            "workspace": {"path": str(self.workspace), "access": "full"},
            "network": {"mode": "none"},
            "documents": {
                "context": {
                    "kind": "file",
                    "max_bytes": 1048576,
                    "initial": documents.TEXT,
                },
                "out": {"kind": "file", "max_bytes": 1048576},
                "state": {"kind": "file", "max_bytes": 1048576},
            },
            "steps": steps,
        }
        path = self.source / (self.run_id + ".yaml")
        path.write_text(json.dumps(flow))
        self.cli("workflows", "check", "--path", str(path))
        self.cli("workflows", "install", "--path", str(path), "--approve")
        if invalid:
            readiness = json.loads(
                self.cli(
                    "workflows",
                    "run",
                    self.run_id + "@0.1.0",
                    "--run-ref",
                    self.run_id,
                    expected_exit=1,
                )
            )
            self.assertEqual(readiness["status"], "not_deliverable")
            self.assertFalse(readiness["run_created"])
            self.assertFalse(self.run_root.exists())
            failures = [
                check for check in readiness["checks"] if check["status"] == "failed"
            ]
            self.assertEqual([check["code"] for check in failures], ["input_schema"])
            self.assertIn(
                "/gate_decision/review_request/review_documents/0/blob",
                failures[0]["message"],
            )
            return readiness
        self.cli("workflows", "run", self.run_id + "@0.1.0", "--run-ref", self.run_id)
        waiting = self.wait("blocked")
        request = waiting["steps"]["review"]["last_result"]["output"]["review_request"]
        self.assertEqual(request["execution"], 1)
        self.assertEqual(request["review_documents"][0]["execution"], 0)
        self.cli(
            "runs",
            "gate",
            "request-changes" if outcome == "request_changes" else "approve",
            self.run_id,
            "operator-review",
            "--actor",
            ACTOR,
            "--reason",
            REASON,
        )
        report = self.wait("success")
        history = list((self.run_root / "gates/history").rglob("*.json"))
        self.assertEqual(len(history), 1)
        committed = json.loads(history[0].read_text())
        self.assertEqual(committed["review_request"], request)
        self.assertEqual(committed["decision"], outcome)
        if self.package == "shell":
            observed = json.loads(
                report["steps"]["consume"]["last_result"]["output"]["stdout"]
            )
            self.assertEqual(observed, committed)
        else:
            observation = json.loads(
                (self.workspace / "gate-observations.jsonl")
                .read_text()
                .splitlines()[-1]
            )
            self.assertEqual(observation["gate"], committed)
            self.assertTrue(
                observation["feedback"]
                if outcome == "request_changes"
                else not any(observation["feedback"])
            )
            self.assertEqual(
                self.blob("out").decode().strip(),
                "# Plan" if self.package == "openspec" else documents.ANSWER,
            )
        return report

    def test_gate_contract(self) -> None:
        """All bindings preserve feedback/approval and reject malformed metadata."""
        selected = set(
            filter(None, os.environ.get("TRANSIST_TEST_PACKAGES", "").split(","))
        )
        cases = [
            (package, action)
            for package, action in PACKAGES
            if not selected or "gate-contract" in selected or package in selected
        ]
        self.assertTrue(cases, "No gate contract cases selected")
        for package, action in cases:
            with self.subTest(package=package, action=action):
                self.command_sequence = 0
                self.runner_pids = set()
                with mock.patch.object(
                    documents, "WRAPPER", WRAPPER
                ), mock.patch.object(
                    documents, "disposable_directory", retained_directory
                ), self.fixture(
                    package
                ):
                    self.env["CURSOR_API_KEY"] = "fixture-only-cursor-secret"
                    (self.workspace / "openspec/changes").mkdir(parents=True)
                    for outcome in ("request_changes", "approved"):
                        self.launch(action, outcome)
                    marker = self.workspace / "provider-called"
                    marker.unlink(missing_ok=True)
                    observations = self.workspace / "gate-observations.jsonl"
                    before = observations.read_bytes() if observations.exists() else b""
                    self.launch(action, "request_changes", invalid=True)
                    self.assertFalse(
                        marker.exists(), "malformed input reached provider"
                    )
                    self.assertEqual(
                        observations.read_bytes() if observations.exists() else b"",
                        before,
                    )
                    print(
                        f"PASS {package}/{action}: request_changes feedback, "
                        "approval, pre-dispatch rejection",
                        flush=True,
                    )


if __name__ == "__main__":
    runtime_binary()
    fixture_base()
    unittest.main(argv=[sys.argv[0], "InstalledGateContract.test_gate_contract"])
