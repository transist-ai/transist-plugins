"""Semantic stdout, promoted guards and bounded evidence in both output modes."""

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from jsonschema import Draft202012Validator, ValidationError

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
import support  # noqa: E402


class CompactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = support.load_plugin_module(ROOT)

    def invoke(
        self,
        action: str,
        payload: dict,
        stdout: str = "",
        stderr: str = "",
        code: int = 0,
    ) -> dict:
        """Exercise the actual APP entrypoint while mocking the child command only."""
        request = support.app_invoke_request(action, payload)
        process = support.completed_process([], code, stdout, stderr)
        exit_code, frames, diagnostics = support.run_process_plugin_main(
            self.plugin,
            request,
            [mock.patch.object(self.plugin, "run_process", return_value=process)],
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(diagnostics, "")
        _, result = support.assert_app_frames(frames, "shell")
        return result

    def test_boolean_echo_stdout_is_exact(self) -> None:
        for mode in (None, "legacy", "compact"):
            payload = {"args": [True, False, 2, " é "]}
            if mode:
                payload["output_mode"] = mode
            result = self.invoke("echo", payload)
            self.assertEqual(result["output"]["stdout"], "true false 2  é ")
            if mode == "compact":
                self.assertEqual(result["artifact_hints"], [])
                self.assertEqual(
                    result["metrics"]["result_bytes"], len(json.dumps(result).encode())
                )
            else:
                self.assertEqual(
                    result["artifact_hints"][0]["contents"], result["output"]["stdout"]
                )
                self.assertNotIn("output_mode", result["output"])

    def test_large_multibyte_business_stdout_is_never_truncated(self) -> None:
        text = ' é"\\\n' * 70000
        for action in ("exec", "exec_with_secret_env"):
            result = self.invoke(
                action,
                {"command": "fixture", "output_mode": "compact"},
                text,
                "diagnostic\n",
            )
            self.assertEqual(result["output"]["stdout"], text)
            self.assertEqual(result["output"]["stderr"], "diagnostic\n")
            self.assertEqual(result["artifact_hints"], [])
            self.assertEqual(
                result["output"]["diagnostics"]["stdout_bytes"], len(text.encode())
            )
            self.assertEqual(
                result["metrics"]["result_bytes"], len(json.dumps(result).encode())
            )

    def test_json_promotion_and_fixed_precedence(self) -> None:
        business = {
            "ready": True,
            "needs_changes": False,
            "nested": {"id": "é"},
            "ok": False,
            "stdout": "spoof",
            "stderr": "spoof",
            "exit_code": 99,
            "output_mode": "spoof",
            "diagnostics": {"spoof": True},
        }
        text = "  " + json.dumps(business) + "\n"
        for mode in ("legacy", "compact"):
            result = self.invoke(
                "exec",
                {"command": "fixture", "output_mode": mode},
                text,
                "actual stderr",
                0,
            )
            output = result["output"]
            self.assertTrue(output["ready"])
            self.assertFalse(output["needs_changes"])
            self.assertEqual(output["nested"], {"id": "é"})
            self.assertTrue(output["ok"])
            self.assertEqual(output["stdout"], text)
            self.assertEqual(output["stderr"], "actual stderr")
            self.assertEqual(output["exit_code"], 0)
            if mode == "compact":
                self.assertEqual(output["output_mode"], "compact")
                self.assertEqual(output["diagnostics"]["stdout_ref"], "output.stdout")
            else:
                self.assertEqual(output["output_mode"], "spoof")
                self.assertEqual(output["diagnostics"], {"spoof": True})

    def test_boolean_business_stdout_and_error_counters(self) -> None:
        for text in ("true\n", "false", "  true  "):
            payload = {"command": "fixture", "output_mode": "compact"}
            result = self.invoke("exec", payload, text, "failure", 2)
            self.assertEqual(result["output"]["stdout"], text)
            self.assertFalse(result["output"]["ok"])
            self.assertEqual(result["error"]["code"], "tool_execution_error")
            self.assertEqual(
                result["metrics"]["input_bytes"], len(json.dumps(payload).encode())
            )
            self.assertEqual(
                result["metrics"]["output_bytes"],
                len(json.dumps(result["output"]).encode()),
            )
            self.assertEqual(
                result["metrics"]["result_bytes"], len(json.dumps(result).encode())
            )

    def test_compact_diagnostics_are_closed(self) -> None:
        schema = support.read_json(ROOT / "schemas/compact.schema.json")["$defs"][
            "diagnostics"
        ]
        result = self.invoke("echo", {"args": [True], "output_mode": "compact"})
        Draft202012Validator(schema).validate(result["output"]["diagnostics"])
        with self.assertRaises(ValidationError):
            Draft202012Validator(schema).validate(
                result["output"]["diagnostics"] | {"unexpected": 1}
            )
        for action in ("echo", "exec", "exec_with_secret_env"):
            schema = support.read_json(ROOT / f"schemas/{action}.input.schema.json")
            payload = {"output_mode": "invalid"}
            if action != "echo":
                payload["command"] = "fixture"
            with self.assertRaises(ValidationError):
                Draft202012Validator(schema).validate(payload)

    def test_evidence_caps_and_secret_environment_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            (path / "00-oversized").write_text("x" * (256 * 1024 + 1))
            (path / "01-boundary").write_text("é" * (128 * 1024), encoding="utf-8")
            for index in range(18):
                (path / f"note-{index:02d}").write_text("fixture evidence")
            with mock.patch.dict(
                self.plugin.os.environ,
                {
                    "TRANSIST_EVIDENCE_DIR": directory,
                    "API_TOKEN": "dummy-fixture-secret",
                },
            ):
                result = self.invoke(
                    "exec_with_secret_env",
                    {"command": "fixture", "output_mode": "compact"},
                    "business",
                )
                environment = self.plugin.command_environment({})
            self.assertEqual(len(result["evidence_hints"]), 16)
            self.assertTrue(
                all(h["name"] != "00-oversized" for h in result["evidence_hints"])
            )
            boundary = next(
                h for h in result["evidence_hints"] if h["name"] == "01-boundary"
            )
            self.assertEqual(len(boundary["contents"].encode()), 256 * 1024)
            self.assertEqual(environment["API_TOKEN"], "dummy-fixture-secret")
            self.assertNotIn("dummy-fixture-secret", json.dumps(result))

    def test_serialization_error_is_not_success(self) -> None:
        result = self.invoke(
            "exec", {"command": "fixture", "output_mode": "compact"}, '{"guard": NaN}'
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "adapter_internal_error")
        self.assertEqual(result["output"], {})

    def test_compact_cleanup_failure_keeps_bounded_captured_hints(self) -> None:
        request = support.app_invoke_request(
            "exec", {"command": "fixture", "output_mode": "compact"}
        )
        _, frames, _ = support.run_process_plugin_main(
            self.plugin,
            request,
            [
                mock.patch.object(
                    self.plugin,
                    "run_process",
                    side_effect=self.plugin.CleanupUnconfirmedError(
                        "partial", "x" * (256 * 1024 + 1)
                    ),
                )
            ],
        )
        _, result = support.assert_app_frames(frames, "shell")
        self.assertEqual(result["error"]["code"], "protocol_error")
        self.assertEqual(result["error"]["retryability"], "none")
        self.assertEqual(
            result["artifact_hints"], [{"name": "stdout.txt", "contents": "partial"}]
        )


if __name__ == "__main__":
    unittest.main()
