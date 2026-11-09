"""Bounded regular-file I/O; paths are data, runtime/OS policy owns authority.

Selected independently by each package using it. No sibling imports. Ancestors
are opened descriptor-relative with O_NOFOLLOW, including output directories.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import sys
from typing import Iterator


def data_path(request: dict, raw: str) -> str:
    """Resolve file data against staging/workspace without selecting a workspace."""
    if not isinstance(raw, str) or not raw or "\0" in raw or "\\" in raw:
        raise ValueError("invalid file path")
    if raw.startswith("run://"):
        root, tail = request["paths"]["run_root"], raw[6:]
    elif raw.startswith("repo://"):
        root, tail = request["paths"]["workspace_dir"], ".transist/" + raw[7:]
    elif "://" in raw:
        raise ValueError("unsupported file path scheme")
    elif os.path.isabs(raw):
        root, tail = "/", raw[1:]
    else:
        root, tail = request["paths"]["workspace_dir"], raw
    if not tail or any(part in ("", ".", "..") for part in tail.split("/")):
        raise ValueError("file path must not contain traversal or empty segments")
    return os.path.join(root, tail)


def _directory_flags() -> int:
    """Search pinned ancestors without requesting directory-listing permission."""
    search = getattr(os, "O_SEARCH", None)
    if search is None:
        # Darwin's public <sys/fcntl.h> O_SEARCH ABI (O_EXEC | O_DIRECTORY),
        # also exported as 0x40100000 by CPython 3.14; absent in older Pythons.
        search = (
            0x40100000
            if sys.platform == "darwin"
            else getattr(os, "O_PATH", os.O_RDONLY)
        )
    return search | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def parent_fd(path: str, create: bool = False) -> Iterator[tuple[int, str]]:
    """Anchor an absolute path without following symlinks; optionally mkdir parents."""
    parts = Path(path).parts
    if not Path(path).is_absolute() or ".." in parts:
        raise ValueError("expected an absolute contained file path")
    flags = _directory_flags()
    fd = os.open("/", flags)
    try:
        for part in parts[1:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def read_bytes(path: str, limit: int) -> bytes:
    """Read at most limit+1 regular-file bytes; reject overflow and special files."""
    with parent_fd(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError("file is nonregular or exceeds byte limit")
            raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("file exceeds byte limit")
    return raw


def write_bytes(path: str, raw: bytes) -> None:
    """Write exact bytes to a regular destination, never through a symlink or FIFO."""
    with parent_fd(path, create=True) as (parent, name):
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(fd, "wb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("output must be a regular file")
            handle.truncate(0)
            handle.write(raw)
