"""Offline command lifecycle and APP compatibility regressions."""

import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
import support  # noqa: E402


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = support.load_plugin_module(ROOT)
        self.request = support.app_invoke_request("current_branch", {})

    def test_finalization_precedes_terminal_and_safe_heartbeat(self) -> None:
        frames = []
        finalized = False

        def run(command, **kwargs):
            nonlocal finalized
            self.assertEqual(command, ["git", "branch", "--show-current"])
            self.assertIsNone(kwargs["timeout"])
            self.assertNotIn("capture_output", kwargs)
            self.assertNotIn("text", kwargs)
            self.assertEqual(kwargs["cwd"], self.request["paths"]["workspace_dir"])
            with mock.patch.object(self.plugin.time, "monotonic", return_value=100):
                kwargs["on_tick"]()
            finalized = True
            return support.completed_process(command, 0, "main\n", "")

        def emit(frame):
            if frame["type"] == "app.invoke.result.v1":
                self.assertTrue(finalized)
            frames.append(frame)

        with mock.patch.object(
            self.plugin, "read_frame", side_effect=[{}, self.request]
        ), mock.patch.object(
            self.plugin, "run_process", side_effect=run
        ), mock.patch.object(
            self.plugin, "emit", side_effect=emit
        ), mock.patch.object(
            self.plugin.time, "monotonic", return_value=0
        ):
            self.assertEqual(self.plugin.main(), 0)
        self.assertEqual(frames[1]["line_preview"], "Command is still running...")
        self.assertEqual(frames[-1]["output"]["branch"], "main")
        for frame in frames:
            support.contract_validator(
                "app/" + frame["type"].removeprefix("app.") + ".schema.json"
            ).validate(frame)

    def test_cleanup_failure_preserves_output_without_retry(self) -> None:
        with mock.patch.object(
            self.plugin,
            "run_process",
            side_effect=self.plugin.CleanupUnconfirmedError(
                "partial\n", "diagnostic\n"
            ),
        ):
            result = self.plugin.handle_request(self.request)
        self.assertEqual(
            result["error"],
            {
                "code": "protocol_error",
                "class": "protocol",
                "retryability": "none",
                "message": "Command process cleanup could not be confirmed",
            },
        )
        self.assertEqual(
            result["artifact_hints"],
            [
                {"name": "stdout.txt", "contents": "partial\n"},
                {"name": "stderr.txt", "contents": "diagnostic\n"},
            ],
        )

    def test_runner_error_is_safe_internal(self) -> None:
        for error in (
            RuntimeError("private argv/token"),
            ValueError("private argv/token"),
        ):
            with self.subTest(error=type(error)), mock.patch.object(
                self.plugin, "run_process", side_effect=error
            ):
                result = self.plugin.handle_request(self.request)
            self.assertEqual(result["error"]["code"], "adapter_internal_error")
            self.assertEqual(result["error"]["message"], "Command runner failed")
            self.assertNotIn("private", str(result))


if __name__ == "__main__":
    unittest.main()
