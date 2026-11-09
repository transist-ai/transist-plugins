#!/usr/bin/env python3
"""Offline APP handshake, schema, and invalid-input smoke checks."""

import pathlib
import sys
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "tests"))
from support import (  # noqa: E402
    app_invoke_request,
    assert_app_frames,
    assert_canonical_error,
    read_json,
    run_process_plugin_subprocess,
    validate_json_schema,
)


def main():
    """Verify package payload and subprocess APP validation without egress."""
    for item in (
        "plugin.yaml",
        "plugin.py",
        "schemas/chat_complete.input.schema.json",
        "schemas/chat_complete.output.schema.json",
    ):
        assert (ROOT / item).is_file(), item
    schema = read_json(ROOT / "schemas/chat_complete.input.schema.json")
    assert schema["$schema"].endswith("2020-12/schema")
    request = app_invoke_request("chat_complete", {"model": "m", "messages": []})
    with mock.patch.dict("os.environ", {}, clear=False):
        code, frames, stderr = run_process_plugin_subprocess(ROOT, request)
    assert code == 0 and not stderr, (code, stderr)
    handshake, response = assert_app_frames(frames, "openai.compatible")
    assert handshake["plugin"]["version"] == "0.1.0"
    assert handshake["capabilities"]["actions"] == ["chat_complete"]
    assert_canonical_error(response["error"], "validation_error")
    try:
        validate_json_schema(schema, request["input"])
    except AssertionError:
        pass
    else:
        raise AssertionError("invalid input unexpectedly matched schema")
    print("openai.compatible smoke passed")


if __name__ == "__main__":
    main()
