#!/usr/bin/env python3
"""Exercise public installed workflow lifecycle in a disposable standalone home."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

from fixtures import seed, snapshot

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE.parents[1] / "tests"))
from installed_fixture import (  # noqa: E402
    TERMINAL,
    settle_runs,
    runtime_binary,
    fixture_base,
    json_args,
    remove_disposable_directory,
)  # noqa: E402


def main() -> None:
    """Install/check/authorize/run/inspect/uninstall using a local Transist CLI.

    Accepts --transist and --keep; creates only /tmp fixtures and local runtime
    state, uses no accounts or network, and raises on failed lifecycle checks.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transist", default=os.environ.get("TRANSIST_BIN"))
    parser.add_argument(
        "--keep", action="store_true", help="Keep the /tmp home and command logs"
    )
    args = parser.parse_args()
    if not args.transist:
        parser.error("No transist binary available; pass --transist /absolute/path")
    args.transist = (
        runtime_binary()
        if not args.transist
        else str(Path(args.transist).resolve(strict=True))
    )
    root = Path(tempfile.mkdtemp(prefix="git-mcp-e2e-", dir=fixture_base())).resolve()
    home = root / "home"
    repo = root / "repo"
    source = root / "source"
    source.mkdir()
    workflow = source / "workflow.yaml"
    shutil.copy2(PACKAGE / "workflow.yaml", workflow)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("TRANSIST_", "GIT_"))
    }
    env["TRANSIST_HOME"] = str(home)
    env["TMPDIR"] = str(root)

    def cli(*parts: str, allow_failure: bool = False) -> Any:
        command = [args.transist, "--home", str(home), *json_args(parts)]
        result = subprocess.run(
            command, cwd=repo, env=env, capture_output=True, text=True, timeout=90
        )
        print("$ " + " ".join(command), flush=True)
        print(result.stdout, end="", flush=True)
        print(result.stderr, end="", flush=True)
        if result.returncode and not allow_failure:
            raise RuntimeError(f"CLI exit {result.returncode}: {parts}")
        if parts[:2] in (("workflows", "check"), ("runs", "show")):
            return json.loads(result.stdout)
        return result.stdout

    print(f"E2E resources: {root}", flush=True)
    try:
        seed(repo)
        before = snapshot(repo)
        cli("home", "init", "--mode", "standalone")
        cli("plugins", "install", "--path", str(PACKAGE / "plugin.yaml"))
        cli("plugins", "list")
        cli("plugins", "inspect", "git.mcp")
        installed = home / "plugins/git.mcp/0.1.0"
        selected = {
            "plugin.yaml",
            "server.py",
            "process_lifecycle.py",
            *[
                str(path.relative_to(PACKAGE))
                for path in (PACKAGE / "schemas").glob("*.json")
            ],
        }
        assert {
            str(path.relative_to(installed))
            for path in installed.rglob("*")
            if path.is_file()
        } == selected
        cli("workflows", "check", "--path", str(workflow))
        cli(
            "workflows",
            "install",
            "--path",
            str(workflow),
            "--approve",
        )
        binding = ["git_mcp_demo@0.1.0", "--input", f"repo_path={repo}"]
        # Current 0.12 checks are validation-only unless --live is requested.
        # These probes run only the local MCP fixture, never external providers.
        first = cli(
            "workflows", "check", *binding, "--local-only", "--live", allow_failure=True
        )
        if (
            first.get("diagnostics", {})
            .get("authorization", {})
            .get("missing_capabilities", 0)
        ):
            cli("workflows", "authorize", *binding, "--approve")
        ready = cli("workflows", "check", *binding, "--local-only", "--live")
        assert ready["status"] == "ready", ready
        cli("workflows", "run", *binding, "--run-ref", "git-mcp-e2e", "--local-only")
        deadline = time.monotonic() + 30
        while True:
            state = cli("runs", "show", "git-mcp-e2e")
            if state["status"] in TERMINAL:
                break
            assert time.monotonic() < deadline, "Run completion timeout"
            time.sleep(0.2)
        assert state["status"] == "success", state
        assert state["steps"]["current_branch"]["last_result"]["output"] == {
            "branch": "main",
            "detached": False,
        }
        assert state["steps"]["list_branches"]["last_result"]["output"] == {
            "branches": ["alpha", "main", "zeta"],
            "truncated": False,
        }
        for step in state["steps"].values():
            assert step["status"] == "succeeded", step
            assert not step["last_result"]["artifacts"]
            assert not step["last_result"].get("usage")
        assert snapshot(repo) == before, "Inspection modified repository files"
        cli("plugins", "uninstall", "git.mcp", "--version", "0.1.0")
        assert not installed.exists()
        print(
            "PASS: selected payload, readiness, both installed MCP actions, "
            "unchanged repository, uninstall"
        )
    finally:
        settle_runs(
            home,
            lambda *args: (
                json.dumps(cli(*args)) if args[:2] == ("runs", "show") else cli(*args)
            ),
        )
        if args.keep:
            print(f"Retained E2E resources: {root}")
        else:
            remove_disposable_directory(root)


if __name__ == "__main__":
    main()
