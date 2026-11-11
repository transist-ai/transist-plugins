"""Exercise static checker success and failure paths without invoking any plugins."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_plugin_structure import (  # noqa: E402
    DRAFT,
    StructureError,
    check_repository,
    is_release_version,
    read_document,
)


class PluginStructureTests(unittest.TestCase):
    """Use tiny disposable source trees to test source-only package contracts."""

    def setUp(self) -> None:
        """Create a Git source fixture with one executable but never execute it."""
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        self.package = self.root / "plugins" / "example"
        (self.package / "schemas").mkdir(parents=True)
        (self.package / "tests").mkdir()
        (self.package / "README.md").write_text("# Example\n")
        self.workflow = {
            "schema": "transist.flow.v1",
            "id": "example_demo",
            "version": "0.1.0",
            "plugins": {"example": "0.1.0"},
        }
        self.write_workflow()
        (self.package / "tests/test_example.py").write_text("# Presence only\n")
        entrypoint = self.package / "plugin.py"
        entrypoint.write_text("raise RuntimeError('must never execute')\n")
        entrypoint.chmod(0o755)
        self.manifest = {
            "schema": "transist.plugin.v1",
            "id": "example",
            "version": "0.1.0",
            "adapter": "process",
            "entrypoint": {"type": "process", "command": "plugin.py"},
            "package": {
                "files": [
                    "plugin.py",
                    "schemas/example.input.schema.json",
                    "schemas/example.output.schema.json",
                ]
            },
            "actions": [
                {
                    "name": "example",
                    "input_schema": "schemas/example.input.schema.json",
                    "output_schema": "schemas/example.output.schema.json",
                }
            ],
        }
        self.write_manifest()
        for kind in ("input", "output"):
            self.write_schema(kind, {"$schema": DRAFT, "type": "object"})

    def write_manifest(self) -> None:
        """Persist a mutated fixture manifest without invoking its implementation."""
        (self.package / "plugin.yaml").write_text(yaml.safe_dump(self.manifest))

    def write_workflow(self) -> None:
        """Persist a mutated demo without invoking its implementation."""
        (self.package / "workflow.yaml").write_text(yaml.safe_dump(self.workflow))

    def write_schema(self, kind: str, schema: object) -> None:
        """Persist a JSON action schema fixture."""
        (self.package / f"schemas/example.{kind}.schema.json").write_text(
            json.dumps(schema)
        )

    def assert_rejected(self, diagnostic: str) -> None:
        """Require a failed gate with an expected actionable diagnostic."""
        self.assertIn(diagnostic, "\n".join(check_repository(self.root)))

    def test_source_only_and_dynamic_inventory(self) -> None:
        """One valid package passes, without a fixed inventory or plugin execution."""
        before = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
        self.assertEqual(check_repository(self.root), [])
        self.assertEqual(
            sorted(path.relative_to(self.root) for path in self.root.rglob("*")), before
        )

    def test_missing_manifest_and_required_layout(self) -> None:
        """Incomplete directories cannot disappear through manifest-only discovery."""
        for relative in ("plugin.yaml", "README.md", "workflow.yaml"):
            with self.subTest(relative=relative):
                path = self.package / relative
                original = path.read_text()
                path.unlink()
                self.assert_rejected(f"{relative}: required source file is missing")
                path.write_text(original)
        (self.package / "tests/test_example.py").unlink()
        self.assert_rejected("tests/: require at least one")

    def test_misplaced_manifest(self) -> None:
        """Public plugin manifests must sit exactly under plugins/<id>/."""
        (self.root / "plugin.yaml").write_text("{}\n")
        self.assert_rejected("plugin manifests belong at plugins/<id>/plugin.yaml")

    def test_identity_and_duplicate_actions(self) -> None:
        """Directory identity and unique action names are enforced."""
        self.manifest["id"] = "wrong"
        self.write_manifest()
        self.assert_rejected("manifest id must match")
        self.manifest["id"] = "example"
        self.manifest["actions"].append(self.manifest["actions"][0].copy())
        self.write_manifest()
        self.assert_rejected("action names must be nonempty and unique")

    def test_independent_plugin_and_workflow_versions(self) -> None:
        """Accept independently released packages and workflows with exact pins."""
        import shutil

        other = self.root / "plugins" / "other"
        shutil.copytree(self.package, other)
        manifest = dict(self.manifest, id="other", version="1.2.3")
        (other / "plugin.yaml").write_text(yaml.safe_dump(manifest))
        for version in ("0.1.1", "0.2.0", "1.0.0", "2.0.0-rc.1+build.42"):
            with self.subTest(version=version):
                self.manifest["version"] = version
                self.write_manifest()
                self.workflow["version"] = "3.0.0"
                self.workflow["plugins"] = {"example": version, "other": "1.2.3"}
                self.write_workflow()
                (other / "workflow.yaml").write_text(yaml.safe_dump(self.workflow))
                self.assertEqual(check_repository(self.root), [])

    def test_invalid_versions_and_dependency_ranges(self) -> None:
        """Reject malformed SemVer and nonexact pins without executing payloads."""
        for version in (
            "",
            "0.1",
            "v0.1.0",
            "01.1.0",
            "1.01.0",
            "1.0.01",
            "1.0.0-01",
            "1.0.0-",
            "1.0.0+",
            "1.0.0-rc..1",
            "1.0.0+build..1",
            "1.0.0\n",
            "^0.1.0",
            ">=0.1.0",
            "*",
            0.1,
            True,
            None,
            [],
            {},
        ):
            with self.subTest(version=version):
                self.assertFalse(is_release_version(version))
                self.manifest["version"] = version
                self.write_manifest()
                self.assert_rejected(
                    "manifest version must be an exact semantic version"
                )
                self.manifest["version"] = "0.1.0"
                self.write_manifest()
                self.workflow["version"] = version
                self.write_workflow()
                self.assert_rejected(
                    "workflow version must be an exact semantic version"
                )
                self.workflow["version"] = "0.1.0"
                self.workflow["plugins"] = {"example": version}
                self.write_workflow()
                self.assert_rejected(
                    "workflow plugin pins must be exact semantic versions"
                )

    def test_dependency_pins_match_each_local_manifest(self) -> None:
        """Valid versions cannot hide stale pins or unavailable dependencies."""
        self.workflow["plugins"] = {"example": "0.1.1"}
        self.write_workflow()
        self.assert_rejected(
            "workflow plugin pin must match its local manifest version"
        )
        self.workflow["plugins"] = {"example": "0.1.0", "missing": "1.0.0"}
        self.write_workflow()
        self.assert_rejected("workflow dependency must have a local plugin manifest")

    def test_workflow_requires_release_and_dependency_mapping(self) -> None:
        """Missing releases and empty/nonmapping pins cannot evade the catalog gate."""
        del self.workflow["version"]
        self.write_workflow()
        self.assert_rejected("workflow version must be an exact semantic version")
        self.workflow["version"] = "0.1.0"
        for pins in (None, {}, [], ["0.1.0"], "0.1.0"):
            with self.subTest(pins=pins):
                self.workflow["plugins"] = pins
                self.write_workflow()
                self.assert_rejected("workflow plugins must be a nonempty mapping")

    def test_release_policy_does_not_reset_schema_identifiers(self) -> None:
        """Release 0.1.0 uses the unchanged v1 manifest/workflow and schema dialect."""
        self.assertEqual(check_repository(self.root), [])
        for document, writer, diagnostic in (
            (self.manifest, self.write_manifest, "manifest schema"),
            (self.workflow, self.write_workflow, "workflow schema"),
        ):
            original = document["schema"]
            document["schema"] = original.replace("v1", "v0.1.0")
            writer()
            self.assert_rejected(diagnostic)
            document["schema"] = original
            writer()

    def test_payload_rejections(self) -> None:
        """Reject empty, duplicate, implicit, missing and nonliteral payload paths."""
        original = self.manifest["package"]["files"].copy()
        for selected, diagnostic in (
            ([], "nonempty list"),
            (original + ["plugin.py"], "duplicate entries"),
            (original + ["plugin.yaml"], "implicit"),
            (original + ["missing.py"], "absent from public sources"),
            (original + ["../outside"], "absent from public sources"),
        ):
            with self.subTest(selected=selected):
                self.manifest["package"]["files"] = selected
                self.write_manifest()
                self.assert_rejected(diagnostic)

    def test_symlinked_file_and_ancestor(self) -> None:
        """Reject symlink payloads even when they resolve inside the package."""
        subprocess.run(["git", "add", "plugins"], cwd=self.root, check=True)
        entrypoint = self.package / "plugin.py"
        entrypoint.rename(self.package / "real.py")
        entrypoint.symlink_to("real.py")
        self.assert_rejected("symlinks are not allowed")
        entrypoint.unlink()
        (self.package / "real.py").rename(entrypoint)
        (self.package / "schemas").rename(self.package / "real-schemas")
        (self.package / "schemas").symlink_to("real-schemas", target_is_directory=True)
        self.assert_rejected("symlinks are not allowed")

    def test_entrypoint_contract(self) -> None:
        """Require matching adapter types and selected executable entrypoints."""
        self.manifest["entrypoint"]["type"] = "mcp"
        self.write_manifest()
        self.assert_rejected("entrypoint.type must match")
        self.manifest["entrypoint"]["type"] = "process"
        self.manifest["entrypoint"]["command"] = "outside.py"
        self.write_manifest()
        self.assert_rejected("entrypoint.command must be a selected")
        self.manifest["entrypoint"]["command"] = "plugin.py"
        self.write_manifest()
        (self.package / "plugin.py").chmod(0o644)
        self.assert_rejected("entrypoint.command must be executable")

    def test_http_and_mcp_layouts(self) -> None:
        """Adapter-specific entrypoints pass without requiring a uniform filename."""
        self.manifest["adapter"] = "http"
        self.manifest["entrypoint"] = {"type": "http", "base_url": "http://localhost"}
        self.write_manifest()
        self.assertEqual(check_repository(self.root), [])
        self.manifest["adapter"] = "mcp"
        self.manifest["entrypoint"] = {"type": "mcp", "command": "plugin.py"}
        self.write_manifest()
        self.assert_rejected("MCP action requires bindings.tool_name")
        self.manifest["actions"][0]["bindings"] = {"tool_name": "example"}
        self.write_manifest()
        self.assertEqual(check_repository(self.root), [])

    def test_schema_validation(self) -> None:
        """Validate schema dialect, syntax and selected membership statically."""
        self.write_schema("input", {"$schema": DRAFT, "type": "invalid-type"})
        self.assert_rejected("invalid Draft 2020-12 schema")
        self.write_schema("input", {"type": "object"})
        self.assert_rejected("must declare JSON Schema Draft 2020-12")
        self.write_schema("input", {"$schema": DRAFT, "type": "object"})
        self.manifest["package"]["files"].remove("schemas/example.input.schema.json")
        self.write_manifest()
        self.assert_rejected("referenced schema must be selected")

    def test_local_refs_fragments_and_cycles(self) -> None:
        """Resolve selected local references and fragments without network requests."""
        self.manifest["package"]["files"].append("schemas/shared.schema.json")
        self.write_manifest()
        shared = self.package / "schemas/shared.schema.json"
        shared.write_text(
            json.dumps({"$defs": {"thing": {"$ref": "example.input.schema.json"}}})
        )
        self.write_schema(
            "input", {"$schema": DRAFT, "$ref": "shared.schema.json#/$defs/thing"}
        )
        self.assertEqual(check_repository(self.root), [])
        self.write_schema(
            "input", {"$schema": DRAFT, "$ref": "shared.schema.json#/missing"}
        )
        self.assert_rejected("missing JSON pointer")
        shared.write_text(json.dumps({"$anchor": "thing", "type": "string"}))
        self.write_schema(
            "input", {"$schema": DRAFT, "$ref": "shared.schema.json#thing"}
        )
        self.assertEqual(check_repository(self.root), [])

    def test_unsafe_schema_refs(self) -> None:
        """Remote, absolute, traversal and unselected reference targets fail closed."""
        for reference, diagnostic in (
            ("https://example.org/schema.json", "remote or qualified"),
            ("/schema.json", "must stay inside"),
            ("%2e%2e/schema.json", "must stay inside"),
            ("missing.schema.json", "unresolved $ref target"),
        ):
            with self.subTest(reference=reference):
                self.write_schema("input", {"$schema": DRAFT, "$ref": reference})
                self.assert_rejected(diagnostic)

    def test_malformed_documents_and_duplicate_keys(self) -> None:
        """Reject malformed YAML/JSON without including their values in diagnostics."""
        for name, content in (
            ("extra.yaml", "x: [\n"),
            ("extra.yaml", "x: 1\nx: 2\n"),
            ("extra.json", '{"x": 1, "x": 2}'),
            ("extra.json", '{"secret":'),
            ("extra.json", '{"x": NaN}'),
        ):
            with self.subTest(content=content):
                filename = self.root / name
                filename.write_text(content)
                self.assertTrue(check_repository(self.root))
                with self.assertRaises(StructureError):
                    read_document(filename)
                filename.unlink()

    def test_yaml_merges_and_explicit_overrides(self) -> None:
        """Allow YAML merges and overrides, while rejecting duplicate authored keys."""
        filename = self.root / "extra.yaml"
        filename.write_text(
            "defaults: &defaults\n  x: 1\nitem:\n  <<: *defaults\n  x: 2\n"
        )
        self.assertEqual(read_document(filename)["item"], {"x": 2})
        self.assertEqual(check_repository(self.root), [])

    def test_literal_refs_in_instance_data(self) -> None:
        """Annotation/example data is not recursively treated as executable schema."""
        self.write_schema(
            "input",
            {
                "$schema": DRAFT,
                "default": {"$ref": "https://example.org/item"},
                "examples": [{"$anchor": "fiction"}],
            },
        )
        self.assertEqual(check_repository(self.root), [])
        self.write_schema(
            "input",
            {
                "$schema": DRAFT,
                "$ref": "#fiction",
                "examples": [{"$anchor": "fiction"}],
            },
        )
        self.assert_rejected("unresolved $ref target")

    def test_schema_resource_ids(self) -> None:
        """Resolve relative and embedded $id resources with their proper base URIs."""
        (self.package / "schemas/nested").mkdir()
        shared = "schemas/nested/shared.schema.json"
        self.manifest["package"]["files"].append(shared)
        self.write_manifest()
        (self.package / shared).write_text(json.dumps({"type": "string"}))
        self.write_schema(
            "input",
            {"$schema": DRAFT, "$id": "nested/root.json", "$ref": "shared.schema.json"},
        )
        self.assertEqual(check_repository(self.root), [])
        self.write_schema(
            "input",
            {
                "$schema": DRAFT,
                "$defs": {
                    "inner": {
                        "$id": "embedded.json",
                        "$anchor": "thing",
                        "type": "string",
                    }
                },
                "$ref": "embedded.json#thing",
            },
        )
        self.assertEqual(check_repository(self.root), [])

    def test_reference_target_must_be_schema(self) -> None:
        """A pointer to an instance value must not pass as a compiled schema."""
        self.write_schema(
            "input", {"$schema": DRAFT, "default": 42, "$ref": "#/default"}
        )
        self.assert_rejected("invalid Draft 2020-12 schema")

    def test_stale_and_symlinked_test_paths(self) -> None:
        """Require actual regular test files even if Git still lists the old path."""
        subprocess.run(["git", "add", "plugins"], cwd=self.root, check=True)
        path = self.package / "tests/test_example.py"
        path.unlink()
        self.assert_rejected("missing or non-regular source file")
        path.symlink_to("../../outside.py")
        self.assert_rejected("symlinks are not allowed")

    def test_generated_ollama_payload(self) -> None:
        """The documented generated binary is optional, but its source is required."""
        destination = self.root / "plugins/ollama.cli"
        self.package.rename(destination)
        self.package = destination
        self.manifest["id"] = "ollama.cli"
        self.workflow["plugins"] = {"ollama.cli": "0.1.0"}
        self.write_workflow()
        self.manifest["entrypoint"]["command"] = "bin/ollama.cli"
        self.manifest["package"]["files"][0] = "bin/ollama.cli"
        (self.package / "cmd/plugin").mkdir(parents=True)
        (self.package / "go.mod").write_text("module ollama.cli\n\ngo 1.22\n")
        (self.package / "cmd/plugin/main.go").write_text("package main\n")
        self.write_manifest()
        self.assertEqual(check_repository(self.root), [])
        (self.package / "cmd/plugin/main.go").unlink()
        self.assert_rejected("generated payload source is missing")

    def test_ignored_material_is_not_discovered(self) -> None:
        """Ignored local packages do not become part of the public source inventory."""
        (self.root / ".gitignore").write_text("/plugins/private/\n")
        private = self.root / "plugins/private"
        private.mkdir()
        (private / "plugin.yaml").write_text("not a manifest\n")
        self.assertEqual(check_repository(self.root), [])

    def test_empty_repository_fails(self) -> None:
        """A source tree containing no public packages cannot silently pass."""
        (self.root / ".gitignore").write_text("/plugins/\n")
        self.assert_rejected("no public source packages found")


if __name__ == "__main__":
    unittest.main()
