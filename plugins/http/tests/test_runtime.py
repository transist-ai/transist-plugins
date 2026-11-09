"""Offline regressions against the vendored 0.12.0 APP schemas."""

import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
import support  # noqa: E402

SPEC = importlib.util.spec_from_file_location("http_plugin", ROOT / "plugin.py")
PLUGIN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLUGIN)


class RuntimeContractTests(unittest.TestCase):
    """Validate envelopes and destination lifetimes without upstream calls."""

    def test_app_frames(self) -> None:
        """Success omits absent optionals and byte progress matches strict APP."""
        request = {"invoke_id": "unit"}
        self.assertEqual(PLUGIN.PLUGIN_VERSION, "0.1.0")
        for frame, name in [
            (PLUGIN.result_frame(request, "success"), "invoke.result"),
            (
                PLUGIN.result_frame(
                    request,
                    "failed",
                    error=PLUGIN.canonical_error("validation_error", "invalid"),
                ),
                "invoke.result",
            ),
            (PLUGIN.progress_frame(request, 65536), "invoke.progress"),
        ]:
            support.contract_validator(f"app/{name}.v1.schema.json").validate(frame)

    def test_dest_containment(self) -> None:
        """Resolve durable/scratch paths in their roots; reject escaping symlinks."""
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            workspace, scratch = root / "workspace", root / "scratch"
            workspace.mkdir()
            scratch.mkdir()
            request = {
                "paths": {"workspace_dir": str(workspace), "run_root": str(scratch)}
            }
            self.assertEqual(
                PLUGIN.resolve_download_dest(request, "files/a.bin"),
                str(workspace / "files/a.bin"),
            )
            self.assertEqual(
                PLUGIN.resolve_download_dest(request, "run://files/a.bin"),
                str(scratch / "files/a.bin"),
            )
            (workspace / "escape").symlink_to(scratch, target_is_directory=True)
            for dest in [
                "../a",
                "escape/a",
                "repo://a",
                "/a",
                "run://../a",
                "run:///a",
                ".",
                "",
            ]:
                with self.subTest(dest=dest), self.assertRaises(PLUGIN.ValidationError):
                    PLUGIN.resolve_download_dest(request, dest)


if __name__ == "__main__":
    unittest.main()
