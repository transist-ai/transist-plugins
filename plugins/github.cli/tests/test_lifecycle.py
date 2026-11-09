"""Offline GitHub runner error mapping and heartbeat compatibility."""

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
        self.request = support.app_invoke_request("pr_list", {})

    def test_runner_success_and_safe_heartbeat(self) -> None:
        frames = []

        def run(command, **kwargs):
            self.assertEqual(command[:3], ["gh", "pr", "list"])
            self.assertIsNone(kwargs["timeout"])
            self.assertNotIn("capture_output", kwargs)
            self.assertNotIn("text", kwargs)
            self.assertEqual(kwargs["cwd"], self.request["paths"]["workspace_dir"])
            with mock.patch.object(self.plugin.time, "monotonic", return_value=100):
                kwargs["on_tick"]()
            return support.completed_process(command, 0, "[]\n", "")

        with mock.patch.object(
            self.plugin, "run_process", side_effect=run
        ), mock.patch.object(
            self.plugin, "emit", side_effect=frames.append
        ), mock.patch.object(
            self.plugin.time, "monotonic", return_value=0
        ):
            result = self.plugin.handle_request(self.request)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"]["pull_requests"], [])
        self.assertEqual(frames[0]["line_preview"], "Command is still running...")
        support.contract_validator("app/invoke.progress.v1.schema.json").validate(
            frames[0]
        )
        support.contract_validator("app/invoke.result.v1.schema.json").validate(result)

    def test_nested_cleanup_failure_is_protocol_not_tool(self) -> None:
        for action, payload in (
            ("pr_list", {}),
            ("pr_unresolved_comments", {}),
            ("pr_create", {"title": "test", "body": "test", "labels": ["test"]}),
        ):
            request = support.app_invoke_request(action, payload)
            cleanup = self.plugin.CleanupUnconfirmedError("partial", "stderr")
            effects = [cleanup]
            if action == "pr_create":
                effects.insert(
                    0, support.completed_process([], 0, "https://example.test/pr/1", "")
                )
            with self.subTest(action=action), mock.patch.object(
                self.plugin, "run_process", side_effect=effects
            ):
                result = self.plugin.handle_request(request)
            self.assertEqual(result["error"]["code"], "protocol_error")
            self.assertEqual(result["error"]["retryability"], "none")
            self.assertEqual(result["artifact_hints"][0]["contents"], "partial")

    def test_thread_resolution_cleanup_cannot_be_reported_as_success(self) -> None:
        request = support.app_invoke_request(
            "pr_publish_review_replies",
            {
                "response_plan_file": "fixture.json",
                "resolve_addressed_threads": True,
            },
        )
        with mock.patch.object(
            self.plugin,
            "read_text_file",
            return_value='{"responses": [], "addressed_thread_ids": ["thread"]}',
        ), mock.patch.object(
            self.plugin, "repo_parts", return_value=("owner", "repo")
        ), mock.patch.object(
            self.plugin, "resolve_pr_number", return_value=1
        ), mock.patch.object(
            self.plugin,
            "run_process",
            side_effect=self.plugin.CleanupUnconfirmedError(),
        ):
            result = self.plugin.handle_request(request)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "protocol_error")

    def test_unexpected_runner_failure_is_safe(self) -> None:
        with mock.patch.object(
            self.plugin, "run_process", side_effect=RuntimeError("secret command")
        ):
            result = self.plugin.handle_request(self.request)
        self.assertEqual(result["error"]["code"], "adapter_internal_error")
        self.assertEqual(result["error"]["message"], "Command runner failed")
        self.assertNotIn("secret command", str(result))


if __name__ == "__main__":
    unittest.main()
