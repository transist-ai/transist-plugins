"""Optional gate review metadata across the finite selected action contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import yaml

from gate_contract_fixture import PACKAGES, decision, malformed_decisions
from support import contract_validator, load_plugin_module, package_schema_validator

ROOT = Path(__file__).resolve().parents[1]


def input_path(package: str) -> Path:
    """Locate the existing action schema, including Cursor's shared two actions."""
    action = {"shell": "exec", "openspec": "generate_plan"}.get(package, "exec_task")
    return ROOT / "plugins" / package / f"schemas/{action}.input.schema.json"


class GateDecisionContractTests(unittest.TestCase):
    """Compare actual upstream definitions, packaged schemas and feedback readers."""

    def test_canonical_bundle_and_selected_payload(self) -> None:
        """Every independent install ships the exact transitive canonical shapes."""
        contracts = ROOT / "tests/contracts"
        provenance = json.loads((contracts / "provenance-step-10.json").read_text())
        for path, digest in provenance["files"].items():
            self.assertEqual(
                hashlib.sha256((contracts / path).read_bytes()).hexdigest(), digest
            )
        gate = json.loads(
            (contracts / "runtime/gate-review.v1.schema.json").read_text()
        )
        documents = json.loads(
            (contracts / "runtime/managed-documents.v1.schema.json").read_text()
        )
        expected = {
            **gate["$defs"],
            **{key: documents["$defs"][key] for key in ("revision", "blob", "digest")},
        }
        expected["request"]["properties"]["review_documents"]["items"][
            "$ref"
        ] = "#/$defs/revision"
        helper_bytes = (ROOT / "plugins/codex.cli/gate_review.py").read_bytes()
        bundle_bytes = (
            ROOT / "plugins/codex.cli/schemas/gate-decision.schema.json"
        ).read_bytes()
        for package in PACKAGES:
            with self.subTest(package=package):
                directory = ROOT / "plugins" / package
                bundle = directory / "schemas/gate-decision.schema.json"
                self.assertEqual(bundle.read_bytes(), bundle_bytes)
                contents = json.loads(bundle.read_text())
                self.assertNotIn("$id", contents)
                self.assertEqual(contents["$defs"], expected)
                manifest = yaml.safe_load((directory / "plugin.yaml").read_text())
                self.assertEqual(manifest["id"], package)
                self.assertIn(
                    "schemas/gate-decision.schema.json", manifest["package"]["files"]
                )
                if package != "shell":
                    self.assertIn("gate_review.py", manifest["package"]["files"])
                    self.assertEqual(
                        (directory / "gate_review.py").read_bytes(), helper_bytes
                    )

    def test_optional_metadata_legacy_timestamp_and_outcomes(self) -> None:
        """All six schemas and five readers preserve legacy decisions and feedback."""
        for package in PACKAGES:
            path = input_path(package)
            schema = json.loads(path.read_text())["properties"]["gate_decision"]
            validator = package_schema_validator(path, schema)
            module = load_plugin_module(path.parents[1])
            for metadata in (False, True):
                for outcome in ("approved", "rejected", "request_changes"):
                    with self.subTest(
                        package=package, metadata=metadata, outcome=outcome
                    ):
                        value = decision(metadata, outcome)
                        validator.validate(value)
                        contract_validator(
                            "runtime/gate-review.v1.schema.json"
                        ).validate(value)
                        if package != "shell":
                            self.assertTrue(module.valid_gate_decision(value))
                            feedback = module.gate_decision_feedback(
                                {"gate_decision": value}
                            )
                            if outcome == "request_changes":
                                text = (
                                    " ".join(feedback)
                                    if isinstance(feedback, tuple)
                                    else feedback
                                )
                                for part in (
                                    value["reason"],
                                    value["actor"],
                                    value["actor_source"],
                                ):
                                    self.assertIn(part, text)
                                self.assertNotIn("a" * 64, text)
                            else:
                                self.assertIn(feedback, ("", ("", "", "")))
                        del value["decided_at_ms"]
                        contract_validator(
                            "runtime/gate-review.v1.schema.json"
                        ).validate(value)
                        self.assertFalse(validator.is_valid(value))
                        if package != "shell":
                            self.assertFalse(module.valid_gate_decision(value))
            # The binding remains optional on unrelated invocations.
            base = {
                "shell": {"command": "true"},
                "openspec": {"change_name": "fixture"},
            }.get(package, {"prompt": "hello"})
            package_schema_validator(path).validate(base)

    def test_nested_metadata_failures_match_canonical(self) -> None:
        """No nested missing/unknown fields, bool/integer confusion or bad bounds."""
        canonical = contract_validator("runtime/gate-review.v1.schema.json")
        for package in PACKAGES:
            path = input_path(package)
            validator = package_schema_validator(
                path, json.loads(path.read_text())["properties"]["gate_decision"]
            )
            # Each reader must load its own selected helper, not cached siblings.
            with mock.patch.dict(sys.modules):
                sys.modules.pop("gate_review", None)
                module = load_plugin_module(path.parents[1])
                if package != "shell":
                    self.assertEqual(
                        Path(module.valid_review_request.__globals__["__file__"]),
                        path.parents[1] / "gate_review.py",
                    )
                for label, value in malformed_decisions():
                    with self.subTest(package=package, case=label):
                        self.assertFalse(canonical.is_valid(value))
                        self.assertFalse(validator.is_valid(value))
                        if package != "shell":
                            self.assertFalse(module.valid_gate_decision(value))
                            with self.assertRaisesRegex(ValueError, "gate_decision"):
                                module.gate_decision_feedback({"gate_decision": value})

    def test_produced_file_tree_and_initial_revision_bounds(self) -> None:
        """Gate execution is positive; document initialization alone permits zero."""
        for package in PACKAGES:
            path = input_path(package)
            validator = package_schema_validator(
                path, json.loads(path.read_text())["properties"]["gate_decision"]
            )
            module = load_plugin_module(path.parents[1])
            for kind in ("file", "tree"):
                value = decision()
                value["review_request"]["execution"] = 4294967295
                revision = value["review_request"]["review_documents"][0]
                revision.update(
                    execution=4294967295,
                    commit_sequence=2**64 - 1,
                    step_id="producer",
                    kind=kind,
                    size_bytes=2**64 - 1,
                )
                revision["blob"]["size_bytes"] = 2**64 - 1
                revision["file_count"] = 1 if kind == "file" else 4294967295
                validator.validate(value)
                if package != "shell":
                    self.assertTrue(module.valid_gate_decision(value))
                # Manifest bytes live behind blob, never inline in the revision.
                revision["manifest"] = {"files": {}, "directories": []}
                self.assertFalse(validator.is_valid(value))
                if package != "shell":
                    self.assertFalse(module.valid_gate_decision(value))

    def test_shell_exact_json_forwarding_and_stale_removal(self) -> None:
        """Shell preserves every metadata field and never inherits stale decisions."""
        module = load_plugin_module(ROOT / "plugins/shell")
        for metadata in (False, True):
            value = decision(metadata)
            with mock.patch.dict(
                "os.environ", {"TRANSIST_GATE_DECISION_JSON": "stale"}
            ):
                env = module.command_environment({"gate_decision": value})
                self.assertEqual(
                    env["TRANSIST_GATE_DECISION_JSON"],
                    json.dumps(value, separators=(",", ":"), sort_keys=True),
                )
                self.assertEqual(json.loads(env["TRANSIST_GATE_DECISION_JSON"]), value)
                self.assertNotIn(
                    "TRANSIST_GATE_DECISION_JSON", module.command_environment({})
                )

    def test_offline_subschema_resolution_never_retrieves(self) -> None:
        """An extracted ref resolves locally; absent/remote refs fail offline."""
        path = input_path("codex.cli")
        for ref in ("missing.schema.json", "https://example.invalid/no-schema.json"):
            validator = package_schema_validator(path, {"$ref": ref})
            with self.assertRaises(Exception):
                validator.validate(decision())


if __name__ == "__main__":
    unittest.main()
