"""Strict APP, nullable overrides, hint association and bounded-failure tests."""

import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

import jsonschema

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
import support  # noqa: E402

SPEC = importlib.util.spec_from_file_location("openspec_plugin", ROOT / "plugin.py")
PLUGIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLUGIN)


class RuntimeContractTests(unittest.TestCase):
    """Exercise current committed schemas and runtime inline-hint limits."""

    def test_runner_preserves_input_environment_and_timeout(self) -> None:
        completed = support.completed_process(["tool"], 7, "output", "stderr")
        with mock.patch.object(PLUGIN, "run_process", return_value=completed) as runner:
            self.assertIs(
                PLUGIN.run_command(
                    ["tool"], "/workspace", 12, "stdin", {"CI": "custom"}
                ),
                completed,
            )
        self.assertEqual(runner.call_args.args, (["tool"],))
        kwargs = runner.call_args.kwargs
        self.assertEqual(kwargs["cwd"], "/workspace")
        self.assertEqual(kwargs["timeout"], 12)
        self.assertEqual(kwargs["input"], "stdin")
        self.assertEqual(kwargs["env"]["CI"], "custom")
        self.assertIn("NO_COLOR", kwargs["env"])
        self.assertNotIn("capture_output", kwargs)
        self.assertNotIn("text", kwargs)

    def test_all_backends_and_validate_have_heartbeat(self) -> None:
        request = support.app_invoke_request("generate_plan", {})
        completed = support.completed_process([], 0, "response", "")
        environment = {
            key: "/fixture"
            for key in (
                "COPILOT_HOME",
                "COPILOT_SESSION_STATE_DIR",
                "XDG_CACHE_HOME",
                "TMPDIR",
            )
        }
        with mock.patch.dict(PLUGIN.os.environ, environment), mock.patch.object(
            PLUGIN, "extract_backend_usage", return_value=None
        ), mock.patch.object(PLUGIN, "emit"), mock.patch.object(
            PLUGIN, "run_process", return_value=completed
        ) as runner:
            for backend in ("gh-copilot", "codex", "cursor"):
                with self.subTest(backend=backend):
                    PLUGIN.generate_backend_response(
                        request, {"backend": backend}, "/fixture", "prompt"
                    )
                    self.assertTrue(callable(runner.call_args.kwargs["on_tick"]))
                    self.assertEqual(runner.call_args.kwargs["timeout"], 600)
            PLUGIN.run_openspec_validate(request, {}, "/fixture", "change")
            self.assertEqual(
                runner.call_args.args[0], ["openspec", "validate", "change"]
            )
            self.assertTrue(callable(runner.call_args.kwargs["on_tick"]))

    def test_cleanup_failure_and_unexpected_error_mapping(self) -> None:
        request = support.app_invoke_request("generate_plan", {})

        def invoke_runner(_request):
            return PLUGIN.run_command(["tool"], "/fixture", 12)

        for error, code, message in (
            (
                PLUGIN.CleanupUnconfirmedError("partial", "stderr"),
                "protocol_error",
                "Command process cleanup could not be confirmed",
            ),
            (
                RuntimeError("secret argv"),
                "adapter_internal_error",
                "Command runner failed",
            ),
            (
                ValueError("secret argv"),
                "adapter_internal_error",
                "Command runner failed",
            ),
        ):
            with self.subTest(code=code), mock.patch.object(
                PLUGIN, "handle_generate_plan", side_effect=invoke_runner
            ), mock.patch.object(PLUGIN, "run_process", side_effect=error):
                result = PLUGIN.handle_request(request)
            self.assertEqual(result["error"]["code"], code)
            self.assertEqual(result["error"]["message"], message)
            self.assertNotIn("secret argv", str(result))
            if code == "protocol_error":
                self.assertEqual(result["error"]["retryability"], "none")
                self.assertEqual(result["artifact_hints"][0]["contents"], "partial")
            support.contract_validator("app/invoke.result.v1.schema.json").validate(
                result
            )

    def test_timeout_retains_existing_error_behavior(self) -> None:
        request = support.app_invoke_request("generate_plan", {})

        def invoke_runner(_request):
            return PLUGIN.run_command(["tool"], "/fixture", 12)

        with mock.patch.object(
            PLUGIN, "handle_generate_plan", side_effect=invoke_runner
        ), mock.patch.object(
            PLUGIN, "run_process", side_effect=subprocess.TimeoutExpired(["tool"], 12)
        ):
            result = PLUGIN.handle_request(request)
        self.assertEqual(
            result["error"],
            {
                "code": "timeout_error",
                "class": "tool",
                "retryability": "safe",
                "message": "command timed out after 12 seconds",
            },
        )

    def test_usage_and_optional_fields(self) -> None:
        """Keep usage strict and extra provider counters in usage.json."""
        validator = support.contract_validator("app/invoke.result.v1.schema.json")
        self.assertEqual(PLUGIN.PLUGIN_VERSION, "0.1.0")
        usage = {
            "model": "fake",
            "input_tokens": 3,
            "reasoning_tokens": 2,
            "cache_read_tokens": 1,
        }
        result = PLUGIN.build_result("unit", "success", {}, usage=usage)
        validator.validate(result)
        self.assertEqual(result["usage"], {"model": "fake", "input_tokens": 3})
        full = next(
            hint for hint in result["artifact_hints"] if hint["name"] == "usage.json"
        )
        self.assertEqual(json.loads(full["contents"]), usage)
        absent = PLUGIN.build_result("unit", "success", {})
        self.assertNotIn("error", absent)
        self.assertNotIn("usage", absent)
        validator.validate(absent)
        failed = PLUGIN.failed_result(
            "unit", "tool_execution_error", "tool", "fixture", usage=usage
        )
        validator.validate(failed)
        self.assertTrue(
            any(hint["name"] == "usage.json" for hint in failed["artifact_hints"])
        )

    def test_path_association(self) -> None:
        """Give formerly colliding paths distinct flat names."""
        paths = [
            "specs/a/b/spec.md",
            "specs/a__b/spec.md",
            "canonical/specs/a/b/spec.md",
            "../bad.md",
            ".",
            "☃/spec.md",
        ]
        hints = [PLUGIN.artifact_hint(path, path) for path in paths]
        self.assertNotEqual(
            PLUGIN.artifact_hint(hints[0]["name"], "")["name"], hints[0]["name"]
        )
        self.assertEqual(len({hint["name"] for hint in hints}), len(paths))
        for hint in hints:
            self.assertNotIn("/", hint["name"])
            self.assertNotIn("\\", hint["name"])
        result = PLUGIN.build_result("unit", "success", {}, hints)
        association = json.loads(result["artifact_hints"][-1]["contents"])
        self.assertEqual(
            association, {hint["name"]: path for hint, path in zip(hints, paths)}
        )

    def test_limits(self) -> None:
        """Per-file, aggregate and count overflow surface explicit bounded errors."""
        with self.assertRaisesRegex(ValueError, "exceeds 262144 bytes"):
            PLUGIN.artifact_hint("large.md", "é" * (PLUGIN.MAX_HINT_BYTES // 2 + 1))
        for hints in [
            [PLUGIN.artifact_hint(f"f{i}", "x") for i in range(32)],
            [
                PLUGIN.artifact_hint(f"f{i}", "x" * PLUGIN.MAX_HINT_BYTES)
                for i in range(4)
            ],
        ]:
            result = PLUGIN.build_result("unit", "success", {}, hints)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "validation_error")
            self.assertEqual(result["artifact_hints"], [])
        boundary = [PLUGIN.artifact_hint(f"f{i}", "x") for i in range(31)]
        self.assertEqual(
            PLUGIN.build_result("unit", "success", {}, boundary)["status"], "success"
        )

    def test_nullable_inputs(self) -> None:
        """Preserve omitted/null overrides; reject empty enum defaults."""
        for action in PLUGIN.ACTIONS:
            schema = json.loads(
                (ROOT / f"schemas/{action}.input.schema.json").read_text()
            )
            validator = jsonschema.Draft202012Validator(schema)
            for extra in [
                {},
                {"model": None, "reasoning_effort": None},
                {"model": "fake", "reasoning_effort": "high"},
            ]:
                validator.validate({"change_name": "unit", **extra})
            with self.assertRaises(jsonschema.ValidationError):
                validator.validate({"change_name": "unit", "reasoning_effort": ""})


if __name__ == "__main__":
    unittest.main()
