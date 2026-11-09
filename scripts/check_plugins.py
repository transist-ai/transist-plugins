#!/usr/bin/env python3
"""Run every public package's deterministic offline tests without provider accounts."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

from check_public_snapshot import candidate_files

ROOT = Path(__file__).resolve().parents[1]
COMPACT_SMOKE_PACKAGES = frozenset({"git.cli", "github.cli", "jira.rest", "shell"})


def offline_commands(directory: Path) -> list[list[str]]:
    """Discover every unit suite and supported legacy/compact offline smoke."""
    tests = directory / "tests"
    commands = []
    if list(tests.glob("test_*.py")):
        commands.append(
            [
                sys.executable,
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                str(tests),
                "-p",
                "test_*.py",
            ]
        )
    if (tests / "smoke.py").is_file():
        smoke = [sys.executable, "-B", str(tests / "smoke.py")]
        commands.append(smoke)
        if directory.name in COMPACT_SMOKE_PACKAGES:
            commands.append([*smoke, "--compact"])
    return commands


def run_offline_command(command: list[str], env: dict[str, str]) -> int:
    """Capture an offline command's streams, preserve diagnostics and enforce timeout.

    Print captured output to the matching parent stream and return nonzero for
    failed launches/timeouts so the caller can finish all remaining checks.
    """
    try:
        result = subprocess.run(
            command, cwd=ROOT, env=env, timeout=300, capture_output=True, text=True
        )
    except subprocess.TimeoutExpired as error:
        for stream, output in ((sys.stdout, error.stdout), (sys.stderr, error.stderr)):
            if output:
                stream.write(
                    output.decode("utf-8", errors="replace")
                    if isinstance(output, bytes)
                    else output
                )
                stream.flush()
        print("Offline command exceeded its 300-second deadline", file=sys.stderr)
        return 1
    except OSError:
        print("Offline command could not be started", file=sys.stderr)
        return 1
    sys.stdout.write(result.stdout or "")
    sys.stdout.flush()
    sys.stderr.write(result.stderr or "")
    sys.stderr.flush()
    return result.returncode


def main() -> int:
    """Check all public units/smokes, retaining a failed gate for any command error."""
    if shutil.which("go") is None:
        print("Go 1.22+ is required for Ollama checks", file=sys.stderr)
        return 1
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    manifests = [
        ROOT / path
        for path in candidate_files(ROOT)
        if len(path.parts) == 3
        and path.parts[0] == "plugins"
        and path.name == "plugin.yaml"
    ]
    if not manifests:
        print("No public package manifests", file=sys.stderr)
        return 1
    failures = 0
    for manifest in manifests:
        directory = manifest.parent
        commands = offline_commands(directory)
        if not commands:
            print(f"No offline tests: {directory.name}", file=sys.stderr)
            failures += 1
            continue
        for command in commands:
            print(f"Checking {directory.name}: {' '.join(command)}", flush=True)
            if run_offline_command(command, env):
                failures += 1
    if failures:
        print(f"Offline checks failed: {failures} command(s).", file=sys.stderr)
        return 1
    print(f"Offline checks passed: {len(manifests)} public packages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
