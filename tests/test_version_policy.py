"""Check public runtime identities without invoking actions or contacting providers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import yaml

from support import contract_validator, resolved_runtime_temp_environment

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from check_public_snapshot import candidate_files  # noqa: E402


class VersionPolicyTests(unittest.TestCase):
    """Exercise actual Python APP/MCP discovery, separate from protocol versions."""

    def test_process_handshakes_match_catalog_release(self) -> None:
        """Every Python process package reports 0.1.0 over unchanged APP v1."""
        manifests = [
            ROOT / relative
            for relative in candidate_files(ROOT)
            if relative.parts[0] == "plugins" and relative.name == "plugin.yaml"
        ]
        checked = []
        for path in manifests:
            manifest = yaml.safe_load(path.read_text())
            if manifest["adapter"] != "process":
                continue
            package = path.parent
            command = package / manifest["entrypoint"]["command"]
            if command.suffix != ".py":
                # Ollama's generated Go payload has its own native version test.
                self.assertEqual(manifest["id"], "ollama.cli")
                continue
            with self.subTest(package=package.name):
                self.assertEqual(manifest["version"], "0.1.0")
                with resolved_runtime_temp_environment(package) as environment:
                    result = subprocess.run(
                        [sys.executable, "-B", str(command)],
                        input=json.dumps(
                            {
                                "type": "app.handshake.request.v1",
                                "protocol_version": "app.v1",
                            }
                        )
                        + "\n",
                        text=True,
                        capture_output=True,
                        cwd=package,
                        env={**os.environ, **environment},
                        timeout=10,
                    )
                # Some APP mains require an invocation after discovery (EOF = 1).
                self.assertIn(result.returncode, (0, 1))
                self.assertEqual(result.stderr, "")
                frames = [json.loads(line) for line in result.stdout.splitlines()]
                self.assertEqual(len(frames), 1)
                contract_validator("app/handshake.response.v1.schema.json").validate(
                    frames[0]
                )
                self.assertEqual(
                    frames[0]["plugin"],
                    {"id": manifest["id"], "version": "0.1.0"},
                )
                checked.append(package.name)
        self.assertIn("shell", checked)
        self.assertIn("http", checked)

    def test_mcp_discovery_matches_catalog_release(self) -> None:
        """Git MCP advertises the package release without resetting MCP's protocol."""
        package = ROOT / "plugins/git.mcp"
        manifest = yaml.safe_load((package / "plugin.yaml").read_text())
        result = subprocess.run(
            [sys.executable, "-B", str(package / "server.py")],
            input=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "version-test", "version": "1"},
                    },
                }
            )
            + "\n",
            text=True,
            capture_output=True,
            cwd=package,
            timeout=10,
            check=True,
        )
        self.assertEqual(result.stderr, "")
        response = json.loads(result.stdout)["result"]
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertEqual(
            response["serverInfo"], {"name": manifest["id"], "version": "0.1.0"}
        )
        self.assertEqual(response["protocolVersion"], "2025-06-18")


if __name__ == "__main__":
    unittest.main()
