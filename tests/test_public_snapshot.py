"""Publication regressions, including fresh-Git inventory, modes and redaction."""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
from check_public_snapshot import (  # noqa: E402
    approved_files,
    candidate_files,
    check_file,
)  # noqa: E402
from export_public_snapshot import export_snapshot  # noqa: E402


class PublicSnapshotGateTests(unittest.TestCase):
    """Use disposable source/destination repositories without credentials or history."""

    def test_redacted_privacy_and_link_errors(self) -> None:
        """Sensitive content and link targets never appear in diagnostics."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = pathlib.Path(directory)
            (root / "README.md").write_text(
                "[private](/Users/" + "private-person" + "/secret)\nHB-" + "123\n"
            )
            errors = check_file(root, pathlib.Path("README.md"))
            self.assertEqual(len(errors), 3)
            self.assertNotIn("private-person", " ".join(errors))
            self.assertNotIn("secret)", " ".join(errors))

    def test_binary_and_private_paths(self) -> None:
        """Reject local trees, credentials, generated paths and compiled bytes."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = pathlib.Path(directory)
            for path in (
                "priv/x",
                "private/x",
                "aws.bedrock/x",
                "plugins/aws.bedrock/x",
                "artifact.sig",
                ".env",
                ".venv/x",
                "build/x",
                "credentials",
            ):
                self.assertIn(
                    "private or generated", check_file(root, pathlib.Path(path))[0]
                )
            (root / "compiled").write_bytes(b"\x7fELF\0binary")
            self.assertIn(
                "compiled binary", check_file(root, pathlib.Path("compiled"))[0]
            )

    def test_secret_canaries(self) -> None:
        """Recognize key/token material without returning its value."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = pathlib.Path(directory)
            content = "-----BEGIN " + "PRIVATE KEY-----\nghp_" + "a" * 36
            (root / "unsafe.txt").write_text(content)
            errors = check_file(root, pathlib.Path("unsafe.txt"))
            self.assertEqual(len(errors), 2)
            self.assertNotIn("a" * 36, " ".join(errors))

    def test_malformed_link_and_filename_redaction(self) -> None:
        """Malformed URL libraries and unsafe filenames cannot disclose their input."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = pathlib.Path(directory)
            token = "ghp_" + "a" * 36
            (root / "README.md").write_text(f"[bad](https://{token}／example.org)")
            errors = check_file(root, pathlib.Path("README.md"))
            self.assertTrue(any("malformed link" in error for error in errors))
            self.assertNotIn(token, " ".join(errors))
            self.assertNotIn(token, " ".join(check_file(root, pathlib.Path(token))))

    def test_copy_mutation_is_revalidated(self) -> None:
        """Source mutation after inventory approval cannot sneak secrets into export."""
        from unittest import mock
        import export_public_snapshot

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = pathlib.Path(directory) / "source"
            root.mkdir()
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            (root / "file.txt").write_text("safe")
            original = export_public_snapshot.read_regular

            def changed(
                base: pathlib.Path, relative: pathlib.Path
            ) -> tuple[bytes, int]:
                """Simulate a source race after the original contents were checked."""
                (base / relative).write_text("ghp_" + "a" * 36)
                return original(base, relative)

            with mock.patch.object(export_public_snapshot, "read_regular", changed):
                with self.assertRaisesRegex(ValueError, "exported content rejected"):
                    export_snapshot(root, root.parent / "export")

    def test_index_transformations_fail_closed(self) -> None:
        """Git attributes cannot silently change staged bytes from approved source."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = pathlib.Path(directory) / "source"
            root.mkdir()
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            (root / ".gitattributes").write_text("*.txt text eol=lf\n")
            (root / "file.txt").write_bytes(b"line\r\n")
            with self.assertRaisesRegex(ValueError, "staged bytes"):
                export_snapshot(root, root.parent / "export", True)

    def test_snapshot_membership_links(self) -> None:
        """Existing excluded destinations and reference-style links must fail."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = pathlib.Path(directory)
            (root / "existing.md").write_text("ok")
            (root / "excluded.md").write_text("not published")
            (root / "README.md").write_text(
                "[good](existing.md?view=1#heading) [bad](excluded.md)\n"
                "[missing]: missing.md\n<img src='absent.png'>"
            )
            included = {pathlib.Path("existing.md"), pathlib.Path("README.md")}
            self.assertEqual(
                len(check_file(root, pathlib.Path("README.md"), included)), 3
            )

    def test_symlink_ancestors(self) -> None:
        """Reject direct links and parent-directory links even to inside-root files."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = pathlib.Path(directory)
            (root / "real").mkdir()
            (root / "real/file").write_text("ok")
            (root / "alias").symlink_to(root / "real", target_is_directory=True)
            self.assertIn("symlinked", check_file(root, pathlib.Path("alias/file"))[0])
            (root / "link").symlink_to(root / "real/file")
            self.assertIn("symlinked", check_file(root, pathlib.Path("link"))[0])

    def test_fresh_git_export_modes_and_bedrock(self) -> None:
        """Ignored Bedrock stays local; approved bytes and modes stage identically."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = pathlib.Path(directory) / "source"
            root.mkdir()
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            (root / ".gitignore").write_text("/aws.bedrock/\n/priv/\n")
            (root / "run.py").write_text("#!/usr/bin/env python3\n")
            (root / "run.py").chmod(0o755)
            (root / "aws.bedrock").mkdir()
            (root / "aws.bedrock/plugin.yaml").write_text("local-only")
            subprocess.run(["git", "add", "--force", "."], cwd=root, check=True)
            (root / "deleted.md").write_text("removed")
            subprocess.run(["git", "add", "deleted.md"], cwd=root, check=True)
            (root / "deleted.md").unlink()
            paths = approved_files(root)
            self.assertEqual(
                paths, [pathlib.Path(".gitignore"), pathlib.Path("run.py")]
            )
            destination = root.parent / "export"
            self.assertEqual(export_snapshot(root, destination, True), paths)
            self.assertEqual(candidate_files(destination), paths)
            self.assertTrue((root / "aws.bedrock/plugin.yaml").exists())
            self.assertFalse((destination / "aws.bedrock").exists())
            self.assertEqual((destination / "run.py").stat().st_mode & 0o777, 0o755)
            with self.assertRaisesRegex(ValueError, "must not exist"):
                export_snapshot(root, destination)

    def test_missing_and_empty_fail_closed(self) -> None:
        """Missing files and empty fresh repositories cannot pass a publication gate."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = pathlib.Path(directory)
            self.assertIn("missing", check_file(root, pathlib.Path("missing"))[0])
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            with self.assertRaisesRegex(ValueError, "empty"):
                approved_files(root)


if __name__ == "__main__":
    unittest.main()
