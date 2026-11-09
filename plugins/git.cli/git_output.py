"""Package-local compact results; command lifecycle remains owned by the runner."""

import json

MAX_DIAGNOSTIC_BYTES = 256 * 1024


def json_bytes(value: object) -> int:
    """Count UTF-8 bytes with the APP emitter's escaping and spacing, no newline."""
    return len(json.dumps(value, allow_nan=False).encode("utf-8"))


def validate_options(payload: dict) -> None:
    """Reject unsupported modes and non-positive optional byte budgets."""
    if payload.get("output_mode", "legacy") not in ("legacy", "compact"):
        raise ValueError("output_mode must be legacy or compact")
    for name in ("max_capture_bytes", "max_diagnostic_bytes"):
        value = payload.get(name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError(f"{name} must be a positive integer")


def compact_result(request: dict, result: dict) -> dict:
    """Keep business output, record each command once, and count final APP bytes."""
    payload = request.get("input") or {}
    if payload.get("output_mode", "legacy") != "compact":
        return result
    output = result["output"]
    commands = request.get("_diagnostic_commands", [])
    if (
        not commands
        and output
        and any(k in output for k in ("stdout", "stderr", "exit_code"))
    ):
        commands = [
            {key: output.get(key, "") for key in ("stdout", "stderr", "exit_code")}
        ]
    contents = json.dumps({"commands": commands}, allow_nan=False)
    size = len(contents.encode("utf-8"))
    cap = min(
        payload.get("max_diagnostic_bytes", MAX_DIAGNOSTIC_BYTES), MAX_DIAGNOSTIC_BYTES
    )
    retained = bool(commands) and size <= cap
    if result.get("error", {}).get("code") != "protocol_error":
        result["artifact_hints"] = (
            [{"name": "diagnostics.json", "contents": contents}] if retained else []
        )
    if output:
        output.pop("stdout", None)
        output.pop("stderr", None)
        output["output_mode"] = "compact"
        output["diagnostics"] = {
            "artifact": "diagnostics.json" if retained else "",
            "bytes": size,
            "retained": retained,
            "stdout_bytes": sum(len(c["stdout"].encode("utf-8")) for c in commands),
            "stderr_bytes": sum(len(c["stderr"].encode("utf-8")) for c in commands),
        }
    if result.get("error", {}).get("code") == "tool_execution_error":
        message = result["error"]["message"]
        preview = message.encode("utf-8")[:2048].decode("utf-8", errors="ignore")
        result["error"]["message"] = preview + (
            " [diagnostic preview; see artifact if retained]"
            if preview != message
            else ""
        )
    result["metrics"].update(
        input_bytes=json_bytes(payload), output_bytes=json_bytes(output)
    )
    result["metrics"]["result_bytes"] = 0
    while result["metrics"]["result_bytes"] != json_bytes(result):
        result["metrics"]["result_bytes"] = json_bytes(result)
    return result
