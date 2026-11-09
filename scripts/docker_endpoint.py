"""Resolve only a local Docker socket before installed runners isolate HOME."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
from urllib.parse import urlsplit


def local_docker_host() -> str:
    """Return an explicit UNIX socket or the parent CLI context's public endpoint.

    No Docker config, credentials or context files are copied to fixture homes.
    Missing sockets, failed context lookup and remote endpoints fail prerequisites.
    """
    daemon = os.environ.get("DOCKER_HOST")
    if daemon is None:
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"HOME", "PATH", "DOCKER_CONFIG", "DOCKER_CONTEXT", "SYSTEMROOT"}
        }
        result = subprocess.run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode:
            raise ValueError("local Docker context lookup failed")
        daemon = result.stdout.strip()
    parsed = urlsplit(daemon)
    if (
        not daemon.startswith("unix:///")
        or any(ord(char) < 33 for char in daemon)
        or parsed.scheme != "unix"
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not Path(parsed.path).is_absolute()
    ):
        raise ValueError("explicit local UNIX Docker socket required")
    if not stat.S_ISSOCK(Path(parsed.path).stat().st_mode):
        raise ValueError("Docker endpoint is not a UNIX socket")
    return daemon
