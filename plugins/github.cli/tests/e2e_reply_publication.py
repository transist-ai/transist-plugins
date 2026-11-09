#!/usr/bin/env python3
"""Finite installed reply fixture, promoted from Step 10's accepted/lost reproducer.

Only gh is fake, via private PATH. Install the unmodified selected production
payload; APP, schemas, sandbox, documents and public retry/resume are real.
Retain isolated homes, transcripts, payload hashes and remote observations.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Iterator
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "plugins/github.cli"
sys.path.insert(0, str(ROOT / "tests"))
from installed_fixture import (  # noqa: E402
    TERMINAL,
    fixture_base,
    json_args,
    runtime_binary,
    settle_runs,
)

FAKE_GH = r"""
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
root = Path.cwd()
assert (root / '.fixture-workspace').is_file(), 'wrong canonical cwd'
if args == ['--version']:
    print('gh version fixture')
    raise SystemExit(0)
path = root / 'remote.json'
state = json.loads(path.read_text())
state['requests'].append(args)
def save():
    path.write_text(json.dumps(state, indent=2))
if args == ['repo', 'view', '--json', 'owner,name']:
    save()
    print('{"owner":{"login":"fixture"},"name":"repo"}')
elif args == ['pr', 'view', '--json', 'number']:
    save()
    print('{"number":7}')
elif args[:3] == ['api', '--method', 'GET']:
    assert len(args) == 8 and args[4:7] == ['-f', 'per_page=100', '-f']
    assert args[7].startswith('page=')
    surface = 'pulls' if '/pulls/' in args[3] else 'issues'
    assert args[3] == f'repos/fixture/repo/{surface}/7/comments'
    page = int(args[7][5:])
    assert 1 <= page <= 100
    save()
    mode = state.get('read_mode', '')
    if mode == 'error':
        print('fixture transient API error', file=sys.stderr)
        raise SystemExit(1)
    if mode == 'sleep':
        (root / 'sleep-pid').write_text(str(os.getpid()))
        time.sleep(60)
    if mode == 'malformed':
        print('[{"id":true,"body":null}]')
    elif mode == 'incomplete':
        batch = [{'id':page*100+i,'body':'other','html_url':'https://example.test/1'}
                 for i in range(100)]
        print(json.dumps(batch))
    else:
        print(json.dumps(state[surface][(page-1)*100:page*100]))
elif args[:1] == ['api'] and 'POST' in args:
    assert args[2:5] == ['-X', 'POST', '-f']
    surface = 'pulls' if '/pulls/' in args[1] else 'issues'
    assert args[1] == f'repos/fixture/repo/{surface}/7/comments'
    assert len(args) == (8 if surface == 'pulls' else 6)
    body = args[5].removeprefix('body=')
    assert body.startswith('<!-- transist-review-reply -->\n')
    assert '\n<!-- transist-review-publication:' in body
    state['post_count'] += 1
    comment = {'id':state['post_count'],'body':body,
               'html_url':f'https://github.com/fixture/repo/pull/7#{state["post_count"]}'}
    if surface == 'pulls':
        assert args[6] == '-F' and args[7].startswith('in_reply_to=')
        comment['in_reply_to_id'] = int(args[7][12:])
    state[surface].append(comment)
    save()
    if state['post_count'] == state.get('lose_at'):
        print('connection lost after accepted POST', file=sys.stderr)
        raise SystemExit(1)
    if state['post_count'] == state.get('malformed_at'):
        print('{')
    else:
        print(json.dumps(comment))
else:
    save()
    raise SystemExit('unexpected fake gh request')
"""


def document(name: str, access: str) -> dict:
    """Use real managed file bindings for the existing three action paths."""
    return {"$document": name, "access": access}


class InstalledReplyPublication(unittest.TestCase):
    """No operational homes, provider credentials, wrappers or replacement APP."""

    @contextmanager
    def fixture(self) -> Iterator[None]:
        """Install exact selected bytes; retain isolated evidence after settlement."""
        runtime_binary(admin=True)
        self.root = Path(
            tempfile.mkdtemp(prefix="reply-publication-", dir=fixture_base())
        )
        self.home = self.root / "home"
        self.workspace = self.root / "workspace"
        self.source = self.root / "source"
        self.workspace.mkdir()
        self.source.mkdir()
        (self.root / "user").mkdir()
        (self.root / "temp").mkdir()
        self.sequence = 0
        self.run_sequence = 0
        self.pids = set()
        self.env = {
            "HOME": str(self.root / "user"),
            "PATH": os.environ.get("PATH", os.defpath),
            "TMPDIR": str(self.root / "temp"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        (self.workspace / ".fixture-workspace").touch()
        # Linux binds the declared workspace, not sibling fixture directories.
        # Keep the fake tool within that existing boundary, as review-actions does.
        private_bin = (self.workspace if sys.platform == "linux" else self.root) / "bin"
        private_bin.mkdir()
        gh = private_bin / "gh"
        # Linux's system-only executable view need not expose the test virtualenv.
        gh.write_text(f"#!{Path(sys.executable).resolve(strict=True)}\n" + FAKE_GH)
        gh.chmod(0o755)
        self.env["PATH"] = str(private_bin) + os.pathsep + self.env["PATH"]
        print("FIXTURE", self.root, flush=True)
        self.cli("home", "init", "--mode", "standalone")
        config_path = self.home / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        config["policy"]["child_env"] = {"inherit": False, "allowlist": ["PATH"]}
        config["policy"].setdefault("remote_secret_delivery", {})["process"] = True
        config_path.write_text(yaml.safe_dump(config))
        self.cli("plugins", "install", "--path", str(PACKAGE / "plugin.yaml"))
        self.cli("plugins", "list")
        self.cli("plugins", "inspect", "github.cli")
        manifest = yaml.safe_load((PACKAGE / "plugin.yaml").read_text())
        installed = self.home / "plugins/github.cli/0.1.0"
        selected = {"plugin.yaml", *manifest["package"]["files"]}
        self.assertEqual(
            {
                p.relative_to(installed).as_posix()
                for p in installed.rglob("*")
                if p.is_file()
            },
            selected,
        )
        hashes = {}
        for name in sorted(selected):
            data = (installed / name).read_bytes()
            self.assertEqual(data, (PACKAGE / name).read_bytes())
            hashes[name] = hashlib.sha256(data).hexdigest()
        (self.root / "payload.json").write_text(
            json.dumps(hashes, indent=2, sort_keys=True)
        )
        print(
            "PAYLOAD",
            hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
            flush=True,
        )
        try:
            yield
        finally:
            self.settle()
            settle_runs(self.home, self.cli)
            self.settle()
            (self.root / "settled-runners.json").write_text(
                json.dumps({"pids": sorted(self.pids), "all_exited": True})
            )

    def cli(self, *args: str) -> str:
        """Retain bounded CLI transcripts and remember detached runners for re-entry."""
        result = subprocess.run(
            [runtime_binary(), "--home", str(self.home), *json_args(args)],
            cwd=self.workspace,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.sequence += 1
        (self.root / f"command-{self.sequence:03}.json").write_text(
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
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        try:
            value = json.loads(result.stdout)
        except ValueError:
            value = {}
        if isinstance(value, dict) and isinstance(value.get("runner_pid"), int):
            self.pids.add(value["runner_pid"])
        return result.stdout

    def settle(self) -> None:
        """Wait for actual runner exit, not merely a durable terminal checkpoint."""
        deadline = time.monotonic() + 30
        pending = set(self.pids)
        while pending:
            for pid in tuple(pending):
                process = subprocess.run(
                    ["/bin/ps", "-p", str(pid), "-o", "command="],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if not process.stdout.strip():
                    pending.remove(pid)
            self.assertLess(time.monotonic(), deadline, "runner did not exit")
            if pending:
                time.sleep(0.05)

    def remote(self, **updates: object) -> dict:
        """Read/update only fake external state, independent of managed revisions."""
        path = self.workspace / "remote.json"
        state = (
            json.loads(path.read_text())
            if path.exists()
            else {"issues": [], "pulls": [], "requests": [], "post_count": 0}
        )
        if updates or not path.exists():
            state.update(updates)
            path.write_text(json.dumps(state, indent=2))
        return state

    def start(
        self,
        responses: list,
        *,
        resolution: bool = False,
        state_limit: int = 65536,
        wait: bool = True,
    ) -> dict:
        """Use both maintained publisher variants with staged paths, no added input."""
        self.run_sequence += 1
        self.name = f"reply_fixture_{self.run_sequence}"
        step_id = "publish_review_responses_" + (
            "with_resolution" if resolution else "without_resolution"
        )
        self.step_id = step_id
        flow = {
            "schema": "transist.flow.v1",
            "id": self.name,
            "version": "0.1.0",
            "plugins": {"github.cli": "0.1.0"},
            "network": {"mode": "none"},
            "workspace": {"path": str(self.workspace), "access": "full"},
            "documents": {
                "plan": {
                    "kind": "file",
                    "max_bytes": 65536,
                    "initial": json.dumps(
                        {"responses": responses, "addressed_thread_ids": []}
                    ),
                },
                "state": {
                    "kind": "file",
                    "max_bytes": state_limit,
                    "initial": '{"responses":{}}\n',
                },
                "out": {"kind": "file", "max_bytes": 65536},
            },
            "steps": [
                {"id": "authored_predecessor", "control": "control.noop"},
                {
                    "id": step_id,
                    "uses": "process:github.cli/pr_publish_review_replies",
                    "with": {
                        "output_mode": "compact",
                        "response_plan_file": document("plan", "read"),
                        "state_file": document("state", "update"),
                        "output_file": document("out", "write"),
                        "resolve_addressed_threads": resolution,
                    },
                },
                {"id": "finish", "control": "control.finish"},
            ],
        }
        path = self.source / (self.name + ".yaml")
        path.write_text(yaml.safe_dump(flow, sort_keys=False))
        self.cli("workflows", "check", "--path", str(path))
        self.cli("workflows", "install", "--path", str(path), "--approve")
        readiness = json.loads(
            self.cli("workflows", "check", self.name + "@0.1.0", "--live")
        )
        self.assertEqual(readiness["status"], "ready", readiness)
        self.cli("workflows", "run", self.name + "@0.1.0", "--run-ref", self.name)
        return self.wait() if wait else {}

    def wait(self, executions: int = 1) -> dict:
        """Poll a finite attempt count so an old failed state cannot satisfy retry."""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = json.loads(self.cli("runs", "show", self.name))
            if (
                state["status"] in TERMINAL
                and state["steps"][self.step_id]["executions"] >= executions
            ):
                (
                    self.root / f"{self.name}-attempt-{executions}-remote.json"
                ).write_text(json.dumps(self.remote(), indent=2))
                return state
            time.sleep(0.05)
        self.fail("run did not settle")

    def heads(self) -> dict:
        """Read actual committed document heads, including absent failed outputs."""
        self.run_root = self.home / "runs" / ("run_" + self.name)
        return json.loads((self.run_root / "documents/heads.json").read_text())

    def blob(self, name: str) -> dict:
        """Read an immutable document selected by the runtime's committed head."""
        head = self.heads()[name]
        return json.loads(
            (self.run_root / "documents/blobs" / head["blob"]["sha256"]).read_text()
        )

    def response(self, source: str, comment_id: str = "fixture-request") -> dict:
        """Freeze the reproducer's exact request and visible reply formatting."""
        return {
            "comment_id": comment_id,
            "source": source,
            "reply": "This is the exact synthetic clarification.",
            "url": "https://github.com/fixture/repo/pull/7#discussion_r42",
            **(
                {"review_comment_id": "42", "thread_id": "PRRT_fixture"}
                if source == "review_thread"
                else {}
            ),
        }

    def assert_failed_documents(self, state: dict) -> None:
        """No failed action or runtime output rejection may advance managed state."""
        self.assertEqual(state["status"], "failed", state)
        self.assertEqual(self.blob("state"), {"responses": {}})
        heads = self.heads()
        self.assertNotIn("out", heads)
        self.assertEqual(heads["state"]["commit_sequence"], 0)
        self.assertEqual(heads["state"]["execution"], 0)
        self.assertEqual(
            heads["state"]["step_id"], "__transist_document_initialization"
        )

    def test_accept_then_lost_response_retry_resume_both_surfaces(self) -> None:
        """Four real CLI re-entries: no duplicate when no local state committed."""
        with self.fixture():
            for source in ("pr_conversation", "review_thread"):
                for reentry in ("retry", "resume"):
                    self.remote(
                        issues=[], pulls=[], requests=[], post_count=0, lose_at=1
                    )
                    response = self.response(source)
                    first = self.start([response], resolution=reentry == "resume")
                    self.assert_failed_documents(first)
                    before = self.remote()
                    self.assertEqual(before["post_count"], 1)
                    surface = "pulls" if source == "review_thread" else "issues"
                    body = before[surface][0]["body"]
                    visible = "This is the exact synthetic clarification."
                    if source == "pr_conversation":
                        visible = (
                            f'Reply to [this comment]({response["url"]}): {visible}'
                        )
                    self.assertEqual(
                        body.splitlines()[:2],
                        ["<!-- transist-review-reply -->", visible],
                    )
                    expected = [
                        "fixture",
                        "repo",
                        7,
                        source,
                        response["comment_id"],
                        surface,
                        "42" if surface == "pulls" else "",
                    ]
                    identity = hashlib.sha256(
                        json.dumps(
                            expected, ensure_ascii=False, separators=(",", ":")
                        ).encode()
                    ).hexdigest()
                    self.assertEqual(
                        body.splitlines()[2],
                        f"<!-- transist-review-publication:{identity} -->",
                    )
                    self.settle()
                    self.cli("runs", reentry, self.name)
                    final = self.wait(2)
                    self.assertEqual(final["status"], "success", final)
                    self.assertEqual(
                        final["steps"]["authored_predecessor"]["executions"], 1
                    )
                    after = self.remote()
                    self.assertEqual(after["post_count"], 1)
                    requests = after["requests"][len(before["requests"]) :]
                    self.assertEqual(sum("GET" in r for r in requests), 2)
                    self.assertFalse(any("POST" in r for r in requests))
                    self.assertEqual(
                        self.blob("state")["responses"][response["comment_id"]][
                            "publication_id"
                        ],
                        identity,
                    )
                    self.assertEqual(self.blob("out")["skipped_count"], 1)
                    print(
                        "PASS accepted-lost", source, reentry, "posts=1/1", flush=True
                    )
                    self.settle()

    def test_partial_batch_malformed_response_and_later_pages(self) -> None:
        """Partial batches and malformed successful responses reconcile later."""
        with self.fixture():
            responses = [
                self.response("pr_conversation"),
                self.response("review_thread", "thread-request"),
            ]
            self.remote(lose_at=2)
            self.assert_failed_documents(self.start(responses))
            before = self.remote()
            # Both accepted markers are beyond the first 100 comments on re-entry.
            for surface in ("issues", "pulls"):
                before[surface][:0] = [
                    {
                        "id": i + 100,
                        "body": "unrelated marker",
                        "html_url": "https://example.test/comment",
                    }
                    for i in range(100)
                ]
            self.remote(**before)
            self.settle()
            self.cli("runs", "retry", self.name)
            final = self.wait(2)
            self.assertEqual(final["status"], "success", final)
            self.assertEqual(self.remote()["post_count"], 2)
            self.assertEqual(self.blob("out")["skipped_count"], 2)
            self.assertEqual(sum("page=2" in r for r in self.remote()["requests"]), 2)
            self.settle()
            self.remote(
                issues=[],
                pulls=[],
                requests=[],
                post_count=0,
                lose_at=0,
                malformed_at=1,
            )
            self.assert_failed_documents(self.start([responses[0]]))
            self.settle()
            self.cli("runs", "resume", self.name)
            self.assertEqual(self.wait(2)["status"], "success")
            self.assertEqual(self.remote()["post_count"], 1)

    def test_managed_publication_rejection_does_not_commit_state(self) -> None:
        """Runtime rejects an oversized state revision after remote acceptance."""
        with self.fixture():
            self.remote()
            response = self.response("pr_conversation")
            self.assert_failed_documents(self.start([response], state_limit=20))
            self.assertEqual(self.remote()["post_count"], 1)
            self.settle()
            # A fresh caller with a valid document bound sees the prior publication.
            final = self.start([response])
            self.assertEqual(final["status"], "success", final)
            self.assertEqual(self.remote()["post_count"], 1)
            self.assertEqual(self.blob("out")["skipped_count"], 1)

    def test_errors_bounds_conflict_and_unrelated_identity(self) -> None:
        """Incomplete/malformed reads fail closed; exact IDs distinguish same text."""
        with self.fixture():
            response = self.response("pr_conversation")
            for mode in ("error", "malformed", "incomplete"):
                self.remote(
                    issues=[], pulls=[], requests=[], post_count=0, read_mode=mode
                )
                self.assert_failed_documents(self.start([response]))
                self.assertEqual(self.remote()["post_count"], 0)
                self.settle()
            self.remote(read_mode="", requests=[])
            self.assertEqual(self.start([response])["status"], "success")
            self.settle()
            state = self.remote()
            state["issues"][0]["body"] = state["issues"][0]["body"].replace(
                "clarification", "changed"
            )
            self.remote(**state)
            result = self.start([response])
            self.assert_failed_documents(result)
            self.assertIn("conflicts", json.dumps(result["steps"][self.step_id]))
            self.assertEqual(self.remote()["post_count"], 1)
            self.settle()
            self.assertEqual(
                self.start(
                    [self.response("pr_conversation", "fixture-request-suffix")]
                )["status"],
                "success",
            )
            self.assertEqual(self.remote()["post_count"], 2)

    def test_cancellation_during_reconciliation_never_posts(self) -> None:
        """Quiet retrieval still obeys public cancellation and owned process cleanup."""
        with self.fixture():
            self.remote(read_mode="sleep")
            self.start([self.response("review_thread")], wait=False)
            deadline = time.monotonic() + 20
            while not (self.workspace / "sleep-pid").exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            pid = int((self.workspace / "sleep-pid").read_text())
            self.cli("runs", "cancel", self.name)
            self.assertEqual(self.wait()["status"], "canceled")
            self.assertEqual(self.remote()["post_count"], 0)
            self.assertEqual(self.blob("state"), {"responses": {}})
            self.settle()
            process = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertFalse(process.stdout.strip(), "fake gh survived cancellation")


if __name__ == "__main__":
    runtime_binary()
    runtime_binary(admin=True)
    unittest.main(verbosity=2)
