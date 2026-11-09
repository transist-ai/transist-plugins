"""Draft 2020-12 package and runtime APP contract tests only."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest

from jsonschema import Draft202012Validator
import yaml
import httpx

from test_tika import AUTH, ROOT, REPO, invoke, service

sys.path.insert(0, str(REPO / "tests"))
from contract_registry import app_registry  # noqa: E402


class SchemaTests(unittest.TestCase):
    """Compile action schemas and validate results against exact runtime contracts."""

    def test_action_schemas_compile_and_close(self) -> None:
        for path in sorted((ROOT / "schemas").glob("*.json")):
            schema = json.loads(path.read_text())
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema)
            if ".input." in path.name:
                value = invoke()["input"]
            elif "extract_text" in path.name:
                value = {"document_sha256": "0" * 64, "text": " text\n"}
            else:
                value = {"document_sha256": "0" * 64, "metadata": {"dc:title": ["T"]}}
            validator.validate(value)
            self.assertFalse(validator.is_valid({**value, "credentials": "private"}))
            self.assertFalse(validator.is_valid({}))

    def test_runtime_app_contracts(self) -> None:
        contracts = REPO / "tests" / "contracts"
        registry = app_registry(contracts)
        for filename, value in (
            ("invoke.request.v1.schema.json", invoke()),
            (
                "invoke.result.v1.schema.json",
                service.result("i", {"document_sha256": "0" * 64, "text": "x"}),
            ),
        ):
            schema = json.loads((contracts / "app" / filename).read_text())
            validator = Draft202012Validator(schema, registry=registry)
            validator.validate(value)
            if filename.startswith("invoke.result"):
                for code in service.ERRORS:
                    validator.validate(service.result("i", code=code))

        async def discovery() -> dict:
            """Read the actual public handshake through ASGI without credentials."""
            app = service.TikaApp(AUTH, "http://backend")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://app"
            ) as client:
                return (await client.get("/app/handshake")).json()

        schema = json.loads(
            (contracts / "app" / "handshake.response.v1.schema.json").read_text()
        )
        handshake = asyncio.run(discovery())
        Draft202012Validator(schema, registry=registry).validate(handshake)
        self.assertEqual(handshake["plugin"], {"id": "tika.http", "version": "0.1.0"})

    def test_package_payload_and_secret_binding(self) -> None:
        manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
        self.assertEqual(manifest["version"], service.IDENTITY["version"])
        self.assertEqual(manifest["default_provider_kind"], "external")
        self.assertEqual(
            manifest["entrypoint"]["base_url"], "https://tika.example.invalid"
        )
        selected = manifest["package"]["files"]
        self.assertEqual(len(selected), len(set(selected)))
        for filename in selected:
            path = ROOT / filename
            self.assertTrue(path.is_file())
            self.assertFalse(path.is_symlink())
            self.assertTrue(path.resolve().is_relative_to(ROOT))
        for action in manifest["actions"]:
            self.assertEqual(action["bindings"], {"input_projection": "action_data_v1"})
            self.assertIn(action["input_schema"], selected)
            self.assertIn(action["output_schema"], selected)
            self.assertEqual(
                action["secret_inputs"],
                [
                    {
                        "name": "TIKA_AUTHORIZATION",
                        "required": True,
                        "binding": {"type": "header", "name": "Authorization"},
                    }
                ],
            )
        for filename in ("plugin.yaml", *selected):
            self.assertNotIn(AUTH, (ROOT / filename).read_text())
        flow = yaml.safe_load((ROOT / "workflow.yaml").read_text())
        self.assertEqual(flow["plugins"], {manifest["id"]: manifest["version"]})
        self.assertEqual(set(flow["inputs"]), {"document_base64", "content_type"})
        self.assertTrue(all(item["required"] for item in flow["inputs"].values()))
        self.assertTrue(
            all(
                step["with"]["secret_refs"] == ["TIKA_AUTHORIZATION"]
                for step in flow["steps"]
            )
        )


if __name__ == "__main__":
    unittest.main()
