"""Regression checks for native installed fixture teardown, without a runtime."""

import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from installed_fixture import remove_disposable_directory, settle_runs


class FixtureTeardownTests(unittest.TestCase):
    """Planned states are active; cleanup must finish before fixture removal."""

    def test_disappearing_child_retries_until_root_is_removed(self) -> None:
        """A concurrent summary rename cannot leave a fixture behind or fail cleanup."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixture"
            root.mkdir()
            child = root / "summary.tmp"
            child.write_text("fixture")
            real_remove = shutil.rmtree
            calls = []

            def remove(path: Path) -> None:
                """Simulate a disappearing child, then actually remove the root."""
                calls.append(path)
                if len(calls) == 1:
                    child.unlink()
                    raise FileNotFoundError(str(child))
                real_remove(path)

            with patch("installed_fixture.shutil.rmtree", side_effect=remove):
                remove_disposable_directory(root)
            self.assertEqual(calls, [root, root])
            self.assertFalse(root.exists())

    def test_fixture_permission_errors_are_not_suppressed(self) -> None:
        """Only transient rename/flush races qualify for a bounded retry."""
        with patch(
            "installed_fixture.shutil.rmtree", side_effect=PermissionError("denied")
        ):
            with self.assertRaises(PermissionError):
                remove_disposable_directory(Path("unused-fixture"))

    def test_planned_cancels_then_waits_for_cleanup(self) -> None:
        """Poll planned/running through cancellation and asynchronous cleanup."""
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            (home / "runs/run_fixture").mkdir(parents=True)
            reports = iter(
                [
                    {"status": "planned"},
                    {"status": "running"},
                    {"status": "canceled"},
                    {"status": "canceled", "cleanup": {}},
                    {"status": "canceled", "cleanup": {"finished_at_ms": 1}},
                ]
            )
            calls = []

            def cli(*args: str) -> str:
                """Record commands and simulate a detached runtime's transitions."""
                calls.append(args)
                return json.dumps(next(reports)) if args[1] == "show" else "{}"

            with patch("installed_fixture.time.sleep"):
                settle_runs(home, cli)
            self.assertEqual(calls[1], ("runs", "cancel", "fixture"))
            self.assertEqual(calls[4], ("runs", "cleanup", "fixture"))
            self.assertEqual(calls[-1], ("runs", "show", "fixture"))

    def test_success_and_finished_cleanup_need_no_mutation(self) -> None:
        """Successful or already-cleaned terminal runs are inspection-only."""
        for report in (
            {"status": "success"},
            {"status": "failed", "cleanup": {"finished_at_ms": 1}},
        ):
            with self.subTest(report=report), tempfile.TemporaryDirectory() as root:
                home = Path(root)
                (home / "runs/run_fixture").mkdir(parents=True)
                calls = []

                def cli(*args: str) -> str:
                    """Return an immutable terminal report and capture any mutation."""
                    calls.append(args)
                    return json.dumps(report)

                settle_runs(home, cli)
                self.assertEqual(calls, [("runs", "show", "fixture")])

    def test_nonterminal_deadline_is_failure(self) -> None:
        """Never turn an uncompleted cancellation into successful teardown."""
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            (home / "runs/run_fixture").mkdir(parents=True)
            with patch("installed_fixture.time.monotonic", side_effect=[0, 21]):
                with self.assertRaisesRegex(AssertionError, "cancellation"):
                    settle_runs(home, lambda *args: '{"status":"planned"}')
