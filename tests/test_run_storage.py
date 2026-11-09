"""Read-only inventory regressions using private, disposable filesystem fixtures."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import inspect_run_storage as inventory  # noqa: E402


class RunStorageTests(unittest.TestCase):
    """Check unique bytes, no-follow boundaries, privacy and bounded payload reads."""

    def setUp(self) -> None:
        """Create explicit real roots, avoiding platform temporary-root symlinks."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.run = self.root / "run"
        self.workspace = self.root / "workspace"
        self.run.mkdir()
        self.workspace.mkdir()

    def write(self, relative: str, content: bytes, base: Path | None = None) -> Path:
        """Write fixture bytes only, returning the path for metadata assertions."""
        path = (base or self.run) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_same_basename_across_roots_and_contained_runs(self) -> None:
        """Neither basename collisions nor nested runs lose their byte contribution."""
        files = [
            self.write("state.json", b"123"),
            self.write("contained_runs/run_child/state.json", b"12345"),
            self.write("state.json", b"1234567", self.workspace),
        ]
        report = inventory.inspect_storage(self.run, self.workspace)
        self.assertEqual(report["totals"]["files"], 3)
        self.assertEqual(report["totals"]["logical_bytes"], 15)
        self.assertEqual(
            report["totals"]["allocated_bytes"],
            sum(path.stat().st_blocks * 512 for path in files),
        )
        self.assertEqual(report["categories"]["state_projections"]["files"], 2)
        self.assertEqual(report["scopes"]["contained_run"]["logical_bytes"], 5)

    def test_layout_categories_and_json_descriptor_candidates(self) -> None:
        """Separate descriptor JSON without parsing private payloads."""
        paths = {
            "step_executions/a/execution_001.json": "immutable_executions",
            "steps/a.json": "step_projections",
            "state.json": "state_projections",
            "summary.json": "summaries",
            "artifacts/a/execution_001/artifacts/stdout.txt": "artifacts",
            "artifacts/a/execution_001/artifacts/a.json": "artifact_json_descriptors",
            "artifacts/a/execution_001/evidence/file.bin": "evidence",
            "artifacts/a/execution_001/evidence/b.json": "evidence_json_descriptors",
            "workflow_assets/blobs/private": "recipe_assets",
            "recipe.yaml": "recipe_assets",
            "gates/history/review.json": "gates",
            "events.jsonl": "events",
            "failures.jsonl": "events",
            "provenance.json": "provenance",
            "context/run_inputs.json": "context",
        }
        for path, category in paths.items():
            self.write(path, b"not even valid JSON")
            self.assertEqual(inventory.classify(tuple(path.split("/")))[0], category)
        report = inventory.inspect_storage(self.run)
        self.assertEqual(
            sum(c["files"] for c in report["categories"].values()), len(paths)
        )

    def test_no_payload_open_by_default_and_redacted_largest(self) -> None:
        """Default scanning opens directories only and reports no private names."""
        self.write("sensitive-name.txt", b"secret fixture contents")
        real_open = os.open
        flags = []

        def checked_open(path: object, mode: int, **kwargs: object) -> int:
            """Observe that the metadata-only path opens no regular file."""
            flags.append(mode)
            return real_open(path, mode, **kwargs)

        with mock.patch.object(inventory.os, "open", side_effect=checked_open):
            report = inventory.inspect_storage(self.run, list_largest=1)
        self.assertTrue(all(mode & os.O_DIRECTORY for mode in flags))
        rendered = json.dumps(report)
        for private in (str(self.run), "sensitive-name", "secret fixture"):
            self.assertNotIn(private, rendered)
        self.assertEqual(len(report["largest_files"][0]["id"]), 64)
        self.assertNotEqual(
            report["largest_files"][0]["id"],
            inventory.inspect_storage(self.run, list_largest=1)["largest_files"][0][
                "id"
            ],
        )

    def test_duplicates_stream_exact_limits_and_different_equal_sizes(self) -> None:
        """Bound candidate reads; equal sizes alone do not prove duplication."""
        content = b"x" * (inventory.CHUNK_BYTES + 7)
        for name, value in (("a", content), ("b", content), ("c", b"y" * len(content))):
            self.write(name, value)
        reads = []
        real_fdopen = os.fdopen

        class Reader:
            """Track requested read sizes without substituting fixture file contents."""

            def __init__(self, descriptor: int, mode: str) -> None:
                """Own the real fixture handle."""
                self.handle = real_fdopen(descriptor, mode)

            def __enter__(self) -> Reader:
                """Return the tracked reader."""
                return self

            def __exit__(self, *args: object) -> None:
                """Close the fixture handle."""
                self.handle.close()

            def fileno(self) -> int:
                """Expose the real descriptor for regular-file checks."""
                return self.handle.fileno()

            def read(self, size: int) -> bytes:
                """Record and delegate a bounded read."""
                reads.append(size)
                return self.handle.read(size)

        with mock.patch.object(inventory.os, "fdopen", Reader):
            report = inventory.inspect_storage(
                self.run,
                hash_duplicates=True,
                max_hash_file_bytes=len(content),
                max_hash_total_bytes=3 * (len(content) + 1),
            )
        self.assertTrue(reads)
        self.assertTrue(all(0 < size <= inventory.CHUNK_BYTES for size in reads))
        groups = report["duplicates"]["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["files"], 2)
        self.assertEqual(groups[0]["duplicate_logical_bytes"], len(content))
        report = inventory.inspect_storage(
            self.run, hash_duplicates=True, max_hash_file_bytes=len(content) - 1
        )
        self.assertEqual(report["duplicates"]["successfully_hashed_bytes"], 0)
        self.assertEqual(report["skipped"]["hash_budget"], 3)
        report = inventory.inspect_storage(
            self.run, hash_duplicates=True, max_hash_total_bytes=len(content)
        )
        self.assertEqual(report["duplicates"]["successfully_hashed_bytes"], 0)

    def test_symlinks_nonregular_and_unsafe_roots(self) -> None:
        """Skip contained links/FIFOs; reject root/ancestor links and traversal."""
        outside = self.write("outside", b"private", self.workspace)
        (self.run / "file-link").symlink_to(outside)
        (self.run / "dir-link").symlink_to(self.workspace, target_is_directory=True)
        os.mkfifo(self.run / "pipe")
        report = inventory.inspect_storage(self.run, hash_duplicates=True)
        self.assertEqual(report["totals"]["files"], 0)
        self.assertEqual(report["skipped"]["symlink"], 2)
        self.assertEqual(report["skipped"]["nonregular"], 1)
        for root in (
            self.run / "dir-link",
            self.run / "dir-link" / "sub",
            self.run / "..",
        ):
            with self.subTest(root=root), self.assertRaises((OSError, ValueError)):
                inventory.inspect_storage(root)
        (self.root / "alias").symlink_to(self.run, target_is_directory=True)
        with self.assertRaises(OSError):
            inventory.inspect_storage(self.root / "alias")

    def test_hardlinks_overlaps_and_sparse_allocation(self) -> None:
        """Unique inodes count once; allocated bytes use blocks, not logical size."""
        original = self.write("a", b"one")
        os.link(original, self.run / "b")
        sparse = self.run / "sparse"
        with sparse.open("wb") as handle:
            handle.seek(1024 * 1024)
            handle.write(b"x")
        report = inventory.inspect_storage(self.run, self.run)
        self.assertEqual(report["totals"]["files"], 2)
        self.assertEqual(report["totals"]["logical_bytes"], 1024 * 1024 + 4)
        self.assertEqual(
            report["totals"]["allocated_bytes"],
            512 * (original.stat().st_blocks + sparse.stat().st_blocks),
        )
        self.assertEqual(report["skipped"]["hardlink_or_overlap"], 1)
        self.assertEqual(report["skipped"]["overlapping_directory"], 1)

    def test_depth_entry_budgets_and_changed_hash_file(self) -> None:
        """Report partial scans; reject a candidate swapped to a symlink."""
        self.write("deep/deeper/a", b"xx")
        self.write("deep/deeper/b", b"xx")
        self.assertTrue(inventory.inspect_storage(self.run, max_depth=1)["truncated"])
        self.assertTrue(inventory.inspect_storage(self.run, max_entries=1)["truncated"])
        original = inventory.digest_file

        def changed(root_fd: int, record: inventory.FileRecord) -> str:
            """Swap the candidate after metadata traversal but before a bounded read."""
            path = self.run.joinpath(*record.relative)
            path.unlink()
            path.symlink_to(self.workspace / "outside")
            return original(root_fd, record)

        self.write("outside", b"xx", self.workspace)
        with mock.patch.object(inventory, "digest_file", side_effect=changed):
            report = inventory.inspect_storage(self.run, hash_duplicates=True)
        self.assertEqual(report["duplicates"]["groups"], [])
        self.assertEqual(report["skipped"]["hash_unreadable_or_changed"], 2)


if __name__ == "__main__":
    unittest.main()
