#!/usr/bin/env python3
import importlib.util
import io
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
from typing import Any
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest import mock

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CANONICAL_ERRORS = {
    "configuration_error": {"class": "configuration", "retryability": "none"},
    "auth_error": {"class": "auth", "retryability": "operator"},
    "secret_resolution_error": {"class": "secret", "retryability": "operator"},
    "validation_error": {"class": "validation", "retryability": "none"},
    "timeout_error": {"class": "timeout", "retryability": "safe"},
    "cancelled": {"class": "cancellation", "retryability": "none"},
    "rate_limit_error": {"class": "rate_limit", "retryability": "safe"},
    "transport_error": {"class": "transport", "retryability": "safe"},
    "protocol_error": {"class": "protocol", "retryability": "none"},
    "tool_execution_error": {"class": "tool", "retryability": "safe"},
    "adapter_internal_error": {"class": "internal", "retryability": "operator"},
}


def read_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_plugin_module(plugin_dir):
    module_name = f"transist_plugins_{plugin_dir.name.replace('.', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, plugin_dir / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def app_invoke_request(action, input_payload, artifacts_dir=None):
    if artifacts_dir is None:
        artifacts_dir = tempfile.mkdtemp(prefix="transist-plugin-artifacts-")
    return {
        "type": "app.invoke.request.v1",
        "protocol_version": "app.v1",
        "invoke_id": "smoke-invoke",
        "run_id": "smoke-root__child",
        "workflow_id": "smoke-workflow",
        "step": {"id": "smoke-step", "execution": 1},
        "plugin": {"id": "smoke-plugin", "version": "0.1.0"},
        "action": action,
        "input": input_payload,
        "requested_capabilities": [],
        "policy": {},
        "metadata": {},
        "paths": {
            "workspace_dir": str(REPO_ROOT),
            "artifacts_dir": str(artifacts_dir),
            "evidence_dir": str(artifacts_dir),
            "run_root": str(artifacts_dir),
        },
    }


def run_process_plugin_main(module, invoke_request, patchers=None):
    handshake_request = {
        "type": "app.handshake.request.v1",
        "protocol_version": "app.v1",
    }
    stdin = io.StringIO(
        json.dumps(handshake_request) + "\n" + json.dumps(invoke_request) + "\n"
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    with ExitStack() as stack:
        stack.enter_context(mock.patch("sys.stdin", stdin))
        stack.enter_context(mock.patch("sys.stdout", stdout))
        stack.enter_context(mock.patch("sys.stderr", stderr))
        for patcher in patchers or []:
            stack.enter_context(patcher)
        exit_code = module.main()
    frames = [
        json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()
    ]
    return exit_code, frames, stderr.getvalue()


@contextmanager
def resolved_runtime_temp_environment(
    plugin_dir: pathlib.Path,
) -> Iterator[dict[str, str]]:
    """Resolve manifest runtime://temp environment values for a direct test process."""
    manifest_text = (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    declarations = re.findall(
        r"^\s+([A-Za-z_][A-Za-z0-9_]*):\s+runtime://temp(?:/([^\s#]+))?\s*$",
        manifest_text,
        flags=re.MULTILINE,
    )
    with tempfile.TemporaryDirectory(prefix=f"{plugin_dir.name}-runtime-") as root_text:
        root = pathlib.Path(root_text)
        environment = {}
        for name, relative_path in declarations:
            path = root.joinpath(*(relative_path.split("/") if relative_path else []))
            path.mkdir(parents=True, exist_ok=True)
            environment[name] = str(path)
        yield environment


def run_process_plugin_subprocess(plugin_dir, invoke_request, timeout=60):
    with resolved_runtime_temp_environment(plugin_dir) as runtime_environment:
        environment = os.environ.copy()
        environment.update(runtime_environment)
        process = subprocess.Popen(
            [sys.executable, str(plugin_dir / "plugin.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=REPO_ROOT,
            env=environment,
            start_new_session=True,
        )
        try:
            handshake_request = {
                "type": "app.handshake.request.v1",
                "protocol_version": "app.v1",
            }
            payload = (
                json.dumps(handshake_request) + "\n" + json.dumps(invoke_request) + "\n"
            )
            stdout, stderr = process.communicate(payload, timeout=timeout)
        finally:
            # Own the entire session, even if its leader exited or communicate
            # raised cancellation/another exception. Reap before temp cleanup.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=2)
            except (subprocess.TimeoutExpired, ValueError, OSError):
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            finally:
                process.wait()
        frames = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        return process.returncode, frames, stderr


def contract_validator(relative: str) -> Draft202012Validator:
    """Compile a vendored APP contract with offline-only local references."""
    directory = REPO_ROOT / "tests" / "contracts"
    resources = []
    for filename in directory.rglob("*.schema.json"):
        schema = read_json(filename)
        resource = Resource.from_contents(schema)
        resources.append((filename.as_uri(), resource))
        if "$id" in schema:
            resources.append((schema["$id"], resource))
    registry = Registry().with_resources(resources)
    target = directory / relative
    return Draft202012Validator({"$ref": target.as_uri()}, registry=registry)


def package_schema_validator(
    schema_path: pathlib.Path, schema: dict[str, Any] | None = None
) -> Draft202012Validator:
    """Compile a package schema/subschema using only contained local resources."""
    schema_path = schema_path.resolve()
    resources = []
    for filename in schema_path.parent.rglob("*.schema.json"):
        contents = read_json(filename)
        Draft202012Validator.check_schema(contents)
        resources.append((filename.as_uri(), Resource.from_contents(contents)))
    registry = Registry().with_resources(resources)
    target = {"$ref": schema_path.as_uri()}
    if schema is not None:
        target = {"$id": schema_path.as_uri(), **schema}
    return Draft202012Validator(target, registry=registry)


def validate_json_schema(
    schema: dict[str, Any],
    payload: Any,
    path: str = "payload",
    *,
    schema_path: pathlib.Path | None = None,
) -> None:
    """Validate Draft 2020-12 fully; report paths without instance values."""
    Draft202012Validator.check_schema(schema)
    validator = (
        package_schema_validator(schema_path, schema)
        if schema_path is not None
        else Draft202012Validator(schema, registry=Registry())
    )
    error = next(validator.iter_errors(payload), None)
    if error:
        location = "/".join(str(part) for part in error.absolute_path)
        raise AssertionError(f"{path}/{location}: invalid {error.validator}")


def assert_app_frames(frames, plugin_id):
    if len(frames) < 2:
        raise AssertionError(f"expected at least 2 APP frames, got {len(frames)}")
    handshake = frames[0]
    progress = frames[1:-1]
    result = frames[-1]
    contract_validator("app/handshake.response.v1.schema.json").validate(handshake)
    contract_validator("app/invoke.result.v1.schema.json").validate(result)
    if handshake["plugin"]["id"] != plugin_id:
        raise AssertionError(
            f"unexpected handshake plugin id: {handshake['plugin']['id']}"
        )
    for frame in progress:
        contract_validator("app/invoke.progress.v1.schema.json").validate(frame)
        if frame["type"] != "app.invoke.progress.v1":
            raise AssertionError(f"unexpected intermediate frame type: {frame['type']}")
    if result["type"] != "app.invoke.result.v1":
        raise AssertionError("unexpected invoke result type")
    return handshake, result


def assert_canonical_error(error, expected_code=None, message_contains=None):
    if not isinstance(error, dict):
        raise AssertionError(f"expected error payload dict, got {type(error)}")
    code = error.get("code")
    if code not in CANONICAL_ERRORS:
        raise AssertionError(f"unexpected canonical error code: {code}")
    expected = CANONICAL_ERRORS[code]
    if error.get("class") != expected["class"]:
        raise AssertionError(
            f"unexpected error class for {code}: "
            f"{error.get('class')} != {expected['class']}"
        )
    if error.get("retryability") != expected["retryability"]:
        raise AssertionError(
            "unexpected retryability for "
            f"{code}: {error.get('retryability')} != {expected['retryability']}"
        )
    if expected_code is not None and code != expected_code:
        raise AssertionError(f"unexpected error code: {code} != {expected_code}")
    message = str(error.get("message") or "")
    for needle in message_contains or []:
        if needle not in message:
            raise AssertionError(f"expected '{needle}' in error message: {message}")
    return error


def completed_process(command, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        command, returncode, stdout=stdout, stderr=stderr
    )
