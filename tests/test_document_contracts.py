"""Focused file-contract security, byte fidelity and relocation regressions."""

from __future__ import annotations

from contextlib import contextmanager
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Iterator
import unittest
from unittest import mock

from jsonschema import Draft202012Validator, ValidationError
import yaml

from support import app_invoke_request, load_plugin_module, REPO_ROOT

PACKAGES = REPO_ROOT / "plugins"


@contextmanager
def fixture(package: str) -> Iterator[tuple]:
    """Create disjoint canonical workspace/staging with real package code."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        workspace, staging = root / "workspace", root / "staging"
        workspace.mkdir()
        staging.mkdir()
        module = load_plugin_module(PACKAGES / package)
        request = app_invoke_request("read_file", {}, str(staging))
        request["paths"]["workspace_dir"] = str(workspace)
        yield module, request, workspace, staging


class DocumentContractTests(unittest.TestCase):
    """Enforce source containment independently from data-path destinations."""

    def test_finite_payload_and_vendored_provenance(self) -> None:
        """Check selected new modules/schemas and actual vendored digests."""
        copies = []
        for package in ("shell", "openai.compatible"):
            directory = PACKAGES / package
            manifest = yaml.safe_load((directory / "plugin.yaml").read_text())
            self.assertEqual(manifest["version"], "0.1.0")
            self.assertIn("file_io.py", manifest["package"]["files"])
            for action in manifest["actions"]:
                for field in ("input_schema", "output_schema"):
                    self.assertIn(action[field], manifest["package"]["files"])
                    Draft202012Validator.check_schema(
                        json.loads((directory / action[field]).read_text())
                    )
            copies.append((directory / "file_io.py").read_bytes())
        self.assertEqual(*copies)
        contracts = REPO_ROOT / "tests/contracts"
        record = json.loads((contracts / "provenance-step-6.json").read_text())
        gate_record = json.loads((contracts / "provenance-step-10.json").read_text())
        actual = {
            path.relative_to(contracts)
            .as_posix(): hashlib.sha256(path.read_bytes())
            .hexdigest()
            for path in contracts.rglob("*.schema.json")
        }
        self.assertEqual({**record["sha256"], **gate_record["files"]}, actual)
        public_record = json.loads(
            (contracts / "provenance-public-v0.1.0.json").read_text()
        )
        self.assertEqual(public_record["sha256"], actual)
        self.assertEqual(public_record["source_tag"], "v0.1.0")
        self.assertEqual(
            public_record["source_commit"],
            "44a762cdd8ec2d967497dbd5be5534618da61ec6",
        )

    def test_shell_exact_bound_and_output(self) -> None:
        """Exact byte ceilings pass without newline translation or trimming."""
        with fixture("shell") as (plugin, request, workspace, staging):
            raw = " \r\ncafé ☃\n\n".encode()
            (workspace / "source.txt").write_bytes(raw)
            request["input"] = {
                "path": "source.txt",
                "max_bytes": len(raw),
                "output_file": str(staging / "out"),
            }
            result = plugin.read_file(request)
            self.assertEqual(result["status"], "success")
            self.assertEqual(
                result["output"], {"text": raw.decode(), "bytes": len(raw)}
            )
            self.assertEqual((staging / "out").read_bytes(), raw)
            (staging / "out").unlink()
            request["input"]["max_bytes"] -= 1
            self.assertEqual(
                plugin.read_file(request)["error"]["code"], "validation_error"
            )
            self.assertFalse((staging / "out").exists())

    def test_shell_rejects_escaping_and_special_sources(self) -> None:
        """Staged output cannot widen workspace source-read authority."""
        with fixture("shell") as (plugin, request, workspace, staging):
            (staging / "private").write_text("must not read")
            (workspace / "link").symlink_to(staging, target_is_directory=True)
            (workspace / "leaf").symlink_to(staging / "private")
            os.mkfifo(workspace / "fifo")
            (workspace / "invalid").write_bytes(b"\xff")
            for path in (
                str(staging / "private"),
                "../staging/private",
                "link/private",
                "leaf",
                "fifo",
                "invalid",
                "missing",
                ".",
                "repo://private",
                "run://private",
            ):
                with self.subTest(path=path):
                    request["input"] = {
                        "path": path,
                        "output_file": str(staging / "out"),
                    }
                    result = plugin.read_file(request)
                    self.assertEqual(result["status"], "failed")
                    self.assertEqual(result["error"]["code"], "validation_error")
                    self.assertFalse((staging / "out").exists())

    def test_shell_output_symlinks_cannot_overwrite_source(self) -> None:
        """Neither final nor ancestor output symlinks can redirect a write."""
        with fixture("shell") as (plugin, request, workspace, staging):
            source = workspace / "source"
            source.write_text("unchanged")
            (staging / "out").symlink_to(source)
            (staging / "parent").symlink_to(workspace, target_is_directory=True)
            for target in (staging / "out", staging / "parent/source"):
                request["input"] = {"path": "source", "output_file": str(target)}
                self.assertEqual(plugin.read_file(request)["status"], "failed")
                self.assertEqual(source.read_text(), "unchanged")
            for value in (True, 0, -1, 1048577, "10", None):
                request["input"] = {"path": "source", "max_bytes": value}
                self.assertEqual(plugin.read_file(request)["status"], "failed")

    def test_openai_exact_file_and_response_bytes(self) -> None:
        """JSON messages and response files preserve exact text bytes."""
        with fixture("openai.compatible") as (plugin, request, _, staging):
            raw = "  café\r\n☃\n\n".encode()
            (staging / "input").write_bytes(raw)
            request.update(
                action="chat_complete", policy={"network": {"mode": "loopback"}}
            )
            request["input"] = {
                "base_url": "http://127.0.0.1:1234/v1",
                "model": "fixture",
                "user_text_file": str(staging / "input"),
                "system_prompt": " rules ",
                "response_file": str(staging / "output"),
            }

            def provider(_endpoint: tuple, body: bytes, *_args: object) -> bytes:
                """Assert exact provider ingestion at the last transport boundary."""
                messages = json.loads(body)["messages"]
                self.assertEqual(
                    messages,
                    [
                        {"role": "system", "content": " rules "},
                        {"role": "user", "content": raw.decode()},
                    ],
                )
                return json.dumps(
                    {
                        "choices": [
                            {"message": {"role": "assistant", "content": raw.decode()}}
                        ]
                    }
                ).encode()

            with mock.patch.dict(
                os.environ, {plugin.SECRET_NAME: "fixture-secret"}
            ), mock.patch.object(
                plugin, "perform_request", side_effect=provider
            ) as transport:
                result = plugin.invoke(request)
            transport.assert_called_once()
            self.assertEqual(result["status"], "success")
            self.assertEqual((staging / "output").read_bytes(), raw)
            self.assertNotIn(str(staging), json.dumps(result))

    def test_openai_schema_and_invalid_inputs_stop_before_transport(self) -> None:
        """Source selection, UTF-8, regular files and budgets fail closed."""
        with fixture("openai.compatible") as (plugin, request, _, staging):
            source = staging / "source"
            source.write_text("é")
            schema = Draft202012Validator(
                json.loads(
                    (
                        PACKAGES
                        / "openai.compatible/schemas/chat_complete.input.schema.json"
                    ).read_text()
                )
            )
            base = {"base_url": "http://127.0.0.1:1234", "model": "fixture"}
            for fields in (
                {},
                {"messages": [], "user_text_file": str(source)},
                {
                    "messages": [{"role": "user", "content": "hello"}],
                    "system_prompt": "bad",
                },
            ):
                with self.assertRaises(ValidationError):
                    schema.validate(base | fields)
            request.update(
                action="chat_complete", policy={"network": {"mode": "loopback"}}
            )
            bad = [
                {},
                {"user_text_file": str(source), "max_prompt_bytes": 1},
                {
                    "user_text_file": str(source),
                    "system_prompt": "a",
                    "max_prompt_bytes": 2,
                },
                {"user_text_file": str(source), "messages": []},
                {"user_text_file": str(staging)},
                {"user_text_file": str(source), "max_prompt_bytes": True},
            ]
            invalid = staging / "invalid"
            invalid.write_bytes(b"\xff")
            bad.append({"user_text_file": str(invalid)})
            (staging / "link").symlink_to(source)
            bad.append({"user_text_file": str(staging / "link")})
            with mock.patch.object(plugin, "perform_request") as transport:
                for fields in bad:
                    request["input"] = base | fields
                    self.assertEqual(
                        plugin.invoke(request)["error"]["code"], "validation_error"
                    )
            transport.assert_not_called()
            source.write_bytes(b"\x01" * 65536)
            with self.assertRaisesRegex(
                plugin.PluginError, "request exceeds size limit"
            ):
                plugin.validate_input(base | {"user_text_file": str(source)}, request)

    def test_jira_relative_manifest_members_relocate_independently(self) -> None:
        """Two relocated manifests retain bytes without staging paths."""
        with fixture("jira.rest") as (plugin, request, workspace, staging):
            request["action"] = "download_attachments"
            issue = staging / "issue.json"
            # Duplicate filenames and a path-shaped provider id cannot escape output.
            issue.write_text(
                json.dumps(
                    {
                        "attachments": [
                            {
                                "id": "../../provider",
                                "filename": "same.txt",
                                "mime_type": "text/plain",
                                "content_url": "https://example.test/file",
                                "size": 4,
                            }
                        ]
                        * 2
                    }
                )
            )
            for index in range(2):
                tree = staging / ("tree" + str(index))
                request["input"] = {
                    "jira_issue_file": str(issue),
                    "output_dir": str(tree),
                    "manifest_file": str(tree / "custom.json"),
                    "max_bytes": 0 if index else plugin.DEFAULT_MAX_BYTES,
                }
                with mock.patch.dict(
                    os.environ, {"JIRA_AUTH_HEADER": "fixture"}
                ), mock.patch.object(
                    plugin.urllib.request,
                    "urlopen",
                    side_effect=lambda _r: io.BytesIO(b"a\x00\xffb"),
                ), mock.patch.object(
                    plugin, "fetch_to_file", wraps=plugin.fetch_to_file
                ) as download:
                    result = plugin.handle_request(request)
                self.assertEqual(result["status"], "success", result)
                self.assertEqual(download.call_count, 2)
                self.assertTrue(
                    all(
                        call.args[-1] == plugin.DEFAULT_MAX_BYTES
                        for call in download.call_args_list
                    )
                )
                moved = workspace / ("relocated" + str(index))
                shutil.move(tree, moved)
                manifest = json.loads((moved / "custom.json").read_text())
                self.assertNotIn(str(staging), json.dumps(manifest))
                self.assertEqual(len(manifest["downloaded"]), 2)
                for item in manifest["downloaded"]:
                    self.assertEqual(
                        (moved / item["local_path"]).read_bytes(), b"a\x00\xffb"
                    )
                    self.assertNotIn("..", Path(item["local_path"]).parts)

    def test_git_and_github_bounded_staged_inputs(self) -> None:
        """Accept staged data while bounded readers reject symlinks/FIFOs."""
        for package in ("git.cli", "github.cli"):
            with self.subTest(package=package), fixture(package) as (
                plugin,
                request,
                workspace,
                staging,
            ):
                path = staging / "input"
                path.write_text('{"pull_request": {"baseRefName": "main"}}')
                self.assertEqual(
                    plugin.command_cwd(request, {"repo_path": str(staging)}),
                    str(workspace),
                )
                self.assertEqual(
                    plugin.read_json_file(request, str(path))["pull_request"][
                        "baseRefName"
                    ],
                    "main",
                )
                (staging / "link").symlink_to(path)
                with self.assertRaises(ValueError):
                    plugin.read_json_file(request, str(staging / "link"))
                os.mkfifo(staging / "fifo")
                with self.assertRaises(ValueError):
                    plugin.read_json_file(request, str(staging / "fifo"))

    def test_model_context_readers_reject_special_files(self) -> None:
        """All model context readers fail without blocking on a FIFO or symlink."""
        for package in (
            "codex.cli",
            "cursor.cli",
            "github.copilot.cli",
            "opencode.cli",
            "openspec",
        ):
            with self.subTest(package=package), fixture(package) as (
                plugin,
                _,
                _,
                staging,
            ):
                path = staging / "fifo"
                os.mkfifo(path)
                link = staging / "link"
                link.symlink_to(path)
                for source in (path, link):
                    with self.assertRaises(ValueError):
                        if package == "openspec":
                            plugin.bounded_text(str(source), 16, strict=True)
                        else:
                            plugin.lean_policy.read_bounded(
                                source, "context", {"max_context_file_bytes": 16}
                            )


if __name__ == "__main__":
    unittest.main()
