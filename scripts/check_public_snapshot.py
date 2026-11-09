#!/usr/bin/env python3
"""Check current publication contents, not history or ownership; list approved files."""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Bedrock is deliberately local-only, even if a previous index tracks its files.
EXCLUDED = {"aws.bedrock", "plugins/aws.bedrock"}
FORBIDDEN_PARTS = {
    ".git",
    "priv",
    "private",
    "signatures",
    "__pycache__",
    ".pytest_cache",
    "build",
    "dist",
    "venv",
    "htmlcov",
}
FORBIDDEN_SUFFIXES = {".pyc", ".sig", ".log", ".key", ".exe", ".so", ".dylib"}
PATTERNS = {
    "employer name or domain": re.compile(rb"su" + rb"mup", re.I),
    "personal name": re.compile(rb"andr" + rb"ey|andr" + rb"ei|chern" + rb"ykh", re.I),
    "personal email host": re.compile(rb"@gmail\.com", re.I),
    "work issue key": re.compile(rb"\bHB-\d+\b", re.I),
    "work package or team name": re.compile(
        rb"hardware[._-]"
        + rb"(?:mcp|backend)|terminal[-_]"
        + rb"hub|fire-"
        + rb"potato",
        re.I,
    ),
    "work workflow name": re.compile(
        rb"backend_development_plan_"
        + rb"delivery|software_engineer_ticket_"
        + rb"delivery",
        re.I,
    ),
    "absolute home path": re.compile(rb"/(?:Users|home)/[^/\s\x00]+/"),
    "private key material": re.compile(
        rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    "access token": re.compile(
        rb"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,}|"
        rb"AKIA[A-Z0-9]{16})\b"
    ),
}
INLINE_LINK = re.compile(r"\]\(([^)\n]+)\)")
REFERENCE_LINK = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
HTML_LINK = re.compile(r"(?:href|src)=[\"']([^\"']+)[\"']")
# Preserve the exact upstream copyright attribution, not private author paths.
LEGAL_CREDIT = b"Copyright 2026 " + b"Andr" + b"ey Ch" + b"ernykh"
LEGAL_FILES = {"NOTICE", "tests/contracts/README.md"}


class SnapshotError(ValueError):
    """A deliberately redacted publication diagnostic safe to show to an operator."""


def is_excluded(relative: pathlib.Path) -> bool:
    """Identify intentionally unpublished package trees independently of Git state."""
    name = relative.as_posix()
    return any(name == prefix or name.startswith(prefix + "/") for prefix in EXCLUDED)


def git_files(root: pathlib.Path, *arguments: str) -> set[pathlib.Path]:
    """Read NUL-delimited Git paths, failing closed without printing Git diagnostics."""
    result = subprocess.run(
        ["git", "ls-files", "-z", *arguments],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return {pathlib.Path(path.decode()) for path in result.stdout.split(b"\0") if path}


def candidate_files(root: pathlib.Path) -> list[pathlib.Path]:
    """List current public files, excluding deleted and intentionally local packages."""
    paths = git_files(root, "--cached", "--others", "--exclude-standard")
    deleted = git_files(root, "--deleted")
    return sorted(path for path in paths - deleted if not is_excluded(path))


def check_file(
    root: pathlib.Path,
    relative: pathlib.Path,
    included: set[pathlib.Path] | None = None,
) -> list[str]:
    """Check paths, bytes and links without echoing rejected content or targets."""
    display = relative.as_posix()
    if any(pattern.search(display.encode()) for pattern in PATTERNS.values()):
        return ["sensitive publication filename (redacted)"]
    parts = relative.parts
    if not parts or relative.is_absolute() or ".." in parts:
        return ["unsafe publication path"]
    if (
        is_excluded(relative)
        or any(part in FORBIDDEN_PARTS or part.startswith(".venv") for part in parts)
        or relative.suffix in FORBIDDEN_SUFFIXES
        or relative.name == ".env"
        or relative.name.startswith(".env.")
        or relative.name.lower() in {"credentials", "id_rsa", "id_ed25519"}
        or relative.as_posix().startswith("plugins/ollama.cli/bin/")
    ):
        return [f"{display}: private or generated path must not enter the snapshot"]
    root = root.resolve()
    file_path = root / relative
    if (
        not file_path.is_file()
        or not file_path.resolve().is_relative_to(root)
        or any(
            parent.is_symlink()
            for parent in [file_path, *file_path.parents]
            if parent != root
        )
    ):
        return [f"{display}: missing, non-regular, or symlinked file"]
    content = file_path.read_bytes()
    errors = []
    if content.startswith(
        (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"MZ")
    ):
        errors.append(f"{display}: compiled binary must not enter the source snapshot")
    for label, pattern in PATTERNS.items():
        scanned = content
        if label == "personal name" and display in LEGAL_FILES:
            scanned = content.replace(LEGAL_CREDIT, b"[upstream copyright credit]")
        match = pattern.search(scanned)
        if match:
            line = scanned.count(b"\n", 0, match.start()) + 1
            errors.append(f"{display}:{line}: {label}")
    if relative.suffix not in {".md", ".yml", ".yaml"}:
        return errors
    text = content.decode("utf-8", errors="replace")
    for pattern in (INLINE_LINK, REFERENCE_LINK, HTML_LINK):
        for match in pattern.finditer(text):
            target = match.group(1).split()[0].strip("<>")
            if not target or target.startswith("#") or "<" in target:
                continue
            try:
                parsed = urlsplit(target)
            except ValueError:
                line = text.count("\n", 0, match.start()) + 1
                errors.append(f"{display}:{line}: malformed link target")
                continue
            if parsed.scheme or parsed.netloc:
                continue
            destination = (file_path.parent / unquote(parsed.path)).resolve()
            valid = destination.is_relative_to(root) and destination.exists()
            if valid:
                dest_relative = destination.relative_to(root)
                valid = not is_excluded(dest_relative)
                if included is not None:
                    valid = valid and (
                        dest_relative in included
                        or (
                            destination.is_dir()
                            and any(dest_relative in path.parents for path in included)
                        )
                    )
            if not valid:
                line = text.count("\n", 0, match.start()) + 1
                errors.append(f"{display}:{line}: broken or unpublished local link")
    return errors


def approved_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Return a checked nonempty snapshot or fail without exposing sensitive values."""
    paths = candidate_files(root)
    if not paths:
        raise SnapshotError("empty publication snapshot")
    included = set(paths)
    errors = [error for path in paths for error in check_file(root, path, included)]
    if errors:
        raise SnapshotError("\n".join(errors))
    return paths


def main() -> int:
    """Print approved inventory or redacted diagnostics without repository changes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list", action="store_true", help="print approved relative paths"
    )
    args = parser.parse_args()
    try:
        paths = approved_files(ROOT)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(
            (
                str(error)
                if isinstance(error, SnapshotError)
                else "snapshot enumeration failed"
            ),
            file=sys.stderr,
        )
        return 1
    if args.list:
        print("\n".join(path.as_posix() for path in paths))
    else:
        print(f"Snapshot gate passed: {len(paths)} files (current contents only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
