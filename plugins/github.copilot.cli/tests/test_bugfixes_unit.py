#!/usr/bin/env python3
import io
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock
from runner_fixture import fixture_runner

ROOT = pathlib.Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT.parents[1] / "tests"))

from support import (  # noqa: E402
    app_invoke_request,
    load_plugin_module,
    run_process_plugin_main,
)


class GithubCopilotCliBugfixUnitTests(unittest.TestCase):
    def setUp(self):
        self.module = load_plugin_module(ROOT)
        self.runtime_temp_dir = tempfile.TemporaryDirectory(
            prefix="copilot-unit-runtime-"
        )
        runtime_root = pathlib.Path(self.runtime_temp_dir.name)
        runtime_env = {
            "COPILOT_HOME": str(runtime_root / "copilot"),
            "COPILOT_CONFIG_DIR": str(runtime_root / "copilot"),
            "COPILOT_SESSION_STATE_DIR": str(
                runtime_root / "copilot" / "session-state"
            ),
            "XDG_CACHE_HOME": str(runtime_root / "copilot" / "cache"),
            "TMPDIR": str(runtime_root / "copilot" / "tmp"),
        }
        for value in runtime_env.values():
            pathlib.Path(value).mkdir(parents=True, exist_ok=True)
        self.environment_patch = mock.patch.dict(os.environ, runtime_env, clear=False)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        self.addCleanup(self.runtime_temp_dir.cleanup)

    def _basic_request(self, input_payload=None):
        request = app_invoke_request(
            "exec_task",
            {
                "prompt": "Say hello.",
                "timeout_seconds": 10,
            },
        )
        if input_payload:
            request["input"].update(input_payload)
        workspace_dir = pathlib.Path(tempfile.mkdtemp(prefix="copilot-unit-workspace-"))
        request["paths"]["workspace_dir"] = str(workspace_dir)
        return request

    def test_heartbeat_continues_after_first_output(self):
        frames = []

        class FakePopen:
            def __init__(self, command, **kwargs):
                self.stdout = io.StringIO("first output line\n")
                self.stderr = io.StringIO("")
                self._returncode = 0

            def wait(self, timeout=None):
                time.sleep(2.2)
                return self._returncode

            def kill(self):
                self._returncode = -9

        request = {
            "invoke_id": "unit-heartbeat",
            "paths": {
                "workspace_dir": tempfile.mkdtemp(prefix="copilot-unit-cwd-"),
            },
        }
        with mock.patch.object(
            self.module, "emit", side_effect=lambda payload: frames.append(payload)
        ):
            with mock.patch.object(
                self.module, "run_process", side_effect=fixture_runner(FakePopen)
            ):
                completed = self.module.run_command(
                    request,
                    ["gh", "copilot", "--", "-p", "hello"],
                    {"timeout_seconds": 10},
                )

        self.assertEqual(completed.returncode, 0)
        status_previews = [
            f.get("line_preview", "")
            for f in frames
            if f.get("type") == "app.invoke.progress.v1" and f.get("stream") == "status"
        ]
        self.assertIn("Waiting for Copilot output...", status_previews)
        self.assertIn("Copilot is still working...", status_previews)

    def test_heartbeat_stops_at_timeout_deadline(self):
        frames = []
        timestamps = []

        class FakePopen:
            def __init__(self, command, **kwargs):
                self.stdout = io.StringIO("")
                self.stderr = io.StringIO("")
                self._returncode = 0

            def wait(self, timeout=None):
                time.sleep(3.2)
                return self._returncode

            def kill(self):
                self._returncode = -9

        request = {
            "invoke_id": "unit-heartbeat-timeout",
            "paths": {
                "workspace_dir": tempfile.mkdtemp(prefix="copilot-unit-cwd-timeout-"),
            },
        }

        start = time.monotonic()

        def record_emit(payload):
            frames.append(payload)
            if (
                payload.get("type") == "app.invoke.progress.v1"
                and payload.get("stream") == "status"
            ):
                timestamps.append(time.monotonic())

        with mock.patch.object(self.module, "emit", side_effect=record_emit):
            with mock.patch.object(
                self.module, "run_process", side_effect=fixture_runner(FakePopen)
            ):
                completed = self.module.run_command(
                    request,
                    ["gh", "copilot", "--", "-p", "hello"],
                    {"timeout_seconds": 2},
                )

        self.assertEqual(completed.returncode, 0)
        self.assertGreaterEqual(len(timestamps), 1)
        self.assertLess(max(timestamps) - start, 3.0)

    def test_timeout_ms_below_grace_threshold_fails_validation(self):
        request = self._basic_request({"timeout_seconds": None, "timeout_ms": 4000})
        request["input"].pop("timeout_seconds", None)

        exit_code, frames, stderr = run_process_plugin_main(self.module, request)
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        result = frames[-1]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "validation_error")
        self.assertIn("timeout_ms must be >=", result["error"]["message"])

    def test_prompt_file_preserves_trailing_newlines(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write("line one\nline two\n\n")
            prompt_path = handle.name

        captured = {}

        class FakePopen:
            def __init__(self, command, **kwargs):
                captured["command"] = command
                self.stdout = io.StringIO("ok\n")
                self.stderr = io.StringIO("")
                self._returncode = 0

            def wait(self, timeout=None):
                return self._returncode

            def kill(self):
                self._returncode = -9

        request = self._basic_request({"prompt": None, "prompt_file": prompt_path})
        request["input"].pop("prompt")

        exit_code, frames, stderr = run_process_plugin_main(
            self.module,
            request,
            [
                mock.patch.object(
                    self.module, "run_process", side_effect=fixture_runner(FakePopen)
                )
            ],
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(frames[-1]["status"], "success")
        composed_prompt = captured["command"][4]
        self.assertTrue(composed_prompt.endswith("line one\nline two\n\n"))

    def test_feedback_context_mode_validated_even_without_feedback(self):
        request = self._basic_request({"feedback_context_mode": "invalid-mode"})

        exit_code, frames, stderr = run_process_plugin_main(self.module, request)
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        result = frames[-1]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "validation_error")
        self.assertIn(
            "feedback_context_mode must be one of", result["error"]["message"]
        )

    def test_available_tools_preferred_and_allowed_tools_deprecated(self):
        captured = {}

        class FakePopen:
            def __init__(self, command, **kwargs):
                captured["command"] = command
                self.stdout = io.StringIO("ok\n")
                self.stderr = io.StringIO("")
                self._returncode = 0

            def wait(self, timeout=None):
                return self._returncode

            def kill(self):
                self._returncode = -9

        request = self._basic_request(
            {
                "available_tools": ["shell(ls:*)", "read"],
                "allowed_tools": ["legacy-tool"],
            }
        )

        exit_code, frames, stderr = run_process_plugin_main(
            self.module,
            request,
            [
                mock.patch.object(
                    self.module, "run_process", side_effect=fixture_runner(FakePopen)
                )
            ],
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(frames[-1]["status"], "success")

        command = captured["command"]
        idx = command.index("--available-tools")
        self.assertEqual(command[idx + 1], "shell(ls:*),read")

        previews = [
            frame.get("line_preview", "")
            for frame in frames
            if frame.get("type") == "app.invoke.progress.v1"
            and frame.get("stream") == "status"
        ]
        self.assertTrue(
            any(
                "Both available_tools and deprecated allowed_tools were set" in line
                for line in previews
            )
        )

    def test_legacy_allowed_tools_still_supported_with_warning(self):
        captured = {}

        class FakePopen:
            def __init__(self, command, **kwargs):
                captured["command"] = command
                self.stdout = io.StringIO("ok\n")
                self.stderr = io.StringIO("")
                self._returncode = 0

            def wait(self, timeout=None):
                return self._returncode

            def kill(self):
                self._returncode = -9

        request = self._basic_request({"allowed_tools": ["shell(git status:*)"]})

        exit_code, frames, stderr = run_process_plugin_main(
            self.module,
            request,
            [
                mock.patch.object(
                    self.module, "run_process", side_effect=fixture_runner(FakePopen)
                )
            ],
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(frames[-1]["status"], "success")

        command = captured["command"]
        idx = command.index("--available-tools")
        self.assertEqual(command[idx + 1], "shell(git status:*)")
        previews = [
            frame.get("line_preview", "")
            for frame in frames
            if frame.get("type") == "app.invoke.progress.v1"
            and frame.get("stream") == "status"
        ]
        self.assertTrue(any("DEPRECATED: allowed_tools" in line for line in previews))

    def test_post_processing_emits_status_previews(self):
        response_root = pathlib.Path(tempfile.mkdtemp(prefix="copilot-post-process-"))
        response_file = response_root / "out.md"
        title_file = response_root / "title.txt"
        pr_body_file = response_root / "pr.md"
        request = self._basic_request(
            {
                "response_file": str(response_file),
                "summary_title_file": str(title_file),
                "pr_body_file": str(pr_body_file),
            }
        )

        class FakePopen:
            def __init__(self, command, **kwargs):
                self.stdout = io.StringIO("Title\n\n## What Changed\n\nBody\n")
                self.stderr = io.StringIO("")
                self._returncode = 0

            def wait(self, timeout=None):
                return self._returncode

            def kill(self):
                self._returncode = -9

        exit_code, frames, stderr = run_process_plugin_main(
            self.module,
            request,
            [
                mock.patch.object(
                    self.module, "run_process", side_effect=fixture_runner(FakePopen)
                )
            ],
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(frames[-1]["status"], "success")

        previews = [
            frame.get("line_preview", "")
            for frame in frames
            if frame.get("type") == "app.invoke.progress.v1"
            and frame.get("stream") == "status"
        ]
        expected = {
            "Copilot finished, processing output...",
            "Parsing Copilot session usage...",
            "Extracting canonical response...",
            "Writing response artifact...",
            "Writing summary title artifact...",
            "Writing PR body artifact...",
            "Persisting interaction history...",
            "Finalizing artifacts...",
        }
        self.assertTrue(expected.issubset(set(previews)))

    def test_large_stdout_is_collected_without_corruption(self):
        line = "x" * 200
        expected_stdout = "".join(f"{line}-{idx}\n" for idx in range(300))

        class FakePopen:
            def __init__(self, command, **kwargs):
                self.stdout = io.StringIO(expected_stdout)
                self.stderr = io.StringIO("")
                self._returncode = 0

            def wait(self, timeout=None):
                time.sleep(0.2)
                return self._returncode

            def kill(self):
                self._returncode = -9

        request = {
            "invoke_id": "unit-large-stdout",
            "paths": {
                "workspace_dir": tempfile.mkdtemp(prefix="copilot-large-cwd-"),
            },
        }
        with mock.patch.object(
            self.module, "run_process", side_effect=fixture_runner(FakePopen)
        ):
            with mock.patch.object(
                self.module, "emit", side_effect=lambda payload: None
            ):
                completed = self.module.run_command(
                    request,
                    ["gh", "copilot", "--", "-p", "hello"],
                    {"timeout_seconds": 10},
                )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, expected_stdout)


if __name__ == "__main__":
    unittest.main()
