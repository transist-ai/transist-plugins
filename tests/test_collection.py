"""Verify finite public package payloads and strict, portable test contracts."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

from support import contract_validator, validate_json_schema

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_plugin_structure import check_repository  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class CollectionTests(unittest.TestCase):
    """Check source packaging without execution, secrets, downloads or sibling trees."""

    def test_all_public_payloads(self) -> None:
        """Every selected schema/reference/entrypoint belongs to its finite package."""
        self.assertEqual(check_repository(ROOT), [])

    def test_complete_schema_validator(self) -> None:
        """Composition, references, patterns and upper bounds are enforced."""
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {"limited": {"type": "string", "pattern": "^ok$"}},
            "allOf": [{"$ref": "#/$defs/limited"}],
        }
        validate_json_schema(schema, "ok")
        with self.assertRaises(AssertionError):
            validate_json_schema(schema, "not-ok")
        with self.assertRaises(AssertionError):
            validate_json_schema({"type": "integer", "maximum": 1}, 2)

    def test_contracts_compile_offline(self) -> None:
        """All vendored contracts compile, including transitive error references."""
        for relative in (
            "app/handshake.response.v1.schema.json",
            "app/invoke.request.v1.schema.json",
            "app/invoke.progress.v1.schema.json",
            "app/invoke.result.v1.schema.json",
        ):
            validator = contract_validator(relative)
            self.assertTrue(list(validator.iter_errors({})))


if __name__ == "__main__":
    unittest.main()
