"""Protocol, schema and workspace-boundary tests against a real local Git."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from fixtures import git, seed, snapshot

PACKAGE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("git_mcp_server", PACKAGE / "server.py")
assert SPEC is not None and SPEC.loader is not None
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


def request(method: str, params: Any = None, request_id: Any = 1) -> dict[str, Any]:
    """Build a JSON-RPC request for protocol tests; no transport side effects."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {} if params is None else params,
    }


def handshake() -> list[dict[str, Any]]:
    """Return the required MCP initialize and initialized exchange inputs."""
    return [
        request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        ),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]


class ServerTests(unittest.TestCase):
    """Exercise isolated stdio sessions and verify no repository mutation."""

    def setUp(self) -> None:
        """Seed each test's disposable repository under /tmp."""
        self.temp = tempfile.TemporaryDirectory(prefix="git-mcp-unit-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        seed(self.repo)

    def _exchange(
        self, data: bytes, env: dict[str, str] | None = None, cwd: Path | None = None
    ) -> list[dict[str, Any]]:
        completed = subprocess.run(
            [str(PACKAGE / "server.py")],
            input=data,
            cwd=cwd or self.repo,
            env=env or os.environ.copy(),
            capture_output=True,
            timeout=15,
            check=True,
        )
        self.assertEqual(completed.stderr, b"")
        for line in completed.stdout.splitlines():
            self.assertLessEqual(len(line), SERVER.MAX_MESSAGE)
        return [json.loads(line) for line in completed.stdout.splitlines()]

    def _session(
        self, calls: list[dict[str, Any]], **kwargs: Any
    ) -> list[dict[str, Any]]:
        data = b"".join(
            json.dumps(item).encode() + b"\r\n" for item in handshake() + calls
        )
        return self._exchange(data, **kwargs)[1:]

    def _call(self, name: str, arguments: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self._session(
            [
                request(
                    "tools/call",
                    {"name": name, "arguments": {} if arguments is None else arguments},
                )
            ],
            **kwargs,
        )[0]

    def test_discovery_ping_and_structured_outputs(self) -> None:
        """Verify discovery, IDs, schemas and real unchanged-repository output."""
        before = snapshot(self.repo)
        replies = self._session(
            [
                request("tools/list", request_id="list"),
                request("ping", request_id=7),
                request("tools/call", {"name": "current_branch"}, request_id="branch"),
                request(
                    "tools/call",
                    {"name": "list_branches", "arguments": {"limit": 2}},
                    request_id=9,
                ),
            ]
        )
        self.assertEqual([reply["id"] for reply in replies], ["list", 7, "branch", 9])
        tools = replies[0]["result"]["tools"]
        self.assertEqual(
            [tool["name"] for tool in tools], ["current_branch", "list_branches"]
        )
        self.assertNotIn("nextCursor", replies[0]["result"])
        self.assertEqual(replies[1]["result"], {})
        self.assertEqual(
            replies[2]["result"]["structuredContent"],
            {"branch": "main", "detached": False},
        )
        self.assertEqual(
            replies[3]["result"]["structuredContent"],
            {"branches": ["alpha", "main"], "truncated": True},
        )
        for tool, reply in zip(tools, replies[2:]):
            Draft202012Validator.check_schema(tool["inputSchema"])
            Draft202012Validator.check_schema(tool["outputSchema"])
            result = reply["result"]
            self.assertFalse(result["isError"])
            Draft202012Validator(tool["outputSchema"]).validate(
                result["structuredContent"]
            )
            self.assertEqual(
                json.loads(result["content"][0]["text"]), result["structuredContent"]
            )
            self.assertNotIn("usage", result)
        self.assertEqual(snapshot(self.repo), before)

    def test_default_limit_exact_limit_and_detached(self) -> None:
        """Exercise default/exact list bounds and detached HEAD semantics."""
        expected = {"branches": ["alpha", "main", "zeta"], "truncated": False}
        self.assertEqual(
            self._call("list_branches")["result"]["structuredContent"], expected
        )
        self.assertEqual(
            self._call("list_branches", {"limit": 3})["result"]["structuredContent"],
            expected,
        )
        git(self.repo, "checkout", "--detach")
        self.assertEqual(
            self._call("current_branch")["result"]["structuredContent"],
            {"branch": None, "detached": True},
        )

    def test_unborn_head(self) -> None:
        """Return the symbolic name of a branch without a first commit."""
        empty = self.root / "empty"
        empty.mkdir()
        git(empty, "init", "--initial-branch=unborn")
        self.assertEqual(
            self._call("current_branch", cwd=empty)["result"]["structuredContent"],
            {"branch": "unborn", "detached": False},
        )
        self.assertEqual(
            self._call("list_branches", cwd=empty)["result"]["structuredContent"],
            {"branches": [], "truncated": False},
        )

    def test_invalid_actions_and_input(self) -> None:
        """Reject command/path authority and malformed bounds before dispatch."""
        cases = [
            ("checkout", {}),
            ("current_branch", {"repo_path": str(self.repo)}),
            ("current_branch", []),
            ("list_branches", {"repo_path": "../../"}),
            ("list_branches", {"limit": True}),
            ("list_branches", {"limit": 1.0}),
            ("list_branches", {"limit": 0}),
            ("list_branches", {"limit": 101}),
            ("list_branches", {"limit": ";touch marker"}),
            ("list_branches", {"limit": None}),
        ]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                self.assertEqual(self._call(name, arguments)["error"]["code"], -32602)

    def test_malformed_frames_and_requests(self) -> None:
        """Reject malformed wire input and keep notification responses silent."""
        for data, code in [
            (b"\n", -32700),
            (b"{broken}\n", -32700),
            (b"\xff\n", -32700),
            (b"{}", -32700),
            (b"Content-Length: 4\n\n{}", -32700),
            (b"[]\n", -32600),
            (b'{"jsonrpc":"2.0","method":"ping","id":true}\n', -32600),
            (b'{"id":1,"id":2}\n', -32700),
            (b'{"jsonrpc":"2.0","method":"ping","id":NaN}\n', -32700),
            (b"x" * (SERVER.MAX_MESSAGE + 1) + b"\n", -32700),
        ]:
            with self.subTest(data=data[:50]):
                self.assertEqual(self._exchange(data)[0]["error"]["code"], code)
        self.assertEqual(
            self._exchange(json.dumps(request("tools/list")).encode() + b"\n")[0][
                "error"
            ]["code"],
            -32600,
        )
        self.assertEqual(
            self._session([request("resources/list")])[0]["error"]["code"], -32601
        )
        self.assertEqual(
            self._session([request("tools/list", {"cursor": "unknown"})])[0]["error"][
                "code"
            ],
            -32602,
        )
        # No response to notifications; following ping retains its own ID.
        replies = self._session(
            [
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 1},
                },
                request("ping", request_id="ok"),
            ]
        )
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["id"], "ok")

    def test_environment_redirection_and_executable_side_effects_ignored(self) -> None:
        """Ignore host Git redirection, pager, aliases and executable settings."""
        other = self.root / "other"
        seed(other)
        git(other, "checkout", "-b", "wrong-repository")
        marker = self.root / "executed"
        executable = self.root / "side-effect"
        executable.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        executable.chmod(0o755)
        git(self.repo, "config", "core.fsmonitor", str(executable))
        git(self.repo, "config", "core.hooksPath", str(self.root))
        git(self.repo, "config", "alias.symbolic-ref", f"!{executable}")
        git(self.repo, "config", "alias.for-each-ref", f"!{executable}")
        env = dict(
            os.environ,
            GIT_DIR=str(other / ".git"),
            GIT_WORK_TREE=str(other),
            GIT_COMMON_DIR=str(other / ".git"),
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="core.worktree",
            GIT_CONFIG_VALUE_0=str(other),
            GIT_CONFIG_GLOBAL=str(executable),
            GIT_PAGER=str(executable),
            GIT_EXTERNAL_DIFF=str(executable),
            PYTHONPATH=str(self.root),
        )
        before = snapshot(self.repo)
        self.assertEqual(
            self._call("current_branch", env=env)["result"]["structuredContent"][
                "branch"
            ],
            "main",
        )
        self.assertFalse(self._call("list_branches", env=env)["result"]["isError"])
        self.assertFalse(marker.exists())
        self.assertEqual(snapshot(self.repo), before)

    def test_metadata_and_config_escape_rejected(self) -> None:
        """Reject repository-local paths that could escape workspace authority."""
        outside = self.root / "outside"
        outside.write_text("ref: refs/heads/main\n")
        head = self.repo / ".git/HEAD"
        original = head.read_bytes()
        head.unlink()
        head.symlink_to(outside)
        self.assertTrue(self._call("current_branch")["result"]["isError"])
        head.unlink()
        head.write_bytes(original)
        config = self.repo / ".git/config"
        original_config = config.read_text()
        for extra in (
            f"[include]\npath = {outside}\n",
            '[includeIf "gitdir:*"]\npath = ../../outside\n',
            "[core]\nworktree = ../../outside\n",
            "[extensions]\nworktreeConfig = true\n",
        ):
            config.write_text(original_config + extra)
            self.assertTrue(self._call("current_branch")["result"]["isError"])
        config.write_text(original_config)
        (self.repo / ".git/objects/info/alternates").write_text(str(outside))
        self.assertTrue(self._call("list_branches")["result"]["isError"])

    def test_no_parent_discovery_or_git_file(self) -> None:
        """A nested CWD or gitdir pointer cannot select another repository."""
        child = self.repo / "child"
        child.mkdir()
        self.assertTrue(self._call("current_branch", cwd=child)["result"]["isError"])
        (child / ".git").write_text(f"gitdir: {self.repo / '.git'}\n")
        self.assertTrue(self._call("current_branch", cwd=child)["result"]["isError"])

    def test_execution_and_output_bounds(self) -> None:
        """Kill slow/oversized fixture executables with bounded pipe memory."""
        # Run actual temporary executables to exercise bounded pipe draining.
        fixture = self.root / "fake-git"
        fixture.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(2)\n")
        fixture.chmod(0o755)
        with patch.object(
            SERVER.shutil, "which", return_value=str(fixture)
        ), patch.object(SERVER, "GIT_TIMEOUT", 0.05):
            with self.assertRaises(SERVER.ToolFailure):
                SERVER._git(["symbolic-ref", "HEAD"])
        fixture.write_text(
            f"#!{sys.executable}\nimport sys\nsys.stderr.write('x' * 300000)\n"
        )
        with patch.object(SERVER.shutil, "which", return_value=str(fixture)):
            with self.assertRaises(SERVER.ToolFailure):
                SERVER._git(["symbolic-ref", "HEAD"])

    def test_process_tree_cleanup_on_limits_errors_and_cancellation(self) -> None:
        """Bound cleanup of pipe-owning descendants and reap the wrapper child."""
        fixture = self.root / "wrapper-git"
        real_popen = subprocess.Popen
        for mode in ("timeout", "output", "error", "cancel"):
            with self.subTest(mode=mode):
                child_pid = self.root / f"{mode}.pid"
                reaped = self.root / f"{mode}.reaped"
                fixture.write_text(
                    f"#!{sys.executable}\n"
                    "import os, signal, subprocess, sys, time\n"
                    "child = subprocess.Popen([sys.executable, '-c', "
                    "'import time; time.sleep(60)'])\n"
                    "def stop(signum, frame):\n"
                    "    child.wait()\n"
                    f"    open({str(reaped)!r}, 'w').write('reaped')\n"
                    "    sys.exit(0)\n"
                    "signal.signal(signal.SIGTERM, stop)\n"
                    f"open({str(child_pid)!r}, 'w').write(str(child.pid))\n"
                    + (
                        "sys.stdout.write('x' * 300000); sys.stdout.flush()\n"
                        if mode == "output"
                        else "print('ready', flush=True)\n"
                    )
                    + "time.sleep(60)\n"
                )
                fixture.chmod(0o755)
                processes: list[subprocess.Popen[bytes]] = []

                def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
                    """Capture the real wrapper process to prove reaping."""
                    self.assertFalse(kwargs.get("start_new_session", False))
                    self.assertNotIn("process_group", kwargs)
                    process = real_popen(*args, **kwargs)
                    self.assertEqual(os.getpgid(process.pid), os.getpgrp())
                    processes.append(process)
                    return process

                started = time.monotonic()
                try:
                    with patch.object(
                        SERVER.shutil, "which", return_value=str(fixture)
                    ), patch.object(SERVER, "GIT_TIMEOUT", 0.5), patch.object(
                        SERVER.subprocess, "Popen", side_effect=spawn
                    ):
                        if mode in ("error", "cancel"):
                            failure = (
                                OSError("read failed")
                                if mode == "error"
                                else KeyboardInterrupt()
                            )
                            select = SERVER.selectors.DefaultSelector.select

                            def interrupted_select(
                                selector: Any, timeout: float | None = None
                            ) -> Any:
                                """Interrupt after the wrapper is ready."""
                                select(selector, timeout)
                                raise failure

                            with patch.object(
                                SERVER.selectors.DefaultSelector,
                                "select",
                                interrupted_select,
                            ):
                                with self.assertRaises(type(failure)):
                                    SERVER._git(["symbolic-ref", "HEAD"])
                        else:
                            with self.assertRaises(SERVER.ToolFailure):
                                SERVER._git(["symbolic-ref", "HEAD"])
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertIsNotNone(processes[0].returncode)
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(processes[0].pid, os.WNOHANG)
                    self.assertTrue(
                        reaped.exists(), "wrapper did not reap its grandchild"
                    )
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(child_pid.read_text()), 0)
                finally:
                    for process in processes:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=1)

    def test_sigterm_resistant_process_is_killed_and_reaped(self) -> None:
        """Escalate to SIGKILL when the owned Git executable ignores SIGTERM."""
        fixture = self.root / "stubborn-git"
        fixture.write_text(
            f"#!{sys.executable}\n"
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n"
        )
        fixture.chmod(0o755)
        processes: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen

        def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
            """Capture the actual child to verify SIGKILL and OS-level reaping."""
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        started = time.monotonic()
        with patch.object(
            SERVER.shutil, "which", return_value=str(fixture)
        ), patch.object(SERVER, "GIT_TIMEOUT", 0.5), patch.object(
            SERVER.subprocess, "Popen", side_effect=spawn
        ):
            with self.assertRaises(SERVER.ToolFailure):
                SERVER._git(["symbolic-ref", "HEAD"])
        self.assertLess(time.monotonic() - started, 5.5)
        self.assertEqual(processes[0].returncode, -signal.SIGKILL)
        with self.assertRaises(ChildProcessError):
            os.waitpid(processes[0].pid, os.WNOHANG)

    def test_unverified_cleanup_cannot_return_tool_data(self) -> None:
        """Even successful Git output fails closed if lifecycle proof is denied."""
        fixture = self.root / "successful-git"
        fixture.write_text("#!/bin/sh\necho refs/heads/main\n")
        fixture.chmod(0o755)
        with patch.object(
            SERVER.shutil, "which", return_value=str(fixture)
        ), patch.object(SERVER.ProcessLifecycle, "cleanup", return_value=False):
            with self.assertRaisesRegex(SERVER.ToolFailure, "git_cleanup_unverified"):
                SERVER._git(["symbolic-ref", "HEAD"])

    def test_maximum_limit_packed_refs_and_unicode_names(self) -> None:
        """Keep the list cap and UTF-8 refnames intact with packed refs."""
        for index in range(101):
            git(self.repo, "branch", f"branch-{index:03d}")
        git(self.repo, "branch", "aardvark-\u2028-name")
        git(self.repo, "pack-refs", "--all")
        result = self._call("list_branches", {"limit": 100})["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(len(result["structuredContent"]["branches"]), 100)
        self.assertTrue(result["structuredContent"]["truncated"])
        self.assertIn("aardvark-\u2028-name", result["structuredContent"]["branches"])
        git(self.repo, "checkout", "aardvark-\u2028-name")
        self.assertEqual(
            self._call("current_branch")["result"]["structuredContent"]["branch"],
            "aardvark-\u2028-name",
        )

    def test_initialization_validation_and_oversized_response_id(self) -> None:
        """Require complete initialization and cap even expanded Unicode IDs."""
        incomplete = request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {}},
        )
        self.assertEqual(
            self._exchange(json.dumps(incomplete).encode() + b"\n")[0]["error"]["code"],
            -32602,
        )
        message = request("ping", request_id="\u2603" * 180000)
        data = json.dumps(message, ensure_ascii=False).encode() + b"\n"
        self.assertEqual(self._exchange(data)[0]["error"]["code"], -32603)


if __name__ == "__main__":
    unittest.main()
