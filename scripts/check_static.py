#!/usr/bin/env python3
"""Run the static CI gate; never import or execute plugin implementations or tests."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from check_plugin_structure import ROOT, check_repository, regular_file, source_files


def run_check(
    label: str, command: list[str], env: dict[str, str] | None = None
) -> None:
    """Run a static-analysis tool from the repository root, failing on tool errors."""
    print(f"Checking {label}", flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True, timeout=120)


def check_go_format(root: Path, files: list[str]) -> None:
    """Require gofmt-clean Go sources without compiling or rewriting any file."""
    print("Checking Go formatting", flush=True)
    result = subprocess.run(
        ["gofmt", "-l", *files],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        timeout=120,
    )
    if result.stdout:
        raise ValueError(f"Go sources need gofmt:\n{result.stdout.rstrip()}")


def scan_secrets(root: Path, files: list[str]) -> None:
    """Scan Git-listed source bytes with isolated configuration and redacted finds."""
    with tempfile.TemporaryDirectory(
        prefix="transist-static-secrets-", dir=os.environ.get("RUNNER_TEMP", "/tmp")
    ) as directory:
        destination = Path(directory) / "source"
        destination.mkdir()
        for relative in files:
            source = regular_file(root, relative)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        # Do not inherit ignored local config, fingerprints or inline suppressions.
        ignore = Path(directory) / "empty.ignore"
        ignore.write_text("")
        env = os.environ.copy()
        env.pop("GITLEAKS_CONFIG", None)
        env.pop("GITLEAKS_CONFIG_TOML", None)
        run_check(
            "current-source secrets",
            [
                "gitleaks",
                "dir",
                "--redact",
                "--no-banner",
                "--ignore-gitleaks-allow",
                "--config",
                str(ROOT / "scripts/gitleaks.toml"),
                "--gitleaks-ignore-path",
                str(ignore),
                str(destination),
            ],
            env=env,
        )


def main() -> int:
    """Check structure, formatting, lint and secrets; return nonzero on any failure."""
    try:
        print("Checking canonical package structure and YAML/JSON", flush=True)
        errors = check_repository(ROOT)
        if errors:
            raise ValueError("\n".join(errors))
        files = sorted(source_files(ROOT))
        for relative in files:
            regular_file(ROOT, relative)
        python_files = [path for path in files if path.endswith(".py")]
        run_check(
            "Python formatting",
            [sys.executable, "-m", "black", "--check", *python_files],
        )
        run_check(
            "Python lint",
            [
                sys.executable,
                "-m",
                "flake8",
                "--max-line-length=88",
                "--extend-ignore=E203,W503",
                *python_files,
            ],
        )
        go_files = [path for path in files if path.endswith(".go")]
        if go_files:
            check_go_format(ROOT, go_files)
        workflows = [
            path
            for path in files
            if path.startswith(".github/workflows/")
            and path.endswith((".yml", ".yaml"))
        ]
        if workflows:
            # Keep optional host-specific ShellCheck/Pyflakes discovery disabled.
            run_check(
                "Actions workflow definitions",
                ["actionlint", "-shellcheck=", "-pyflakes=", *workflows],
            )
        scan_secrets(ROOT, files)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Static checks failed: {error}", file=sys.stderr)
        return 1
    print("All static checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
