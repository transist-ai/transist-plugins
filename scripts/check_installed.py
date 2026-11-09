#!/usr/bin/env python3
"""Explicit deterministic installed-test inventory. Missing prerequisites fail."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from docker_endpoint import local_docker_host

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT_PACKAGES = (
    "shell",
    "jira.rest",
    "git.cli",
    "github.cli",
    "codex.cli",
    "cursor.cli",
    "github.copilot.cli",
    "opencode.cli",
    "openspec",
    "openai.compatible",
)
LIFECYCLE_PACKAGES = (
    "shell",
    "git.cli",
    "github.cli",
    "codex.cli",
    "github.copilot.cli",
    "opencode.cli",
    "openspec",
)


@dataclass(frozen=True)
class Runner:
    """One named runner and its package selectors; no discovery-dependent omissions."""

    name: str
    path: str
    packages: tuple[str, ...]


RUNNERS = (
    Runner("http", "plugins/http/tests/e2e.py", ("http",)),
    Runner("git-mcp", "plugins/git.mcp/tests/e2e.py", ("git.mcp",)),
    Runner("text-http", "plugins/text.http/tests/e2e.py", ("text.http",)),
    Runner("openai", "plugins/openai.compatible/tests/e2e.py", ("openai.compatible",)),
    Runner("anthropic", "plugins/anthropic/tests/e2e.py", ("anthropic",)),
    Runner("openspec", "plugins/openspec/tests/e2e.py", ("openspec",)),
    Runner(
        "optional-cli",
        "plugins/codex.cli/tests/e2e_optional_inputs.py",
        ("codex.cli", "github.copilot.cli", "opencode.cli"),
    ),
    Runner(
        "cursor-lifecycle", "plugins/cursor.cli/tests/e2e_lifecycle.py", ("cursor.cli",)
    ),
    Runner("process-lifecycle", "tests/e2e_process_lifecycle.py", LIFECYCLE_PACKAGES),
    Runner("tika", "plugins/tika.http/tests/e2e.py", ("tika.http",)),
    Runner(
        "document-actions",
        "tests/e2e_document_actions.py",
        ("document-actions", *DOCUMENT_PACKAGES),
    ),
    Runner("lean-storage", "scripts/benchmark_run_persistence.py", ("codex.cli",)),
    Runner(
        "review-actions",
        "tests/e2e_review_actions.py",
        ("review-actions", "git.cli", "github.cli"),
    ),
    Runner(
        "reply-publication",
        "plugins/github.cli/tests/e2e_reply_publication.py",
        ("reply-publication", "github.cli"),
    ),
    Runner(
        "gate-contract",
        "tests/e2e_gate_decision_contract.py",
        (
            "gate-contract",
            "shell",
            "codex.cli",
            "cursor.cli",
            "github.copilot.cli",
            "opencode.cli",
            "openspec",
        ),
    ),
)
PACKAGES = frozenset(package for runner in RUNNERS for package in runner.packages)


def executable(raw: str, name: str) -> Path:
    """Require an explicit executable and exact 0.1.0 identity, never search PATH."""
    path = Path(raw)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"{name} must be an existing absolute executable path")
    result = subprocess.run(
        [str(path), "--version"], capture_output=True, text=True, timeout=10
    )
    if result.returncode or result.stdout.strip() != f"{name} 0.1.0":
        raise ValueError(f"{name} must report {name} 0.1.0")
    return path.resolve()


def select(packages: list[str]) -> tuple[Runner, ...]:
    """Deduplicate checked selectors and return inventory-order runners exactly once."""
    unknown = set(packages) - PACKAGES
    if unknown:
        raise ValueError("unknown package selector: " + ", ".join(sorted(unknown)))
    return tuple(
        runner
        for runner in RUNNERS
        if not packages or set(packages).intersection(runner.packages)
    )


def command(runner: Runner, binary: Path, fixture_base: Path) -> list[str]:
    """Return the explicit command for one inventory entry, including special CLIs."""
    args = [sys.executable, "-B", str(ROOT / runner.path)]
    if runner.name == "cursor-lifecycle":
        args += [
            "--transist",
            str(binary),
            "--fixture-base",
            str(fixture_base),
            "--candidate-lifecycle",
        ]
    elif runner.name == "lean-storage":
        args += ["--transist", str(binary), "--fixture-base", str(fixture_base)]
    elif runner.name == "git-mcp":
        args += ["--transist", str(binary)]
    return args


def main() -> int:
    """Run selected suites with explicit binaries and fresh fixtures; reject skips."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transist-bin", required=True)
    parser.add_argument("--admin-bin", required=True)
    parser.add_argument("--fixture-base", type=Path, required=True)
    parser.add_argument("--package", action="append", default=[])
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    try:
        runners = select(args.package)
        binary = executable(args.transist_bin, "transist")
        admin = executable(args.admin_bin, "transist-admin")
        base = args.fixture_base
        if not base.is_absolute() or not base.is_dir() or base.is_symlink():
            raise ValueError("fixture-base must be an existing absolute directory")
        base = base.resolve()
        if not __debug__ or os.environ.get("PYTHONOPTIMIZE"):
            raise ValueError("assertions must be enabled")
        for path in (binary, admin):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            print(
                f"BINARY {path.name} sha256={digest}",
                flush=True,
            )
        if args.list:
            for runner in runners:
                print(runner.name, runner.path, ",".join(runner.packages))
            return 0
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT"}
        }
        env.update(
            TRANSIST_BIN=str(binary),
            TRANSIST_ADMIN_BIN=str(admin),
            TRANSIST_FIXTURE_BASE=str(base),
            TMPDIR=str(base),
            TRANSIST_TEST_PACKAGES=",".join(dict.fromkeys(args.package)),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONNOUSERSITE="1",
        )
        if any(runner.name == "tika" for runner in runners):
            # Resolve while the parent context still exists. Forward only the
            # socket; all runners retain credential-free isolated HOME values.
            env["DOCKER_HOST"] = local_docker_host()
        failures = 0
        with tempfile.TemporaryDirectory(
            prefix="installed-checker-", dir=base
        ) as isolated_home:
            env["HOME"] = isolated_home
            for runner in runners:
                argv = command(runner, binary, base)
                print("RUN " + runner.name + ": " + " ".join(argv), flush=True)
                result = subprocess.run(
                    argv,
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=1200,
                )
                sys.stdout.write(result.stdout)
                sys.stderr.write(result.stderr)
                # unittest exits zero even when it skips its entire required suite.
                skipped = (
                    "skipped=" in result.stderr
                    or "SKIP:" in result.stdout
                    or "SKIP:" in result.stderr
                    or "Ran 0 tests" in result.stderr
                )
                failed = bool(result.returncode or skipped)
                failures += failed
                print(
                    f"{'FAIL' if failed else 'PASS'} {runner.name} "
                    f"exit={result.returncode}",
                    flush=True,
                )
        print(f"RESULT {len(runners) - failures} passed, {failures} failed")
        return int(bool(failures))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Installed checks failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
