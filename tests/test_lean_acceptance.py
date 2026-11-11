"""Root acceptance matrix for public pins, discovery, copied payloads and storage."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_plugin_structure  # noqa: E402
from benchmark_run_persistence import benchmark  # noqa: E402
from check_plugins import COMPACT_SMOKE_PACKAGES, offline_commands  # noqa: E402
from check_public_snapshot import candidate_files  # noqa: E402
from support import validate_json_schema  # noqa: E402

MODEL_PACKAGES = ("codex.cli", "cursor.cli", "github.copilot.cli", "opencode.cli")


class LeanAcceptanceTests(unittest.TestCase):
    """Enforce public contracts without operational homes or real providers."""

    def test_every_public_manifest_and_template_exact_pins(self) -> None:
        """Public releases are valid and template pins match local manifests."""
        manifests = [
            path
            for path in candidate_files(ROOT)
            if path.parts[0] == "plugins" and path.name == "plugin.yaml"
        ]
        self.assertTrue(manifests)
        catalog = {
            relative.parent.name: yaml.safe_load((ROOT / relative).read_text())
            for relative in manifests
        }
        for relative in manifests:
            with self.subTest(package=relative.parent.name):
                manifest = catalog[relative.parent.name]
                self.assertEqual(manifest["schema"], "transist.plugin.v1")
                self.assertEqual(manifest["id"], relative.parent.name)
                self.assertTrue(
                    check_plugin_structure.is_release_version(manifest["version"])
                )
                flow = yaml.safe_load(
                    (ROOT / relative.parent / "workflow.yaml").read_text()
                )
                self.assertEqual(flow["schema"], "transist.flow.v1")
                self.assertTrue(
                    check_plugin_structure.is_release_version(flow["version"])
                )
                self.assertTrue(flow["plugins"])
                for package, version in flow["plugins"].items():
                    self.assertIn(package, catalog)
                    self.assertTrue(check_plugin_structure.is_release_version(version))
                    self.assertEqual(version, catalog[package]["version"])

    def test_required_only_templates_preserve_literal_lean_controls(self) -> None:
        """Materialize optional inputs as null while preserving literal controls."""
        for plugin in MODEL_PACKAGES:
            directory = ROOT / "plugins" / plugin
            flow = yaml.safe_load((directory / "workflow.yaml").read_text())
            bindings = {
                key: definition.get("default")
                for key, definition in flow["inputs"].items()
            }
            bindings["prompt"] = "Fixture prompt"
            payload = {
                key: (
                    bindings[value.removeprefix("{{inputs.").removesuffix("}}")]
                    if isinstance(value, str) and value.startswith("{{inputs.")
                    else value
                )
                for key, value in flow["steps"][0]["with"].items()
            }
            payload["prompt"] = bindings["prompt"]
            validate_json_schema(
                json.loads(
                    (directory / "schemas/exec_task.input.schema.json").read_text()
                ),
                payload,
            )
            self.assertEqual(payload["output_mode"], "compact")
            self.assertEqual(payload["history_mode"], "invocation")
            self.assertEqual(payload["max_context_file_bytes"], 65536)
            self.assertEqual(payload["max_prompt_bytes"], 98304)

    def test_copied_lean_policy_is_selected_and_identical(self) -> None:
        """Each model package ships its own exact policy, independent of other roots."""
        expected = (
            ROOT / "plugins" / MODEL_PACKAGES[0] / "lean_policy.py"
        ).read_bytes()
        for plugin in MODEL_PACKAGES:
            directory = ROOT / "plugins" / plugin
            self.assertEqual((directory / "lean_policy.py").read_bytes(), expected)
            manifest = yaml.safe_load((directory / "plugin.yaml").read_text())
            self.assertIn("lean_policy.py", manifest["package"]["files"])

    def test_discovery_includes_model_matrix_and_only_supported_compact_smokes(
        self,
    ) -> None:
        """Discovery collects all units; only opted-in smoke CLIs receive --compact."""
        for plugin in (
            *MODEL_PACKAGES,
            *sorted(COMPACT_SMOKE_PACKAGES),
            "openspec",
            "http",
        ):
            commands = offline_commands(ROOT / "plugins" / plugin)
            self.assertTrue(commands)
            compact = [command for command in commands if "--compact" in command]
            self.assertEqual(bool(compact), plugin in COMPACT_SMOKE_PACKAGES)
        commands = offline_commands(ROOT / "plugins/codex.cli")
        discover = next(command for command in commands if "discover" in command)
        self.assertEqual(discover[-1], "test_*.py")
        self.assertTrue(
            (ROOT / "plugins/codex.cli/tests/test_lean_cli_contract.py").is_file()
        )

    def test_transcript_heavy_storage_savings_and_linear_replay(self) -> None:
        """Real handler/schema fixtures satisfy labelled synthetic storage targets."""
        report = benchmark(iterations=4, depth=3, transcript_bytes=32768)
        self.assertGreaterEqual(report["savings_fraction"], 0.75)
        self.assertAlmostEqual(report["compact_history_growth_ratio"], 2, delta=0.05)
        self.assertGreater(
            report["legacy"]["components"]["cumulative_legacy_history_artifacts"], 0
        )
        self.assertGreater(report["compact"]["workspace_history_files"], 0)
        self.assertEqual(report["installed_runtime_smoke"], "not_run")
        self.assertIn("not_runtime_disk", report["kind"])


if __name__ == "__main__":
    unittest.main()
