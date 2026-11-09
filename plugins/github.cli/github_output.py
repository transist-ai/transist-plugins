"""Compact projection for GitHub results without changing handoff control fields."""

import json

MAX_DIAGNOSTIC_BYTES = 256 * 1024


def json_bytes(value: object) -> int:
    """Count serialized APP bytes, including JSON escapes, excluding the newline."""
    return len(json.dumps(value, allow_nan=False).encode("utf-8"))


def validate_options(payload: dict) -> None:
    """Validate the opt-in mode and positive retrieval/diagnostic budgets."""
    if payload.get("output_mode", "legacy") not in ("legacy", "compact"):
        raise ValueError("output_mode must be legacy or compact")
    for name in (
        "max_response_bytes",
        "max_total_response_bytes",
        "max_comment_pages",
        "max_comments",
        "max_plan_bytes",
        "max_replies",
        "max_diagnostic_bytes",
    ):
        value = payload.get(name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError(f"{name} must be a positive integer")


def project_output(output: dict, payload: dict, commands: list) -> tuple[dict, list]:
    """Project full business data and a single capped, nonduplicated diagnostic hint."""
    if payload.get("output_mode", "legacy") != "compact":
        return output, []
    projected = {k: v for k, v in output.items() if k not in ("stdout", "stderr")}
    contents = json.dumps({"commands": commands}, allow_nan=False)
    size = len(contents.encode("utf-8"))
    cap = min(
        payload.get("max_diagnostic_bytes", MAX_DIAGNOSTIC_BYTES), MAX_DIAGNOSTIC_BYTES
    )
    retained = bool(commands) and size <= cap
    projected["output_mode"] = "compact"
    projected["diagnostics"] = {
        "artifact": "diagnostics.json" if retained else "",
        "bytes": size,
        "retained": retained,
        "stdout_bytes": sum(len(c["stdout"].encode("utf-8")) for c in commands),
        "stderr_bytes": sum(len(c["stderr"].encode("utf-8")) for c in commands),
    }
    hints = [{"name": "diagnostics.json", "contents": contents}] if retained else []
    return projected, hints


def compact_result(request: dict, result: dict) -> dict:
    """Finalize compact output and measure the final serialized result honestly."""
    payload = request.get("input") or {}
    if payload.get("output_mode", "legacy") != "compact":
        return result
    commands = request.get("_diagnostic_commands", [])
    if result["output"]:
        result["output"], result["artifact_hints"] = project_output(
            result["output"], payload, commands
        )
    elif result.get("error", {}).get("code") != "protocol_error":
        _, result["artifact_hints"] = project_output({}, payload, commands)
    if result.get("error", {}).get("code") == "tool_execution_error":
        message = result["error"]["message"]
        preview = message.encode("utf-8")[:2048].decode("utf-8", errors="ignore")
        result["error"]["message"] = preview + (
            " [diagnostic preview; see artifact if retained]"
            if preview != message
            else ""
        )
    result["metrics"].update(
        input_bytes=json_bytes(payload), output_bytes=json_bytes(result["output"])
    )
    result["metrics"]["result_bytes"] = 0
    while result["metrics"]["result_bytes"] != json_bytes(result):
        result["metrics"]["result_bytes"] = json_bytes(result)
    return result
