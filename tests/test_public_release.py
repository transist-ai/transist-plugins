"""Lock the initial public catalog and its active release instructions."""

from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_plugin_structure  # noqa: E402
from check_public_snapshot import candidate_files  # noqa: E402

PACKAGES = frozenset(
    {
        "anthropic",
        "codex.cli",
        "cursor.cli",
        "git.cli",
        "git.mcp",
        "github.cli",
        "github.copilot.cli",
        "http",
        "jira.rest",
        "ollama.cli",
        "openai.compatible",
        "opencode.cli",
        "openspec",
        "shell",
        "text.http",
        "tika.http",
    }
)


class PublicReleaseTests(unittest.TestCase):
    """Reject missing packages, drifting pins and obsolete active instructions."""

    def test_exact_catalog_and_demo_pins(self) -> None:
        """Catalog releases are valid and demo pins match each local manifest."""
        paths = candidate_files(ROOT)
        manifests = [
            path
            for path in paths
            if len(path.parts) == 3
            and path.parts[0] == "plugins"
            and path.name == "plugin.yaml"
        ]
        self.assertEqual({path.parent.name for path in manifests}, PACKAGES)
        versions = {}
        for path in manifests:
            with self.subTest(path=str(path)):
                manifest = yaml.safe_load((ROOT / path).read_text())
                self.assertEqual(manifest["schema"], "transist.plugin.v1")
                self.assertEqual(manifest["id"], path.parent.name)
                self.assertTrue(
                    check_plugin_structure.is_release_version(manifest["version"])
                )
                versions[manifest["id"]] = manifest["version"]
        for path in paths:
            if path.parts[0] != "plugins" or path.suffix not in {".yaml", ".yml"}:
                continue
            document = yaml.safe_load((ROOT / path).read_text())
            if not isinstance(document, dict) or document.get("schema") not in {
                "transist.flow.v1",
                "transist.module.v1",
            }:
                continue
            with self.subTest(path=str(path)):
                self.assertTrue(
                    check_plugin_structure.is_release_version(document["version"])
                )
                for package, version in document.get("plugins", {}).items():
                    self.assertIn(package, PACKAGES)
                    self.assertTrue(check_plugin_structure.is_release_version(version))
                    self.assertEqual(version, versions[package])

    def test_active_documentation_uses_public_runtime(self) -> None:
        """Current reader entrypoints must not require the private development line."""
        paths = [
            "README.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "docs/authoring.md",
            "docs/compatibility.md",
            "docs/validation.md",
            "docs/publication.md",
            "docs/ci.md",
            "tests/services/tika_qdrant/README.md",
            *(f"plugins/{package}/README.md" for package in sorted(PACKAGES)),
        ]
        for path in paths:
            with self.subTest(path=path):
                text = (ROOT / path).read_text()
                self.assertNotIn("transist-ai/transist.oss", text)
                self.assertIsNone(re.search(r"\b0\.12(?:\.0)?\b", text))
        compatibility = (ROOT / "docs/compatibility.md").read_text()
        self.assertIn("44a762cdd8ec2d967497dbd5be5534618da61ec6", compatibility)


if __name__ == "__main__":
    unittest.main()
