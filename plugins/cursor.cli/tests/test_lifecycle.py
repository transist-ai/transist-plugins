"""Actual-subprocess lifecycle regressions; only the outer fixture owns a group."""

import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import plugin  # noqa: E402
from process_lifecycle import (  # noqa: E402
    Identity,
    NativeProcesses,
    PipeReader,
    ProcessLifecycle,
    finish_readers,
)


PROVIDER = r"""
import json, os, signal, sys, time
from pathlib import Path
mode, root = sys.argv[1:3]
root = Path(root)
def record(name):
    (root / name).write_text(json.dumps([os.getpid(), os.getpgrp()]))
record("provider")
if mode in {"child", "grandchild", "pipes", "resistant", "timeout", "exception",
            "observe", "denied", "late"}:
    child = os.fork()
    if child == 0:
        if mode == "grandchild":
            if os.fork() != 0:
                os._exit(0)
        if mode in {"resistant", "late", "denied"}:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        record("child")
        if mode == "pipes":
            os.write(1, b"unterminated ordinary output")
        while True:
            if mode == "late":
                print(json.dumps({"type":"assistant", "message":{"content":[
                    {"type":"text", "text":"safe ordinary child output"}]}}),
                    flush=True)
            time.sleep(.03)
    if mode == "grandchild":
        os.waitpid(child, 0)
    while not (root / "child").exists():
        time.sleep(.005)
print(json.dumps({"type":"system", "subtype":"init", "session_id":"session",
                  "model":"fixture"}), flush=True)
print(json.dumps({"type":"result", "result":"safe response", "session_id":"session",
                  "usage":{"inputTokens":3,"outputTokens":2}}), flush=True)
print("safe stderr", file=sys.stderr, flush=True)
if mode in {"timeout", "exception"}:
    time.sleep(30)
sys.exit(7 if mode == "failure" else 0)
"""


def worker(mode: str, root: Path) -> None:
    """Run the real APP entrypoint with a real provider and injectable faults."""
    original_popen = subprocess.Popen
    original_wait = subprocess.Popen.wait
    captured = []
    terminal_clean = []
    original_emit = plugin.emit

    def emit(frame: dict) -> None:
        if frame["type"] == "app.invoke.result.v1":
            live = []
            for name in ("provider", "child"):
                if (root / name).exists():
                    pid, _ = json.loads((root / name).read_text())
                    info = NativeProcesses().read(pid)
                    live.append(info is not None and info.live)
            terminal_clean.append(not any(live))
        captured.append(frame)
        original_emit(frame)

    def popen(command: list[str], **kwargs: object) -> subprocess.Popen:
        assert not kwargs.get("start_new_session")
        assert "process_group" not in kwargs and "preexec_fn" not in kwargs
        child = original_popen(command, **kwargs)
        if mode == "exception":

            def broken_wait(timeout: float | None = None) -> int:
                # Let the provider fork/write so finally must clean both processes.
                if not (root / "child").exists():
                    return original_wait(child, timeout=timeout)
                raise RuntimeError("sensitive-command-must-not-appear")

            child.wait = broken_wait
        return child

    original_lifecycle = plugin.ProcessLifecycle

    def lifecycle() -> ProcessLifecycle:
        result = original_lifecycle()
        assert os.getpid() in result.protected
        assert os.getppid() in result.protected
        if mode == "observe":
            result.native.members = mock.Mock(side_effect=PermissionError("private"))
        if mode == "denied":
            result.native.send = mock.Mock(return_value=False)
        return result

    with mock.patch.object(plugin, "emit", emit), mock.patch.object(
        plugin,
        "build_command",
        return_value=(
            [
                sys.executable,
                str(root / "provider.py"),
                mode,
                str(root),
                "--workspace",
                str(root),
            ],
            str(root),
        ),
    ), mock.patch.object(plugin.subprocess, "Popen", popen), mock.patch.object(
        plugin,
        "ProcessLifecycle",
        lifecycle,
    ):
        plugin.main()
        frozen = json.dumps(captured, sort_keys=True)
        time.sleep(0.15)
        (root / "audit").write_text(
            json.dumps(
                {
                    "frozen": frozen == json.dumps(captured, sort_keys=True),
                    "threads": len(threading.enumerate()),
                    "terminal_clean": terminal_clean,
                }
            )
        )


@unittest.skipUnless(sys.platform in {"darwin", "linux"}, "native OS required")
class LifecycleTests(unittest.TestCase):
    """Verify real descendant cleanup and terminal APP ordering."""

    def run_fixture(self, mode: str) -> tuple[dict, list[dict], float]:
        """Own an isolated outer fixture group and assert state at terminal time."""
        with tempfile.TemporaryDirectory(
            prefix="CursorLifecycleRemediation-", dir="/tmp"
        ) as directory:
            root = Path(directory)
            (root / "provider.py").write_text(PROVIDER)
            env = os.environ.copy()
            env.update(
                {
                    name: str(root)
                    for name in (
                        "CURSOR_CONFIG_DIR",
                        "CURSOR_DATA_DIR",
                        "TMPDIR",
                    )
                }
            )
            request = {
                "type": "app.invoke.request.v1",
                "invoke_id": "lifecycle",
                "run_id": "fixture",
                "action": "exec_task_cli",
                "input": {
                    "prompt": "fixture",
                    "timeout_seconds": 1 if mode == "timeout" else 5,
                },
                "paths": {
                    "workspace_dir": str(root),
                    "artifacts_dir": str(root),
                    "run_root": str(root),
                },
            }
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(Path(__file__)),
                    "--worker",
                    mode,
                    str(root),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                # Only the outer test harness creates the runtime-owned group.
                start_new_session=True,
            )
            started = time.monotonic()
            try:
                data = json.dumps({"type": "app.handshake.request.v1"}) + "\n"
                data += json.dumps(request) + "\n"
                stdout, stderr = process.communicate(data, timeout=12)
                elapsed = time.monotonic() - started
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stderr, "")
                frames = [json.loads(line) for line in stdout.splitlines()]
                terminals = [f for f in frames if f["type"] == "app.invoke.result.v1"]
                self.assertEqual(len(terminals), 1)
                self.assertEqual(frames[-1], terminals[0])
                self.assertEqual(
                    json.loads((root / "audit").read_text()),
                    {
                        "frozen": True,
                        "threads": 1,
                        "terminal_clean": [mode not in {"observe", "denied"}],
                    },
                )
                for name in ("provider", "child"):
                    if not (root / name).exists():
                        continue
                    pid, pgid = json.loads((root / name).read_text())
                    self.assertEqual(pgid, process.pid, "provider escaped group")
                    info = NativeProcesses().read(pid)
                    if mode not in {"observe", "denied"}:
                        self.assertTrue(
                            info is None or not info.live, f"live {name} after terminal"
                        )
                return terminals[0], frames, elapsed
            finally:
                # Fault cases intentionally fail closed; outer runtime owns recovery.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)

    def test_clean_success_and_failure(self) -> None:
        for mode, code in (("success", 0), ("failure", 7)):
            with self.subTest(mode=mode):
                result, _, _ = self.run_fixture(mode)
                self.assertEqual(result["status"], "success" if code == 0 else "failed")
                self.assertEqual(result["output"]["exit_code"], code)
                self.assertEqual(result["output"]["response_text"], "safe response")
                self.assertEqual(result["output"]["session_id"], "session")
                self.assertEqual(result["usage"]["total_tokens"], 5)

    def test_parent_child_and_orphan_grandchild(self) -> None:
        for mode in ("child", "grandchild"):
            with self.subTest(mode=mode):
                result, _, elapsed = self.run_fixture(mode)
                self.assertEqual(result["status"], "success")
                self.assertLess(elapsed, 5.7)

    def test_inherited_pipes_no_blocking_drain(self) -> None:
        result, _, elapsed = self.run_fixture("pipes")
        self.assertEqual(result["status"], "success")
        self.assertIn("unterminated ordinary output", result["output"]["stdout"])
        self.assertLess(elapsed, 5.7)

    def test_term_resistant_child_and_heartbeat(self) -> None:
        result, frames, elapsed = self.run_fixture("resistant")
        self.assertEqual(result["status"], "success")
        self.assertGreaterEqual(elapsed, 2)
        self.assertLess(elapsed, 5.7)
        self.assertTrue(
            any("still working" in f.get("line_preview", "") for f in frames)
        )

    def test_timeout_and_exception_finalization(self) -> None:
        for mode, error in (
            ("timeout", "timeout_error"),
            ("exception", "adapter_internal_error"),
        ):
            with self.subTest(mode=mode):
                result, _, elapsed = self.run_fixture(mode)
                self.assertEqual(result["error"]["code"], error)
                self.assertNotIn("sensitive-command", json.dumps(result))
                self.assertLess(elapsed, 6.7)

    def test_observation_and_permission_fail_closed(self) -> None:
        for mode in ("observe", "denied"):
            with self.subTest(mode=mode):
                result, _, elapsed = self.run_fixture(mode)
                self.assertEqual(result["error"]["code"], "protocol_error")
                self.assertEqual(
                    result["error"]["message"],
                    "Cursor process cleanup could not be confirmed",
                )
                hints = {h["name"]: h["contents"] for h in result["artifact_hints"]}
                self.assertEqual(hints["response.txt"], "safe response")
                self.assertIn("safe stderr", hints["stderr.txt"])
                self.assertNotIn("private", json.dumps(result))
                self.assertLess(elapsed, 5.7)

    def test_no_progress_or_mutation_after_terminal(self) -> None:
        result, _, _ = self.run_fixture("late")
        self.assertEqual(result["status"], "success")

    def test_changed_identity_and_unrelated_group_never_signaled(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True
        )
        try:
            native = NativeProcesses()
            info = native.read(process.pid)
            self.assertIsNotNone(info)
            for expected in (
                replace(info, birth=(0,)),
                replace(info, pgid=os.getpgrp()),
            ):
                with mock.patch("os.kill") as kill, mock.patch.object(
                    signal,
                    "pidfd_send_signal",
                    create=True,
                ) as pidfd_signal:
                    self.assertFalse(native.send(expected, signal.SIGTERM))
                    kill.assert_not_called()
                    pidfd_signal.assert_not_called()
                self.assertIsNone(process.poll())
            with mock.patch.object(
                native, "read", side_effect=PermissionError
            ), mock.patch("os.kill") as kill, mock.patch.object(
                signal,
                "pidfd_send_signal",
                create=True,
            ) as pidfd_signal:
                self.assertFalse(native.send(info, signal.SIGTERM))
                kill.assert_not_called()
                pidfd_signal.assert_not_called()
            self.assertIsNone(process.poll())
        finally:
            process.kill()
            process.wait(timeout=2)

    def test_protected_ancestors_and_preexisting_members(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        try:
            lifecycle = ProcessLifecycle()
            self.assertIn(os.getpid(), lifecycle.protected)
            if os.getppid() != 0:
                self.assertIn(os.getppid(), lifecycle.protected)
            self.assertIn(process.pid, lifecycle.protected)
            with mock.patch.object(lifecycle.native, "send") as send:
                self.assertTrue(lifecycle.cleanup(lambda: None))
                send.assert_not_called()
            self.assertIsNone(process.poll())
        finally:
            process.kill()
            process.wait(timeout=2)

    def test_prelaunch_observation_failure_does_not_launch(self) -> None:
        with mock.patch.object(
            plugin, "ProcessLifecycle", side_effect=PermissionError
        ), mock.patch.object(plugin.subprocess, "Popen") as launch, mock.patch.object(
            plugin, "emit"
        ):
            with self.assertRaises(plugin.CleanupUnconfirmedError):
                plugin.run_command({"invoke_id": "test"}, ["fixture"], {})
            launch.assert_not_called()

    def test_reused_owned_pid_fails_closed_and_zombies_complete(self) -> None:
        lifecycle = ProcessLifecycle()
        info = Identity(2147483647, (1,), os.getpid(), lifecycle.pgid, "Z")
        with mock.patch.object(lifecycle.native, "members", return_value=[info]):
            self.assertEqual(lifecycle.remaining(), [])
        with mock.patch.object(
            lifecycle.native,
            "members",
            return_value=[replace(info, birth=(2,), state="R")],
        ), mock.patch.object(lifecycle.native, "send") as send:
            self.assertFalse(lifecycle.cleanup(lambda: None))
            send.assert_not_called()

    def test_reader_stop_with_live_inherited_writer(self) -> None:
        import queue

        read_fd, write_fd = os.pipe()
        stream = io.TextIOWrapper(io.FileIO(read_fd, "r"))
        events = queue.Queue()
        reader = PipeReader(stream, events, "stdout")
        try:
            os.write(write_fd, b"no newline")
            reader.start()
            time.sleep(0.05)
            started = time.monotonic()
            self.assertTrue(finish_readers([reader]))
            self.assertLess(time.monotonic() - started, 1.1)
            self.assertEqual(events.get_nowait(), ("stdout", "no newline"))
            snapshot = events.qsize()
            time.sleep(0.05)
            self.assertEqual(events.qsize(), snapshot)
        finally:
            os.close(write_fd)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        worker(sys.argv[2], Path(sys.argv[3]))
    else:
        unittest.main()
