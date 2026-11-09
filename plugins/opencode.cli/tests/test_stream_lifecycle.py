"""Offline streaming parser, runner, and APP cleanup regressions."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
from support import (  # noqa: E402
    app_invoke_request,
    load_plugin_module,
    resolved_runtime_temp_environment,
    run_process_plugin_main,
)


class StreamLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_plugin_module(ROOT)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.request = app_invoke_request(
            "exec_task", {"prompt": "fixture"}, self.tmp.name
        )
        self.request["paths"]["workspace_dir"] = self.tmp.name

    def test_native_multiline_events_and_final_plain_flush(self) -> None:
        event = {
            "type": "text",
            "sessionID": "fixture-session",
            "part": {"text": "reply"},
        }
        text = json.dumps(event, indent=2) + "\nhttps://example.invalid/share"
        threads = []
        with mock.patch.object(
            self.module,
            "emit",
            side_effect=lambda _: threads.append(threading.get_ident()),
        ):
            result = self.module.run_command(
                self.request,
                [sys.executable, "-c", f"import sys; sys.stdout.write({text!r})"],
                input_payload={"timeout_seconds": 5},
            )
        self.assertEqual(result["completed"].stdout, text)
        self.assertEqual(result["response_text"], "reply")
        self.assertEqual(result["session_id"], "fixture-session")
        self.assertEqual(result["share_url"], "https://example.invalid/share")
        self.assertEqual(set(threads), {threading.get_ident()})

    def test_export_cleanup_failure_fails_invocation(self) -> None:
        err = self.module.CleanupUnconfirmedError.__new__(
            self.module.CleanupUnconfirmedError
        )
        Exception.__init__(err, "native exception secret marker")
        err.stdout, err.stderr = "partial export\n", "ordinary diagnostic\n"
        event = (
            json.dumps({"type": "step_start", "sessionID": "fixture-session"}) + "\n"
        )

        def runner(command: list, **kwargs) -> subprocess.CompletedProcess:
            if command[1] == "export":
                raise err
            kwargs["on_line"]("stdout", event)
            return subprocess.CompletedProcess(command, 0, event, "")

        with resolved_runtime_temp_environment(ROOT) as env, mock.patch.dict(
            os.environ, env
        ):
            code, frames, stderr = run_process_plugin_main(
                self.module,
                self.request,
                [mock.patch.object(self.module, "run_process", side_effect=runner)],
            )
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(frames[0]["plugin"]["version"], "0.1.0")
        result = frames[-1]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "protocol_error")
        self.assertEqual(result["error"]["retryability"], "none")
        self.assertNotIn("secret marker", json.dumps(frames))
        self.assertEqual(result["artifact_hints"][0]["contents"], err.stdout)

    def test_callback_value_error_is_safe_internal_error(self) -> None:
        with resolved_runtime_temp_environment(ROOT) as env, mock.patch.dict(
            os.environ, env
        ):
            _, frames, _ = run_process_plugin_main(
                self.module,
                self.request,
                [
                    mock.patch.object(
                        self.module,
                        "run_process",
                        side_effect=ValueError("secret marker"),
                    )
                ],
            )
        self.assertEqual(frames[-1]["error"]["code"], "adapter_internal_error")
        self.assertNotIn("secret marker", json.dumps(frames))

    def test_native_callback_failure_reaps_provider_before_return(self) -> None:
        pids = []

        def emit(frame: dict) -> None:
            if frame.get("stream") == "stderr":
                pids.append(int(frame["line_preview"]))
                raise ValueError("secret marker")

        with mock.patch.object(self.module, "emit", side_effect=emit):
            with self.assertRaises(RuntimeError):
                self.module.run_command(
                    self.request,
                    [
                        sys.executable,
                        "-c",
                        "import os,sys,time; print(os.getpid(), file=sys.stderr, "
                        "flush=True); time.sleep(30)",
                    ],
                    input_payload={"timeout_seconds": 5},
                )
        self.assertEqual(len(pids), 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(pids[0], 0)

    def test_native_timeout_retains_output_after_cleanup(self) -> None:
        with mock.patch.object(self.module, "emit"):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                self.module.run_command(
                    self.request,
                    [
                        sys.executable,
                        "-c",
                        "import os,time; print(os.getpid(),flush=True); time.sleep(30)",
                    ],
                    input_payload={"timeout_seconds": 1},
                )
        with self.assertRaises(ProcessLookupError):
            os.kill(int(caught.exception.output.strip()), 0)


if __name__ == "__main__":
    unittest.main()
