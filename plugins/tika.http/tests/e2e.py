#!/usr/bin/env python3
"""Fail-fast Step 3 real-Tika E2E; private evidence outside the checkout, no skips."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import ProxyHandler, HTTPSHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
FIXTURE = REPO / "tests/services/tika_qdrant"
sys.path.insert(0, str(REPO / "scripts"))
from docker_endpoint import local_docker_host  # noqa: E402
from source_identity import source_identity  # noqa: E402

VERSION = next(
    line.partition(":")[2].strip()
    for line in (ROOT / "plugin.yaml").read_text().splitlines()
    if line.startswith("version:")
)
TIKA = (
    "apache/tika:3.3.0.0@sha256:"
    "2a565f1ea1290bdcb74a7d35957d16a989ed44ef98790dcdcc28121d728fa583"
)
TERMINAL = {"success", "failed", "canceled"}

# Execute only inside the actual Linux bwrap + runtime-launcher boundary. A
# launcher/setup failure cannot count as write denial: this program must finish
# successfully after verifying namespaces, EROFS, network:none and dropped caps.
LINUX_SANDBOX_PROBE = r"""
import errno, fcntl, json, os, socket, struct, sys
from pathlib import Path
parents = json.loads(sys.argv[1])
namespaces = {name: os.readlink('/proc/self/ns/' + name) for name in parents}
assert all(namespaces[name] != parent for name, parent in parents.items())
assert (os.geteuid(), os.getegid()) == (int(sys.argv[2]), int(sys.argv[3]))
assert Path('/probe/read/source').read_bytes() == b'read-only sandbox fixture\n'
try:
    Path('/probe/read/sandbox-must-not-exist').write_text('denied')
except OSError as error:
    assert error.errno == errno.EROFS, 'write failed for an unrelated reason'
else:
    raise AssertionError('read-only sandbox write succeeded')
assert {name for _, name in socket.if_nameindex()} == {'lo'}
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
    flags = struct.unpack_from('H', fcntl.ioctl(
        connection.fileno(), 0x8913, struct.pack('16sH', b'lo', 0)), 16)[0]
assert not flags & 1, 'network:none left loopback up'
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text()
              .splitlines() if ':' in line)
assert all(int(status[name].strip(), 16) == 0 for name in (
    'CapEff', 'CapPrm', 'CapInh', 'CapBnd', 'CapAmb'))
assert status['NoNewPrivs'].strip() == '1'
Path('/probe/write/completed').write_text('native Linux sandbox verified\n')
print(json.dumps({'backend':'bwrap', 'read_only':True, 'network_none':True,
                  'capabilities_dropped':True, 'namespaces':namespaces}))
"""


def sandbox_backend(host_platform: str) -> str:
    """Select only a runtime-supported native backend; unknown platforms fail."""
    backend = {"darwin": "sandbox-exec", "linux": "bwrap"}.get(host_platform)
    if backend is None:
        raise RuntimeError("Required native sandbox is unsupported on this platform")
    return backend


def sandbox_executable(host_platform: str, candidates: dict[str, str | None]) -> str:
    """Require the selected host's executable without cross-platform fallback."""
    backend = sandbox_backend(host_platform)
    executable = candidates.get(backend)
    if not executable:
        raise RuntimeError(
            f"Required native sandbox executable is unavailable: {backend}"
        )
    return executable


def linux_probe_command(
    bwrap: str | None,
    python: Path,
    launcher: Path,
    root: Path,
    namespaces: dict[str, str],
) -> list[str]:
    """Build the runtime's strict bwrap/launcher shape with fixture-only mounts.

    System interpreter roots are read-only. Only probe output is writable; no
    host-root, Docker socket, operator home or credentials are mounted. Namespace
    identities are supplied by the actual parent and verified after real exec.
    """
    binary = sandbox_executable("linux", {"bwrap": bwrap})
    if set(namespaces) != {"mnt", "net", "user"} or not all(namespaces.values()):
        raise RuntimeError("Linux sandbox probe requires parent namespace identities")
    command = [
        binary,
        "--die-with-parent",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--unshare-net",
        "--unshare-user",
    ]
    for capability in ("CAP_NET_ADMIN", "CAP_SETPCAP", "CAP_SETGID", "CAP_SETUID"):
        command.extend(["--cap-add", capability])
    system_roots = ("/usr", "/bin", "/lib", "/lib64", "/sbin")
    if not any(python.is_relative_to(path) for path in system_roots):
        raise RuntimeError(
            "Linux probe Python must resolve beneath runtime system roots"
        )
    for path in system_roots:
        if Path(path).exists():
            command.extend(["--ro-bind", path, path])
    command.extend(
        [
            "--ro-bind",
            str(root / "sandbox-read"),
            "/probe/read",
            "--bind",
            str(root / "sandbox-write"),
            "/probe/write",
            "--ro-bind",
            str(launcher),
            "/tmp/transist-sandbox-launcher",
            "--chdir",
            "/probe/read",
            "--",
            "/tmp/transist-sandbox-launcher",
            "--network-mode",
            "none",
            str(os.geteuid()),
            str(os.getegid()),
            str(python),
            "-I",
            "-B",
            "-c",
            LINUX_SANDBOX_PROBE,
            json.dumps(namespaces),
            str(os.geteuid()),
            str(os.getegid()),
        ]
    )
    return command


def verify_sandbox_readiness(report: dict, host_platform: str) -> str:
    """Require exact native backend construction for both named Tika actions."""
    backend = sandbox_backend(host_platform)
    boundaries = [
        check
        for check in report.get("checks", [])
        if check.get("code") == "filesystem_workspace_boundary"
    ]
    message = (
        f"workspace-scoped filesystem sandbox backend '{backend}' can be constructed"
    )
    assert (
        report.get("status") == "ready"
        and len(boundaries) == 2
        and {check.get("step_path") for check in boundaries} == {"text", "metadata"}
        and all(
            check.get("status") == "passed" and check.get("message") == message
            for check in boundaries
        )
    ), "Runtime native sandbox was not verified for both actions by live readiness"
    return backend


def variants(value: str) -> set[bytes]:
    """Cover raw, escaped, URL, hex and padded/unpadded Base64 credential forms."""
    raw = value.encode()
    result = {raw, quote(value, safe="").encode(), raw.hex().encode()}
    result.add(json.dumps(value)[1:-1].encode())
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        encoded = encode(raw)
        result.update((encoded, encoded.rstrip(b"=")))
    return result


def failure_details(value: Any) -> list[str]:
    """Collect failed checks/errors, excluding recipe/input policy descriptions."""
    findings: list[str] = []
    if isinstance(value, dict):
        if value.get("status") in {"failed", "blocked"} and "message" in value:
            findings.append(str(value.get("code", "")) + " " + str(value["message"]))
        for key, child in value.items():
            if key in {"error", "errors", "reason_code", "failure_reason", "failure"}:
                findings.append(json.dumps(child))
            elif key not in {"recipe", "workflow", "inputs", "input", "output"}:
                findings.extend(failure_details(child))
    elif isinstance(value, list):
        for child in value:
            findings.extend(failure_details(child))
    return findings


class E2E:
    """Own one isolated Docker project, runtime homes and bounded evidence commands."""

    def __init__(self) -> None:
        """Allocate private scratch outside the repo; never inherit operator state."""
        base = os.environ.get("TRANSIST_FIXTURE_BASE")
        if not base or not Path(base).is_absolute() or not Path(base).is_dir():
            raise RuntimeError(
                "TRANSIST_FIXTURE_BASE must be an existing absolute directory"
            )
        self.root = Path(tempfile.mkdtemp(prefix="tika-step3-", dir=base)).resolve()
        self.root.chmod(0o700)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.workspace = self.root / "client-workspace"
        self.workspace.mkdir()
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.project = "tika-step3-" + uuid.uuid4().hex[:12]
        self.env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(self.root),
            "TMPDIR": str(self.root),
        }
        self.needles: set[bytes] = set()
        self.homes: list[Path] = []
        self.failed_runs: list[tuple[Path, str]] = []
        self.summary: dict[str, Any] = {
            "project": self.project,
            "platform": platform.platform(),
            "plugin_version": VERSION,
            "commands": [],
            "passed": [],
            "accepted": False,
        }
        self.compose: list[str] = []
        self.sequence = 0

    def scan_bytes(self, data: bytes) -> None:
        """Fail without disclosing the matching credential or its transformed form."""
        if any(needle in data for needle in self.needles):
            raise AssertionError("Credential persistence detected (value withheld)")

    def command(
        self,
        args: list[str],
        *,
        ok: bool = True,
        timeout: int = 60,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Capture bounded commands privately; credentials are environment-only."""
        self.sequence += 1
        name = f"{self.sequence:03d}-{Path(args[0]).name}.log"
        self.summary["commands"].append({"argv": args, "log": name})
        result = subprocess.run(
            args,
            cwd=self.workspace,
            env=self.env if env is None else env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        data = (result.stdout + result.stderr).encode()
        (self.logs / name).write_bytes(data)
        self.summary["commands"][-1]["exit"] = result.returncode
        self.scan_bytes(data)
        if ok and result.returncode:
            raise RuntimeError(f"Command failed (exit {result.returncode}); see {name}")
        return result

    def cli(
        self, home: Path, *args: str, ok: bool = True, credential: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Use only a disposable home, delivering raw secrets outside argv/files."""
        env = self.env.copy()
        env["TIKA_AUTHORIZATION"] = (
            self.authorization if credential is None else credential
        )
        return self.command(
            [self.transist, "--home", str(home), *args, "--format", "json"],
            ok=ok,
            env=env,
        )

    def passed(self, case: str) -> None:
        """Record a completed assertion, never a planned or skipped test."""
        self.summary["passed"].append(case)
        print(f"PASS: {case}", flush=True)

    def prerequisites(self) -> None:
        """Require matching binaries, real Docker and a functioning platform sandbox."""
        if not __debug__ or os.environ.get("PYTHONOPTIMIZE"):
            raise RuntimeError("Assertions must be enabled")
        self.transist = os.environ.get("TRANSIST_BIN")
        self.admin = os.environ.get("TRANSIST_ADMIN_BIN")
        if any(
            not raw
            or not Path(raw).is_absolute()
            or not Path(raw).is_file()
            or not os.access(raw, os.X_OK)
            for raw in (self.transist, self.admin)
        ):
            raise RuntimeError(
                "Explicit executable TRANSIST_BIN and TRANSIST_ADMIN_BIN required"
            )
        if not self.transist or not self.admin or not shutil.which("docker"):
            raise RuntimeError("Docker and matching Transist/admin binaries required")
        for name, binary in (
            ("transist", self.transist),
            ("transist-admin", self.admin),
        ):
            result = self.command([binary, "--version"])
            assert result.stdout.strip() == f"{name} 0.1.0", "Matching 0.1.0 required"
            self.summary[name] = {
                "version": result.stdout.strip(),
                "sha256": hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
            }
        self.summary["source_identity"] = source_identity(REPO)
        self.summary["revision"] = self.summary["source_identity"]["commit"]
        self.summary["dirty_source"] = self.command(
            ["git", "-C", str(REPO), "status", "--porcelain"]
        ).stdout.splitlines()
        # Desktop/Colima store their daemon socket in the operator's context.
        # Resolve only the public endpoint before isolating HOME, not credentials.
        self.env["DOCKER_HOST"] = local_docker_host()
        info = self.command(
            ["docker", "info", "--format", "{{.OSType}} {{.Architecture}}"]
        ).stdout.strip()
        assert info in {"linux arm64", "linux aarch64"}, "Locked Linux aarch64 required"
        self.summary["docker_platform"] = info
        self.summary["docker_host"] = self.env["DOCKER_HOST"]
        self.command(["docker", "pull", "--platform", "linux/arm64", TIKA], timeout=300)
        image = self.command(
            [
                "docker",
                "image",
                "inspect",
                TIKA,
                "--format",
                "{{.Os}} {{.Architecture}}",
            ]
        ).stdout.strip()
        assert image == "linux arm64", "Pinned Tika must support native Linux arm64"
        self.summary["tika_image_platform"] = image
        self.probe_sandbox()
        self.passed("matching 0.1.0 binaries and enforced platform sandbox")

    def probe_sandbox(self) -> None:
        """Execute the host's real read-only/namespace boundary; never skip failures."""
        backend = sandbox_backend(sys.platform)
        binary = sandbox_executable(
            sys.platform,
            {
                backend: shutil.which(backend, path=self.env["PATH"]),
            },
        )
        if backend == "bwrap":
            self.probe_linux_sandbox(binary)
            return
        probe = self.root / "sandbox-must-not-exist"
        result = self.command(
            [
                binary,
                "-p",
                "(version 1)(allow default)(deny file-write*)",
                sys.executable,
                "-c",
                "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('denied')",
                str(probe),
            ],
            ok=False,
        )
        assert result.returncode != 0 and "Operation not permitted" in result.stderr
        assert not probe.exists(), "Sandbox write denial was not enforced"
        self.summary["host_sandbox"] = {"backend": backend, "write_denied": True}

    def probe_linux_sandbox(self, bwrap: str) -> None:
        """Use the actual runtime launcher to enforce network:none and drop caps."""
        candidates = [
            Path(self.transist).resolve().with_name("transist-sandbox-launcher"),
            Path("/usr/libexec/transist/transist-sandbox-launcher"),
            Path("/usr/local/libexec/transist/transist-sandbox-launcher"),
        ]
        launcher = next(
            (
                path
                for path in candidates
                if path.is_file() and os.access(path, os.X_OK)
            ),
            None,
        )
        if launcher is None:
            raise RuntimeError(
                "Linux sandbox requires the actual runtime network launcher"
            )
        namespaces = {
            name: os.readlink("/proc/self/ns/" + name)
            for name in ("mnt", "net", "user")
        }
        for name in ("sandbox-read", "sandbox-write"):
            (self.root / name).mkdir()
        source = self.root / "sandbox-read/source"
        source.write_bytes(b"read-only sandbox fixture\n")
        command = linux_probe_command(
            bwrap, Path(sys.executable).resolve(), launcher, self.root, namespaces
        )
        result = self.command(command)
        proof = json.loads(result.stdout)
        assert proof["backend"] == "bwrap" and all(
            proof.get(key) is True
            for key in ("read_only", "network_none", "capabilities_dropped")
        ), "Linux sandbox probe did not complete enforcement checks"
        assert set(proof["namespaces"]) == set(namespaces) and all(
            proof["namespaces"][name] != original
            for name, original in namespaces.items()
        ), "Linux sandbox did not unshare all required namespaces"
        assert source.read_bytes() == b"read-only sandbox fixture\n"
        assert not (source.parent / "sandbox-must-not-exist").exists()
        assert (self.root / "sandbox-write/completed").read_text() == (
            "native Linux sandbox verified\n"
        )
        self.summary["host_sandbox"] = {
            **proof,
            "launcher": str(launcher),
            "launcher_sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
        }

    def docker_resources(self, kind: str) -> set[str]:
        """Read global identifiers only, without exposing container environments."""
        args = {
            "containers": ["docker", "ps", "-aq"],
            "volumes": ["docker", "volume", "ls", "-q"],
            "networks": ["docker", "network", "ls", "-q"],
        }[kind]
        return set(self.command(args).stdout.splitlines())

    def start_fixture(self) -> None:
        """Run real private Tika and the production APP over verified HTTPS only."""
        compose_tool = shutil.which("docker-compose")
        if not compose_tool:
            raise RuntimeError("docker-compose required")
        self.before = {
            kind: self.docker_resources(kind)
            for kind in ("containers", "volumes", "networks")
        }
        spec = {
            "services": {
                "provision": {
                    "platform": "linux/arm64",
                    "build": {"context": str(FIXTURE), "target": "server"},
                    "command": ["python", "provision.py", "--tika-only"],
                    "volumes": ["assets:/assets"],
                    "network_mode": "none",
                },
                "tika": {
                    "image": TIKA,
                    "platform": "linux/arm64",
                    "networks": ["private"],
                },
                "app": {
                    "platform": "linux/arm64",
                    "build": {"context": str(ROOT)},
                    "command": ["python", "/service/tika_app.py"],
                    "volumes": [
                        "assets:/assets:ro",
                        f"{FIXTURE / 'tika_app.py'}:/service/tika_app.py:ro",
                    ],
                    "read_only": True,
                    "cap_drop": ["ALL"],
                    "security_opt": ["no-new-privileges:true"],
                    "networks": ["private", "frontend"],
                    "ports": ["127.0.0.1::8443"],
                },
            },
            "networks": {"private": {"internal": True}, "frontend": {}},
            "volumes": {"assets": {}},
        }
        config = self.root / "compose.json"
        config.write_text(json.dumps(spec))
        self.compose = [compose_tool, "-p", self.project, "-f", str(config)]
        self.command(self.compose + ["build"], timeout=900)
        self.command(
            self.compose + ["run", "--rm", "--no-deps", "provision"], timeout=120
        )
        self.command(self.compose + ["up", "-d", "tika", "app"], timeout=120)
        for service in ("tika", "app"):
            cid = self.command(self.compose + ["ps", "-q", service]).stdout.strip()
            info = json.loads(self.command(["docker", "inspect", cid]).stdout)[0]
            if service == "tika":
                assert not info["HostConfig"]["PortBindings"], "Backend port published"
            for network in info["NetworkSettings"]["Networks"]:
                internal = self.command(
                    [
                        "docker",
                        "network",
                        "inspect",
                        network,
                        "--format",
                        "{{.Internal}}",
                    ]
                )
                if service == "tika" or network.endswith("_private"):
                    assert (
                        internal.stdout.strip() == "true"
                    ), "Backend network not private"
            image = json.loads(
                self.command(["docker", "image", "inspect", info["Image"]]).stdout
            )[0]
            assert (image["Os"], image["Architecture"]) == ("linux", "arm64")
            self.summary.setdefault("images", {})[service] = {
                "id": info["Image"],
                "repo_digests": image["RepoDigests"],
                "platform": "linux/arm64",
            }
            if service == "app":
                self.app_container = cid
                self.command(["docker", "cp", cid + ":/assets/.", str(self.private)])
                binding = info["NetworkSettings"]["Ports"]["8443/tcp"][0]
                assert binding["HostIp"] == "127.0.0.1"
                self.port = int(binding["HostPort"])
        assert not (self.private / "model").exists(), "Tika provision fetched a model"
        credentials = json.loads((self.private / "credentials.json").read_text())
        self.authorization = "Bearer " + credentials["tika"]
        self.bad_authorization = "Bearer " + secrets.token_urlsafe(32)
        for value in (self.authorization, self.bad_authorization):
            self.needles.update(variants(value))
            self.needles.update(variants(value.removeprefix("Bearer ")))
        self.origin = f"https://127.0.0.1:{self.port}"
        self.context = ssl.create_default_context(cafile=str(self.private / "ca.pem"))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                assert self.get("/app/health") == {"status": "ok"}
                break
            except (OSError, AssertionError):
                time.sleep(0.25)
        else:
            raise RuntimeError("HTTPS production service did not become healthy")
        assert self.get("/app/handshake")["plugin"] == {
            "id": "tika.http",
            "version": VERSION,
        }
        # APP health is liveness, not parser readiness. Wait for actual Tika
        # startup inside the private network; never retry document invocations.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            probe = self.command(
                [
                    "docker",
                    "exec",
                    self.app_container,
                    "python",
                    "-c",
                    "import httpx; r=httpx.get('http://tika:9998/version', "
                    "trust_env=False, timeout=3); r.raise_for_status(); "
                    "print(r.text.strip())",
                ],
                ok=False,
            )
            if probe.returncode == 0:
                assert "3.3.0" in probe.stdout, "Unexpected Tika version"
                self.summary["backend_version"] = probe.stdout.strip()
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Real Tika backend did not become ready")
        self.passed(
            "real pinned Tika private backend; verified HTTPS production APP; no model"
        )

    def get(self, path: str) -> dict[str, Any]:
        """Make secret-free TLS-verified counter/discovery requests without proxies."""
        client = build_opener(ProxyHandler({}), HTTPSHandler(context=self.context))
        with client.open(self.origin + path, timeout=5) as response:
            return json.load(response)

    def package(
        self, name: str, *, ca: bool = True, hostname: str = "127.0.0.1"
    ) -> Path:
        """Configure scratch endpoint/CA at the manifest's current package version."""
        package = self.root / name
        shutil.copytree(
            ROOT, package, ignore=shutil.ignore_patterns("tests", "__pycache__")
        )
        manifest = package / "plugin.yaml"
        text = manifest.read_text().replace(
            "https://tika.example.invalid", f"https://{hostname}:{self.port}"
        )
        if ca:
            shutil.copyfile(self.private / "ca.pem", package / "ca.pem")
            text = text.replace("entrypoint:\n", "entrypoint:\n  ca_cert: ca.pem\n")
            text = text.replace("  files:\n", "  files:\n    - ca.pem\n")
        manifest.write_text(text)
        return package

    def init_home(self, name: str, mode: str, package: Path) -> Path:
        """Initialize a disposable home and enable HTTP delivery only for success."""
        home = self.root / name
        self.homes.append(home)
        self.cli(home, "home", "init", "--mode", mode)
        config = home / "config.yaml"
        text = config.read_text()
        assert "http: false" in text
        config.write_text(text.replace("http: false", "http: true"))
        if mode == "governed":
            self.cli(home, "trust", "import", str(self.trust))
        self.cli(home, "plugins", "install", "--path", str(package / "plugin.yaml"))
        listing = json.loads(self.cli(home, "plugins", "list").stdout)
        assert "tika.http" in json.dumps(listing)
        inspect = self.cli(home, "plugins", "inspect", "tika.http").stdout
        assert all(
            value in inspect for value in (VERSION, "extract_text", "extract_metadata")
        )
        self.passed(f"{mode}/{name}: install/list/inspect")
        return home

    def flow(
        self,
        name: str,
        *,
        host: str = "127.0.0.1",
        deny_network: bool = False,
        extra_hosts: tuple[str, ...] = (),
    ) -> Path:
        """Bind both actions to byte inputs; explicitly grant no workspace writes."""
        source = self.root / f"{name}.yaml"
        network = (
            {"mode": "none"}
            if deny_network
            else {
                "mode": "allowlist",
                "allow_hosts": [host, *extra_hosts],
                "allow_ports": [self.port],
                "tls_required": True,
            }
        )
        steps = [
            {
                "id": step,
                "uses": f"http:tika.http/{action}",
                "with": {
                    "document_base64": "{{inputs.document_base64}}",
                    "content_type": "{{inputs.content_type}}",
                    "secret_refs": ["TIKA_AUTHORIZATION"],
                },
            }
            for step, action in (
                ("text", "extract_text"),
                ("metadata", "extract_metadata"),
            )
        ]
        fields = {
            "plugins": {"tika.http": VERSION},
            "workspace": {"path": str(self.workspace), "access": "read_only"},
            "inputs": {
                key: {"type": "string", "required": True}
                for key in ("document_base64", "content_type")
            },
            "network": network,
            "steps": steps,
        }
        # Admin packaging requires a literal top-level YAML version field.
        source.write_text(
            f"schema: transist.flow.v1\nid: {name}\nversion: 0.1.0\n"
            + "".join(f"{key}: {json.dumps(value)}\n" for key, value in fields.items())
        )
        return source

    def inputs(self, suffix: str) -> tuple[Path, bytes, str]:
        """Use generated fixture bytes, not URLs or client document path inputs."""
        data = (self.private / "documents" / f"document.{suffix}").read_bytes()
        types = {
            "pdf": "application/pdf",
            "txt": "text/plain",
            "docx": (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        }
        path = self.root / f"inputs-{suffix}.json"
        path.write_text(
            json.dumps(
                {
                    "document_base64": base64.b64encode(data).decode(),
                    "content_type": types[suffix],
                }
            )
        )
        return path, data, types[suffix]

    def install_flow(
        self, home: Path, name: str, source: Path, inputs: Path, *, governed: bool
    ) -> None:
        """Check input, install signed/standalone code and authorize exact gaps."""
        checked = self.cli(
            self.authoring if governed else home,
            "workflows",
            "check",
            "--path",
            str(source),
            "--inputs-file",
            str(inputs),
            ok=False,
        )
        if checked.returncode:
            report = json.loads(checked.stdout)
            failures = [
                check
                for check in report.get("checks", [])
                if check.get("status") == "failed"
            ]
            if any(
                check.get("code") == "input_schema"
                and "/additionalProperties" in check.get("message", "")
                for check in failures
            ):
                raise RuntimeError(
                    "Runtime contract blocker: bound input_schema rejects the closed "
                    "Tika input with required with.secret_refs at /additionalProperties"
                )
            raise RuntimeError("Authored bound workflow check failed; see command log")
        if governed:
            release = self.root / f"{name}-release"
            self.command(
                [
                    self.admin,
                    "workflow",
                    "package",
                    str(source),
                    "--version",
                    "0.1.0",
                    "--private-key-file",
                    str(self.key),
                    "--output",
                    str(release),
                ]
            )
            self.cli(
                home, "workflows", "install", "--package", str(release), "--approve"
            )
        else:
            self.cli(home, "workflows", "install", "--path", str(source), "--approve")
        binding = [name + "@0.1.0", "--inputs-file", str(inputs)]
        ready = self.cli(home, "workflows", "check", *binding, "--live", ok=False)
        report = json.loads(ready.stdout)
        if report.get("status") != "ready":
            assert (
                report.get("diagnostics", {}).get("authorization", {}).get("eligible")
            ), "Readiness has non-authority blockers; see command log"
            self.cli(home, "workflows", "authorize", *binding, "--approve")
        else:
            # Exercise the public authorization lifecycle even when standalone
            # policy already covers all requests (the command must be a no-op).
            self.cli(home, "workflows", "authorize", *binding, "--approve")
        report = json.loads(
            self.cli(home, "workflows", "check", *binding, "--live").stdout
        )
        backend = verify_sandbox_readiness(report, sys.platform)
        self.summary["runtime_sandbox"] = f"live readiness constructed {backend}"
        self.passed(f"{name}: authored check/install/check/authorize/ready")

    def run(
        self,
        home: Path,
        name: str,
        run_id: str,
        inputs: Path,
        *,
        failure: bool = False,
        credential: str | None = None,
        local_only: bool = False,
    ) -> dict[str, Any]:
        """Start real runtime dispatch and require an exact terminal persisted state."""
        args = [
            "workflows",
            "run",
            name + "@0.1.0",
            "--run-ref",
            run_id,
            "--inputs-file",
            str(inputs),
        ]
        if local_only:
            args.append("--local-only")
        result = self.cli(home, *args, ok=False, credential=credential)
        if not failure:
            assert result.returncode == 0, "Run birth failed; see command log"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            shown = self.cli(home, "runs", "show", run_id, ok=False)
            if shown.returncode == 0:
                report = json.loads(shown.stdout)
                if report.get("status") in TERMINAL:
                    if report["status"] == "failed":
                        self.failed_runs.append((home, run_id))
                    assert report["status"] == (
                        "failed" if failure else "success"
                    ), "Unexpected terminal state; see runs-show log"
                    self.scan_client()
                    return report
            elif failure and result.returncode != 0:
                # Current readiness can deny birth. Require structured denial;
                # don't confuse a generic CLI failure with a persisted failure.
                if result.stdout.strip():
                    report = json.loads(result.stdout)
                    assert report.get("run_created") is False
                else:
                    assert "preflight failed before run birth:" in result.stderr
                    assert not (home / "runs" / ("run_" + run_id)).exists()
                    report = {"run_created": False, "error": result.stderr}
                self.scan_client()
                return report
            time.sleep(0.1)
        raise AssertionError("Run failed to reach terminal state in 60 seconds")

    def assert_outputs(
        self, report: dict[str, Any], data: bytes, content_type: str
    ) -> None:
        """Verify both persisted outputs, preserved text, metadata shape and hashes."""
        steps = report["steps"]
        for step in ("text", "metadata"):
            request = steps[step]["last_request"]
            assert request["input"]["secret_refs"] == ["TIKA_AUTHORIZATION"]
            assert request["metadata"]["secret_refs"] == ["TIKA_AUTHORIZATION"]
            assert any(
                cap["capability"] == "secret"
                and cap["resource"] == "TIKA_AUTHORIZATION"
                for cap in request["requested_capabilities"]
            ), "Projection changed original secret authority"
        text = steps["text"]["last_result"]
        meta = steps["metadata"]["last_result"]
        assert text["status"] == meta["status"] == "success"
        digest = hashlib.sha256(data).hexdigest()
        assert (
            text["output"]["document_sha256"]
            == meta["output"]["document_sha256"]
            == digest
        )
        assert (
            "Calibration fixture: calibrate the temperature sensor at zero and 100 C."
            in text["output"]["text"]
        )
        metadata = meta["output"]["metadata"]
        # Tika legitimately includes charset parameters for plain-text metadata;
        # retain the full value while checking its parsed media type.
        assert metadata and [
            value.split(";", 1)[0].strip() for value in metadata["Content-Type"]
        ] == [content_type]
        assert metadata["Content-Length"] == [str(len(data))]
        if content_type == "text/plain":
            assert text["output"]["text"] == data.decode() + "\n"
        allowed = {
            "Content-Type",
            "Content-Length",
            "Content-Encoding",
            "dc:title",
            "dc:creator",
            "dcterms:created",
            "dcterms:modified",
            "xmpTPg:NPages",
        }
        assert set(metadata) <= allowed
        assert all(
            isinstance(v, list) and all(isinstance(s, str) for s in v)
            for v in metadata.values()
        )
        if content_type == "application/pdf":
            assert metadata["xmpTPg:NPages"] == ["1"]
        self.summary.setdefault("documents", {})[content_type] = {
            "sha256": digest,
            "text_sha256": hashlib.sha256(text["output"]["text"].encode()).hexdigest(),
            "metadata": metadata,
        }

    def scan_client(self) -> None:
        """Scan every runtime file/log and assert no plugin-owned client documents."""
        for root in [*self.homes, self.logs]:
            for path in root.rglob("*"):
                if path.is_file():
                    self.scan_bytes(path.read_bytes())
        assert not list(
            self.workspace.rglob("*")
        ), "Plugin wrote client workspace files"
        for home in self.homes:
            assert not any(
                path.suffix in {".pdf", ".docx", ".txt"}
                for path in home.rglob("*")
                if path.is_file()
            ), "Plugin-created document file in client home"
            assert not list(
                (home / ".transist-staging").glob("invoke-*")
            ), "Staging leaked"

    def negative(
        self,
        home: Path,
        name: str,
        inputs: Path,
        case: str,
        needles: tuple[str, ...],
        **kwargs: Any,
    ) -> None:
        """Require a cause-specific denial and no successful extraction dispatch."""
        before = self.get("/fixture/counts")
        report = self.run(home, name, case, inputs, failure=True, **kwargs)
        serialized = " ".join(failure_details(report)).lower()
        assert any(
            needle.lower() in serialized for needle in needles
        ), f"{case}: expected cause missing; see private inspection log"
        if case.endswith(("untrusted-ca", "wrong-hostname")):
            failed = [
                check for check in report["checks"] if check.get("status") == "failed"
            ]
            assert len(failed) == 2 and all(
                check["code"] == "adapter_handshake" for check in failed
            ), "TLS case failed for an unrelated readiness reason"
        after = self.get("/fixture/counts")
        assert after.get("POST /app/invoke 200", 0) == before.get(
            "POST /app/invoke 200", 0
        )
        if case.endswith("bad-credential"):
            assert after.get("POST /app/invoke 401", 0) > before.get(
                "POST /app/invoke 401", 0
            )
        else:
            assert after.get("POST /app/invoke 401", 0) == before.get(
                "POST /app/invoke 401", 0
            )
        self.passed(case)

    def verify_tls_failure(self, case: str, host: str, *, ca: bool) -> None:
        """Prove the TLS cause independently while keeping runtime errors sanitized."""
        context = ssl.create_default_context(
            cafile=str(self.private / "ca.pem") if ca else None
        )
        with socket.create_connection(
            ("127.0.0.1", self.port), timeout=5
        ) as connection:
            try:
                with context.wrap_socket(connection, server_hostname=host):
                    raise AssertionError("Invalid certificate unexpectedly trusted")
            except ssl.SSLCertVerificationError as error:
                expected = {62} if case == "wrong-hostname" else {20, 21}
                assert (
                    error.verify_code in expected
                ), "Unexpected TLS verification cause"
                self.summary.setdefault("tls_failures", {})[case] = {
                    "verify_code": error.verify_code,
                    "verify_message": error.verify_message,
                }

    def uninstall(self, home: Path) -> None:
        """Verify uninstall removes the package and subsequent inspection fails."""
        for failed_home, run_id in self.failed_runs:
            if failed_home != home:
                continue
            self.cli(home, "runs", "cleanup", run_id)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                report = json.loads(self.cli(home, "runs", "show", run_id).stdout)
                if report.get("cleanup", {}).get("finished_at_ms") is not None:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError(
                    "Failed-run cleanup did not finish before uninstall"
                )
        self.failed_runs = [item for item in self.failed_runs if item[0] != home]
        self.cli(home, "plugins", "uninstall", "tika.http", "--version", VERSION)
        assert "tika.http" not in self.cli(home, "plugins", "list").stdout
        assert (
            self.cli(home, "plugins", "inspect", "tika.http", ok=False).returncode != 0
        )
        assert not list((home / "plugins").rglob("plugin.yaml"))
        self.passed(f"{home.name}: uninstall/list/inspect absence")

    def acceptance(self) -> None:
        """Run every named Step 3 acceptance in standalone and signed/governed homes."""
        package = self.package("deployment")
        self.key, self.trust = self.private / "signing.key", self.private / "trust.pub"
        self.command(
            [
                self.admin,
                "key",
                "generate",
                "--name",
                "tika-step3",
                "--private-key-file",
                str(self.key),
            ]
        )
        self.command(
            [
                self.admin,
                "key",
                "export-public",
                "--private-key-file",
                str(self.key),
                "--trust-root-file",
                str(self.trust),
            ]
        )
        self.command(
            [
                self.admin,
                "plugin",
                "sign",
                str(package),
                "--private-key-file",
                str(self.key),
            ]
        )
        self.command(
            [
                self.admin,
                "plugin",
                "verify",
                str(package),
                "--signature",
                str(package.with_suffix(".sig")),
                "--trust-root-file",
                str(self.trust),
            ]
        )
        # Governed homes correctly reject unsigned authored source. Validate
        # source in a disposable authoring home, then check signed installed code
        # and exact capabilities in the governed execution home.
        self.authoring = self.init_home("authoring", "standalone", package)
        for mode in ("standalone", "governed"):
            home = self.init_home(mode, mode, package)
            name = f"tika_{mode}"
            source = self.flow(name)
            inputs, _, _ = self.inputs("txt")
            self.install_flow(home, name, source, inputs, governed=mode == "governed")
            for suffix in ("txt", "pdf", "docx"):
                inputs, data, content_type = self.inputs(suffix)
                report = self.run(home, name, f"{mode}-{suffix}", inputs)
                assert report["runtime_mode"] == mode
                if mode == "governed":
                    assert report["trusted_execution_mode"] is True
                    assert report["policy_profile"] == "allowlist"
                    assert report["capability_enforcement"] == "enforced"
                self.assert_outputs(report, data, content_type)
                self.passed(
                    f"{mode}/{suffix}: run/poll/text/metadata/document checksum"
                )
            inputs, _, _ = self.inputs("txt")
            self.negative(
                home,
                name,
                inputs,
                mode + "-bad-credential",
                ("401", "unauthorized"),
                credential=self.bad_authorization,
            )
            self.negative(
                home,
                name,
                inputs,
                mode + "-local-only",
                ("local_only_denied_external", "local-only"),
                local_only=True,
            )
            config = home / "config.yaml"
            saved = config.read_text()
            try:
                config.write_text(saved.replace("http: true", "http: false"))
                self.negative(
                    home,
                    name,
                    inputs,
                    mode + "-secret-denied",
                    ("remote_secret_policy", "remote_secret_delivery"),
                )
            finally:
                config.write_text(saved)
            denied_name = f"tika_{mode}_network_denied"
            denied = self.flow(denied_name, deny_network=True)
            self.cli(
                self.authoring if mode == "governed" else home,
                "workflows",
                "check",
                "--path",
                str(denied),
            )
            if mode == "governed":
                release = self.root / (denied_name + "-release")
                self.command(
                    [
                        self.admin,
                        "workflow",
                        "package",
                        str(denied),
                        "--version",
                        "0.1.0",
                        "--private-key-file",
                        str(self.key),
                        "--output",
                        str(release),
                    ]
                )
                self.cli(
                    home, "workflows", "install", "--package", str(release), "--approve"
                )
                # Missing authority is independent of the authored network ceiling.
                # Authorize only the exact plan; do not widen network:none.
                self.cli(
                    home,
                    "workflows",
                    "authorize",
                    denied_name + "@0.1.0",
                    "--inputs-file",
                    str(inputs),
                    "--approve",
                    ok=False,
                )
            else:
                self.cli(
                    home, "workflows", "install", "--path", str(denied), "--approve"
                )
            before = self.get("/fixture/counts")
            self.negative(
                home,
                denied_name,
                inputs,
                mode + "-network-denied",
                (
                    "network_policy",
                    "network policy",
                    "network access",
                    "network denied",
                ),
            )
            assert self.get("/fixture/counts") == before, "Network denial contacted APP"
            self.uninstall(home)
            for case, ca, host, reason in (
                ("untrusted-ca", False, "127.0.0.1", ("adapter_handshake",)),
                (
                    "wrong-hostname",
                    True,
                    "localhost",
                    ("adapter_handshake",),
                ),
            ):
                bad_package = self.package(mode + "-" + case, ca=ca, hostname=host)
                if mode == "governed":
                    self.command(
                        [
                            self.admin,
                            "plugin",
                            "sign",
                            str(bad_package),
                            "--private-key-file",
                            str(self.key),
                        ]
                    )
                bad_home = self.init_home(
                    mode + "-" + case + "-home", mode, bad_package
                )
                bad_name = f"tika_{mode}_{case.replace('-', '_')}"
                bad_flow = self.flow(
                    bad_name,
                    host=host,
                    extra_hosts=(
                        ("127.0.0.1",)
                        if mode == "governed" and host != "127.0.0.1"
                        else ()
                    ),
                )
                # TLS readiness is expected to fail; installed workflow dispatch
                # must independently fail too. No success readiness is manufactured.
                self.cli(
                    self.authoring if mode == "governed" else bad_home,
                    "workflows",
                    "check",
                    "--path",
                    str(bad_flow),
                )
                if mode == "governed":
                    # Generate exact authority in a separate valid-TLS home.
                    # Transfer the CLI-generated fragment unchanged to isolate
                    # TLS, without rewriting grants or reusing a version for
                    # different bytes in any one home. No run/asset lock is born.
                    authority_home = self.init_home(
                        mode + "-" + case + "-authority", mode, package
                    )
                    self.install_flow(
                        authority_home, bad_name, bad_flow, inputs, governed=True
                    )
                    fragments = list(
                        (authority_home / "policy/workflow-authorizations").glob(
                            "*.yaml"
                        )
                    )
                    assert len(fragments) == 1, "Expected one exact authority fragment"
                    destination = bad_home / "policy/workflow-authorizations"
                    destination.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(fragments[0], destination / fragments[0].name)
                    assert (destination / fragments[0].name).read_bytes() == fragments[
                        0
                    ].read_bytes()
                    release = self.root / (bad_name + "-release")
                    self.cli(
                        bad_home,
                        "workflows",
                        "install",
                        "--package",
                        str(release),
                        "--approve",
                    )
                else:
                    self.cli(
                        bad_home,
                        "workflows",
                        "install",
                        "--path",
                        str(bad_flow),
                        "--approve",
                    )
                assert self.get("/app/health") == {"status": "ok"}
                before = self.get("/fixture/counts")
                self.verify_tls_failure(case, host, ca=ca)
                self.negative(bad_home, bad_name, inputs, mode + "-" + case, reason)
                assert (
                    self.get("/fixture/counts") == before
                ), "TLS failure sent an HTTP request"
                self.uninstall(bad_home)
        # A stale valid signature must reject modified selected bytes on install.
        with (package / "service.py").open("a") as file:
            file.write("\n# deliberate signed-payload tamper\n")
        verify = self.command(
            [
                self.admin,
                "plugin",
                "verify",
                str(package),
                "--signature",
                str(package.with_suffix(".sig")),
                "--trust-root-file",
                str(self.trust),
            ],
            ok=False,
        )
        assert (
            verify.returncode != 0
            and "mismatch" in (verify.stdout + verify.stderr).lower()
        )
        home = self.root / "tamper-home"
        self.homes.append(home)
        self.cli(home, "home", "init", "--mode", "governed")
        self.cli(home, "trust", "import", str(self.trust))
        rejected = self.cli(
            home, "plugins", "install", "--path", str(package), ok=False
        )
        assert rejected.returncode != 0
        assert "tika.http" not in self.cli(home, "plugins", "list").stdout
        self.passed(
            "signed selected-payload tampering rejected by admin and governed install"
        )
        self.scan_client()
        self.passed(
            "runtime records/logs free of raw/encoded credentials "
            "and prefix-free tokens"
        )
        self.passed("no plugin-created client documents or invocation staging")

    def cleanup(self) -> None:
        """Collect logs and remove only owned resources, including on failure."""
        if self.compose:
            try:
                self.command(self.compose + ["logs", "--no-color"], ok=False)
            finally:
                self.command(
                    self.compose + ["down", "--volumes", "--remove-orphans"],
                    timeout=120,
                )
            for kind, before in self.before.items():
                assert before <= self.docker_resources(
                    kind
                ), f"Unrelated {kind} removed"
                commands = {
                    "containers": ["docker", "ps", "-aq"],
                    "volumes": ["docker", "volume", "ls", "-q"],
                    "networks": ["docker", "network", "ls", "-q"],
                }
                left = self.command(
                    commands[kind]
                    + ["--filter", f"label=com.docker.compose.project={self.project}"]
                )
                assert not left.stdout.strip(), f"Owned {kind} remain"
            self.summary["cleanup"] = (
                "owned resources removed; unrelated resources preserved"
            )
        try:
            if self.needles:
                # Failed acceptance still removes installed plugin payloads from
                # disposable homes. Runtime records remain as private evidence.
                for home in self.homes:
                    if list((home / "plugins").rglob("plugin.yaml")):
                        self.uninstall(home)
                self.scan_client()
        finally:
            shutil.rmtree(self.private)

    def main(self) -> None:
        """Fail closed with partial evidence; acceptance requires every assertion."""
        print(f"Step 3 evidence: {self.root}", flush=True)
        try:
            self.prerequisites()
            self.start_fixture()
            self.acceptance()
            self.summary["accepted"] = True
        except Exception as error:
            self.summary["blocker"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            try:
                self.cleanup()
            except Exception as error:
                self.summary["accepted"] = False
                self.summary["cleanup_blocker"] = f"{type(error).__name__}: {error}"
                raise
            finally:
                # Credential assets must be removed even if Docker teardown or
                # a persistence scan fails. Logs/homes remain private evidence.
                shutil.rmtree(self.private, ignore_errors=True)
                (self.root / "summary.json").write_text(
                    json.dumps(self.summary, indent=2) + "\n"
                )
        print(f"PASS: ALL named Step 3 acceptance; evidence: {self.root}", flush=True)


if __name__ == "__main__":
    E2E().main()
