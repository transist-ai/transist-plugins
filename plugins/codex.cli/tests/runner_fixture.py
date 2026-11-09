"""Adapt in-memory smoke fixtures without launching provider commands."""

import subprocess
from collections.abc import Callable


def fixture_runner(factory: Callable) -> Callable:
    """Return a caller-thread callback runner backed by an in-memory fixture."""

    def run(command: list, **kwargs) -> subprocess.CompletedProcess:
        process = factory(command, **kwargs)
        stdout = process.stdout.getvalue()
        stderr = process.stderr.getvalue()
        if process.stdin is not None:
            process.stdin.write(kwargs.get("input") or "")
        for stream, text in (("stdout", stdout), ("stderr", stderr)):
            for line in text.splitlines(keepends=True):
                kwargs["on_line"](stream, line)
        try:
            code = process.wait(timeout=kwargs.get("timeout"))
        except subprocess.TimeoutExpired as err:
            raise subprocess.TimeoutExpired(
                command, err.timeout, output=stdout, stderr=stderr
            ) from err
        kwargs["on_tick"]()
        return subprocess.CompletedProcess(command, code, stdout, stderr)

    return run
