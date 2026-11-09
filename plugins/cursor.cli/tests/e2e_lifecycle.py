#!/usr/bin/env python3
"""Offline installed-runtime lifecycle checks; stdlib only, no plugin invocation."""

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

PACKAGE = Path(__file__).resolve().parents[1]
SELECTED = (
    "plugin.yaml",
    "plugin.py",
    "gate_review.py",
    "lean_policy.py",
    "process_lifecycle.py",
    "schemas/exec_task.input.schema.json",
    "schemas/gate-decision.schema.json",
    "schemas/exec_task.output.schema.json",
)
SENTINEL = "offline-cursor-key-sentinel-never-report"
TERMINAL = {"success", "failed", "canceled"}
MODES = (
    "success",
    "child",
    "pipes",
    "resistant",
    "timeout",
    "failure",
    "cancel",
    "api",
    "leader",
    "orphan",
)

# This executable is the provider boundary, not a wrapper around plugin.py.
PROVIDER = r"""
import json, os, signal, socket, sys, time
from pathlib import Path
root = Path(sys.argv[sys.argv.index("--workspace") + 1])
mode = next(arg.split("=", 1)[1] for arg in sys.argv if arg.startswith("--fixture="))
def record(name):
    (root / (name + ".pid")).write_text(json.dumps([os.getpid(), os.getpgrp()]))
record("provider")
api = mode == "api"
assert bool(os.environ.get("CURSOR_API_KEY")) == api
assert bool(os.environ.get("HOME")) == api
assert bool(os.environ.get("CURSOR_CONFIG_DIR")) == api
assert bool(os.environ.get("CURSOR_DATA_DIR")) == api
assert Path(os.environ["TMPDIR"]).is_dir()
port = int(next(arg.split("=", 1)[1] for arg in sys.argv
                if arg.startswith("--probe-port=")))
try:
    connection = socket.create_connection(("127.0.0.1", port), timeout=.3)
except OSError:
    (root / "network-denied").write_text("denied")
else:
    connection.close()
    raise AssertionError("network unexpectedly permitted")
if mode in {"child", "pipes", "resistant", "timeout", "cancel"}:
    if os.fork() == 0:
        if mode in {"resistant", "cancel"}:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if mode == "child":
            os.close(1)
            os.close(2)
        record("child")
        if mode == "pipes":
            os.write(1, b"ordinary inherited pipe output")
        while True:
            time.sleep(.05)
    while not (root / "child.pid").exists():
        time.sleep(.01)
print(json.dumps({"type":"system", "subtype":"init", "model":"offline-fixture",
                  "session_id":"fixture-session"}), flush=True)
print(json.dumps({"type":"result", "result":"safe fixture response",
                  "session_id":"fixture-session",
                  "usage":{"inputTokens":3,"outputTokens":2,"totalTokens":5}}),
      flush=True)
if api:
    print(os.environ["CURSOR_API_KEY"], flush=True)
    print(os.environ["CURSOR_API_KEY"], file=sys.stderr, flush=True)
else:
    print("safe fixture stderr", file=sys.stderr, flush=True)
if mode in {"timeout", "cancel"}:
    time.sleep(120)
sys.exit(7 if mode == "failure" else 0)
"""

GENERIC = r"""
import json, os, signal, sys, time
from pathlib import Path
def emit(frame):
    print(json.dumps(frame), flush=True)
json.loads(sys.stdin.readline())
emit({"type":"app.handshake.response.v1", "protocol_version":"app.v1",
      "plugin":{"id":"fixture.lifecycle", "version":"0.1.0"},
      "adapter_kind":"process", "capabilities":{"actions":["execute"],
      "supports_cancel":False,"supports_health":False,
      "supports_artifact_hints":True,"supports_evidence_hints":False}})
request = json.loads(sys.stdin.readline())
root = Path(request["paths"]["workspace_dir"])
mode = request["input"]["mode"]
(root / "provider.pid").write_text(json.dumps([os.getpid(), os.getpgrp()]))
if os.fork() == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / "child.pid").write_text(json.dumps([os.getpid(), os.getpgrp()]))
    while True:
        time.sleep(.05)
while not (root / "child.pid").exists():
    time.sleep(.01)
emit({"type":"app.invoke.result.v1", "protocol_version":"app.v1",
      "invoke_id":request["invoke_id"], "status":"success",
      "output":{"response":"ordinary generic output"},
      "artifact_hints":[{"name":"ordinary.txt","contents":"ordinary hint"}],
      "evidence_hints":[],"metrics":{}})
if mode == "leader":
    time.sleep(120)
"""


def require(condition: bool, message: str) -> None:
    """Raise a safe assertion without printing fixture values or raw reports."""
    if not condition:
        raise AssertionError(message)


def secret_free(text: str) -> None:
    """Reject raw/base64 sentinel leakage before any CLI diagnostics are printed."""
    require(
        SENTINEL not in text
        and base64.b64encode(SENTINEL.encode()).decode() not in text,
        "secret sentinel leaked (content suppressed)",
    )


def objects(value: object) -> list[dict]:
    """Collect nested report objects without relying on CLI presentation paths."""
    if isinstance(value, dict):
        return [value] + [item for v in value.values() for item in objects(v)]
    if isinstance(value, list):
        return [item for v in value for item in objects(v)]
    return []


def native_processes() -> object:
    """Load only the package's native observer; never call its APP entrypoint."""
    spec = importlib.util.spec_from_file_location(
        "cursor_fixture_native", PACKAGE / "process_lifecycle.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.NativeProcesses()


def executable(raw: str) -> Path:
    """Require an explicit real executable path, without PATH lookup or fallback."""
    path = Path(raw).expanduser().resolve()
    require(path.is_file() and os.access(path, os.X_OK), "runtime executable missing")
    return path


def fixture_interpreter() -> Path:
    """Use the real system interpreter, not a virtualenv alias hidden by sandboxing."""
    return Path(sys.executable).resolve(strict=True)


def safe_error(error: Exception) -> str:
    """Return fixed diagnostics; never render raw subprocess/JSON exceptions."""
    message = str(error) if isinstance(error, AssertionError) else type(error).__name__
    secret_free(message)
    return message


class Driver:
    """Own one disposable home/workspace and all external fixture processes."""

    def __init__(self, binary: Path, root: Path) -> None:
        self.binary = binary
        self.root = root
        self.home = root / "home"
        self.workspace = root / "workspace"
        self.source = root / "source"
        self.workspace.mkdir()
        self.source.mkdir()
        self.native = native_processes()
        self.identities = {}
        self.runners = {}
        self.runs = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.setblocking(False)
        # Deliberately do not copy host agent, account or policy environment.
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(root / "operator"),
            "TMPDIR": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CURSOR_AGENT_PATH": str(self.workspace / "fake-cursor"),
            "CURSOR_API_KEY": SENTINEL,
        }
        (root / "operator").mkdir()

    def cli(self, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
        """Run the selected CLI with an explicit home and a bounded timeout."""
        result = subprocess.run(
            [
                str(self.binary),
                "--home",
                str(self.home),
                *args,
                *([] if args[0].startswith("--") else ["--format", "json"]),
            ],
            cwd=self.workspace,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        secret_free(result.stdout + result.stderr)
        require(not ok or result.returncode == 0, f"CLI {args[0]} failed")
        return result

    def initialize(self) -> None:
        """Install exact unmodified 0.1.0 bytes and permit only fixture env delivery."""
        self.cli("home", "init", "--mode", "standalone")
        config = self.home / "config.yaml"
        text = config.read_text()
        require("process: false" in text, "unexpected home secret policy")
        text = text.replace("process: false", "process: true")
        require("child_env:" not in text, "unexpected home child-env policy")
        text = text.replace(
            "policy:\n",
            "policy:\n  child_env:\n    inherit: false\n"
            "    allowlist: [PATH, CURSOR_AGENT_PATH]\n",
            1,
        )
        config.write_text(text)
        fake = self.workspace / "fake-cursor"
        fake.write_text(f"#!{fixture_interpreter()}\n" + PROVIDER)
        fake.chmod(0o755)
        self.cli("plugins", "install", "--path", str(PACKAGE / "plugin.yaml"))
        self.cli("plugins", "list")
        inspection = self.cli("plugins", "inspect", "cursor.cli").stdout
        require("0.1.0" in inspection, "installed version differs")
        manifests = list((self.home / "plugins").rglob("plugin.yaml"))
        require(len(manifests) == 1, "unexpected installed package count")
        installed = manifests[0].parent
        for relative in SELECTED:
            require(
                (installed / relative).read_bytes()
                == (PACKAGE / relative).read_bytes(),
                "installed selected payload differs",
            )
        require(not (installed / "tests").exists(), "unselected tests installed")

    def capture(self) -> None:
        """Track verified birth identities of fixture processes and their boundary."""
        for path in self.workspace.glob("*.pid"):
            try:
                pid, pgid = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            info = self.native.read(pid)
            if info is not None:
                require(info.pgid == pgid, "fixture escaped invocation group")
                require(pgid != os.getpgrp(), "fixture shares caller process group")
                self.identities.setdefault(pid, info)
                for member in self.native.members(pgid):
                    self.identities.setdefault(member.pid, member)

    def install_flow(self, mode: str, generic: bool = False) -> str:
        """Install a network-denied workflow and check input-bound readiness."""
        name = "lifecycle_" + mode
        action = "exec_task" if mode == "api" else "exec_task_cli"
        values = {
            "extra_args": [
                "--fixture=" + mode,
                "--probe-port=" + str(self.listener.getsockname()[1]),
            ],
            "response_file": "response.txt",
            "timeout_seconds": 1 if mode == "timeout" else 30,
            "timeout_ms": 10000,
        }
        if mode == "api":
            values["secret_refs"] = ["CURSOR_API_KEY"]
        plugin = "fixture.lifecycle" if generic else "cursor.cli"
        step = {
            "id": "execute",
            "uses": f"process:{plugin}/{'execute' if generic else action}",
            "with": {"mode": mode, "timeout_ms": 2500} if generic else values,
        }
        if not generic:
            step["prompt"] = "{{inputs.prompt}}"
        flow = {
            "schema": "transist.flow.v1",
            "id": name,
            "version": "0.1.0",
            "plugins": {plugin: "0.1.0"},
            "workspace": {"path": str(self.workspace), "access": "full"},
            "network": {"mode": "none"},
            "inputs": {"prompt": {"type": "string", "required": True}},
            "steps": [step],
        }
        # Linux mounts recipe source read-only: it must not cover runtime staging.
        path = self.source / f"{name}.yaml"
        path.write_text(
            json.dumps(flow)
        )  # JSON is a YAML subset; no PyYAML dependency.
        self.cli("workflows", "check", "--path", str(path))
        self.cli("workflows", "install", "--path", str(path), "--approve")
        flags = (name + "@0.1.0", "--input", "prompt=offline fixture")
        result = self.cli("workflows", "check", *flags, "--live", ok=False)
        report = json.loads(result.stdout)
        if report["status"] != "ready":
            blocking = (
                report.get("diagnostics", {})
                .get("authorization", {})
                .get("blocking_codes", [])
            )
            require(
                report.get("diagnostics", {}).get("authorization", {}).get("eligible"),
                "readiness blocked beyond exact missing authority: "
                + ", ".join(str(code) for code in blocking),
            )
            self.cli("workflows", "authorize", *flags, "--approve")
            report = json.loads(self.cli("workflows", "check", *flags, "--live").stdout)
        require(report["status"] == "ready", "installed workflow not ready")
        return name

    def run(self, mode: str, generic: bool = False) -> dict:
        """Start detached execution, cancel when requested, and poll terminal state."""
        name = self.install_flow(mode, generic)
        self.runs.append(name)
        launch = self.cli(
            "workflows",
            "run",
            name + "@0.1.0",
            "--run-ref",
            name,
            "--input",
            "prompt=offline fixture",
        )
        announcement = json.loads(launch.stdout)
        require(
            announcement.get("schema") == "transist.runner_spawned.v1",
            "detached runner announcement missing",
        )
        runner = self.native.read(announcement["runner_pid"])
        if runner is not None:
            self.runners[runner.pid] = runner
        deadline = time.monotonic() + 18
        canceled = False
        while time.monotonic() < deadline:
            self.capture()
            if (
                mode == "cancel"
                and not canceled
                and (self.workspace / "child.pid").exists()
            ):
                self.cli("runs", "cancel", name)
                canceled = True
            report = json.loads(self.cli("runs", "show", name).stdout)
            if report.get("status") in TERMINAL:
                require(mode != "cancel" or canceled, "cancel fixture completed early")
                self.assert_dead()
                return report
            time.sleep(0.05)
        raise AssertionError(
            "detached runner did not reach terminal state within budget"
        )

    def assert_dead(self) -> None:
        """Confirm every recorded provider/child and tracked owned PID is not live."""
        self.capture()
        for identity in self.identities.values():
            current = self.native.read(identity.pid)
            require(
                current is None or current.birth != identity.birth or not current.live,
                "live owned PID at terminal state",
            )

    def close(self) -> None:
        """Cancel active runs and kill only birth-verified external fixture members."""
        try:
            for name in self.runs:
                report = json.loads(self.cli("runs", "show", name).stdout)
                if report.get("status") not in TERMINAL:
                    self.cli("runs", "cancel", name, ok=False)
            self.capture()
        finally:
            self.listener.close()
            for identity in self.identities.values():
                self.native.send(identity, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    self.assert_dead()
                    live_runners = [
                        identity
                        for identity in self.runners.values()
                        if (current := self.native.read(identity.pid)) is not None
                        and current.birth == identity.birth
                        and current.live
                    ]
                    if not live_runners:
                        return
                    if deadline - time.monotonic() < 2:
                        for identity in live_runners:
                            self.native.send(identity, signal.SIGKILL)
                except AssertionError:
                    pass
                time.sleep(0.05)
            raise AssertionError("fixture cleanup unconfirmed")

    def generic_package(self) -> None:
        """Install a separate APP fixture violating post-terminal process exit."""
        package = self.workspace / "generic-package"
        package.mkdir()
        script = package / "fixture.py"
        script.write_text(f"#!{fixture_interpreter()}\n" + GENERIC)
        script.chmod(0o755)
        for name in ("input", "output"):
            (package / f"{name}.json").write_text(json.dumps({"type": "object"}))
        manifest = {
            "schema": "transist.plugin.v1",
            "id": "fixture.lifecycle",
            "version": "0.1.0",
            "adapter": "process",
            "default_provider_kind": "local",
            "entrypoint": {"type": "process", "command": "fixture.py"},
            "prerequisites": {"binaries": [{"name": "python3"}]},
            "package": {"files": ["fixture.py", "input.json", "output.json"]},
            "actions": [
                {
                    "name": "execute",
                    "input_schema": "input.json",
                    "output_schema": "output.json",
                }
            ],
            "capabilities": {
                "supports_cancel": False,
                "supports_health": False,
                "supports_artifact_hints": True,
                "supports_evidence_hints": False,
            },
        }
        (package / "plugin.yaml").write_text(json.dumps(manifest))
        self.cli("plugins", "install", "--path", str(package / "plugin.yaml"))

    def check_result(self, mode: str, report: dict, candidate: bool) -> None:
        """Assert persisted outcomes, artifacts, redaction and lifecycle diagnostics."""
        nested = objects(report)
        codes = {item.get("code") for item in nested}
        if mode in {"leader", "orphan"}:
            require(report["status"] == "failed", "generic lingering run succeeded")
            expected = "protocol_error" if candidate else "timeout_error"
            require(expected in codes, f"generic expected {expected}")
            require(
                any(item.get("output") == {} for item in nested),
                "failed output not empty",
            )
            if candidate:
                files = list(
                    self.home.rglob("*transist_process_lifecycle_evidence.json")
                )
                require(len(files) == 1, "lifecycle evidence missing")
                evidence = json.loads(files[0].read_text())
                require(
                    evidence.get("schema") == "transist.process_lifecycle.v1",
                    "evidence schema",
                )
                require(
                    evidence.get("reason") == "process_exit_grace_exceeded",
                    "evidence reason",
                )
                require(evidence.get("cleanup") == "confirmed", "evidence cleanup")
                require(
                    evidence.get("original_output")
                    == {"response": "ordinary generic output"},
                    "original output evidence",
                )
            else:
                require(
                    any(item.get("exit_code") == 124 for item in nested),
                    "baseline timeout exit != 124",
                )
        elif mode == "cancel":
            require(report["status"] == "canceled", "runtime cancellation did not win")
        elif mode in {"timeout", "failure"}:
            require(report["status"] == "failed", "provider failure succeeded")
            expected = "timeout_error" if mode == "timeout" else "tool_execution_error"
            require(expected in codes, f"provider expected {expected}")
            if mode == "failure":
                require(
                    any(item.get("exit_code") == 7 for item in nested),
                    "provider exit code differs",
                )
        else:
            require(report["status"] == "success", "cleaned provider did not succeed")
            require(
                any(
                    item.get("response_text") == "safe fixture response"
                    for item in nested
                ),
                "response output absent",
            )
            require(
                (self.workspace / "response.txt").read_text().strip()
                == "safe fixture response",
                "response file differs",
            )
            usage = list(self.home.rglob("*usage.json"))
            require(bool(usage), "usage artifact absent")
            require(
                any(json.loads(p.read_text()).get("total_tokens") == 5 for p in usage),
                "usage tokens differ",
            )
            stdout = list(self.home.rglob("*stdout.txt"))
            require(
                any("safe fixture response" in p.read_text() for p in stdout),
                "stdout artifact absent",
            )
        if mode not in {"leader", "orphan"}:
            require(
                (self.workspace / "network-denied").is_file(),
                "provider network denial not exercised",
            )
            try:
                connection, _ = self.listener.accept()
            except BlockingIOError:
                pass
            else:
                connection.close()
                raise AssertionError("denied-network fixture reached host listener")
        for path in self.home.rglob("*"):
            if path.is_file():
                secret_free(path.read_bytes().decode("utf-8", errors="replace"))
        if mode == "api":
            for stream in ("stdout", "stderr"):
                artifacts = list(self.home.rglob(f"*{stream}.txt"))
                require(
                    any("[REDACTED" in p.read_text().upper() for p in artifacts),
                    f"API {stream} sentinel redaction absent",
                )


def main() -> int:
    """Run separate baseline/candidate matrices; all missing prerequisites fail."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transist", default=os.environ.get("TRANSIST_BIN"))
    parser.add_argument("--baseline-transist")
    parser.add_argument(
        "--candidate-lifecycle",
        action="store_true",
        help="expect Step-1 post-result evidence, never infer from version",
    )
    parser.add_argument(
        "--fixture-base",
        type=Path,
        required=True,
        help="existing sandbox-allowed directory, outside protected /tmp on macOS",
    )
    parser.add_argument(
        "--case",
        action="append",
        choices=MODES,
        help="run only explicitly named cases (repeatable); default is all ten",
    )
    args = parser.parse_args()
    require(bool(args.transist), "--transist or TRANSIST_BIN is required")
    base = args.fixture_base.expanduser().resolve(strict=True)
    require(base.is_dir(), "fixture base must be a directory")
    if sys.platform == "darwin":
        require(
            not base.is_relative_to(Path("/private/tmp")),
            "macOS fixture base is protected",
        )
    binaries = [("selected", executable(args.transist), args.candidate_lifecycle)]
    if args.baseline_transist:
        binaries.insert(0, ("baseline", executable(args.baseline_transist), False))
    source_digest = {
        p: hashlib.sha256((PACKAGE / p).read_bytes()).hexdigest() for p in SELECTED
    }
    count = 0
    failures = 0
    for label, binary, candidate in binaries:
        modes = args.case or MODES
        for mode in modes:
            try:
                with tempfile.TemporaryDirectory(
                    prefix="cursor-installed-", dir=base
                ) as tmp:
                    driver = Driver(binary, Path(tmp).resolve())
                    try:
                        version = driver.cli("--version").stdout.strip()
                        require(
                            version == "transist 0.1.0",
                            "runtime version differs from 0.1.0",
                        )
                        driver.initialize()
                        generic = mode in {"leader", "orphan"}
                        if generic:
                            driver.generic_package()
                        report = driver.run(mode, generic)
                        driver.check_result(mode, report, candidate)
                    finally:
                        driver.close()
            except (
                AssertionError,
                OSError,
                subprocess.SubprocessError,
                ValueError,
            ) as error:
                failures += 1
                print(f"FAIL {label}: {mode}: {safe_error(error)}", flush=True)
                continue
            count += 1
            print(f"PASS {label}: {mode}", flush=True)
    require(
        source_digest
        == {
            p: hashlib.sha256((PACKAGE / p).read_bytes()).hexdigest() for p in SELECTED
        },
        "source payload changed",
    )
    print(f"RESULT {count} passed, {failures} failed; native sandbox, no live provider")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, subprocess.SubprocessError, ValueError) as error:
        # Subprocess/JSON exceptions may contain sensitive stdout; never print them.
        print("FAIL " + safe_error(error), file=sys.stderr)
        raise SystemExit(1)
