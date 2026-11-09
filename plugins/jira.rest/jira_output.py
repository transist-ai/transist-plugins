"""Opt-in normalized Jira output with a bounded raw-response diagnostic hint."""

import json

MAX_DIAGNOSTIC_BYTES = 256 * 1024


def json_bytes(value: object) -> int:
    """Count final APP serialization bytes with escaping, excluding its newline."""
    return len(json.dumps(value, allow_nan=False).encode("utf-8"))


def project_output(output: dict, payload: dict) -> tuple[dict, list]:
    """Remove only raw duplicates; preserve normalized attachments and page tokens."""
    if payload.get("output_mode", "legacy") != "compact":
        return output, []
    projected = {k: v for k, v in output.items() if k != "raw"}
    raw = output.get("raw")
    contents = json.dumps(raw, allow_nan=False) if raw is not None else ""
    size = len(contents.encode("utf-8"))
    cap = min(
        payload.get("max_diagnostic_bytes", MAX_DIAGNOSTIC_BYTES), MAX_DIAGNOSTIC_BYTES
    )
    retained = bool(contents) and size <= cap
    projected["output_mode"] = "compact"
    projected["diagnostics"] = {
        "artifact": "response.json" if retained else "",
        "bytes": size,
        "retained": retained,
    }
    hints = [{"name": "response.json", "contents": contents}] if retained else []
    return projected, hints


def compact_result(request: dict, result: dict) -> dict:
    """Finalize compact results, leaving legacy shapes and metrics unchanged."""
    payload = request.get("input") or {}
    if payload.get("output_mode", "legacy") != "compact":
        return result
    if result["output"]:
        result["output"], result["artifact_hints"] = project_output(
            result["output"], payload
        )
    result["metrics"].update(
        input_bytes=json_bytes(payload), output_bytes=json_bytes(result["output"])
    )
    result["metrics"]["result_bytes"] = 0
    while result["metrics"]["result_bytes"] != json_bytes(result):
        result["metrics"]["result_bytes"] = json_bytes(result)
    return result
