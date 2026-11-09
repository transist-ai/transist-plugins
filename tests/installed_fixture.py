"""Native installed-E2E fixture polling and bounded run teardown (no sandbox bypass)."""

from collections.abc import Callable
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Iterator

TERMINAL = {"success", "failed", "canceled"}
Command = Callable[..., str]


@contextmanager
def disposable_directory(prefix: str) -> Iterator[str]:
    """Remove fixtures after bounded supervisor flushes; never hide I/O errors."""
    path = tempfile.mkdtemp(prefix=prefix, dir=fixture_base())
    try:
        yield path
    finally:
        remove_disposable_directory(Path(path))


def remove_disposable_directory(path: Path) -> None:
    """Delete an owned fixture within five seconds despite asynchronous temp renames.

    Retry disappearing descendants only while the root still exists. Permission,
    link and other I/O failures remain errors; never remove unrelated directories.
    """
    deadline = time.monotonic() + 5
    while True:
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            if not path.exists() and not path.is_symlink():
                return
            if time.monotonic() >= deadline:
                raise
        except OSError as error:
            if error.errno != errno.ENOTEMPTY or time.monotonic() >= deadline:
                raise
        time.sleep(0.05)


def runtime_binary(admin: bool = False) -> str:
    """Require the explicit executable; missing env never falls back to PATH."""
    key = "TRANSIST_ADMIN_BIN" if admin else "TRANSIST_BIN"
    raw = os.environ.get(key, "")
    path = Path(raw)
    if (
        not raw
        or not path.is_absolute()
        or not path.is_file()
        or not os.access(path, os.X_OK)
    ):
        raise RuntimeError(f"{key} must name an existing absolute executable")
    return str(path)


def fixture_base() -> str:
    """Require existing disposable storage without a host temp-dir fallback."""
    path = Path(os.environ.get("TRANSIST_FIXTURE_BASE", ""))
    if not path.is_absolute() or not path.is_dir():
        raise RuntimeError(
            "TRANSIST_FIXTURE_BASE must name an existing absolute directory"
        )
    return str(path.resolve())


def json_args(args: tuple[str, ...]) -> list[str]:
    """Put JSON format on the current subcommand; version queries take no format."""
    return (
        [*args, "--format", "json"]
        if args and not args[0].startswith("--")
        else list(args)
    )


def settle_runs(home: Path, cli: Command) -> None:
    """Cancel active fixture runs and finish failed/canceled cleanup before removal."""
    for directory in sorted((home / "runs").glob("run_*")):
        if not directory.is_dir():
            continue
        name = directory.name.removeprefix("run_")
        report = json.loads(cli("runs", "show", name))
        if report.get("status") not in TERMINAL:
            cli("runs", "cancel", name)
            deadline = time.monotonic() + 20
            while report.get("status") not in TERMINAL:
                if time.monotonic() >= deadline:
                    raise AssertionError("fixture cancellation did not become terminal")
                time.sleep(0.05)
                report = json.loads(cli("runs", "show", name))
        if report.get("status") in {"failed", "canceled"}:
            if report.get("cleanup", {}).get("finished_at_ms") is not None:
                continue
            cli("runs", "cleanup", name)
            deadline = time.monotonic() + 20
            while True:
                report = json.loads(cli("runs", "show", name))
                if report.get("cleanup", {}).get("finished_at_ms") is not None:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError("fixture cleanup did not finish")
                time.sleep(0.05)
