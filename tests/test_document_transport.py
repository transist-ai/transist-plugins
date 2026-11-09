"""The installed document fixture models bounded REST, never generic gh success."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import ModuleType
import unittest
from unittest import mock

from e2e_document_actions import WRAPPER


class DocumentTransportTests(unittest.TestCase):
    """Check fixture paging/acknowledgments independently of production handlers."""

    def setUp(self) -> None:
        """Load only the fixture transport with an isolated fake plugin module."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        plugin = ModuleType("plugin")
        plugin.PLUGIN_ID = "github.cli"
        plugin.command_cwd = lambda *_args: str(self.root)
        plugin.main = lambda: 0
        self.namespace = {"WORKSPACE": str(self.root)}
        with mock.patch.dict("sys.modules", {"plugin": plugin}):
            with self.assertRaises(SystemExit) as stopped:
                exec(WRAPPER, self.namespace)
        self.assertEqual(stopped.exception.code, 0)

    def call(self, command: list[str]) -> object:
        """Execute the fixture-only transport and decode its actual JSON response."""
        result = self.namespace["fake"]({"input": {}}, command)
        return json.loads(result.stdout)

    def get(self, surface: str, page: int) -> list[str]:
        """Build the exact current reconciliation command, including page bounds."""
        return [
            "gh",
            "api",
            "--method",
            "GET",
            f"repos/fixture/repo/{surface}/1/comments",
            "-f",
            "per_page=100",
            "-f",
            f"page={page}",
        ]

    def test_both_surfaces_page_real_remote_observations(self) -> None:
        """Return at most 100 real comments per page and exact posted marker/target."""
        marker = "<!-- transist-review-publication:" + "a" * 64 + " -->"
        body = "<!-- transist-review-reply -->\nexact reply\n" + marker
        for surface in ("issues", "pulls"):
            self.assertEqual(self.call(self.get(surface, 1)), [])
            command = [
                "gh",
                "api",
                f"repos/fixture/repo/{surface}/1/comments",
                "-X",
                "POST",
                "-f",
                "body=" + body,
            ]
            if surface == "pulls":
                command += ["-F", "in_reply_to=42"]
            posted = self.call(command)
            self.assertEqual(posted["body"], body)
            self.assertEqual(self.call(self.get(surface, 1)), [posted])
            if surface == "pulls":
                self.assertEqual(posted["in_reply_to_id"], 42)
        path = self.root / "fixture-gh-remote.json"
        remote = json.loads(path.read_text())
        for surface in ("issues", "pulls"):
            remote[surface] *= 101
        path.write_text(json.dumps(remote))
        for surface in ("issues", "pulls"):
            self.assertEqual(len(self.call(self.get(surface, 1))), 100)
            self.assertEqual(len(self.call(self.get(surface, 2))), 1)
            self.assertEqual(self.call(self.get(surface, 3)), [])

    def test_unknown_calls_and_missing_marker_are_not_success(self) -> None:
        """Bad endpoints/pages and generic reply acknowledgments cannot false-pass."""
        for command in (
            ["gh", "unknown"],
            self.get("issues", 0),
            self.get("pulls", 101),
            self.get("unknown", 1),
            [
                "gh",
                "api",
                "repos/fixture/repo/issues/1/comments",
                "-X",
                "POST",
                "-f",
                "body=no marker",
            ],
        ):
            with self.subTest(command=command), self.assertRaises(AssertionError):
                self.call(command)
        self.assertFalse((self.root / "fixture-gh-remote.json").exists())
