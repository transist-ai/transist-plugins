"""Self-contained local fixtures; no shared repository test helpers."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess


def git(repo: Path, *args: str) -> str:
    """Run trusted fixture Git arguments in repo, returning UTF-8 stdout.

    Only tests use this helper to create or mutate their temporary repository.
    Host configuration, hooks, signing and inherited Git variables are ignored.
    """
    executable = shutil.which("git")
    assert executable is not None
    env = {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        "LC_ALL": "C",
    }
    return subprocess.run(
        [
            executable,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            *args,
        ],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


def seed(repo: Path) -> None:
    """Create a deterministic main/alpha/zeta fixture in a new directory."""
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "commit", "--allow-empty", "-m", "fixture")
    git(repo, "branch", "alpha")
    git(repo, "branch", "zeta")


def snapshot(repo: Path) -> dict[str, str]:
    """Hash every regular fixture file to detect writes made by inspection."""
    return {
        str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in repo.rglob("*")
        if path.is_file()
    }
