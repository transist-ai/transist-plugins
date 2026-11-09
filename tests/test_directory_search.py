"""Real descriptor I/O with missing O_SEARCH exports, including Darwin sandboxing."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "shell/file_io.py",
    "openai.compatible/file_io.py",
    "git.cli/instruction_comments.py",
    "github.cli/review_context.py",
)

# Run production helpers in a child so the native sandbox denial is observable.
PROBE = r"""
import importlib.util, json, os, pathlib, sys
repo, root = map(pathlib.Path, sys.argv[1:])
os.__dict__.pop("O_SEARCH", None)
modules = []
for index, name in enumerate(json.loads(os.environ["SEARCH_MODULES"])):
    spec = importlib.util.spec_from_file_location(
        f"search_{index}", repo / "plugins" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    modules.append(module)
if sys.platform == "darwin":
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except PermissionError:
        pass
    else:
        os.close(fd)
        raise AssertionError("sandbox did not deny ancestor directory read")
raw = b" exact caf\xc3\xa9\r\n\n"
for index, module in enumerate(modules[:2]):
    assert module.read_bytes(str(root / "data/source"), len(raw)) == raw
    module.write_bytes(str(root / f"data/new-{index}/output"), raw)
    assert (root / f"data/new-{index}/output").read_bytes() == raw
scanner, review = modules[2:]
assert scanner.scan_file(root / "data", root / "data/request.py") == [
    ("request.py", 1, "preserve this request")]
assert scanner.confirmed_missing(root / "data", root / "data/missing/file.py")
scanner.write_output(root / "data/scan.md", [("request.py", 1, "preserve")])
value = {"comments": [], "count": 0, "pull_request": {}}
assert review.read(root / "data/input.json") == value
review.write(root / "data/projected.json", value)
assert review.read(root / "data/projected.json") == value
for module in modules[:2]:
    for path in ("data/link/source", "data/leaf", "data/../data/source", "data/fifo"):
        try:
            module.read_bytes(str(root / path), len(raw))
        except (OSError, ValueError):
            pass
        else:
            raise AssertionError("unsafe source accepted: " + path)
    outputs = ["data/link/source", "data/leaf", "data/fifo"]
    if sys.platform == "darwin":
        outputs.append("readonly/output")
    for path in outputs:
        try:
            module.write_bytes(str(root / path), b"changed")
        except (OSError, ValueError):
            pass
        else:
            raise AssertionError("unsafe output accepted: " + path)
for path in (
    root / "data/link/input.json", root / "data/json-link", root / "data/fifo"
):
    for operation in (lambda: review.read(path), lambda: review.write(path, value)):
        try:
            operation()
        except (OSError, ValueError):
            pass
        else:
            raise AssertionError("unsafe review file accepted")
operations = [
    lambda: scanner.open_source(root / "data", root / "data/link/source"),
    lambda: scanner.write_output(root / "data/link/source", []),
]
if sys.platform == "darwin":
    operations.extend([
        lambda: scanner.write_output(root / "readonly/output", []),
        lambda: review.write(root / "readonly/output", value),
    ])
for operation in operations:
    try:
        operation()
    except (OSError, ValueError):
        pass
    else:
        raise AssertionError("unsafe scanner/review path accepted")
assert (root / "data/source").read_bytes() == raw
assert not (root / "readonly/output").exists()
print("PASS missing O_SEARCH: readers, scanner/deletion, atomic projection, "
      "symlinks, traversal, FIFO; Darwin sandbox also denies readonly writes")
"""


class DirectorySearchTests(unittest.TestCase):
    """Search-only flags preserve file access without granting ancestor listings."""

    def test_platform_flags_with_and_without_export(self) -> None:
        """All independently selected modules use the same verified platform ABI."""
        for index, relative in enumerate(MODULES):
            spec = importlib.util.spec_from_file_location(
                f"directory_flags_{index}", ROOT / "plugins" / relative
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with mock.patch.object(os, "O_SEARCH", None, create=True):
                expected = (
                    0x40100000
                    if sys.platform == "darwin"
                    else getattr(os, "O_PATH", os.O_RDONLY)
                )
                self.assertEqual(
                    module._directory_flags(), expected | os.O_DIRECTORY | os.O_NOFOLLOW
                )
            if sys.platform == "darwin" and hasattr(os, "O_SEARCH"):
                self.assertEqual(os.O_SEARCH, 0x40100000)

    def test_real_io_without_search_export(self) -> None:
        """Deny ancestor read and readonly writes in the real macOS sandbox."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "data").mkdir()
            (root / "readonly").mkdir()
            (root / "data/source").write_bytes(b" exact caf\xc3\xa9\r\n\n")
            (root / "data/request.py").write_text("# tst: preserve this request\n")
            (root / "data/input.json").write_text(
                '{"comments":[],"count":0,"pull_request":{}}'
            )
            (root / "data/link").symlink_to(root / "data", target_is_directory=True)
            (root / "data/leaf").symlink_to(root / "data/source")
            (root / "data/json-link").symlink_to(root / "data/input.json")
            os.mkfifo(root / "data/fifo")
            command = [sys.executable, "-B", "-c", PROBE, str(ROOT), str(root)]
            if sys.platform == "darwin":
                profile = (
                    "(version 1)(allow default)(deny file-read-data (literal "
                    + json.dumps(str(root))
                    + "))(deny file-write* (subpath "
                    + json.dumps(str(root / "readonly"))
                    + "))"
                )
                command = ["sandbox-exec", "-p", profile, *command]
            else:
                (root / "readonly").chmod(0o500)
            try:
                env = {**os.environ, "SEARCH_MODULES": json.dumps(MODULES)}
                env.pop("PYTHONPATH", None)
                result = subprocess.run(
                    command, env=env, text=True, capture_output=True, timeout=15
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("PASS missing O_SEARCH", result.stdout)
            finally:
                (root / "readonly").chmod(0o700)
