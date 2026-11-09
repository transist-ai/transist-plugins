"""Fail-fast Step 1 validator; isolate and remove only this invocation's resources."""

import json
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "scripts"))
from source_identity import source_identity  # noqa: E402


def output(args: list[str]) -> str:
    """Run a secret-free inspection command and return output, failing on error."""
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


def resources(kind: str) -> set[str]:
    """Snapshot Docker container/volume/network identifiers without config values."""
    commands = {
        "containers": ["docker", "ps", "-aq"],
        "volumes": ["docker", "volume", "ls", "-q"],
        "networks": ["docker", "network", "ls", "-q"],
    }
    return set(output(commands[kind]).splitlines())


def fixture_unit_command(compose: list[str]) -> list[str]:
    """Run root-only Tika provisioning units in the locked, network-none image."""
    return compose + [
        "run",
        "--rm",
        "--no-deps",
        "--entrypoint",
        "python",
        "offline",
        "-B",
        "-m",
        "unittest",
        "discover",
        "-s",
        "/fixture",
        "-p",
        "test_tika_fixture.py",
    ]


def validate() -> None:
    """Build, provision and exercise real fixtures, recording evidence under /tmp."""
    if os.environ.get("PYTHONOPTIMIZE") or not __debug__:
        raise RuntimeError("Assertions must be enabled")
    for tool in ("docker", "docker-compose", "transist", "transist-admin"):
        if not shutil.which(tool):
            raise RuntimeError(f"Missing prerequisite: {tool}")
    versions = {
        tool: output([tool, "--version"]) for tool in ("transist", "transist-admin")
    }
    if versions != {
        "transist": "transist 0.1.0",
        "transist-admin": "transist-admin 0.1.0",
    }:
        raise RuntimeError("Matching Transist/admin 0.1.0 required")
    architecture = output(["docker", "info", "--format", "{{.Architecture}}"])
    if architecture not in {"aarch64", "arm64"}:
        raise RuntimeError(
            "Artifact hash locks require the tested Linux aarch64 platform"
        )
    project = "tqstep1-" + uuid.uuid4().hex[:12]
    logs = Path(tempfile.mkdtemp(prefix="tq-step1-", dir="/tmp"))
    logs.chmod(0o700)
    compose = ["docker-compose", "-p", project, "-f", str(HERE / "compose.yaml")]
    before = {kind: resources(kind) for kind in ("containers", "volumes", "networks")}
    summary = {
        "project": project,
        "platform": platform.platform(),
        "docker_architecture": architecture,
        "versions": versions,
        "source_identity": source_identity(HERE.parents[2]),
        "binary_sha256": {
            tool: hashlib.sha256(Path(shutil.which(tool)).read_bytes()).hexdigest()
            for tool in versions
        },
        "dirty_source": bool(output(["git", "status", "--porcelain"])),
        "commands": [],
    }
    print(f"Step 1 project: {project}; logs: {logs}", flush=True)

    def run(
        name: str, args: list[str], timeout: int = 900, data: bytes | None = None
    ) -> None:
        summary["commands"].append(args)
        with (logs / f"{name}.log").open("wb") as log:
            result = subprocess.run(
                args,
                cwd=HERE,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                input=data,
            )
        if result.returncode:
            raise RuntimeError(f"{name} failed; see {logs / (name + '.log')}")

    succeeded = False
    try:
        run("build", compose + ["build"])
        run("fixture-units", fixture_unit_command(compose))
        summary["tika_provision_unit_tests"] = "passed"
        run("pull", compose + ["pull", "tika", "qdrant"])
        run("provision", compose + ["run", "--rm", "--no-deps", "provision"])
        run("offline", compose + ["run", "--rm", "--no-deps", "offline"])
        run("start", compose + ["up", "-d", "tika", "qdrant", "gateway"])
        ids = output(compose + ["ps", "-q"]).splitlines()
        summary["images"] = []
        for container in ids:
            info = json.loads(output(["docker", "inspect", container]))[0]
            service = info["Config"]["Labels"]["com.docker.compose.service"]
            ports = info["HostConfig"]["PortBindings"]
            if service in {"tika", "qdrant"} and ports:
                raise RuntimeError("Backend port published")
            for network in info["NetworkSettings"]["Networks"]:
                internal = output(
                    [
                        "docker",
                        "network",
                        "inspect",
                        network,
                        "--format",
                        "{{.Internal}}",
                    ]
                )
                if internal != "true":
                    raise RuntimeError("Runtime service has download egress")
            image = json.loads(output(["docker", "image", "inspect", info["Image"]]))[0]
            summary["images"].append(
                {
                    "service": service,
                    "id": info["Image"],
                    "repo_digests": image["RepoDigests"],
                }
            )
        run("seed", compose + ["run", "--rm", "--no-deps", "seed"])
        capture = project + "-capture"
        run(
            "probe",
            compose + ["run", "--name", capture, "--no-deps", "probe"],
            timeout=240,
        )
        run(
            "capture",
            [
                "docker",
                "cp",
                capture + ":/evidence/native-output.json",
                str(logs / "native-output.json"),
            ],
        )
        run(
            "model",
            [
                "docker",
                "cp",
                capture + ":/assets/model/manifest.json",
                str(logs / "model-manifest.json"),
            ],
        )
        succeeded = True
    finally:
        try:
            run("service", compose + ["logs", "--no-color"])
            if succeeded:
                data = b"\n".join(
                    path.read_bytes() for path in logs.iterdir() if path.is_file()
                )
                run(
                    "secret-scan",
                    compose
                    + ["run", "--rm", "-T", "--no-deps", "probe", "python", "scan.py"],
                    data=data,
                )
        finally:
            run(
                "teardown",
                compose + ["down", "--volumes", "--remove-orphans"],
                timeout=120,
            )
            for kind, original in before.items():
                if not original <= resources(kind):
                    raise RuntimeError(f"Unrelated Docker {kind} removed")
            for command in (
                [
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                ],
                [
                    "docker",
                    "volume",
                    "ls",
                    "-q",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                ],
                [
                    "docker",
                    "network",
                    "ls",
                    "-q",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                ],
            ):
                if output(command):
                    raise RuntimeError("Fixture resources remain after teardown")
            summary["teardown"] = "own resources removed; unrelated resources preserved"
            (logs / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"PASS: all Step 1 real-service acceptance checks; evidence: {logs}")


if __name__ == "__main__":
    validate()
