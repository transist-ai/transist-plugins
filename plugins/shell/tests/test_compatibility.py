"""Offline public-plugin regressions against the vendored 0.12.0 APP schemas."""

import importlib.util
import json
import pathlib
import sys
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType
from unittest import mock

import jsonschema
import yaml

REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
import support  # noqa: E402

SELECTED = (
    "shell",
    "git.cli",
    "github.cli",
    "jira.rest",
    "ollama.cli",
    "anthropic",
    "openai.compatible",
    "codex.cli",
    "cursor.cli",
    "github.copilot.cli",
    "opencode.cli",
)


@contextmanager
def load_smoke(name: str) -> Iterator[ModuleType]:
    """Load/run an offline suite with package-local helpers; restore import state."""
    root = REPO / "plugins" / name
    spec = importlib.util.spec_from_file_location(
        f"compatibility_smoke_{name.replace('.', '_')}",
        root / "tests/smoke.py",
    )
    module = importlib.util.module_from_spec(spec)
    # These bare imports deliberately resolve locally in standalone execution.
    # Keep them isolated through main(), which loads the plugin dynamically too.
    helpers = (
        "runner_fixture",
        "process_runner",
        "process_lifecycle",
        "lean_policy",
        "gate_review",
    )
    previous = {key: sys.modules[key] for key in helpers if key in sys.modules}
    try:
        for key in helpers:
            sys.modules.pop(key, None)
        with mock.patch.object(
            sys, "path", [str(root / "tests"), str(root), *sys.path]
        ):
            spec.loader.exec_module(module)
            yield module
    finally:
        for key in helpers:
            sys.modules.pop(key, None)
        sys.modules.update(previous)


class CompatibilityTests(unittest.TestCase):
    """Validate real smoke frames, demo materialization, and provider details."""

    def assert_strict_frames(self, frames: list, plugin_id: str) -> tuple:
        """Validate every emitted frame and ensure the manifest/handshake agree."""
        for frame in frames:
            filename = frame["type"].removeprefix("app.") + ".schema.json"
            support.contract_validator(f"app/{filename}").validate(frame)
        manifest = yaml.safe_load(
            (REPO / "plugins" / plugin_id / "plugin.yaml").read_text()
        )
        self.assertEqual(
            frames[0]["plugin"], {"id": plugin_id, "version": manifest["version"]}
        )
        self.assertTrue(
            all(frame["type"] == "app.invoke.progress.v1" for frame in frames[1:-1])
        )
        self.assertEqual(frames[-1]["type"], "app.invoke.result.v1")
        return frames[0], frames[-1]

    def test_smoke_frames(self) -> None:
        """Run all selected offline smoke paths with strict APP frame validation."""
        with mock.patch.object(support, "assert_app_frames", self.assert_strict_frames):
            for name in SELECTED:
                with self.subTest(plugin=name):
                    with load_smoke(name) as suite:
                        suite.main()

    def test_smoke_helpers_are_local_and_cache_is_restored(self) -> None:
        """A prior package's bare helper imports cannot leak into another suite."""
        helpers = (
            "runner_fixture",
            "process_runner",
            "process_lifecycle",
            "lean_policy",
            "gate_review",
        )
        cached = {key: ModuleType(key) for key in helpers}
        with mock.patch.dict(sys.modules, cached):
            for name in ("codex.cli", "github.copilot.cli", "opencode.cli"):
                with self.subTest(plugin=name), load_smoke(name) as suite:
                    root = REPO / "plugins" / name
                    self.assertEqual(
                        pathlib.Path(suite.fixture_runner.__code__.co_filename),
                        root / "tests/runner_fixture.py",
                    )
                    # Loading alone cannot launch a provider or signal a group.
                    support.load_plugin_module(root)
                    for key in helpers:
                        expected = root / (
                            "tests/runner_fixture.py"
                            if key == "runner_fixture"
                            else f"{key}.py"
                        )
                        self.assertEqual(
                            pathlib.Path(sys.modules[key].__file__), expected
                        )
                for key in helpers:
                    self.assertIs(sys.modules[key], cached[key])

    def test_required_only_demo_materialization(self) -> None:
        """Missing optional workflow values bind as null and pass action schemas."""
        for name in ("codex.cli", "github.copilot.cli", "opencode.cli"):
            with self.subTest(plugin=name):
                root = REPO / "plugins" / name
                flow = yaml.safe_load((root / "workflow.yaml").read_text())
                bindings = {
                    key: value.get("default") for key, value in flow["inputs"].items()
                }
                bindings["prompt"] = "Required-only prompt"
                action_input = {
                    key: (
                        bindings[value.removeprefix("{{inputs.").removesuffix("}}")]
                        if isinstance(value, str) and value.startswith("{{inputs.")
                        else value
                    )
                    for key, value in flow["steps"][0]["with"].items()
                }
                for key, value in flow["steps"][0]["with"].items():
                    if isinstance(value, str) and value.startswith("{{inputs."):
                        self.assertIsNone(action_input[key])
                    else:
                        self.assertEqual(action_input[key], value)
                self.assertEqual(action_input["output_mode"], "compact")
                self.assertEqual(action_input["history_mode"], "invocation")
                action_input["prompt"] = bindings["prompt"]
                schema = json.loads(
                    (root / "schemas/exec_task.input.schema.json").read_text()
                )
                jsonschema.Draft202012Validator(schema).validate(action_input)
                # Explicit options remain available; malformed efforts still fail.
                for effort in ("low", "medium", "high", "xhigh"):
                    jsonschema.Draft202012Validator(schema).validate(
                        action_input
                        | {"model": "chosen-model", "reasoning_effort": effort}
                    )
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.Draft202012Validator(schema).validate(
                        action_input | {"reasoning_effort": "invalid"}
                    )

    def test_usage_projection_does_not_mutate_provider_details(self) -> None:
        """Canonical APP usage excludes extras while preserving provider artifacts."""
        details = {
            "provider": "fixture",
            "model": "fixture-model",
            "input_tokens": 2,
            "output_tokens": 3,
            "total_tokens": 5,
            "cost_usd": 0.25,
            "reasoning_tokens": 1,
            "cache_read_tokens": 4,
        }
        canonical = {
            key: details[key]
            for key in (
                "provider",
                "model",
                "input_tokens",
                "output_tokens",
                "total_tokens",
            )
        }
        for name in ("codex.cli", "github.copilot.cli", "cursor.cli", "opencode.cli"):
            with self.subTest(plugin=name):
                plugin = support.load_plugin_module(REPO / "plugins" / name)
                hints = [{"name": "usage.json", "contents": json.dumps(details)}]
                frame = plugin.invoke_result(
                    "test", "success", usage=details, artifact_hints=hints
                )
                self.assertEqual(frame["usage"], canonical)
                self.assertNotIn("error", frame)
                self.assertEqual(
                    json.loads(frame["artifact_hints"][0]["contents"]), details
                )
                self.assertNotIn("usage", plugin.invoke_result("test", "success"))


if __name__ == "__main__":
    unittest.main()
