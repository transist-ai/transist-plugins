#!/usr/bin/env python3
"""Selected installed Git/GitHub actions: native sandbox, fake gh, fixture Git only."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Iterator
import unittest

import yaml

from installed_fixture import (
    TERMINAL,
    disposable_directory,
    json_args,
    runtime_binary,
    settle_runs,
)
from review_action_fixtures import comment, fetch, valid_projected_locations

ROOT = Path(__file__).resolve().parents[1]
FAKE_GH = r"""#!/usr/bin/env python3
import json
from pathlib import Path
import sys
root = Path.cwd()
assert (root / '.fixture-workspace').is_file(), 'wrong canonical cwd'
args = sys.argv[1:]
assert args[:2] == ['pr', 'view'], 'unexpected command'
assert args.count('--json') == 1, 'unsupported JSON transport'
index = args.index('--json')
assert index == 2 and len(args) == 4, 'unknown flags'
fields = args[index + 1].split(',')
assert set(fields) <= {
 'number', 'title', 'state', 'url', 'headRefName', 'baseRefName', 'isDraft',
 'author', 'reviewDecision', 'mergedAt', 'latestReviews', 'mergeStateStatus',
 'mergeable', 'statusCheckRollup'}, 'unknown JSON fields'
with (root / '.fake-calls').open('a') as handle:
    handle.write('view\n')
if (root / '.fake-error').exists():
    print((root / '.fake-error').read_text(), file=sys.stderr)
    raise SystemExit(1)
print((root / '.fake-pr.json').read_text())
"""


def document(name: str, access: str) -> dict:
    """Bind process file data to a runtime-managed document revision."""
    return {"$document": name, "access": access}


class InstalledReviewActions(unittest.TestCase):
    """Install finite production bytes plus fake executable fixtures in fresh homes."""

    @contextmanager
    def fixture(self) -> Iterator[None]:
        """Require explicit runtime/admin and isolate every byte of test state."""
        runtime_binary(admin=True)
        with disposable_directory("review-actions-") as temporary:
            self.root = Path(temporary).resolve()
            self.home, self.workspace, self.source = [
                self.root / n for n in ("home", "workspace", "source")
            ]
            self.workspace.mkdir()
            self.source.mkdir()
            self.sequence = 0
            self.env = {
                "PATH": os.environ.get("PATH", os.defpath),
                "HOME": str(self.root),
                "TMPDIR": str(self.root),
                "PYTHONDONTWRITEBYTECODE": "1",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
            }
            (self.workspace / ".fixture-workspace").touch()
            subprocess.run(
                ["git", "init", "-q"],
                cwd=self.workspace,
                env=self.env,
                check=True,
                capture_output=True,
            )
            self.cli("home", "init", "--mode", "standalone")
            config = self.home / "config.yaml"
            config.write_text(
                config.read_text().replace("process: false", "process: true")
            )
            self.plugin_versions: dict[str, str] = {}
            for name in ("git.cli", "github.cli"):
                source = ROOT / "plugins" / name
                target = self.source / name
                target.mkdir()
                manifest = yaml.safe_load((source / "plugin.yaml").read_text())
                self.plugin_versions[name] = manifest["version"]
                for relative in manifest["package"]["files"]:
                    path = target / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source / relative, path)
                if name == "github.cli":
                    (target / "bin").mkdir()
                    gh = target / "bin/gh"
                    gh.write_text(FAKE_GH)
                    gh.chmod(0o755)
                    wrapper = target / "fixture.py"
                    wrapper.write_text(
                        "#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\n"
                        'binary_dir = str(Path(__file__).parent / "bin")\n'
                        'path = os.environ["PATH"]\n'
                        'os.environ["PATH"] = binary_dir + os.pathsep + path\n'
                        "import plugin\nraise SystemExit(plugin.main())\n"
                    )
                    wrapper.chmod(0o755)
                    manifest["entrypoint"]["command"] = "fixture.py"
                    manifest["package"]["files"] += ["fixture.py", "bin/gh"]
                    # Fake gh is selected payload, not a host prerequisite/login.
                    manifest["prerequisites"] = {"binaries": [{"name": "python3"}]}
                (target / "plugin.yaml").write_text(yaml.safe_dump(manifest))
                self.cli("plugins", "install", "--path", str(target / "plugin.yaml"))
                self.assertIn(
                    self.plugin_versions[name], self.cli("plugins", "inspect", name)
                )
                installed = self.home / "plugins" / name / self.plugin_versions[name]
                selected = {"plugin.yaml", *manifest["package"]["files"]}
                self.assertEqual(
                    {
                        p.relative_to(installed).as_posix()
                        for p in installed.rglob("*")
                        if p.is_file()
                    },
                    selected,
                )
                for relative in selected:
                    self.assertEqual(
                        (installed / relative).read_bytes(),
                        (target / relative).read_bytes(),
                    )
            self.assertIn("github.cli", self.cli("plugins", "list"))
            try:
                yield
            finally:
                settle_runs(self.home, self.cli)

    def cli(self, *args: str) -> str:
        """Run the explicit current runtime CLI with bounded subprocess lifetime."""
        result = subprocess.run(
            [runtime_binary(), "--home", str(self.home), *json_args(args)],
            cwd=self.workspace,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def run_flow(
        self,
        steps: list[dict],
        initial: dict[str, str] | None = None,
        expected: str = "success",
    ) -> dict:
        """Compile/check/install and execute typed actions with managed file staging."""
        self.sequence += 1
        run_id = f"review_actions_{self.sequence}"
        docs = {
            name: {"kind": "file", "max_bytes": 5 * 1024 * 1024, "initial": text}
            for name, text in (initial or {}).items()
        }
        docs.setdefault("out", {"kind": "file", "max_bytes": 1048576})
        flow = {
            "schema": "transist.flow.v1",
            "id": run_id,
            "version": "0.1.0",
            "plugins": {
                s["uses"]
                .split(":", 1)[1]
                .split("/", 1)[0]: self.plugin_versions[
                    s["uses"].split(":", 1)[1].split("/", 1)[0]
                ]
                for s in steps
                if "uses" in s
            },
            "workspace": {"path": str(self.workspace), "access": "full"},
            "network": {"mode": "none"},
            "documents": docs,
            "steps": steps,
        }
        path = self.source / (run_id + ".yaml")
        path.write_text(json.dumps(flow))
        print("CHECK/RUN", run_id, [s["id"] for s in steps], flush=True)
        self.cli("workflows", "check", "--path", str(path))
        self.cli("workflows", "install", "--path", str(path), "--approve")
        self.cli("workflows", "run", run_id + "@0.1.0", "--run-ref", run_id)
        self.run_root = self.home / "runs" / ("run_" + run_id)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            report = json.loads(self.cli("runs", "show", run_id))
            if report["status"] in TERMINAL:
                break
            time.sleep(0.05)
        self.assertEqual(report["status"], expected, json.dumps(report))
        for record in self.run_root.rglob("*.json"):
            if record.name in ("recipe.json", "inputs.json"):
                continue
            self.assertNotIn("/documents/read/binding-", record.read_text())
            self.assertNotIn("/documents/write/binding-", record.read_text())
        return report

    def blob(self, name: str) -> bytes:
        """Read the actual committed document revision for an assertion."""
        heads = json.loads((self.run_root / "documents/heads.json").read_text())
        return (
            self.run_root / "documents/blobs" / heads[name]["blob"]["sha256"]
        ).read_bytes()

    def test_typed_approval_mergeability_dataflow_and_conditions(self) -> None:
        """Both true and false stay JSON Booleans through APP and steps.out binding."""
        with self.fixture():
            for ready in (True, False):
                (self.workspace / ".fake-pr.json").write_text(
                    json.dumps(
                        {
                            "state": "OPEN",
                            "mergeStateStatus": "CLEAN" if ready else "BLOCKED",
                            "latestReviews": [],
                        }
                    )
                )
                report = self.run_flow(
                    [
                        {
                            "id": "merge",
                            "uses": "process:github.cli/pr_check_mergeability",
                            "with": {"output_mode": "compact"},
                        },
                        {
                            "id": "approval",
                            "uses": "process:github.cli/pr_check_approvals",
                            "with": {
                                "required_approvals": 0,
                                "soft_fail": "{{steps.merge.out.merge_ready}}",
                                "output_mode": "compact",
                            },
                        },
                        {
                            "id": "negative",
                            "uses": "process:github.cli/pr_check_approvals",
                            "with": {
                                "required_approvals": 2,
                                "soft_fail": "{{steps.approval.out.approval_ready}}",
                                "output_mode": "compact",
                            },
                            "set": {
                                "approved": "{{steps.approval.out.approval_ready}}"
                            },
                        },
                        {
                            "id": "branch",
                            "control": "control.noop",
                            "if": {
                                "guard": "vars.approved == true",
                                "then": [{"ref": "chosen"}],
                            },
                        },
                        {
                            "id": "chosen",
                            "uses": "process:github.cli/pr_check_mergeability",
                            "with": {"output_file": document("out", "write")},
                        },
                    ]
                )
                self.assertIs(
                    report["steps"]["merge"]["last_result"]["output"]["merge_ready"],
                    ready,
                )
                self.assertIs(
                    report["steps"]["approval"]["last_result"]["output"][
                        "approval_ready"
                    ],
                    True,
                )
                self.assertIs(
                    report["steps"]["negative"]["last_result"]["output"][
                        "approval_ready"
                    ],
                    False,
                )
                self.assertIs(json.loads(self.blob("out"))["merge_ready"], ready)

    def test_pre_and_post_gate_projection_and_incomplete_fetch(self) -> None:
        """Keep staged requests; incomplete/oversized fetches publish none."""
        with self.fixture():
            initial = {
                "raw": json.dumps(
                    fetch([comment("C"), comment("T", source="review_thread")])
                ),
                "later": json.dumps(
                    fetch(
                        [comment("new", created_at="2026-01-01T12:00:00Z")],
                        pr_conversation_comments_after="2026-01-01T11:00:00Z",
                    )
                ),
                "pr": '{"pull_request":{"state":"OPEN","number":7}}',
            }
            payload = {
                "input_file": document("raw", "read"),
                "pr_file": document("pr", "read"),
                "output_file": document("out", "write"),
                "output_mode": "compact",
            }
            self.run_flow(
                [
                    {
                        "id": "pre",
                        "uses": "process:github.cli/pr_prepare_review_context",
                        "with": payload,
                    },
                    {
                        "id": "post",
                        "uses": "process:github.cli/pr_prepare_review_context",
                        "with": {
                            **payload,
                            "input_file": document("later", "read"),
                            "pending_file": document("out", "read"),
                            "output_file": document("out", "write"),
                        },
                    },
                ],
                initial,
            )
            self.assertEqual(
                [c["id"] for c in json.loads(self.blob("out"))["comments"]],
                ["C", "new"],
            )
            self.assertEqual(self.blob("raw"), initial["raw"].encode())
            self.assertEqual(self.blob("later"), initial["later"].encode())
            self.assertEqual(
                json.loads(self.blob("out"))["comments"][0]["body"],
                comment("C")["body"],
            )
            before = json.dumps({"comments": [comment("pending")]})
            for raw in (
                json.dumps(fetch([], ok=False)),
                json.dumps(fetch([comment("big", body="é" * 32768)])),
                '{"comments":[],"comments":[{}]}',
            ):
                initial.update(raw=raw, out=before)
                self.run_flow(
                    [
                        {
                            "id": "bad",
                            "uses": "process:github.cli/pr_prepare_review_context",
                            "with": {
                                **payload,
                                "pending_file": document("out", "read"),
                                "output_file": document("out", "write"),
                            },
                        }
                    ],
                    initial,
                    expected="failed",
                )
                self.assertEqual(self.blob("out"), before.encode())
            self.assertFalse((self.workspace / ".fake-calls").exists())

    def test_scanner_uses_real_fixture_git_and_staged_output(self) -> None:
        """Git index/ignore/language rules execute in the runtime-selected workspace."""
        with self.fixture():
            for name, text in {
                "source.py": 'value = """\n# tst: quoted\n"""\n# tst: exact café\n',
                ".transist/old.md": "# tst: stale\n",
                "old.interaction.md": "# tst: stale\n",
            }.items():
                path = self.workspace / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
                subprocess.run(
                    ["git", "add", "-f", "--", name],
                    cwd=self.workspace,
                    env=self.env,
                    check=True,
                    capture_output=True,
                )
            report = self.run_flow(
                [
                    {
                        "id": "scan",
                        "uses": "process:git.cli/scan_instruction_comments",
                        "with": {
                            "output_file": document("out", "write"),
                            "output_mode": "compact",
                        },
                    }
                ]
            )
            self.assertIs(
                report["steps"]["scan"]["last_result"]["output"]["has_comments"], True
            )
            self.assertIn("source.py:4` -- exact café".encode(), self.blob("out"))
            self.assertNotIn(b"stale", self.blob("out"))
            (self.workspace / "source.py").write_text("# tst: " + "é" * 2049 + "\n")
            self.run_flow(
                [
                    {
                        "id": "overflow",
                        "uses": "process:git.cli/scan_instruction_comments",
                        "with": {"output_file": document("out", "write")},
                    }
                ],
                {"out": "previous complete context"},
                expected="failed",
            )
            self.assertEqual(self.blob("out"), b"previous complete context")

    def test_invalid_projected_locations_preserve_workspace_and_documents(self) -> None:
        """Reject bad locations before workspace replacement or document publish."""
        cases = [
            ("line", "12"),
            ("original_line", "12"),
            ("side", 12),
            ("start_line", "12"),
            ("diff_hunk_header", 12),
            ("line", True),
            ("original_line", False),
            ("start_line", True),
            ("side", False),
            ("diff_hunk_header", None),
        ]
        with self.fixture():
            pending = {
                "comments": [comment("pending")],
                "count": 1,
                "pull_request": {"number": 7},
            }
            before = (json.dumps(pending, ensure_ascii=False, indent=2) + "\n").encode()
            workspace_pending = self.workspace / "pending.json"
            for field, value in cases:
                for destination in ("workspace-alias", "managed"):
                    print(
                        "P2 LOCATION",
                        field,
                        type(value).__name__,
                        destination,
                        flush=True,
                    )
                    workspace_pending.write_bytes(before)
                    initial = {
                        "raw": json.dumps(
                            fetch(
                                [
                                    comment(
                                        "fresh",
                                        source="review_thread",
                                        **{field: value},
                                    )
                                ]
                            )
                        ),
                        "pr": '{"pull_request":{"state":"OPEN","number":7}}',
                        "pending": before.decode(),
                    }
                    payload = {
                        "input_file": document("raw", "read"),
                        "pr_file": document("pr", "read"),
                    }
                    if destination == "workspace-alias":
                        payload.update(
                            pending_file="pending.json",
                            output_file="pending.json",
                            output_mode="legacy",
                        )
                    else:
                        payload.update(
                            pending_file=document("pending", "read"),
                            output_file=document("out", "write"),
                            output_mode="compact",
                        )
                    report = self.run_flow(
                        [
                            {
                                "id": "bad_location",
                                "uses": "process:github.cli/pr_prepare_review_context",
                                "with": payload,
                            },
                        ],
                        initial,
                        expected="failed",
                    )
                    result = report["steps"]["bad_location"]["last_result"]
                    self.assertEqual(result["error"]["code"], "validation_error")
                    self.assertEqual(
                        result["error"]["message"],
                        "Review projection failed; pending requests were not discarded",
                    )
                    self.assertEqual(workspace_pending.read_bytes(), before)
                    self.assertEqual(self.blob("pending"), before)
                    self.assertNotIn(
                        "out",
                        json.loads(
                            (self.run_root / "documents/heads.json").read_text()
                        ),
                    )
            self.assertFalse((self.workspace / ".fake-calls").exists())

    def test_valid_projected_locations_match_current_runtime_schema(self) -> None:
        """Native validation accepts nullable/integral locations without new bounds."""
        with self.fixture():
            locations = valid_projected_locations()
            current = fetch(
                [
                    comment(str(index), source="review_thread", **fields)
                    for index, fields in enumerate(locations)
                ]
            )
            report = self.run_flow(
                [
                    {
                        "id": "valid_locations",
                        "uses": "process:github.cli/pr_prepare_review_context",
                        "with": {
                            "input_file": document("raw", "read"),
                            "pr_file": document("pr", "read"),
                            "output_file": document("out", "write"),
                            "output_mode": "compact",
                        },
                    },
                ],
                {"raw": json.dumps(current), "pr": '{"pull_request":{"state":"OPEN"}}'},
            )
            projected = json.loads(self.blob("out"))
            self.assertEqual(
                report["steps"]["valid_locations"]["last_result"]["output"]["count"],
                len(locations),
            )
            for original, saved in zip(locations, projected["comments"]):
                for field, value in original.items():
                    self.assertEqual(saved[field], value)
            self.assertFalse((self.workspace / ".fake-calls").exists())

    def test_unknown_gh_flags_fail_and_false_checks_do_not_mask_transport_errors(
        self,
    ) -> None:
        """Unknown flags fail, even when approval checking requests soft failure."""
        with self.fixture():
            (self.workspace / ".fake-error").write_text("unknown flag: --json")
            for action, extra in (
                ("pr_check_mergeability", {}),
                ("pr_check_approvals", {"required_approvals": 0, "soft_fail": True}),
            ):
                report = self.run_flow(
                    [
                        {
                            "id": "error",
                            "uses": "process:github.cli/" + action,
                            "with": {"output_mode": "compact", **extra},
                        }
                    ],
                    expected="failed",
                )
                self.assertEqual(
                    report["steps"]["error"]["last_result"]["error"]["code"],
                    "tool_execution_error",
                )
            self.assertEqual(
                (self.workspace / ".fake-calls").read_text().splitlines(),
                ["view", "view"],
            )


if __name__ == "__main__":
    runtime_binary()
    runtime_binary(admin=True)
    unittest.main(verbosity=2)
