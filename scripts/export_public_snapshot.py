#!/usr/bin/env python3
"""Export validated bytes/modes without old history or ignored local files."""

from __future__ import annotations

import argparse
import os
import pathlib
import stat
import subprocess
import tempfile

from check_public_snapshot import (
    ROOT,
    SnapshotError,
    approved_files,
    candidate_files,
    check_file,
)


def read_regular(root: pathlib.Path, relative: pathlib.Path) -> tuple[bytes, int]:
    """Read a regular source file through no-follow directory handles on POSIX."""
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            following = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = following
        descriptor = os.open(
            relative.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise SnapshotError("source must be a regular file")
            return source.read(), metadata.st_mode & 0o777
    finally:
        os.close(directory)


def stage_verified(
    destination: pathlib.Path, expected: dict[pathlib.Path, tuple[bytes, int]]
) -> None:
    """Stage under isolated Git config, then compare actual index blobs and modes."""
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    with tempfile.TemporaryDirectory(prefix="snapshot-git-", dir="/tmp") as home:
        environment.update(
            HOME=home,
            XDG_CONFIG_HOME=home,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_ATTR_NOSYSTEM="1",
        )

        def git(*arguments: str) -> bytes:
            """Run config-isolated Git without exposing subprocess diagnostics."""
            return subprocess.run(
                [
                    "git",
                    "-c",
                    "core.autocrlf=false",
                    "-c",
                    "core.filemode=true",
                    "-c",
                    "core.attributesFile=" + os.devnull,
                    *arguments,
                ],
                cwd=destination,
                env=environment,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout

        git("init", "--quiet", "--initial-branch=main", "--template=", ".")
        if candidate_files(destination) != sorted(expected):
            raise SnapshotError(
                "destination ignore rules changed publication inventory"
            )
        git("add", "--", ".")
        entries = git("ls-files", "--stage", "-z").split(b"\0")
        seen = set()
        for entry in entries:
            if not entry:
                continue
            header, name = entry.split(b"\t", 1)
            mode, digest, stage = header.split()
            relative = pathlib.Path(os.fsdecode(name))
            if stage != b"0" or relative not in expected:
                raise SnapshotError("unexpected staged publication file")
            content, permissions = expected[relative]
            expected_mode = b"100755" if permissions & 0o111 else b"100644"
            if (
                mode != expected_mode
                or git("cat-file", "blob", digest.decode()) != content
            ):
                raise SnapshotError("staged bytes or executable mode differ")
            seen.add(relative)
        if seen != set(expected):
            raise SnapshotError("staged publication inventory differs")


def export_snapshot(
    root: pathlib.Path, destination: pathlib.Path, init_git: bool = False
) -> list[pathlib.Path]:
    """Validate copied bytes in a NEW private directory; optionally stage clean Git."""
    root = root.resolve()
    destination = destination.absolute()
    resolved = destination.resolve()
    if destination.exists() or destination.is_symlink():
        raise SnapshotError("destination must not exist")
    if resolved.is_relative_to(root) or root.is_relative_to(resolved):
        raise SnapshotError("destination must be separate from source repository")
    if not destination.parent.is_dir():
        raise SnapshotError("destination parent must exist")
    paths = approved_files(root)
    destination.mkdir(mode=0o700)
    expected = {}
    for relative in paths:
        content, mode = read_regular(root, relative)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(mode)
        expected[relative] = (content, mode)
    included = set(paths)
    errors = [
        error for path in paths for error in check_file(destination, path, included)
    ]
    if errors:
        raise SnapshotError("exported content rejected:\n" + "\n".join(errors))
    if init_git:
        stage_verified(destination, expected)
    return paths


def main() -> int:
    """Export to a new destination without creating a commit or publishing anything."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=pathlib.Path, required=True)
    parser.add_argument(
        "--init-git", action="store_true", help="initialize and stage; never commit"
    )
    args = parser.parse_args()
    try:
        paths = export_snapshot(ROOT, args.destination, args.init_git)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        diagnostic = str(error) if isinstance(error, SnapshotError) else "export failed"
        parser.exit(1, diagnostic + "\n")
    print(f"Exported {len(paths)} files; no history copied and no commit created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
