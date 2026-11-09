"""Identify exact public source bytes without requiring a publication commit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

from check_public_snapshot import approved_files
from export_public_snapshot import read_regular


def source_identity(root: Path) -> dict:
    """Return optional HEAD and a path/content/mode digest of approved current files.

    Read only regular contained files; do not stage files, create commits or infer
    that HEAD identifies a dirty worktree. Invalid repositories/inventories fail.
    """
    records = []
    for relative in approved_files(root):
        content, permissions = read_regular(root, relative)
        records.append(
            {
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "mode": "100755" if permissions & 0o111 else "100644",
            }
        )
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if head.returncode:
        # A symbolic branch with no refs is an unborn publication repository.
        subprocess.run(
            ["git", "symbolic-ref", "HEAD"],
            cwd=root,
            capture_output=True,
            check=True,
            timeout=10,
        )
        refs = subprocess.run(
            ["git", "show-ref"], cwd=root, capture_output=True, timeout=10
        )
        if head.returncode not in (1, 128) or refs.returncode != 1 or refs.stdout:
            raise ValueError("source HEAD cannot be verified")
    return {
        "commit": head.stdout.strip() if head.returncode == 0 else None,
        "inventory_sha256": hashlib.sha256(
            json.dumps(records, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "file_count": len(records),
    }
