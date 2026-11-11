#!/usr/bin/env python3
"""Installed document contracts, fake transports only, real adapter/sandbox/store.

The wrapper replaces only provider transport. Production schemas, handlers,
prompt composition, file I/O, lifecycle selection and runtime publication execute.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
import base64
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import select
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Iterator
import unittest

import yaml

from installed_fixture import (
    TERMINAL,
    fixture_base,
    json_args,
    runtime_binary,
    settle_runs,
    disposable_directory,
)
import namespace_peers as _namespace_loader

NamespacePeers = _namespace_loader.load_file(
    Path(_namespace_loader.__file__)
).NamespacePeers

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = (
    "shell",
    "jira.rest",
    "git.cli",
    "github.cli",
    "codex.cli",
    "cursor.cli",
    "github.copilot.cli",
    "opencode.cli",
    "openspec",
    "openai.compatible",
)
TEXT = "  diagnostic café\r\nsecond line ☃\n\n"
ANSWER = (
    "# Summary\n\nDocument fixture answer\n\n## Changes\n\n- Exact context received."
)
ATTACHMENT = b"\x00\xfffixture\r\nattachment\n"
PLAN = {
    "summary_markdown": "# Plan",
    "proposal_markdown": "# Proposal",
    "tasks_markdown": "- [ ] Test",
    "design_markdown": "# Design",
    "specs": [
        {
            "capability": "offline",
            "spec_markdown": (
                "## ADDED Requirements\n### Requirement: Fixture\n"
                "The fixture SHALL work.\n#### Scenario: Test\n"
                "- WHEN invoked\n- THEN succeed"
            ),
        }
    ],
}

WRAPPER = r"""
import json, os, subprocess
from pathlib import Path
import plugin

def completed(command: list, text: str) -> subprocess.CompletedProcess:
    "Return the deterministic provider result, without modifying production I/O."
    return subprocess.CompletedProcess(command, 0, text, '')

def fake(request: dict, command: list, *args: object, **kwargs: object) -> object:
    "Assert provider ingestion and return a canonical fake answer or command output."
    assert plugin.command_cwd(request, request['input']) == WORKSPACE
    if plugin.PLUGIN_ID in ('git.cli', 'github.cli'):
        if command[:3] == ['git', 'status', '--porcelain']:
            return completed(command, ' M source.txt\n')
        if command[:3] == ['git', 'rev-parse', 'HEAD']:
            return completed(command, 'f' * 40 + '\n')
        if command[:2] == ['git', 'commit']:
            assert command[command.index('-m') + 1] == '[DEMO-1] Document fixture title'
        if command[:3] == ['gh', 'pr', 'create']:
            assert command[command.index('--body') + 1].encode() == TEXT.encode()
            title = command[command.index('--title') + 1]
            assert title == '[DEMO-1] Document fixture title'
            return completed(command, 'https://example.test/pull/1\n')
        if command[:3] == ['gh', 'pr', 'view']:
            return completed(command, json.dumps({
                'number': 1, 'baseRefName': 'main', 'headRefName': 'fixture',
                'state': 'OPEN'}))
        if command[:2] == ['gh', 'api']:
            remote_path = Path(WORKSPACE, 'fixture-gh-remote.json')
            remote = (json.loads(remote_path.read_text()) if remote_path.exists()
                      else {'issues': [], 'pulls': [], 'requests': []})
            remote['requests'].append(command)
            surface = 'pulls' if any('/pulls/' in arg for arg in command) else 'issues'
            endpoint = f'repos/fixture/repo/{surface}/1/comments'
            if command[2:4] == ['--method', 'GET']:
                assert len(command) == 9 and command[4] == endpoint
                assert command[5:8] == ['-f', 'per_page=100', '-f']
                assert command[8].startswith('page=')
                page = int(command[8][5:])
                assert 1 <= page <= 100
                result = remote[surface][(page - 1) * 100:page * 100]
            else:
                assert command[2:6] == [endpoint, '-X', 'POST', '-f']
                assert len(command) == (9 if surface == 'pulls' else 7)
                assert command[6].startswith('body=')
                body = command[6][5:]
                assert body.startswith('<!-- transist-review-reply -->\n')
                import re
                assert re.fullmatch(
                    r'<!-- transist-review-publication:[0-9a-f]{64} -->',
                    body.splitlines()[-1])
                result = {'id':len(remote['issues']) + len(remote['pulls']) + 1,
                          'body':body, 'html_url':'https://example.test/reply/1'}
                if surface == 'pulls':
                    assert command[7] == '-F' and command[8].startswith('in_reply_to=')
                    result['in_reply_to_id'] = int(command[8][12:])
                remote[surface].append(result)
            remote_path.write_text(json.dumps(remote))
            return completed(command, json.dumps(result))
        assert command[0] != 'gh', 'unexpected fixture gh request'
        return completed(command, '')
    prompt = kwargs.get('stdin_text') or next(
        (v for v in command if isinstance(v, str) and TEXT in v), '')
    assert TEXT.encode() in prompt.encode(), 'exact staged context bytes were lost'
    assert ANSWER not in prompt, 'another invocation history leaked into staged context'
    Path(WORKSPACE, 'provider-called').write_text('yes')
    usage = {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}
    if plugin.PLUGIN_ID == 'codex.cli':
        Path(request['paths']['artifacts_dir'], 'last_message.txt').write_text(ANSWER)
        return completed(command, 'diagnostic only')
    if plugin.PLUGIN_ID == 'github.copilot.cli':
        return completed(command, json.dumps({
            'type': 'assistant.message', 'data': {'content': ANSWER}}))
    return {'completed': completed(command, 'diagnostic only'), 'response_text': ANSWER,
            'session_id': 'fixture', 'share_url': '', 'result_event': None,
            'last_error': None, 'event_error': None, 'usage': usage, 'env': {}}

if plugin.PLUGIN_ID == 'jira.rest':
    os.environ['JIRA_AUTH_HEADER'] = 'Basic fixture-only'
    plugin.jira_request = lambda *a, **k: {'key': 'DEMO-1', 'fields': {
        'summary': 'Document fixture title', 'description': None, 'attachment': [{
            'id': '1', 'filename': 'evidence.txt', 'mimeType': 'text/plain',
            'size': len(ATTACHMENT), 'content': 'https://example.test/attachment'}]}}
    def download(url: str, auth: str, dest: str, limit: int) -> int:
        "Write the deterministic downloaded attachment bytes."
        Path(dest).write_bytes(ATTACHMENT)
        return len(ATTACHMENT)
    plugin.fetch_to_file = download
elif plugin.PLUGIN_ID == 'openspec':
    def backend(request: dict, payload: dict, working_dir: str, prompt: str,
                **kwargs: object) -> tuple:
        "Assert the exact staged provider prompt and return a typed plan/report."
        assert TEXT.encode() in prompt.encode(), 'exact staged context bytes were lost'
        data = ({'passed': True, 'report_markdown': '# PASS', 'findings': []}
                if request['action'] == 'verify' else PLAN)
        text = json.dumps(data)
        usage = {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}
        return 'gh-copilot', completed([], text), text, usage
    plugin.generate_backend_response = backend
    plugin.run_openspec_validate = lambda *a, **k: completed([], 'validated')
elif plugin.PLUGIN_ID not in ('shell', 'openai.compatible'):
    plugin.run_command = fake
    if plugin.PLUGIN_ID == 'codex.cli':
        plugin.extract_codex_usage = lambda _: {
            'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}
    if plugin.PLUGIN_ID == 'github.copilot.cli':
        plugin.require_json_transport = lambda *a: None
        plugin.snapshot_copilot_sessions = lambda *a: {}
        plugin.extract_copilot_session_usage = lambda *a, **k: {
            'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}
    if plugin.PLUGIN_ID == 'opencode.cli':
        plugin.export_session_data = lambda *a, **k: None
raise SystemExit(plugin.main())
"""


def document(name: str, access: str) -> dict:
    """Return a reserved process document binding for installed materialization."""
    return {"$document": name, "access": access}


class FakeChat(http.server.BaseHTTPRequestHandler):
    """Capture exact decoded prompt bytes and return a bounded answer/usage."""

    def do_POST(self) -> None:
        """Handle only the local fake endpoint; retain observations in memory."""
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if hasattr(self.server, "expected_authorization"):
            self.server.authenticated_requests += int(
                self.headers.get("Authorization") == self.server.expected_authorization
            )
        self.server.requests.append(body)
        raw = json.dumps(
            {
                "choices": [{"message": {"role": "assistant", "content": TEXT}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args: object) -> None:
        """Keep provider observations out of access logs."""


def retain_secret_free_runtime(home: Path, transcripts: Path, secret: str) -> dict:
    """Copy/scan runtime-owned data, excluding installed source and fixture drivers.

    The canary is generated only in memory, delivered as an environment secret,
    and never written to fixture source or observations. This scans all retained
    runtime metadata/log/artifact/config bytes, not a source-privacy heuristic.
    """
    destination = Path(
        tempfile.mkdtemp(prefix="namespace-runtime-audit-", dir=fixture_base())
    )
    raw = secret.encode()
    needles = {raw, raw.hex().encode(), json.dumps(secret)[1:-1].encode()}
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        value = encode(raw)
        needles.update((value, value.rstrip(b"=")))
    overlap = max(map(len, needles))
    inventory = {}
    total = 0
    inputs = [
        (prefix, root, path)
        for prefix, root in (("runtime", home), ("cli", transcripts))
        for path in sorted(root.rglob("*"))
    ]
    for prefix, root, path in inputs:
        relative = path.relative_to(root)
        if prefix == "runtime" and relative.parts[0] == "plugins":
            continue  # Immutable installed code is audited by selected-byte hashes.
        if path.is_symlink():
            raise AssertionError("runtime audit refuses symlinked retained data")
        if not path.is_file():
            continue
        if len(inventory) >= 10000:
            raise AssertionError("runtime audit file budget exceeded")
        relative = Path(prefix) / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        tail = b""
        with path.open("rb") as source, target.open("wb") as output:
            while True:
                chunk = source.read(131072)
                if not chunk:
                    break
                total += len(chunk)
                if total > 128 * 1024 * 1024:
                    raise AssertionError("runtime audit byte budget exceeded")
                window = tail + chunk
                if any(needle in window for needle in needles):
                    raise AssertionError(
                        "synthetic secret persisted in runtime data (value withheld)"
                    )
                tail = window[-overlap:]
                output.write(chunk)
                digest.update(chunk)
        inventory[relative.as_posix()] = digest.hexdigest()
    if not inventory or not any(name.endswith("runner.log") for name in inventory):
        raise AssertionError(
            "runtime secret audit did not retain actual execution logs"
        )
    return {
        "directory": str(destination),
        "files": inventory,
        "bytes": total,
        "synthetic_secret_absent": True,
        "secret_fingerprint": hashlib.sha256(raw).hexdigest(),
        "scope": (
            "runtime home excluding plugins plus captured CLI output; "
            "fixture source/provider observations excluded"
        ),
    }


def stop_fixture_server(
    server: http.server.HTTPServer, thread: threading.Thread
) -> None:
    """Bound shutdown of the fixture's existing host-side server thread."""
    server._BaseServer__shutdown_request = True
    thread.join(1)
    if thread.is_alive():
        raise AssertionError("fixture host HTTP server did not stop within deadline")


class HeldChat(FakeChat):
    """Hold a complete file-mode request until cancellation closes its connection."""

    def do_POST(self) -> None:
        """Record exact text, withhold the answer/usage, and observe peer exit."""
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        self.server.received.set()
        try:
            deadline = time.monotonic() + 60
            while not self.server.release.wait(0.05):
                if time.monotonic() >= deadline:
                    self.server.hold_timed_out.set()
                    return
                if select.select([self.connection], [], [], 0)[0]:
                    if not self.connection.recv(1, socket.MSG_PEEK):
                        self.server.peer_closed.set()
                        return
            # A premature release must not masquerade as cancellation coverage.
            self.server.response_attempted.set()
            raw = json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "canceled provider answer",
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 777, "completion_tokens": 888},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            self.server.peer_closed.set()
        finally:
            self.server.finished.set()


class InstalledDocuments(unittest.TestCase):
    """Run each changed package in its own fresh home; never replace an install."""

    @contextmanager
    def fixture(self, package: str) -> Iterator[None]:
        """Copy the finite payload and install a fixture-only provider wrapper."""
        with disposable_directory("document-actions-") as temporary:
            self.root = Path(temporary).resolve()
            self.home, self.workspace, self.source = [
                self.root / name for name in ("home", "workspace", "source")
            ]
            self.workspace.mkdir()
            self.source.mkdir()
            self.package = package
            self.sequence = 0
            self.env = {
                "PATH": os.environ.get("PATH", os.defpath),
                "HOME": str(self.root),
                "TMPDIR": str(self.root),
                "PYTHONDONTWRITEBYTECODE": "1",
                "OPENAI_COMPATIBLE_API_KEY": "fixture-only-chat-secret",
            }
            self.env["JIRA_AUTH_HEADER"] = "Basic fixture-only"
            target = self.source / "package"
            target.mkdir()
            source = ROOT / "plugins" / package
            manifest = yaml.safe_load((source / "plugin.yaml").read_text())
            self.plugin_version = manifest["version"]
            for relative in manifest["package"]["files"]:
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / relative, path)
            manifest["prerequisites"] = {"binaries": [{"name": "python3"}]}
            if package not in ("shell", "openai.compatible"):
                wrapper = target / "fixture.py"
                wrapper.write_text(
                    "#!/usr/bin/env python3\n"
                    + f"WORKSPACE={str(self.workspace)!r}\nTEXT={TEXT!r}\n"
                    + f"ANSWER={ANSWER!r}\nATTACHMENT={ATTACHMENT!r}\nPLAN={PLAN!r}\n"
                    + WRAPPER
                )
                wrapper.chmod(0o755)
                manifest["entrypoint"]["command"] = "fixture.py"
                manifest["package"]["files"].append("fixture.py")
            (target / "plugin.yaml").write_text(yaml.safe_dump(manifest))
            self.cli("home", "init", "--mode", "standalone")
            config = self.home / "config.yaml"
            config.write_text(
                config.read_text().replace("process: false", "process: true")
            )
            self.cli("plugins", "install", "--path", str(target / "plugin.yaml"))
            self.assertIn(self.plugin_version, self.cli("plugins", "inspect", package))
            installed = self.home / "plugins" / package / self.plugin_version
            selected_files = {"plugin.yaml", *manifest["package"]["files"]}
            self.assertEqual(
                {
                    path.relative_to(installed).as_posix()
                    for path in installed.rglob("*")
                    if path.is_file()
                },
                selected_files,
            )
            for relative in selected_files:
                self.assertEqual(
                    (installed / relative).read_bytes(),
                    (target / relative).read_bytes(),
                )
            try:
                yield
            finally:
                settle_runs(self.home, self.cli)

    def cli(self, *args: str) -> str:
        """Run a bounded current public CLI command against this disposable home."""
        transport = getattr(self, "namespace_peers", None)
        execute = transport.run if transport is not None else subprocess.run
        result = execute(
            [runtime_binary(), "--home", str(self.home), *json_args(args)],
            cwd=self.workspace,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        transcripts = getattr(self, "namespace_transcripts", None)
        if transcripts is not None:
            self.namespace_transcript_index += 1
            (transcripts / f"cli-{self.namespace_transcript_index:04}.json").write_text(
                json.dumps(
                    {
                        "command": args[:2],
                        "exit": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    }
                )
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def run_flow(
        self,
        actions: list[tuple[str, dict]],
        initial: dict[str, str] | None = None,
        trees: tuple[str, ...] = (),
        network: dict | None = None,
        expected: str = "success",
        after_start: Callable[[dict], None] | None = None,
    ) -> dict:
        """Dispatch a new identity, optionally coordinate cancellation, then inspect."""
        self.sequence += 1
        self.run_id = "documents_" + str(self.sequence)
        docs = {
            name: {"kind": "file", "max_bytes": 1048576, "initial": text}
            for name, text in (initial or {}).items()
        }
        docs.update(
            {
                name: {"kind": "file", "max_bytes": 1048576}
                for name in ("out", "title", "body", "state", "extra")
                if name not in docs
            }
        )
        docs.update(
            {
                name: {"kind": "tree", "max_bytes": 1048576, "max_files": 20}
                for name in trees
            }
        )
        flow = {
            "schema": "transist.flow.v1",
            "id": self.run_id,
            "version": "0.1.0",
            "plugins": {self.package: self.plugin_version},
            "workspace": {"path": str(self.workspace), "access": "full"},
            "network": network or {"mode": "none"},
            "documents": docs,
            "steps": [
                {
                    "id": "call_" + str(index),
                    "uses": f"process:{self.package}/{action}",
                    "with": payload,
                }
                for index, (action, payload) in enumerate(actions)
            ],
        }
        path = self.source / (self.run_id + ".yaml")
        path.write_text(json.dumps(flow))
        self.cli("workflows", "check", "--path", str(path))
        self.cli("workflows", "install", "--path", str(path), "--approve")
        reference = self.run_id + "@0.1.0"
        self.run_root = self.home / "runs" / ("run_" + self.run_id)
        launch = self.cli("workflows", "run", reference, "--run-ref", self.run_id)
        if after_start is not None:
            after_start(json.loads(launch))
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            report = json.loads(self.cli("runs", "show", self.run_id))
            if report["status"] in TERMINAL:
                break
            time.sleep(0.05)
        self.assertEqual(
            report["status"],
            expected,
            json.dumps(
                {
                    name: state.get("last_result")
                    for name, state in report.get("steps", {}).items()
                }
            ),
        )
        # Runtime staging must not become durable request/output metadata.
        for record in self.run_root.rglob("*.json"):
            if record.name in ("recipe.json", "inputs.json"):
                continue
            data = record.read_text()
            self.assertNotIn("/documents/read/binding-", data)
            self.assertNotIn("/documents/write/binding-", data)
        return report

    def blob(self, name: str) -> bytes:
        """Read the actual committed document blob by its authoritative head digest."""
        heads = json.loads((self.run_root / "documents/heads.json").read_text())
        digest = heads[name]["blob"]["sha256"]
        return (self.run_root / "documents/blobs" / digest).read_bytes()

    def test_shell(self) -> None:
        """Workspace-only source publishes exact UTF-8 bytes outside the repository."""
        with self.fixture("shell"):
            (self.workspace / "diagnostic.txt").write_bytes(TEXT.encode())
            report = self.run_flow(
                [
                    (
                        "read_file",
                        {
                            "path": "diagnostic.txt",
                            "output_file": document("out", "write"),
                            "output_mode": "compact",
                        },
                    )
                ]
            )
            self.assertEqual(self.blob("out"), TEXT.encode())
            self.assertEqual(
                report["steps"]["call_0"]["last_result"]["output"]["text"], TEXT
            )
            denied = self.run_flow(
                [
                    (
                        "read_file",
                        {
                            "path": document("context", "read"),
                            "output_file": document("out", "write"),
                        },
                    )
                ],
                {"context": TEXT},
                expected="failed",
            )
            self.assertEqual(
                denied["steps"]["call_0"]["last_result"]["error"]["code"],
                "validation_error",
            )
            self.assertNotIn(
                "out", json.loads((self.run_root / "documents/heads.json").read_text())
            )

    def test_jira(self) -> None:
        """Issue output feeds attachment input; the published tree relocates exactly."""
        with self.fixture("jira.rest"):
            self.run_flow(
                [
                    (
                        "get_issue",
                        {
                            "issue_key": "DEMO-1",
                            "site_url": "https://example.test",
                            "output_file": document("out", "write"),
                            "output_mode": "compact",
                            "secret_refs": ["JIRA_AUTH_HEADER"],
                        },
                    ),
                    (
                        "download_attachments",
                        {
                            "jira_issue_file": document("out", "read"),
                            "output_dir": document("attachments", "write"),
                            "output_mode": "compact",
                            "secret_refs": ["JIRA_AUTH_HEADER"],
                        },
                    ),
                ],
                trees=("attachments",),
            )
            tree = json.loads(self.blob("attachments"))
            relocated = self.root / "relocated"
            relocated.mkdir()
            for name, member in tree["files"].items():
                path = relocated / name
                path.parent.mkdir(parents=True, exist_ok=True)
                (path).write_bytes(
                    (self.run_root / "documents/blobs" / member["sha256"]).read_bytes()
                )
            manifest = json.loads((relocated / "manifest.json").read_text())
            for member in manifest["downloaded"]:
                self.assertFalse(Path(member["local_path"]).is_absolute())
                self.assertNotIn("..", Path(member["local_path"]).parts)
                self.assertEqual(
                    (relocated / member["local_path"]).read_bytes(), ATTACHMENT
                )
            self.assertNotIn(str(self.root), json.dumps(manifest))

    def test_git(self) -> None:
        """Staged commit summary/PR JSON are data while Git cwd stays selected."""
        with self.fixture("git.cli"):
            self.run_flow(
                [
                    (
                        "commit_worktree",
                        {
                            "jira_ticket_id": "DEMO-1",
                            "summary_file": document("summary", "read"),
                            "output_mode": "compact",
                        },
                    ),
                    (
                        "sync_branch_from_pr",
                        {
                            "pr_file": document("pr", "read"),
                            "remote": "origin",
                            "output_mode": "compact",
                        },
                    ),
                ],
                {
                    "summary": "Document fixture title\n",
                    "pr": json.dumps({"pull_request": {"baseRefName": "main"}}),
                },
            )

    def test_github(self) -> None:
        """PR body and review plan/state/output work across staged invocations."""
        with self.fixture("github.cli"):
            self.run_flow(
                [
                    (
                        "pr_create",
                        {
                            "repo": "fixture/repo",
                            "jira_issue_file": document("issue", "read"),
                            "body_file": document("draft", "read"),
                            "output_file": document("out", "write"),
                            "output_mode": "compact",
                        },
                    ),
                    (
                        "pr_publish_review_replies",
                        {
                            "repo": "fixture/repo",
                            "pull_request": "1",
                            "response_plan_file": document("review", "read"),
                            "state_file": document("state", "write"),
                            "output_file": document("extra", "write"),
                            "output_mode": "compact",
                        },
                    ),
                    (
                        "pr_publish_review_replies",
                        {
                            "repo": "fixture/repo",
                            "pull_request": "1",
                            "response_plan_file": document("review", "read"),
                            "state_file": document("state", "update"),
                            "output_file": document("extra", "write"),
                            "output_mode": "compact",
                        },
                    ),
                    (
                        "pr_publish_review_replies",
                        {
                            "repo": "fixture/repo",
                            "pull_request": "1",
                            "response_plan_file": document("review", "read"),
                            "state_file": document("remote_state", "update"),
                            "output_file": document("extra", "write"),
                            "output_mode": "compact",
                        },
                    ),
                ],
                {
                    "issue": json.dumps(
                        {"key": "DEMO-1", "summary": "Document fixture title"}
                    ),
                    "draft": TEXT,
                    "remote_state": '{"responses":{}}',
                    "review": json.dumps(
                        {
                            "responses": [
                                {
                                    "comment_id": "1",
                                    "source": "pr_conversation",
                                    "reply": "Addressed café",
                                    "url": "https://example.test/comment/1",
                                },
                                {
                                    "comment_id": "thread-2",
                                    "source": "review_thread",
                                    "review_comment_id": "42",
                                    "reply": "Addressed thread café",
                                    "url": "https://example.test/comment/42",
                                },
                            ]
                        }
                    ),
                },
            )
            output = json.loads(self.blob("extra"))
            self.assertEqual(output["skipped_count"], 2)
            self.assertEqual(output["posted_count"], 0)
            self.assertTrue(
                all(
                    item["reason"] == "remote_duplicate_reply"
                    for item in output["responses"]
                )
            )
            state = json.loads(self.blob("state"))["responses"]
            remote = json.loads((self.workspace / "fixture-gh-remote.json").read_text())
            for surface, identity in (("issues", "1"), ("pulls", "thread-2")):
                self.assertEqual(len(remote[surface]), 1)
                self.assertEqual(
                    remote[surface][0]["body"].splitlines()[-1],
                    "<!-- transist-review-publication:"
                    + state[identity]["publication_id"]
                    + " -->",
                )
                self.assertEqual(
                    sum(
                        "GET" in call and f"/{surface}/" in call[4]
                        for call in remote["requests"]
                    ),
                    2,
                )
            self.assertEqual(remote["pulls"][0]["in_reply_to_id"], 42)
            self.assertEqual(len(state), 2)

    def check_model(self, package: str) -> None:
        """Assert exact provider context and isolated lean history."""
        with self.fixture(package):
            payload = {
                "prompt": "Perform the fixture task",
                "context_files": [document("context", "read")],
                "response_file": document("out", "write"),
                "output_mode": "compact",
                "history_mode": "invocation",
                "max_context_file_bytes": 65536,
                "max_prompt_bytes": 98304,
            }
            if package != "codex.cli":
                payload.update(
                    summary_title_file=document("title", "write"),
                    pr_body_file=document("body", "write"),
                )
            action = "exec_task_cli" if package == "cursor.cli" else "exec_task"
            for _ in range(2):
                report = self.run_flow([(action, payload)], {"context": TEXT})
                self.assertEqual(self.blob("out").decode().strip(), ANSWER)
                output = report["steps"]["call_0"]["last_result"]["output"]
                self.assertEqual(
                    report["steps"]["call_0"]["last_result"]["usage"]["total_tokens"], 5
                )
                if package != "codex.cli":
                    self.assertEqual(
                        self.blob("title").decode().strip(), output["summary_title"]
                    )
                    self.assertEqual(
                        self.blob("body").decode().strip(), output["pr_body_text"]
                    )
                self.assertNotIn("stdout", output)
                self.assertNotIn("stderr", output)
            self.assertFalse(list(self.workspace.rglob("*.invocations")))

    def test_codex(self) -> None:
        """Codex staged context/response with preserved byte budgets."""
        self.check_model("codex.cli")

    def test_cursor(self) -> None:
        """Cursor staged context, response, summary and PR body."""
        self.check_model("cursor.cli")

    def test_copilot(self) -> None:
        """Copilot staged context, response, summary and PR body."""
        self.check_model("github.copilot.cli")

    def test_opencode(self) -> None:
        """OpenCode staged context, response, summary and PR body."""
        self.check_model("opencode.cli")

    def test_openspec(self) -> None:
        """OpenSpec documents coexist with workspace source deliverables."""
        with self.fixture("openspec"):
            (self.workspace / "openspec/changes").mkdir(parents=True)
            self.run_flow(
                [
                    (
                        "generate_plan",
                        {
                            "change_name": "fixture",
                            "prompt": "Plan fixture",
                            "context_files": [document("context", "read")],
                            "response_file": document("out", "write"),
                            "change_metadata_file": document("state", "write"),
                            "output_mode": "compact",
                            "max_context_file_bytes": 65536,
                            "max_prompt_bytes": 98304,
                        },
                    ),
                    (
                        "verify",
                        {
                            "change_name": "fixture",
                            "prompt": "Verify fixture",
                            "context_files": [document("context", "read")],
                            "report_file": document("extra", "write"),
                            "output_mode": "compact",
                            "max_context_file_bytes": 65536,
                            "max_prompt_bytes": 98304,
                        },
                    ),
                ],
                {"context": TEXT},
            )
            self.assertEqual(self.blob("out").decode().strip(), "# Plan")
            self.assertEqual(self.blob("extra").decode().strip(), "# PASS")
            metadata = json.loads(self.blob("state"))
            self.assertEqual(
                metadata["change_dir"], str(self.workspace / "openspec/changes/fixture")
            )
            self.assertNotIn("/documents/write/binding-", json.dumps(metadata))
            self.assertTrue(
                (self.workspace / "openspec/changes/fixture/proposal.md").is_file()
            )

    def test_openai(self) -> None:
        """Exact bytes reach loopback transport and document publication."""
        with self.fixture("openai.compatible"), http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeChat
        ) as server:
            server.requests = []
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.run_flow(
                    [
                        (
                            "chat_complete",
                            {
                                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                                "model": "fixture",
                                "user_text_file": document("context", "read"),
                                "system_prompt": "Preserve exact text",
                                "response_file": document("out", "write"),
                                "secret_refs": ["OPENAI_COMPATIBLE_API_KEY"],
                            },
                        )
                    ],
                    {"context": TEXT},
                    network={
                        "mode": "allowlist",
                        "allow_hosts": ["127.0.0.1"],
                        "allow_ports": [server.server_port],
                        "tls_required": False,
                    },
                )
                self.assertEqual(
                    server.requests[0]["messages"],
                    [
                        {"role": "system", "content": "Preserve exact text"},
                        {"role": "user", "content": TEXT},
                    ],
                )
                self.assertEqual(self.blob("out"), TEXT.encode())
                self.run_flow(
                    [
                        (
                            "chat_complete",
                            {
                                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                                "model": "fixture",
                                "user_text_file": document("context", "read"),
                                "max_prompt_bytes": 2,
                                "response_file": document("out", "write"),
                                "secret_refs": ["OPENAI_COMPATIBLE_API_KEY"],
                            },
                        )
                    ],
                    {"context": TEXT},
                    network={
                        "mode": "allowlist",
                        "allow_hosts": ["127.0.0.1"],
                        "allow_ports": [server.server_port],
                        "tls_required": False,
                    },
                    expected="failed",
                )
                self.assertEqual(
                    len(server.requests), 1, "oversized prompt reached transport"
                )
                self.assertNotIn(
                    "out",
                    json.loads((self.run_root / "documents/heads.json").read_text()),
                )
            finally:
                server.shutdown()
                thread.join()

    def test_openai_file_cancellation(self) -> None:
        """Cancel a held file-mode call without publishing or leaking its outcome."""
        spec = importlib.util.spec_from_file_location(
            "document_cancellation_native", ROOT / "plugins/shell/process_lifecycle.py"
        )
        native = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = native
        spec.loader.exec_module(native)
        observer = native.NativeProcesses()
        previous = "committed response before canceled update\n"
        captured = {}
        with self.fixture("openai.compatible"), http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), HeldChat
        ) as server:
            server.requests = []
            for name in (
                "received",
                "release",
                "peer_closed",
                "finished",
                "hold_timed_out",
                "response_attempted",
            ):
                setattr(server, name, threading.Event())
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def cancel_active(announcement: dict) -> None:
                """Cancel after exact file bytes arrive during a live invocation."""
                self.assertTrue(server.received.wait(10), "provider received no file")
                self.assertEqual(
                    server.requests[0]["messages"],
                    [
                        {"role": "system", "content": "Preserve exact text"},
                        {"role": "user", "content": TEXT},
                    ],
                )
                self.assertEqual(
                    server.requests[0]["messages"][1]["content"].encode(), TEXT.encode()
                )
                report = json.loads(self.cli("runs", "show", self.run_id))
                self.assertNotIn(report["status"], TERMINAL)
                self.assertEqual(report["steps"]["call_0"]["status"], "running")
                runner = observer.read(announcement["runner_pid"])
                self.assertIsNotNone(runner, "runner exited before cancellation")
                self.assertTrue(runner.live)
                captured["runner"] = runner
                captured["heads"] = json.loads(
                    (self.run_root / "documents/heads.json").read_text()
                )
                self.assertEqual(self.blob("out"), previous.encode())
                self.assertFalse(server.response_attempted.is_set())
                self.cli("runs", "cancel", self.run_id)

            try:
                report = self.run_flow(
                    [
                        (
                            "chat_complete",
                            {
                                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                                "model": "fixture",
                                "timeout_ms": 30000,
                                "user_text_file": document("context", "read"),
                                "system_prompt": "Preserve exact text",
                                "response_file": document("out", "update"),
                                "secret_refs": ["OPENAI_COMPATIBLE_API_KEY"],
                            },
                        )
                    ],
                    {"context": TEXT, "out": previous},
                    network={
                        "mode": "allowlist",
                        "allow_hosts": ["127.0.0.1"],
                        "allow_ports": [server.server_port],
                        "tls_required": False,
                    },
                    expected="canceled",
                    after_start=cancel_active,
                )
                runner = captured["runner"]
                deadline = time.monotonic() + 15
                while True:
                    current = observer.read(runner.pid)
                    if (
                        current is None
                        or current.birth != runner.birth
                        or not current.live
                    ):
                        break
                    self.assertLess(time.monotonic(), deadline, "live canceled runner")
                    time.sleep(0.05)
                self.assertTrue(server.peer_closed.wait(5), "provider socket left open")
                self.assertTrue(server.finished.wait(5), "provider worker left running")
                self.assertFalse(server.response_attempted.is_set())
                self.assertFalse(server.hold_timed_out.is_set())
                self.assertEqual(len(server.requests), 1)
                canceled_id = self.run_id
                report = json.loads(self.cli("runs", "show", canceled_id))
                self.assertEqual(report["status"], "canceled")
                canceled_root = self.run_root
                self.assertEqual(
                    json.loads((canceled_root / "documents/heads.json").read_text()),
                    captured["heads"],
                )
                self.assertEqual(self.blob("out"), previous.encode())
                result = report["steps"]["call_0"].get("last_result") or {}
                self.assertNotEqual(result.get("status"), "success")
                self.assertFalse(result.get("usage"))
                self.assertFalse((result.get("output") or {}).get("response_text"))
                self.assertFalse(list(canceled_root.rglob("*usage.json")))
                self.assertFalse(list((canceled_root / "active_invocations").glob("*")))
                for record in canceled_root.rglob("*.json"):
                    self.assertNotIn("canceled provider answer", record.read_text())

                # A fresh namespace in the same installed home must receive only
                # its own file prompt, response revision and real fake-provider usage.
                with http.server.ThreadingHTTPServer(
                    ("127.0.0.1", 0), FakeChat
                ) as next_server:
                    next_server.requests = []
                    next_thread = threading.Thread(
                        target=next_server.serve_forever, daemon=True
                    )
                    next_thread.start()
                    try:
                        next_url = f"http://127.0.0.1:{next_server.server_port}/v1"
                        fresh = self.run_flow(
                            [
                                (
                                    "chat_complete",
                                    {
                                        "base_url": next_url,
                                        "model": "fixture",
                                        "user_text_file": document("context", "read"),
                                        "response_file": document("out", "write"),
                                        "secret_refs": ["OPENAI_COMPATIBLE_API_KEY"],
                                    },
                                )
                            ],
                            {"context": TEXT},
                            network={
                                "mode": "allowlist",
                                "allow_hosts": ["127.0.0.1"],
                                "allow_ports": [next_server.server_port],
                                "tls_required": False,
                            },
                        )
                        self.assertEqual(
                            next_server.requests[0]["messages"],
                            [{"role": "user", "content": TEXT}],
                        )
                        self.assertEqual(len(next_server.requests), 1)
                        self.assertEqual(self.blob("out"), TEXT.encode())
                        self.assertEqual(
                            fresh["steps"]["call_0"]["last_result"]["usage"][
                                "total_tokens"
                            ],
                            5,
                        )
                        self.assertEqual(
                            json.loads(
                                (canceled_root / "documents/heads.json").read_text()
                            ),
                            captured["heads"],
                        )
                        canceled_again = json.loads(
                            self.cli("runs", "show", canceled_id)
                        )
                        self.assertEqual(canceled_again["status"], "canceled")
                        self.assertEqual(
                            canceled_again["steps"]["call_0"].get("last_result") or {},
                            result,
                        )
                    finally:
                        next_server.shutdown()
                        next_thread.join(timeout=5)
                        self.assertFalse(next_thread.is_alive())
                print(
                    "PASS OpenAI file cancellation: exact prompt, unchanged heads, "
                    "no success/usage, closed socket, stopped runner, "
                    "isolated next run",
                    flush=True,
                )
            finally:
                server.release.set()
                server.shutdown()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

    def test_openai_namespace_peer_boundaries(self) -> None:
        """Exact fake HTTP peers work in private loopback; other origins stay denied."""
        with self.fixture("openai.compatible"), http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeChat
        ) as server, http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeChat
        ) as unregistered, socket.socket() as wrong_port:
            server.requests = []
            unregistered.requests = []
            canary = "review2-only-" + secrets.token_urlsafe(32)
            self.env["OPENAI_COMPATIBLE_API_KEY"] = canary
            server.expected_authorization = "Bearer " + canary
            server.authenticated_requests = 0
            wrong_port.bind(("127.0.0.1", 0))  # Owned, deliberately not listening.
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            other_thread = threading.Thread(
                target=unregistered.serve_forever,
                kwargs={"poll_interval": 0.05},
                daemon=True,
            )
            thread.start()
            other_thread.start()
            with socket.create_connection(unregistered.server_address, timeout=1):
                pass  # Prove this unregistered peer really LISTENS on the host.
            receipt = self.root / "namespace-peers.json"
            self.namespace_transcripts = self.root / "namespace-cli-metadata"
            self.namespace_transcripts.mkdir(mode=0o700)
            self.namespace_transcript_index = 0
            original_env = self.env
            try:
                with NamespacePeers(
                    [server],
                    self.workspace / "namespace-tools",
                    [self.home / "plugins"],
                    receipt,
                ) as transport:
                    self.namespace_peers = transport
                    self.env = transport.environment(original_env)
                    try:
                        for host, port, mode, expected in (
                            ("127.0.0.1", server.server_port, "loopback", "success"),
                            (
                                "127.0.0.1",
                                wrong_port.getsockname()[1],
                                "loopback",
                                "failed",
                            ),
                            ("203.0.113.1", server.server_port, "loopback", "failed"),
                            ("127.0.0.1", server.server_port, "none", "failed"),
                            (
                                "127.0.0.1",
                                unregistered.server_port,
                                "loopback" if sys.platform == "linux" else "none",
                                "failed",
                            ),
                        ):
                            self.run_flow(
                                [
                                    (
                                        "chat_complete",
                                        {
                                            "base_url": f"http://{host}:{port}/v1",
                                            "model": "fixture",
                                            "user_text_file": document(
                                                "context", "read"
                                            ),
                                            "response_file": document("out", "write"),
                                            "secret_refs": [
                                                "OPENAI_COMPATIBLE_API_KEY"
                                            ],
                                        },
                                    )
                                ],
                                {"context": TEXT},
                                network={"mode": mode},
                                expected=expected,
                            )
                            if expected == "success":
                                self.assertEqual(self.blob("out"), TEXT.encode())
                            self.assertEqual(len(server.requests), 1)
                            self.assertEqual(unregistered.requests, [])
                        settle_runs(self.home, self.cli)
                        self.assertEqual(server.authenticated_requests, 1)
                    finally:
                        self.env = original_env
                        del self.namespace_peers
                proof = json.loads(receipt.read_text())
                proof["business_http_requests"] = len(server.requests)
                proof["unregistered_host_business_requests"] = len(
                    unregistered.requests
                )
                proof["namespace_binding_count"] = sum(
                    event["event"] == "bound" for event in proof["events"]
                )
                if sys.platform == "linux":
                    bound = [
                        event for event in proof["events"] if event["event"] == "bound"
                    ]
                    self.assertTrue(
                        bound, "actual private namespace peer was never bound"
                    )
                    self.assertTrue(
                        all(event["netns"] != event["host_netns"] for event in bound)
                    )
                    self.assertTrue(
                        all(event["ports"] == [server.server_port] for event in bound)
                    )
                    self.assertTrue(
                        any(event["loopback_up"] is False for event in bound)
                    )
                self.assertEqual(proof["listeners_remaining"], 0)
                self.assertEqual(proof["namespace_fds_remaining"], 0)
                self.assertFalse(proof["controller_alive"])
                print("NAMESPACE PEER PROOF " + json.dumps(proof), flush=True)
                print(
                    "SYNTHETIC SECRET SCAN "
                    + json.dumps(
                        retain_secret_free_runtime(
                            self.home, self.namespace_transcripts, canary
                        )
                    ),
                    flush=True,
                )
            finally:
                del self.namespace_transcripts
                stop_fixture_server(server, thread)
                stop_fixture_server(unregistered, other_thread)

    def test_namespace_peer_run_abort_is_bounded(self) -> None:
        """Timeout/interrupt reclaim inherited-pipe writers with a live controller."""
        with self.fixture("openai.compatible"), http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), FakeChat
        ) as server:
            server.requests = []
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            thread.start()
            receipt = self.root / "abort-peers.json"
            original_env = self.env
            try:
                with NamespacePeers(
                    [server],
                    self.workspace / "abort-tools",
                    [self.home / "plugins"],
                    receipt,
                ) as transport:
                    self.namespace_peers = transport
                    self.env = transport.environment(original_env)
                    try:
                        origin = f"http://127.0.0.1:{server.server_port}/v1"
                        self.run_flow(
                            [
                                (
                                    "chat_complete",
                                    {
                                        "base_url": origin,
                                        "model": "fixture",
                                        "user_text_file": document("context", "read"),
                                        "response_file": document("out", "write"),
                                        "secret_refs": ["OPENAI_COMPATIBLE_API_KEY"],
                                    },
                                )
                            ],
                            {"context": TEXT},
                            network={"mode": "loopback"},
                        )
                        settle_runs(self.home, self.cli)
                        for mode in ("timeout", "interrupt", "fast-detach"):
                            marker = self.root / (mode + "-writer.json")
                            program = (
                                "import subprocess,sys,time,json; "
                                "from pathlib import Path; writer=subprocess.Popen("
                                "[sys.executable,'-c',"
                                "'import time; time.sleep(600)'],"
                                "start_new_session=True); "
                                f"Path({str(marker)!r}).write_text("
                                "json.dumps({'pid':writer.pid})); "
                                "print('writer started',flush=True); "
                                + (
                                    "time.sleep(0.05)"
                                    if mode == "fast-detach"
                                    else "time.sleep(600)"
                                )
                            )
                            interrupter = None
                            if mode == "interrupt":

                                def interrupt_when_started() -> None:
                                    """Interrupt our caller once the writer exists."""
                                    deadline = time.monotonic() + 5
                                    while (
                                        not marker.exists()
                                        and time.monotonic() < deadline
                                    ):
                                        time.sleep(0.005)
                                    os.kill(os.getpid(), signal.SIGINT)

                                interrupter = threading.Thread(
                                    target=interrupt_when_started
                                )
                                interrupter.start()
                            started = time.monotonic()
                            try:
                                expected = (
                                    subprocess.TimeoutExpired
                                    if mode != "interrupt"
                                    else KeyboardInterrupt
                                )
                                with self.assertRaises(expected):
                                    transport.run(
                                        [sys.executable, "-c", program],
                                        capture_output=True,
                                        text=True,
                                        timeout=0.3 if mode != "interrupt" else 6,
                                    )
                            finally:
                                if interrupter is not None:
                                    interrupter.join(1)
                                    self.assertFalse(interrupter.is_alive())
                            self.assertLess(time.monotonic() - started, 7)
                            self.assertTrue(
                                marker.exists(),
                                "regression never spawned its pipe holder",
                            )
                            pid = json.loads(marker.read_text())["pid"]
                            observed = subprocess.run(
                                ["/bin/ps", "-p", str(pid), "-o", "stat="],
                                capture_output=True,
                                text=True,
                                timeout=1,
                            )
                            self.assertFalse(
                                observed.stdout.strip()
                                and not observed.stdout.strip().startswith("Z"),
                                "owned inherited-pipe writer survived",
                            )
                    finally:
                        self.env = original_env
                        del self.namespace_peers
                proof = json.loads(receipt.read_text())
                for key in (
                    "handlers_remaining",
                    "listeners_remaining",
                    "namespace_fds_remaining",
                    "pidfds_remaining",
                ):
                    self.assertEqual(proof[key], 0)
                self.assertEqual(proof["owned_live_processes"], [])
                self.assertIsNone(proof["error"])
                print("NAMESPACE ABORT PROOF " + json.dumps(proof), flush=True)
            finally:
                stop_fixture_server(server, thread)


if __name__ == "__main__":
    runtime_binary()
    fixture_base()
    selected = set(
        filter(None, os.environ.get("TRANSIST_TEST_PACKAGES", "").split(","))
    )
    methods = dict(
        zip(
            PACKAGES,
            (
                "shell",
                "jira",
                "git",
                "github",
                "codex",
                "cursor",
                "copilot",
                "opencode",
                "openspec",
                "openai",
            ),
        )
    )
    names = [
        "InstalledDocuments.test_" + method
        for package, method in methods.items()
        if not selected or "document-actions" in selected or package in selected
    ]
    if (
        not selected
        or "document-actions" in selected
        or "openai.compatible" in selected
    ):
        names.append("InstalledDocuments.test_openai_file_cancellation")
        names.append("InstalledDocuments.test_openai_namespace_peer_boundaries")
        names.append("InstalledDocuments.test_namespace_peer_run_abort_is_bounded")
    if not names:
        raise SystemExit("No document action fixtures selected")
    unittest.main(argv=[sys.argv[0], *names])
