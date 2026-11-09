"""Compact shell metadata; stdout and promoted business fields remain exact."""

import json


def json_bytes(value: object) -> int:
    """Count APP JSON bytes with escaping and spacing, excluding the newline."""
    return len(json.dumps(value, allow_nan=False).encode("utf-8"))


def compact_result(request: dict, result: dict) -> dict:
    """Suppress duplicate stream hints, retaining full semantic output and evidence."""
    payload = request.get("input") or {}
    if payload.get("output_mode", "legacy") != "compact":
        return result
    output = result["output"]
    if "stdout" in output:
        # These fixed metadata fields override any same-named promoted JSON keys.
        output["output_mode"] = "compact"
        output["diagnostics"] = {
            "stdout_ref": "output.stdout",
            "stderr_ref": "output.stderr",
            "stdout_bytes": len(output["stdout"].encode("utf-8")),
            "stderr_bytes": len(output["stderr"].encode("utf-8")),
        }
        result["artifact_hints"] = [
            hint
            for hint in result["artifact_hints"]
            if hint["name"] not in ("stdout.txt", "stderr.txt")
        ]
    result["metrics"].update(
        input_bytes=json_bytes(payload), output_bytes=json_bytes(output)
    )
    # emit() omits optional nulls; account for exactly that final serialization.
    result = {
        k: v for k, v in result.items() if k not in ("usage", "error") or v is not None
    }
    result["metrics"]["result_bytes"] = 0
    while result["metrics"]["result_bytes"] != json_bytes(result):
        result["metrics"]["result_bytes"] = json_bytes(result)
    return result
