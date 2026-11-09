"""Behavioral scanner regressions moved with scan_tst_comments.py into git.cli."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
import support  # noqa: E402
from test_compact import validator  # noqa: E402


class InstructionComments(unittest.TestCase):
    """Use disposable Git indices and bounded synthetic source, never the real repo."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.plugin = support.load_plugin_module(ROOT)
        self.scanner = self.plugin.instruction_comments
        self.git("init", "-q")

    def git(self, *args: str) -> None:
        """Execute fixture-only Git with no inherited system/global configuration."""
        subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            check=True,
            env={
                "PATH": os.environ.get("PATH", os.defpath),
                "HOME": str(self.root),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            },
        )

    def file(self, name: str, content: str) -> Path:
        """Create and force-track fictional source, including ignored-boundary cases."""
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.git("add", "-f", "--", name)
        return path

    def invoke(self, output: Path, mode: str = "legacy") -> dict:
        """Invoke actual APP handling and validate native decision/output schemas."""
        request = support.app_invoke_request(
            "scan_instruction_comments",
            {"output_file": str(output), "output_mode": mode},
        )
        request["paths"]["workspace_dir"] = str(self.root)
        result = self.plugin.handle_request(request)
        support.contract_validator("app/invoke.result.v1.schema.json").validate(result)
        if result["output"]:
            validator("scan_instruction_comments").validate(result["output"])
        return result

    def test_leading_delimiters_only_and_no_quoted_or_trailing_markers(self) -> None:
        path = self.file(
            "source.txt",
            "# tst: hash\n // tst: slash\n-- tst: sql\n/* tst: block */\n"
            '<!-- tst: html -->\nvalue = "# tst: quoted"\n'
            '"// tst: quoted"\nx = 1 # tst: trailing\n* tst: ambiguous\n'
            "/* tst: comment */ code()\n<!-- tst: comment --> code()\n",
        )
        hits = self.scanner.scan_file(self.root, path)
        self.assertEqual(
            [h[2] for h in hits], ["hash", "slash", "sql", "block", "html"]
        )
        self.assertEqual([h[1] for h in hits], [1, 2, 3, 4, 5])

    def test_python_and_common_multiline_literals_remain_data(self) -> None:
        for name, text in (
            ("source.py", 'text = """\n# tst: quoted\n"""\n# tst: actual\n'),
            ("source.js", "const text = `\n// tst: quoted\n`;\n// tst: actual\n"),
            ("source.ex", 'text = """\n# tst: quoted\n"""\n# tst: actual\n'),
            ("source.sql", "SELECT '\n-- tst: quoted\n';\n-- tst: actual\n"),
        ):
            with self.subTest(name=name):
                path = self.file(name, text)
                self.assertEqual(
                    self.scanner.scan_file(self.root, path), [(name, 4, "actual")]
                )

    def test_tracked_operational_files_and_destination_are_excluded(self) -> None:
        for name in (
            ".transist/context.md",
            "nested/run.interaction.json",
            "nested/run.interaction.md",
        ):
            self.file(name, "# tst: stale\n")
        self.file("src/real.txt", "# tst: located\n")
        output = self.file("context.md", "# tst: self\n")
        result = self.invoke(output, "compact")
        self.assertEqual(result["status"], "success")
        self.assertIs(result["output"]["has_comments"], True)
        self.assertIn("src/real.txt:1", output.read_text())
        self.assertNotIn("stale", output.read_text())
        self.assertNotIn("self", output.read_text())

    def test_comment_file_line_and_total_budgets_preserve_prior_output(self) -> None:
        path = self.file("source.txt", "# tst: " + "é" * 2048 + "\n")
        self.assertEqual(len(self.scanner.scan_file(self.root, path)), 1)
        output = self.root / "result.md"
        for text in (
            "# tst: " + "é" * 2049 + "\n",
            "x" * (self.scanner.MAX_LINE_BYTES + 1),
            ("# tst: " + "x" * 4096 + "\n") * 8,
            "x" * (self.scanner.MAX_FILE_BYTES + 1),
        ):
            path.write_text(text)
            output.write_text("previous complete context")
            self.assertEqual(self.invoke(output)["status"], "failed")
            self.assertEqual(output.read_text(), "previous complete context")

    def test_combined_output_limit_is_enforced(self) -> None:
        for n in range(8):
            self.file(f"source{n}.txt", "# tst: " + "x" * 4096 + "\n")
        output = self.root / "result.md"
        self.assertEqual(self.invoke(output)["status"], "failed")
        self.assertFalse(output.exists())

    def test_missing_unreadable_symlink_and_outside_source_fail(self) -> None:
        path = self.file("source.txt", "# tst: actual\n")
        with patch.object(self.scanner, "open_source", side_effect=PermissionError):
            self.assertEqual(self.invoke(self.root / "result.md")["status"], "failed")
        with self.assertRaises(ValueError):
            self.scanner.scan_file(self.root, self.root.parent / "outside.txt")
        path.unlink()
        with patch.object(self.scanner, "tracked_files", return_value=[path]):
            self.assertEqual(self.invoke(self.root / "result.md")["status"], "failed")
        path.symlink_to(self.root / "absent.txt")
        self.assertEqual(self.invoke(self.root / "result.md")["status"], "failed")

    def test_symlink_parents_and_special_outputs_fail(self) -> None:
        source = self.file("src/source.txt", "# tst: actual\n")
        target = self.root / "renamed"
        source.parent.rename(target)
        source.parent.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.invoke(self.root / "result.md")["status"], "failed")
        with self.assertRaises(OSError):
            self.scanner.write_output(self.root / "src/result.md", [])
        output = self.root / "fifo"
        os.mkfifo(output)
        with self.assertRaises((OSError, ValueError)):
            self.scanner.write_output(output, [])

    def test_binary_excluded_and_git_failure_fatal(self) -> None:
        path = self.file("binary.bin", "\0# tst: not source\n")
        self.assertEqual(self.scanner.scan_file(self.root, path), [])
        with patch.object(
            self.scanner.subprocess,
            "Popen",
            side_effect=subprocess.CalledProcessError(1, "git"),
        ):
            self.assertEqual(self.invoke(self.root / "result.md")["status"], "failed")

    def test_raw_netpbm_without_nul_is_excluded_by_content(self) -> None:
        for kind, fields in (
            (b"P4", b"8 2"),
            (b"P5", b"2 1\n255"),
            (b"P6", b"2 1\n65535"),
        ):
            with self.subTest(kind=kind):
                path = self.file("asset.txt", "")
                raw = kind + b"\n# tst: false request\n" + fields + b"\n" + b"\xff" * 12
                self.assertNotIn(b"\0", raw)
                path.write_bytes(raw)
                self.assertEqual(self.scanner.scan_file(self.root, path), [])
        output = self.root / "result.md"
        self.file("real.txt", "# tst: actual request\n")
        result = self.invoke(output, "compact")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"]["count"], 1)
        self.assertNotIn("false request", output.read_text())

    def test_binary_header_excludes_even_utf8_compatible_false_markers(self) -> None:
        path = self.file("asset.py", "P4\n# tst: false request\n8 1\nA")
        self.assertEqual(self.scanner.scan_file(self.root, path), [])

    def test_common_binary_signatures_exclude_leading_false_markers(self) -> None:
        path = self.file("asset.txt", "")
        for magic in (
            b"\x89PNG\r\n\x1a\n",
            b"\xff\xd8\xff",
            b"GIF87a",
            b"GIF89a",
            b"II*\0",
            b"MM\0*",
            b"%PDF-1.7",
            b"PK\x03\x04",
            b"PK\x05\x06",
            b"\x1f\x8b\x08",
            b"BZh9",
            b"\xfd7zXZ\0",
            b"\x28\xb5\x2f\xfd",
            b"\x7fELF",
            b"wOFF",
            b"wOF2",
        ):
            with self.subTest(magic=magic):
                path.write_bytes(magic + b"\n# tst: false request\n\xff")
                self.assertEqual(self.scanner.scan_file(self.root, path), [])

    def test_plain_netpbm_and_incomplete_probe_remain_strict_text(self) -> None:
        path = self.file("asset", "")
        for kind in ("P1", "P2", "P3", "P4 source"):
            path.write_text(kind + "\n# tst: real request\n", encoding="utf-8")
            self.assertEqual(
                self.scanner.scan_file(self.root, path), [("asset", 2, "real request")]
            )
        path.write_bytes(b"P4\n#" + b"x" * 8192 + b"\n8 1\n\xff")
        with self.assertRaises(self.scanner.SourceScanError) as raised:
            self.scanner.scan_file(self.root, path)
        self.assertEqual(raised.exception.reason, "invalid_utf8")

    def test_netpbm_lookalikes_and_asset_suffix_do_not_hide_bad_text(self) -> None:
        path = self.file("source.pbm", "")
        for raw in (
            b"P4 source\n",
            b"P40\n8 1\n",
            b"P4\n0 1\n",
            b"P5\n1 1\n65536\n",
            b"P6\n1 1\n",
            b"ordinary text\n",
        ):
            with self.subTest(raw=raw):
                path.write_bytes(raw + b"\xff")
                with self.assertRaises(self.scanner.SourceScanError) as raised:
                    self.scanner.scan_file(self.root, path)
                self.assertEqual(raised.exception.reason, "invalid_utf8")

    def test_malformed_utf8_is_fatal_after_hits_without_publication(self) -> None:
        self.file("a.txt", "# tst: earlier request\n")
        output = self.root / "result.md"
        for name in ("nested/source.txt", "nested/source.py"):
            path = self.file(name, "")
            for prefix in (b"", b"# tst: do not publish\n" + b"# padding\n" * 1000):
                path.write_bytes(prefix + b"# synthetic-secret\xff\n")
                for mode in ("legacy", "compact"):
                    with self.subTest(name=name, late=bool(prefix), mode=mode):
                        output.write_text("previous complete context")
                        with patch.object(self.scanner, "write_output") as publish:
                            result = self.invoke(output, mode)
                        publish.assert_not_called()
                        self.assertEqual(result["status"], "failed")
                        self.assertEqual(result["error"]["code"], "validation_error")
                        message = result["error"]["message"]
                        self.assertIn(name, message)
                        self.assertIn("UTF-8", message)
                        self.assertNotIn("synthetic-secret", str(result))
                        self.assertNotIn(str(self.root), str(result))
                        self.assertEqual(
                            output.read_text(), "previous complete context"
                        )
            path.unlink()

    def test_source_diagnostics_are_controlled_and_bounded(self) -> None:
        self.file("nested/source.txt", "# tst: actual\n")
        with patch.object(
            self.scanner,
            "open_source",
            side_effect=PermissionError("synthetic-secret /private/host/source"),
        ):
            result = self.invoke(self.root / "result.md")
        self.assertIn("nested/source.txt", result["error"]["message"])
        self.assertIn("read", result["error"]["message"])
        self.assertNotIn("synthetic-secret", str(result))
        self.assertNotIn("/private/host", str(result))
        for name in (
            "bad\nname.txt",
            "`markup`.txt",
            "bad\x7fname.txt",
            "bad\u202ename.txt",
            "a/" * 150 + "source.txt",
        ):
            error = self.scanner.SourceScanError(Path(name), "invalid_utf8")
            self.assertLessEqual(len(str(error).encode("utf-8")), 512)
            self.assertNotIn(name, str(error))
            self.assertNotIn("\n", str(error))
        for outside in (self.root.parent / "outside.txt", self.root / "../outside.txt"):
            with self.assertRaises(ValueError):
                self.scanner.scan_file(self.root, outside)

    def test_python_bad_encoding_and_tokenization_fail_safely(self) -> None:
        path = self.file("source.py", "")
        for raw, reason in (
            (b"# coding: latin-1\n# synthetic-secret\xff\n", "invalid_utf8"),
            (b"# coding: synthetic-secret\n", "tokenization"),
            (b"text = '''synthetic-secret\n", "tokenization"),
        ):
            with self.subTest(reason=reason):
                path.write_bytes(raw)
                with self.assertRaises(self.scanner.SourceScanError) as raised:
                    self.scanner.scan_file(self.root, path)
                self.assertEqual(raised.exception.reason, reason)
                result = self.invoke(self.root / "result.md")
                self.assertEqual(result["status"], "failed")
                self.assertIn("source.py", result["error"]["message"])
                self.assertNotIn("synthetic-secret", str(result))

    def test_unexpected_scanner_failure_keeps_generic_safe_fallback(self) -> None:
        with patch.object(
            self.scanner,
            "scan",
            side_effect=RuntimeError("synthetic-secret /private/host/source"),
        ):
            result = self.invoke(self.root / "result.md")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["error"]["message"],
            "Reviewer scan failed; no partial context published",
        )

    def test_binary_probe_remains_bounded_and_charges_budgets(self) -> None:
        path = self.file("asset", "")
        path.write_bytes(b"P4\n8 1\n" + b"\xff" * 20000)
        with path.open("rb") as handle, patch.object(
            self.scanner, "open_source", return_value=handle
        ), patch.object(handle, "read", wraps=handle.read) as read, patch.object(
            handle, "readline", wraps=handle.readline
        ) as readline:
            self.assertEqual(self.scanner.scan_file(self.root, path), [])
            read.assert_called_once_with(8192)
            readline.assert_not_called()
        for limits in (
            {"MAX_FILE_BYTES": 10},
            {"MAX_SOURCE_BYTES": 10},
            {"MAX_FILES": 0},
            {"MAX_SCAN_SECONDS": 0},
        ):
            with self.subTest(limits=limits), patch.multiple(self.scanner, **limits):
                with self.assertRaises(ValueError):
                    self.scanner.scan_file(self.root, path)

    def test_git_confirmed_deletion_excluded_but_replacement_symlink_fails(
        self,
    ) -> None:
        path = self.file("deleted.txt", "# tst: obsolete\n")
        path.unlink()
        self.assertEqual(self.scanner.tracked_files(self.root), [])
        result = self.invoke(self.root / "result.md")
        self.assertEqual(result["status"], "success")
        self.assertIs(result["output"]["has_comments"], False)
        path.symlink_to(self.root / "absent.txt")
        with patch.object(self.scanner, "git_names", return_value={"deleted.txt"}):
            result = self.invoke(self.root / "result.md")
            self.assertEqual(result["status"], "failed")
            self.assertIn("deleted.txt", result["error"]["message"])

    def test_git_deleted_path_beneath_symlink_rejected(self) -> None:
        source = self.file("src/source.txt", "# tst: actual\n")
        source.unlink()
        source.parent.rmdir()
        source.parent.symlink_to(
            self.root / "absent-directory", target_is_directory=True
        )
        self.assertEqual(self.invoke(self.root / "result.md")["status"], "failed")

    def test_aggregate_file_source_and_time_limits_never_report_false_clean(
        self,
    ) -> None:
        first = self.file("first.txt", "plain source\n")
        second = self.file("second.txt", "plain source\n")
        output = self.root / "result.md"
        for limits in (
            {"MAX_SOURCE_BYTES": 20},
            {"MAX_FILES": 1},
            {"MAX_SCAN_SECONDS": 0},
        ):
            with patch.multiple(self.scanner, **limits), patch.object(
                self.scanner, "tracked_files", return_value=[first, second]
            ):
                output.write_text("previous complete context")
                self.assertEqual(self.invoke(output)["status"], "failed")
                self.assertEqual(output.read_text(), "previous complete context")

    def test_git_filename_count_bytes_and_deadline_limits(self) -> None:
        self.file("first.txt", "plain source\n")
        self.file("second.txt", "plain source\n")
        for limits in ({"MAX_GIT_OUTPUT_BYTES": 4}, {"MAX_FILES": 1}):
            with patch.multiple(self.scanner, **limits):
                self.assertEqual(
                    self.invoke(self.root / "result.md")["status"], "failed"
                )
        original = self.scanner.subprocess.Popen

        def sleeping_git(command: list[str], **kwargs: object) -> subprocess.Popen:
            """Replace only the fixture Git process with a bounded test sleeper."""
            return original(
                [sys.executable, "-c", "import time; time.sleep(1)"], **kwargs
            )

        with patch.object(
            self.scanner.subprocess, "Popen", side_effect=sleeping_git
        ), patch.object(self.scanner, "GIT_TIMEOUT_SECONDS", 0.01):
            self.assertEqual(self.invoke(self.root / "result.md")["status"], "failed")

    def test_growth_during_binary_probe_charged_before_exclusion(self) -> None:
        path = self.file("growing.bin", "\0" + "x" * 11)
        before_growth = SimpleNamespace(st_size=0, st_mode=0o100600)
        with patch.object(
            self.scanner.os, "fstat", return_value=before_growth
        ), patch.object(self.scanner, "MAX_SOURCE_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "aggregate"):
                self.scanner.scan_file(self.root, path)
        path.write_bytes(b"P4\n8 1\n" + b"\xff" * 12)
        with patch.object(
            self.scanner.os, "fstat", return_value=before_growth
        ), patch.object(self.scanner, "MAX_FILE_BYTES", 10):
            with self.assertRaises(self.scanner.SourceScanError) as raised:
                self.scanner.scan_file(self.root, path)
            self.assertEqual(raised.exception.reason, "file_budget")

    def test_external_staged_output_does_not_select_alternate_workspace(self) -> None:
        self.file("real.txt", "# tst: selected workspace\n")
        with tempfile.TemporaryDirectory() as staging:
            output = Path(staging).resolve() / "context.md"
            result = self.invoke(output)
            self.assertEqual(result["status"], "success")
            self.assertIn("selected workspace", output.read_text())
            self.assertEqual(result["output"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
