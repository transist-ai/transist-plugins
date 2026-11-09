"""Publication evidence identifies current files even before the initial commit."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from source_identity import source_identity  # noqa: E402


class SourceIdentityTests(unittest.TestCase):
    """Preserve history and distinguish content, modes and unpublished snapshots."""

    def test_unborn_identity_changes_with_bytes_and_modes(self) -> None:
        """A stable uncommitted inventory is exact without inventing HEAD."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory).resolve()
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            path = root / "example.txt"
            path.write_text("first")
            first = source_identity(root)
            self.assertIsNone(first["commit"])
            self.assertEqual(first["file_count"], 1)
            self.assertEqual(first, source_identity(root))
            path.write_text("second")
            second = source_identity(root)
            self.assertNotEqual(first["inventory_sha256"], second["inventory_sha256"])
            path.chmod(0o755)
            self.assertNotEqual(
                second["inventory_sha256"], source_identity(root)["inventory_sha256"]
            )
            result = subprocess.run(
                ["git", "rev-list", "--all", "--count"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(result.stdout.strip(), "0")

    def test_missing_repository_and_symlinks_fail_closed(self) -> None:
        """Source identity cannot follow links or accept a non-Git source tree."""
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory).resolve()
            (root / "example.txt").write_text("source")
            with self.assertRaises(subprocess.CalledProcessError):
                source_identity(root)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            (root / "alias.txt").symlink_to(root / "example.txt")
            with self.assertRaises(ValueError):
                source_identity(root)


if __name__ == "__main__":
    unittest.main()
