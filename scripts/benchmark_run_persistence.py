#!/usr/bin/env python3
"""Measure labelled synthetic serialized storage using real, fake-provider handlers."""

from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from installed_fixture import TERMINAL, settle_runs  # noqa: E402
from inspect_run_storage import inspect_storage  # noqa: E402


def serialized_bytes(value: object) -> int:
    """Count UTF-8 JSON bytes with the Python APP emitter's default encoding."""
    return len(json.dumps(value, allow_nan=False).encode("utf-8"))


def contract_fixture() -> object:
    """Load canonical Codex handler fixtures without executing their test suite."""
    path = ROOT / "plugins/codex.cli/tests/test_lean_cli_contract.py"
    spec = importlib.util.spec_from_file_location("benchmark_cli_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LeanCliContractTests()


def measure_mode(iterations: int, depth: int, transcript_bytes: int, mode: str) -> dict:
    """Invoke real Codex handlers, then model history and latest-state projections.

    The synthetic parent projection forwards the complete child output at each
    level. Only the latest state at each level is retained; immutable records
    and reported hint contents accumulate. Workspace counts are actual fixture
    file sizes, not the runtime's disk allocation or serialization.
    """
    fixture = contract_fixture()
    payload = {
        "prompt": "Revise the fixture answer.",
        "response_file": ".transist/draft.md",
        "output_mode": mode,
        "history_mode": "invocation" if mode == "compact" else "legacy",
        "feedback_context_mode": "focused",
    }
    if mode == "compact":
        payload.update(max_context_file_bytes=65536, max_prompt_bytes=98304)
    transcript = "t" * transcript_bytes + "\n"
    answer = "Fixture answer\n\n## What Changed\n\n" + "a" * 512
    categories: Counter = Counter()
    history_bytes = 0
    history_files = 0
    last = {}
    with fixture.fixture("codex.cli", payload) as (module, _, request, workspace, _):
        for iteration in range(1, iterations + 1):
            request["invoke_id"] = f"fixture-{iteration:06d}"
            request.pop("_lean_context", None)
            frame, provider = fixture.invoke(
                module,
                request,
                stdout=transcript,
                stderr="fixture diagnostic",
                answer=answer,
            )
            if frame["status"] != "success" or provider.call_count != 1:
                raise AssertionError("fake-provider handler did not succeed")
            fixture.validator("codex.cli").validate(frame["output"])
            hints = frame["artifact_hints"]
            descriptors = []
            for hint in hints:
                count = len(hint["contents"].encode("utf-8"))
                categories["artifacts"] += count
                categories["artifact_files"] += 1
                if "interaction" in hint["name"]:
                    categories["cumulative_legacy_history_artifacts"] += count
                descriptors.append({"name": hint["name"], "bytes": count})
            categories["artifact_descriptors"] += serialized_bytes(descriptors)
            leaf = {
                "execution": iteration,
                "request": {"input": request["input"]},
                "result": {
                    "status": frame["status"],
                    "output": frame["output"],
                    "artifacts": descriptors,
                },
            }
            # This is an explicit forwarding assumption, not a runtime-schema
            # instance or a claim that every subflow repeats child outputs.
            projected = leaf
            for level in range(depth + 1):
                categories["immutable_executions"] += serialized_bytes(projected)
                last[level] = copy.deepcopy(projected)
                projected = {
                    "execution": iteration,
                    "result": {"child_output": projected["result"]},
                }
            categories["events"] += (
                serialized_bytes({"execution": iteration, "status": "success"}) + 1
            )
        categories["state_projections"] = sum(
            serialized_bytes({"last_result": projection})
            for projection in last.values()
        )
        categories["workspace"] = sum(
            path.stat().st_size for path in workspace.rglob("*") if path.is_file()
        )
        for path in workspace.rglob("*"):
            if path.is_file() and any(
                part == "invocations" or part.endswith(".invocations")
                for part in path.relative_to(workspace).parts[:-1]
            ):
                history_bytes += path.stat().st_size
                history_files += 1
        if mode == "legacy":
            history_bytes = sum(
                path.stat().st_size
                for path in workspace.rglob("*interaction.*")
                if path.is_file()
            )
            history_files = len(list(workspace.rglob("*interaction.*")))
    total = sum(
        categories[key]
        for key in (
            "immutable_executions",
            "state_projections",
            "artifacts",
            "artifact_descriptors",
            "events",
            "workspace",
        )
    )
    return {
        "serialized_storage_bytes": total,
        "components": dict(categories),
        "workspace_history_bytes": history_bytes,
        "workspace_history_files": history_files,
    }


def benchmark(
    iterations: int = 8, depth: int = 3, transcript_bytes: int = 96 * 1024
) -> dict:
    """Return synthetic mode comparisons; assert savings and linear compact history."""
    if not 2 <= iterations <= 64 or not 0 <= depth <= 16:
        raise ValueError("iterations must be 2..64 and depth 0..16")
    if not 4096 <= transcript_bytes <= 192 * 1024:
        raise ValueError("transcript bytes must be 4096..196608")
    legacy = measure_mode(iterations, depth, transcript_bytes, "legacy")
    compact = measure_mode(iterations, depth, transcript_bytes, "compact")
    doubled = measure_mode(iterations * 2, depth, transcript_bytes, "compact")
    savings = (
        1 - compact["serialized_storage_bytes"] / legacy["serialized_storage_bytes"]
    )
    growth = doubled["workspace_history_bytes"] / compact["workspace_history_bytes"]
    if savings < 0.75:
        raise AssertionError("transcript-heavy synthetic fixture saved less than 75%")
    if not 1.95 <= growth <= 2.05:
        raise AssertionError("compact replay history did not grow linearly")
    return {
        "kind": "synthetic_serialized_storage_not_runtime_disk",
        "provider": "mocked_codex_command_real_handler_and_output_schema",
        "assumptions": {
            "iterations": iterations,
            "contained_depth": depth,
            "transcript_bytes_per_invocation": transcript_bytes,
            "parent_projection": "complete_child_output_forwarding_at_each_level",
            "state_projection": "latest_only_at_each_level_not_all_rewrites",
            "artifact_promotion": "modeled_hints_once_plus_synthetic_descriptors",
            "legacy_hint_caps": "uncapped_model_not_installed_adapter_acceptance",
            "workspace": "actual_fixture_sizes_cumulative_legacy_or_minimal_replay",
            "excluded": (
                "runtime_evidence_recipe_provenance_gates_allocation_and_write_io"
            ),
        },
        "legacy": legacy,
        "compact": compact,
        "compact_double_iterations": doubled,
        "savings_fraction": savings,
        "compact_history_growth_ratio": growth,
        "acceptance": {"savings_at_least_75_percent": True, "linear_history": True},
        "installed_runtime_smoke": "not_run",
    }


def installed_smoke(binary: Path, fixture_base: Path) -> dict:
    """Dispatch two offline modes with an explicit binary in a disposable home.

    Copy selected package bytes into fixture storage and substitute only the
    provider transport; no account, operational home, signing key or network is
    used. Measure the resulting run trees separately from the synthetic model.
    """
    import yaml

    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("explicit executable required")
    if not fixture_base.is_dir():
        raise ValueError("existing explicit fixture base required")
    binary = binary.resolve()
    with tempfile.TemporaryDirectory(
        prefix="lean-benchmark-", dir=fixture_base.resolve()
    ) as temporary:
        root = Path(temporary)
        home, workspace, package, source = [
            root / name for name in ("home", "workspace", "package", "source")
        ]
        for directory in (workspace, package, source):
            directory.mkdir()
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(root / "provider-home"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }

        def cli(*arguments: str) -> str:
            """Use only this disposable home and bounded subprocess calls."""
            result = subprocess.run(
                [str(binary), "--home", str(home), *arguments, "--format", "json"],
                cwd=workspace,
                env=environment,
                capture_output=True,
                text=True,
                timeout=90,
            )
            if result.returncode:
                raise RuntimeError(
                    "installed fixture CLI failed: "
                    + " ".join(arguments[:2])
                    + f" (exit {result.returncode})"
                    + " "
                    + (result.stdout + result.stderr).replace(str(root), "<fixture>")[
                        :2048
                    ]
                )
            return result.stdout

        version = subprocess.run(
            [str(binary), "--version"],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        original = ROOT / "plugins/codex.cli"
        manifest = yaml.safe_load((original / "plugin.yaml").read_text())
        for relative in manifest["package"]["files"]:
            target = package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original / relative, target)
        manifest["entrypoint"]["command"] = "fixture.py"
        manifest["package"]["files"].append("fixture.py")
        manifest["prerequisites"] = {"binaries": [{"name": "python3"}]}
        (package / "plugin.yaml").write_text(yaml.safe_dump(manifest))
        wrapper = package / "fixture.py"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import subprocess\nfrom pathlib import Path\nimport plugin\n"
            "def fake(request, command, *args, **kwargs):\n"
            "    Path(request['paths']['artifacts_dir'], 'last_message.txt')"
            ".write_text('Fixture answer')\n"
            "    return subprocess.CompletedProcess(command, 0, 't' * 98304, '')\n"
            "plugin.run_command = fake\n"
            "plugin.extract_codex_usage = lambda _: None\n"
            "raise SystemExit(plugin.main())\n"
        )
        wrapper.chmod(0o755)
        cli("home", "init", "--mode", "standalone")
        try:
            cli("plugins", "install", "--path", str(package / "plugin.yaml"))
            inventories = {}
            for mode in ("legacy", "compact"):
                workflow_id = "benchmark_" + mode
                flow = {
                    "schema": "transist.flow.v1",
                    "id": workflow_id,
                    "version": "0.1.0",
                    "plugins": {"codex.cli": manifest["version"]},
                    "network": {"mode": "none"},
                    "workspace": {"path": str(workspace), "access": "full"},
                    "steps": [
                        {
                            "id": "invoke",
                            "uses": "process:codex.cli/exec_task",
                            "with": {
                                "prompt": "Fixture prompt",
                                "response_file": mode + ".md",
                                "output_mode": mode,
                                "history_mode": (
                                    "invocation" if mode == "compact" else "legacy"
                                ),
                            },
                        }
                    ],
                }
                flow_path = source / (workflow_id + ".yaml")
                flow_path.write_text(yaml.safe_dump(flow))
                cli("workflows", "check", "--path", str(flow_path))
                cli("workflows", "install", "--path", str(flow_path), "--approve")
                cli("workflows", "run", workflow_id + "@0.1.0", "--run-ref", mode)
                deadline = time.monotonic() + 60
                while True:
                    report = json.loads(cli("runs", "show", mode))
                    if report.get("status") in TERMINAL:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("installed fixture did not settle")
                    time.sleep(0.1)
                if report["status"] != "success":
                    raise RuntimeError("installed fake-provider run failed")
                output = report["steps"]["invoke"]["last_result"]["output"]
                if output["response_text"] != "Fixture answer":
                    raise AssertionError("installed run lost the authoritative answer")
                if mode == "compact" and (
                    output.get("output_mode") != "compact"
                    or "stdout" in output
                    or "stderr" in output
                ):
                    raise AssertionError(
                        "installed compact output retained inline logs"
                    )
                inventories[mode] = inspect_storage(home / "runs" / ("run_" + mode))
            with binary.open("rb") as handle:
                binary_digest = hashlib.file_digest(handle, "sha256").hexdigest()
            return {
                "status": "passed",
                "runtime_version": version,
                "runtime_binary_sha256": binary_digest,
                "scope": "two_single_invocations_no_nested_or_iteration_disk_claim",
                "payload": "fixture_copy_provider_transport_substituted_unsigned",
                "runs": inventories,
            }
        finally:
            settle_runs(home, cli)


def main() -> int:
    """Print evidence; installed mode requires an explicit binary and fixture base."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--transcript-bytes", type=int, default=96 * 1024)
    parser.add_argument("--transist", type=Path)
    parser.add_argument("--fixture-base", type=Path)
    args = parser.parse_args()
    if bool(args.transist) != bool(args.fixture_base):
        parser.error("installed smoke requires --transist and --fixture-base together")
    try:
        report = benchmark(args.iterations, args.depth, args.transcript_bytes)
        if args.transist:
            report["installed_runtime_smoke"] = installed_smoke(
                args.transist, args.fixture_base
            )
    except Exception as error:
        detail = (
            str(error)
            if isinstance(error, (RuntimeError, AssertionError, ValueError))
            else type(error).__name__
        )
        print("Benchmark failed; no acceptance claim: " + detail, file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
