"""Provenance JSON is not a schema; every required APP contract still fails closed."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

from jsonschema import SchemaError

from contract_registry import APP_CONTRACTS, app_registry

ROOT = Path(__file__).resolve().parent / "contracts"


class ContractRegistryTests(unittest.TestCase):
    """Exercise the registry used by both HTTP service suites."""

    def test_metadata_is_not_registered(self) -> None:
        """The finite schema set is unaffected by even malformed metadata JSON."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "contracts"
            shutil.copytree(ROOT, root)
            (root / "provenance-not-a-schema.json").write_text("{not a schema")
            registry = app_registry(root)
            self.assertEqual(
                set(registry),
                {"https://transist.dev/schemas/" + path for path in APP_CONTRACTS},
            )

    def test_required_schemas_fail_closed(self) -> None:
        """Missing/invalid JSON, identities, dialects and schema types are fatal."""
        for relative in APP_CONTRACTS:
            for mutation in ("missing", "json", "id", "dialect", "schema"):
                with self.subTest(path=relative, mutation=mutation):
                    with tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary) / "contracts"
                        shutil.copytree(ROOT, root)
                        path = root / relative
                        schema = json.loads(path.read_text())
                        if mutation == "missing":
                            path.unlink()
                        elif mutation == "json":
                            path.write_text("{")
                        else:
                            if mutation == "id":
                                schema.pop("$id")
                            elif mutation == "dialect":
                                schema["$schema"] = "https://example.invalid/schema"
                            else:
                                schema["type"] = 123
                            path.write_text(json.dumps(schema))
                        with self.assertRaises((OSError, ValueError, SchemaError)):
                            app_registry(root)
