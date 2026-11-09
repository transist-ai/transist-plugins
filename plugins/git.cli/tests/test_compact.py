"""Offline compact contracts, aggregate diagnostics and byte-bound regressions."""

import json
import pathlib
import sys
import unittest
from unittest import mock

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
import support  # noqa: E402


def validator(action: str) -> Draft202012Validator:
    """Resolve output contracts from package-local bytes without network access."""
    compact = support.read_json(ROOT / "schemas/compact.schema.json")
    registry = Registry().with_resource(
        "compact.schema.json", Resource.from_contents(compact)
    )
    schema = support.read_json(ROOT / f"schemas/{action}.output.schema.json")
    return Draft202012Validator(schema, registry=registry)


class CompactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = support.load_plugin_module(ROOT)

    def invoke(self, action: str, payload: dict, processes: list) -> dict:
        """Execute mocked commands through the actual package APP handler."""
        request = support.app_invoke_request(action, payload)
        with mock.patch.object(self.plugin, "run_process", side_effect=processes):
            result = self.plugin.handle_request(request)
        support.contract_validator("app/invoke.result.v1.schema.json").validate(result)
        return result

    def assert_bytes(self, payload: dict, result: dict) -> None:
        """Assert counters against the actual escaped final APP serialization."""
        for key, value in (
            ("input_bytes", payload),
            ("output_bytes", result["output"]),
            ("result_bytes", result),
        ):
            self.assertEqual(
                result["metrics"][key], len(json.dumps(value).encode("utf-8"))
            )

    def test_all_closed_output_contracts_both_modes(self) -> None:
        for path in (ROOT / "schemas").glob("*.output.schema.json"):
            schema = support.read_json(path)
            legacy = {}
            for key in schema["required"] + [
                k for k in ("stdout", "stderr") if k in schema["properties"]
            ]:
                kind = schema["properties"][key]["type"]
                legacy[key] = {
                    "string": "",
                    "boolean": True,
                    "integer": 0,
                    "array": [],
                }[kind]
            compact = {k: v for k, v in legacy.items() if k not in ("stdout", "stderr")}
            compact.update(
                output_mode="compact",
                diagnostics={
                    "artifact": "",
                    "bytes": 0,
                    "retained": False,
                    "stdout_bytes": 0,
                    "stderr_bytes": 0,
                },
            )
            check = validator(path.name.split(".output")[0])
            with self.subTest(action=path.name):
                check.validate(legacy)
                check.validate(compact)
                for bad in (
                    legacy | {"unexpected": 1},
                    compact | {"stdout": "raw"},
                    compact | {"output_mode": "legacy"},
                    compact
                    | {"diagnostics": compact["diagnostics"] | {"unexpected": 1}},
                    {k: v for k, v in compact.items() if k != "diagnostics"},
                ):
                    with self.assertRaises(ValidationError):
                        check.validate(bad)

    def test_branch_modes_and_exact_metrics(self) -> None:
        process = support.completed_process([], 0, 'feature/é"\\\n', "note\n")
        for mode in (None, "legacy", "compact"):
            payload = {} if mode is None else {"output_mode": mode}
            result = self.invoke("current_branch", payload, [process])
            self.assertEqual(result["output"]["branch"], process.stdout.strip())
            validator("current_branch").validate(result["output"])
            if mode == "compact":
                self.assert_bytes(payload, result)
                self.assertNotIn("stdout", result["output"])
                self.assertEqual(len(result["artifact_hints"]), 1)
            else:
                self.assertEqual(result["output"]["stdout"], process.stdout)
                self.assertNotIn("output_mode", result["output"])
                self.assertEqual(result["metrics"], {})

    def test_commit_diagnostics_contain_each_command_once(self) -> None:
        processes = [
            support.completed_process([], 0, text, "")
            for text in ("added\n", " M file\n", "committed\n", "abc123\n")
        ]
        payload = {"message": "fixture", "output_mode": "compact"}
        result = self.invoke("commit_worktree", payload, processes)
        self.assertEqual(result["output"]["commit_sha"], "abc123")
        self.assertTrue(result["output"]["committed"])
        self.assertEqual(result["output"]["message"], "fixture")
        validator("commit_worktree").validate(result["output"])
        commands = json.loads(result["artifact_hints"][0]["contents"])["commands"]
        self.assertEqual([c["stdout"] for c in commands], [c.stdout for c in processes])
        self.assertEqual(len(commands), 4)
        self.assert_bytes(payload, result)

    def test_capture_utf8_boundary_and_explicit_failure(self) -> None:
        process = support.completed_process([], 0, "é\n", "")
        for cap, status in ((3, "success"), (2, "failed")):
            payload = {"output_mode": "compact", "max_capture_bytes": cap}
            result = self.invoke("current_branch", payload, [process])
            self.assertEqual(result["status"], status)
            if status == "failed":
                self.assertIn("incomplete", result["error"]["message"])
                self.assertEqual(result["error"]["code"], "tool_execution_error")
                self.assertEqual(result["error"]["retryability"], "safe")
            self.assert_bytes(payload, result)

    def test_artifact_escaping_boundary(self) -> None:
        process = support.completed_process([], 0, 'é"\\\n', "")
        first = self.invoke("current_branch", {"output_mode": "compact"}, [process])
        size = len(first["artifact_hints"][0]["contents"].encode("utf-8"))
        self.assertGreater(size, len(process.stdout.encode("utf-8")))
        for cap, retained in ((size, True), (size - 1, False)):
            payload = {"output_mode": "compact", "max_diagnostic_bytes": cap}
            result = self.invoke("current_branch", payload, [process])
            self.assertEqual(result["output"]["diagnostics"]["retained"], retained)
            self.assertEqual(result["output"]["diagnostics"]["bytes"], size)
            self.assertEqual(result["output"]["branch"], process.stdout.strip())
            self.assertEqual(bool(result["artifact_hints"]), retained)
            self.assert_bytes(payload, result)

    def test_failure_preview_and_cleanup_are_honest(self) -> None:
        payload = {"output_mode": "compact"}
        process = support.completed_process([], 1, "", "é" * 5000)
        result = self.invoke("current_branch", payload, [process])
        self.assertLess(len(result["error"]["message"].encode("utf-8")), 2200)
        self.assertEqual(
            json.loads(result["artifact_hints"][0]["contents"])["commands"][0][
                "stderr"
            ],
            process.stderr,
        )
        self.assert_bytes(payload, result)
        request = support.app_invoke_request("current_branch", payload)
        with mock.patch.object(
            self.plugin,
            "run_process",
            side_effect=self.plugin.CleanupUnconfirmedError("partial", "diagnostic"),
        ):
            result = self.plugin.handle_request(request)
        self.assertEqual(result["error"]["retryability"], "none")
        self.assertEqual(result["artifact_hints"][0]["contents"], "partial")

    def test_invalid_options_do_not_launch(self) -> None:
        for option in (
            {"output_mode": "other"},
            {"max_capture_bytes": True},
            {"max_capture_bytes": 0},
        ):
            with mock.patch.object(self.plugin, "run_process") as run:
                result = self.plugin.handle_request(
                    support.app_invoke_request("current_branch", option)
                )
            run.assert_not_called()
            self.assertEqual(result["error"]["code"], "validation_error")

    def test_serialization_failure_is_terminal_failure(self) -> None:
        request = support.app_invoke_request(
            "current_branch", {"output_mode": "compact"}
        )
        with mock.patch.object(
            self.plugin,
            "handle_action",
            return_value=self.plugin.invoke_result(
                request, "success", {"nonfinite": float("nan")}
            ),
        ):
            result = self.plugin.handle_request(request)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "adapter_internal_error")
        self.assertEqual(result["output"], {})

    def test_capture_failure_after_commit_requires_operator_and_keeps_sha(self) -> None:
        """A completed commit or later read overflow must not replay that commit."""
        sha = "a" * 40
        for mode in ("legacy", "compact"):
            for overflow_on_head in (False, True):
                processes = [
                    support.completed_process([], 0, "M x\n", ""),
                    support.completed_process([], 0, "ok\n", ""),
                ]
                if overflow_on_head:
                    processes.append(support.completed_process([], 0, sha + "\n", ""))
                payload = {
                    "message": "fixture",
                    "add_all": False,
                    "output_mode": mode,
                    "max_capture_bytes": 7 if overflow_on_head else 4,
                }
                request = support.app_invoke_request("commit_worktree", payload)
                with self.subTest(mode=mode, head=overflow_on_head), mock.patch.object(
                    self.plugin, "run_process", side_effect=processes
                ) as run:
                    result = self.plugin.handle_request(request)
                support.contract_validator("app/invoke.result.v1.schema.json").validate(
                    result
                )
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"]["retryability"], "operator")
                self.assertEqual(run.call_count, len(processes))
                self.assertEqual(
                    sum(c.args[0][1] == "commit" for c in run.call_args_list), 1
                )
                self.assertTrue(result["output"]["committed"])
                self.assertEqual(
                    result["output"]["commit_sha"], sha if overflow_on_head else ""
                )
                self.assertEqual(result["output"]["message"], "fixture")
                validator("commit_worktree").validate(result["output"])
                if mode == "compact":
                    self.assert_bytes(payload, result)

    def test_completed_push_capture_failure_requires_operator(self) -> None:
        """Output policy must not mark a completed push safe to dispatch again."""
        for mode in ("legacy", "compact"):
            payload = {
                "branch": "feature",
                "remote": "origin",
                "output_mode": mode,
                "max_capture_bytes": 1,
            }
            request = support.app_invoke_request("push_current_branch", payload)
            with mock.patch.object(
                self.plugin,
                "run_process",
                return_value=support.completed_process([], 0, "pushed\n", ""),
            ) as run:
                result = self.plugin.handle_request(request)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][:2], ["git", "push"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["retryability"], "operator")
            self.assertEqual(result["output"]["branch"], "feature")
            self.assertEqual(result["output"]["remote"], "origin")
            validator("push_current_branch").validate(result["output"])

            # Reusing the caller's request cannot carry a prior mutation boundary.
            request["input"] = {"output_mode": mode, "max_capture_bytes": 1}
            with mock.patch.object(
                self.plugin,
                "run_process",
                return_value=support.completed_process([], 0, "feature\n", ""),
            ) as run:
                probe_failure = self.plugin.handle_request(request)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0], ["git", "branch", "--show-current"])
            self.assertEqual(probe_failure["error"]["retryability"], "safe")

    def test_optional_diagnostic_omission_after_push_is_success(self) -> None:
        """Dropping optional logs after mutation does not create a retryable failure."""
        result = self.invoke(
            "push_current_branch",
            {"branch": "feature", "output_mode": "compact", "max_diagnostic_bytes": 1},
            [support.completed_process([], 0, "pushed\n", "")],
        )
        self.assertEqual(result["status"], "success")
        self.assertNotIn("error", result)
        self.assertFalse(result["output"]["diagnostics"]["retained"])


if __name__ == "__main__":
    unittest.main()
