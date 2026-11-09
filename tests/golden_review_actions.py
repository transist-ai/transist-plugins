#!/usr/bin/env python3
"""Explicit extraction parity against pinned local Git objects; no provider calls.

Only the fixed known helper paths at WORKFLOW_BASELINE are imported, from a
temporary fixture directory. Neither authored workflow code nor current sibling
worktree bytes are executed. Helper CLI entrypoints are never launched.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType
from unittest.mock import patch

import support
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from review_action_fixtures import (
    comment,
    fetch,
    merge_cases,
    invalid_projected_locations,
    valid_projected_locations,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_BASELINE = "88e6871cd8155b2d021e8fc1420685b273b7e5ea"
PLUGIN_BASELINE = "86d375c6459a916fa1a15d55c14de285f477dbcb"


def git_bytes(repo: Path, revision: str, path: str) -> bytes:
    """Read one exact pinned object from a local repository, without checkout writes."""
    return subprocess.check_output(["git", "show", f"{revision}:{path}"], cwd=repo)


def load(path: Path, name: str, data: bytes) -> ModuleType:
    """Import trusted pinned bytes under a non-main name inside disposable fixtures."""
    path.write_bytes(data)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    print("REFERENCE", name, "sha256=" + hashlib.sha256(data).hexdigest(), flush=True)
    return module


def invoke(
    plugin: ModuleType, root: Path, action: str, payload: dict, pr: dict
) -> dict:
    """Invoke real production handling with only the gh process response mocked."""
    request = support.app_invoke_request(action, payload, str(root))
    request["paths"]["workspace_dir"] = str(root)
    completed = subprocess.CompletedProcess([], 0, json.dumps(pr), "")
    with patch.object(plugin, "run_process", return_value=completed):
        return plugin.handle_request(request)


def main() -> int:
    """Compare decisions, request bytes and failures, printing a finite ledger."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow-repo", required=True, type=Path)
    parser.add_argument("--fixture-base", required=True, type=Path)
    args = parser.parse_args()
    if not args.fixture_base.is_absolute() or not args.fixture_base.is_dir():
        parser.error("fixture-base must be an existing absolute directory")
    gh = support.load_plugin_module(ROOT / "plugins/github.cli")
    git = support.load_plugin_module(ROOT / "plugins/git.cli")
    with tempfile.TemporaryDirectory(
        prefix="golden-review-", dir=args.fixture_base
    ) as temporary:
        root = Path(temporary).resolve()
        helpers = {}
        for name in (
            "check_pr_mergeability",
            "prepare_review_context",
            "scan_tst_comments",
        ):
            relative = f"examples/hotfix_ticket_delivery/modules/scripts/{name}.py"
            data = git_bytes(args.workflow_repo, WORKFLOW_BASELINE, relative)
            if name != "prepare_review_context":
                other = git_bytes(
                    args.workflow_repo,
                    WORKFLOW_BASELINE,
                    relative.replace("hotfix_", "cursor_"),
                )
                assert data == other, "delivery helper copies differ"
            helpers[name] = load(root / (name + ".py"), "reference_" + name, data)
        old_plugin = load(
            root / "plugin.py",
            "reference_github",
            git_bytes(ROOT, PLUGIN_BASELINE, "plugins/github.cli/plugin.py"),
        )
        old_merge = helpers["check_pr_mergeability"]
        for index, (pr, statuses) in enumerate(merge_cases()):
            completed = subprocess.CompletedProcess([], 0, json.dumps(pr), "")
            path = root / "merge.json"
            argv = [
                "helper",
                "--repo-path",
                str(root),
                "--output-file",
                str(path),
                "--soft-fail",
            ]
            for status in statuses:
                argv.extend(["--allowed-merge-state-status", status])
            with patch.object(sys, "argv", argv), patch.object(
                old_merge, "fetch_pull_request", return_value=(completed, pr, None)
            ), contextlib.redirect_stdout(io.StringIO()):
                assert old_merge.main() == 0
            old = json.loads(path.read_bytes())
            new = invoke(
                gh,
                root,
                "pr_check_mergeability",
                {"allowed_merge_state_statuses": statuses},
                pr,
            )
            assert new["status"] == "success"
            assert new["output"] == old, (old, new)
            print(
                "PARITY merge",
                index,
                "ready=",
                old["merge_ready"],
                "reasons=",
                old["reasons"],
            )
        for index, reviews in enumerate(
            (
                [],
                [
                    {"author": {"login": "A"}, "state": "APPROVED", "submittedAt": "2"},
                    {
                        "author": {"login": "a"},
                        "state": "DISMISSED",
                        "submittedAt": "3",
                    },
                    {"author": {"login": "B"}, "state": "APPROVED", "submittedAt": "1"},
                    {"author": {"login": "author"}, "state": "APPROVED"},
                ],
            )
        ):
            for state in ("OPEN", "MERGED"):
                for required in (0, 1, 2):
                    pr = {
                        "state": state,
                        "author": {"login": "author"},
                        "latestReviews": reviews,
                    }
                    old = invoke(
                        old_plugin,
                        root,
                        "pr_check_approvals",
                        {"required_approvals": required},
                        pr,
                    )
                    new = invoke(
                        gh,
                        root,
                        "pr_check_approvals",
                        {"required_approvals": required},
                        pr,
                    )
                    assert new["output"].pop("approval_ready") is old["output"]["ok"]
                    assert old == new
                    print(
                        "PARITY approvals", index, state, required, old["output"]["ok"]
                    )
        old_review = helpers["prepare_review_context"]
        pr = {"pull_request": {"number": 7, "state": "OPEN"}, "stdout": "duplicate"}
        initial = fetch([comment("C"), comment("T", source="review_thread")])
        pending = old_review.project(initial, {}, pr)
        cases = [
            (initial, {}),
            (fetch([], pr_conversation_comments_after="2026-01-01T11:00:00Z"), pending),
            (
                fetch(
                    [
                        comment("C", body="!!! changed"),
                        comment("C", body="!!! changed"),
                    ],
                    pr_conversation_comments_after="2026-01-01T11:00:00Z",
                ),
                pending,
            ),
            (
                fetch(
                    [
                        comment(
                            "T",
                            source="review_thread",
                            body="edited thread",
                            thread_id="T",
                            review_comment_id="23",
                            diff_hunk="@@ -1 +2 @@\nrepeated",
                        )
                    ]
                ),
                pending,
            ),
            (
                fetch(
                    [
                        comment("slash", body="/retry"),
                        comment("bot", body="<!-- transist-review-reply -->"),
                        comment("mandatory", body="!!! /fix"),
                    ]
                ),
                {},
            ),
        ]
        for index, (current, pending) in enumerate(cases):
            old = old_review.project(current, pending, pr)
            for name, value in (("raw", current), ("pending", pending), ("pr", pr)):
                (root / (name + ".json")).write_text(json.dumps(value))
            request = support.app_invoke_request(
                "pr_prepare_review_context",
                {
                    "input_file": str(root / "raw.json"),
                    "pending_file": str(root / "pending.json"),
                    "pr_file": str(root / "pr.json"),
                    "output_file": str(root / "out.json"),
                },
                str(root),
            )
            request["paths"]["workspace_dir"] = str(root)
            new = gh.handle_request(request)
            assert new["status"] == "success"
            assert {k: new["output"][k] for k in old} == old
            assert (root / "out.json").read_bytes() == old_review.encode(old)
            print("PARITY review", index, [c["id"] for c in old["comments"]])
        failures = [
            fetch([], ok=False),
            fetch([], exit_code=1),
            fetch([], count=True),
            fetch([comment("", body="bad")]),
            fetch([comment("large", body="é" * 32768)]),
        ]
        for field in fetch([]):
            value = fetch([])
            value.pop(field)
            failures.append(value)
        for index, current in enumerate(failures):
            for module in (old_review, gh.review_context):
                try:
                    module.project(current, {}, pr)
                except ValueError:
                    pass
                else:
                    raise AssertionError("invalid fetch accepted")
            print("PARITY review rejected", index)
        schema = support.read_json(
            ROOT
            / "plugins/github.cli/schemas/pr_prepare_review_context.output.schema.json"
        )
        compact = support.read_json(
            ROOT / "plugins/github.cli/schemas/compact.schema.json"
        )
        check = Draft202012Validator(
            schema,
            registry=Registry().with_resource(
                "compact.schema.json", Resource.from_contents(compact)
            ),
        )
        original = old_review.project(fetch([comment("pending")]), {}, pr)
        before = old_review.encode(original)
        for field, value in invalid_projected_locations():
            current = fetch(
                [comment("fresh", source="review_thread", **{field: value})]
            )
            old = old_review.project(current, original, pr)
            assert not check.is_valid({"ok": True, **old})
            for name, data in (("raw", current), ("pending", original), ("pr", pr)):
                (root / (name + ".json")).write_bytes(old_review.encode(data))
            request = support.app_invoke_request(
                "pr_prepare_review_context",
                {
                    "input_file": str(root / "raw.json"),
                    "pending_file": str(root / "pending.json"),
                    "pr_file": str(root / "pr.json"),
                    "output_file": str(root / "pending.json"),
                },
                str(root),
            )
            request["paths"]["workspace_dir"] = str(root)
            new = gh.handle_request(request)
            assert (
                new["status"] == "failed" and new["error"]["code"] == "validation_error"
            )
            assert (root / "pending.json").read_bytes() == before
            print(
                "STRICT SCHEMA BOUNDARY",
                field,
                type(value).__name__,
                "old=accepted-schema-invalid new=rejected pending=unchanged",
            )
        for index, fields in enumerate(valid_projected_locations()):
            current = fetch([comment("located", **fields)])
            old = old_review.project(current, {}, pr)
            new = gh.review_context.project(current, {}, pr)
            assert check.is_valid({"ok": True, **new}) and old == new
            assert old_review.encode(old) == gh.review_context.encode(new)
            print("PARITY nullable/integral/unbounded locations", index)
        replaced = comment("C", line="malformed")
        filtered_cases = [
            (
                fetch([]),
                {"comments": [comment("T", source="review_thread", line="malformed")]},
            ),
            (fetch([comment("slash", body="/retry", line="malformed")]), {}),
            (fetch([replaced, comment("C")]), {}),
            (fetch([comment("C")]), {"comments": [replaced]}),
            (
                fetch(
                    [comment("T", diff_hunk="@@ -1 +2 @@\nbody", diff_hunk_header=None)]
                ),
                {},
            ),
        ]
        for index, (current, pending) in enumerate(filtered_cases):
            old = old_review.project(current, pending, pr)
            new = gh.review_context.project(current, pending, pr)
            assert check.is_valid({"ok": True, **new}) and old == new
            print("PARITY final projection filtering/supersession", index)
        old_scan = helpers["scan_tst_comments"]
        source = root / "workspace"
        source.mkdir()
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        subprocess.run(
            ["git", "init", "-q", str(source)], env=env, check=True, capture_output=True
        )
        texts = {
            "source.txt": (
                "# tst: hash\n// tst: slash\n-- tst: sql\n/* tst: block */\n"
                '<!-- tst: html -->\n"# tst: quoted"\nx = 1 # tst: trailing\n'
            ),
            "source.py": 'value = """\n# tst: quoted\n"""\n# tst: actual\n',
            "source.js": "const value = `\n// tst: quoted\n`;\n// tst: actual\n",
            "source.ex": 'value = """\n# tst: quoted\n"""\n# tst: actual\n',
            "source.sql": "SELECT '\n-- tst: quoted\n';\n-- tst: actual\n",
            ".transist/old.md": "# tst: stale\n",
            "old.interaction.md": "# tst: stale\n",
            "binary.bin": "\0# tst: binary\n",
        }
        for name, text in texts.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        subprocess.run(
            ["git", "add", "-f", "."],
            cwd=source,
            env=env,
            check=True,
            capture_output=True,
        )
        old_hits = [
            hit
            for path in old_scan.tracked_files(source)
            for hit in old_scan.scan_file(source, path)
        ]
        new_hits = [
            hit
            for path in git.instruction_comments.tracked_files(source)
            for hit in git.instruction_comments.scan_file(source, path)
        ]
        assert old_hits == new_hits
        assert old_scan.render_output(
            old_hits
        ) == git.instruction_comments.render_output(new_hits)
        request = support.app_invoke_request(
            "scan_instruction_comments",
            {"output_file": str(root / "scan.md")},
            str(root),
        )
        request["paths"]["workspace_dir"] = str(source)
        result = git.handle_request(request)
        assert result["status"] == "success" and result["output"]["count"] == len(
            old_hits
        )
        assert (root / "scan.md").read_bytes() == old_scan.render_output(old_hits)
        print(
            "PARITY scanner",
            len(old_hits),
            "exact located requests / byte-identical Markdown",
        )
    print(
        "PASS golden extraction parity; workflow="
        + WORKFLOW_BASELINE
        + " plugins="
        + PLUGIN_BASELINE
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
