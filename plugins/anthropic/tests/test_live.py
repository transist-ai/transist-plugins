"""Offline configuration, one-invocation and confidentiality tests for live smoke."""

import base64
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "anthropic_live", pathlib.Path(__file__).with_name("live.py")
)
live = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live)

KEY = "live-credential-canary-\u00ff"
MODEL = "externally-selected-model"
PRIVATE = "private provider output / exception / error code"


def completed(response: object = None) -> subprocess.CompletedProcess:
    """Build captured APP success frames, or a caller-supplied malformed result."""
    if response is None:
        response = {
            "type": "app.invoke.result.v1",
            "protocol_version": "app.v1",
            "invoke_id": "live-smoke",
            "status": "success",
            "output": {"response_text": PRIVATE},
        }
    handshake = {
        "type": "app.handshake.response.v1",
        "protocol_version": "app.v1",
        "plugin": {"id": "anthropic", "version": "0.1.0"},
    }
    return subprocess.CompletedProcess(
        [], 0, json.dumps(handshake) + "\n" + json.dumps(response) + "\n", ""
    )


class LiveTests(unittest.TestCase):
    """Mock the subprocess boundary so no test can contact the provider."""

    def run_live(self, env: dict, **kwargs: object) -> tuple:
        """Run with an isolated environment and capture only runner diagnostics."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(live.subprocess, "run", **kwargs) as run,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = live.main()
        return status, stdout.getvalue(), stderr.getvalue(), run

    def test_missing_configuration(self) -> None:
        """Either absent/empty value skips, or fails when explicitly required."""
        for key, model in (
            (None, MODEL),
            (KEY, None),
            (None, None),
            ("", MODEL),
            (KEY, ""),
        ):
            for required in (None, "0", "1"):
                with self.subTest(
                    key_present=bool(key), model=model, required=required
                ):
                    env = {}
                    for name, value in (
                        ("ANTHROPIC_API_KEY", key),
                        ("ANTHROPIC_TEST_MODEL", model),
                        ("REQUIRE_LIVE_PROVIDER", required),
                    ):
                        if value is not None:
                            env[name] = value
                    status, out, err, run = self.run_live(env)
                    self.assertEqual(status, int(required == "1"))
                    self.assertEqual(
                        out, "" if required == "1" else live.SKIPPED + "\n"
                    )
                    self.assertEqual(
                        err, live.REQUIRED + "\n" if required == "1" else ""
                    )
                    run.assert_not_called()

    def test_success_and_exact_request(self) -> None:
        """External inputs produce one official bounded request without disclosure."""
        env = {
            "ANTHROPIC_API_KEY": KEY,
            "ANTHROPIC_TEST_MODEL": MODEL,
            "REQUIRE_LIVE_PROVIDER": "1",
            "ANTHROPIC_BASE_URL": "http://unapproved.invalid",
        }
        status, out, err, run = self.run_live(env, return_value=completed())
        self.assertEqual((status, out, err), (0, live.PASSED + "\n", ""))
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(
            args[0], [live.sys.executable, "-B", str(live.ROOT / "plugin.py")]
        )
        self.assertEqual(kwargs["timeout"], 45)
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["env"]["ANTHROPIC_API_KEY"], KEY)
        self.assertNotIn("ANTHROPIC_TEST_MODEL", kwargs["env"])
        self.assertNotIn("REQUIRE_LIVE_PROVIDER", kwargs["env"])
        handshake, request = map(json.loads, kwargs["input"].splitlines())
        self.assertEqual(
            handshake,
            {"type": "app.handshake.request.v1", "protocol_version": "app.v1"},
        )
        self.assertEqual(request["action"], "chat_complete")
        self.assertEqual(request["invoke_id"], "live-smoke")
        self.assertEqual(
            request["input"],
            {
                "base_url": "https://api.anthropic.com",
                "model": MODEL,
                "messages": [
                    {"role": "user", "content": "Reply with a brief greeting."}
                ],
                "max_tokens": 16,
                "timeout_ms": 30000,
                "secret_refs": ["ANTHROPIC_API_KEY"],
            },
        )
        self.assertEqual(
            request["policy"],
            {
                "network": {
                    "mode": "allowlist",
                    "allow_hosts": ["api.anthropic.com"],
                    "allow_ports": [443],
                    "tls_required": True,
                }
            },
        )
        self.assertNotIn(KEY, kwargs["input"])

    def test_unusable_results_are_fixed_failures_without_retry(self) -> None:
        """Provider data and malformed frames never escape fixed diagnostics."""
        failures = [
            completed(value)
            for value in (
                [],
                PRIVATE,
                {},
                {"status": "failed", "error": {"code": PRIVATE}},
            )
        ]
        for output in (
            None,
            [],
            {"response_text": None},
            {"response_text": " \n"},
            {"response_text": 12},
        ):
            response = json.loads(completed().stdout.splitlines()[-1])
            response["output"] = output
            failures.append(completed(response))
        for field in ("type", "protocol_version", "invoke_id", "status"):
            response = json.loads(completed().stdout.splitlines()[-1])
            response[field] = PRIVATE
            failures.append(completed(response))
        for stdout in ("", PRIVATE, "{}\n", completed().stdout + "{}\n"):
            failures.append(subprocess.CompletedProcess([], 0, stdout, ""))
        failures.extend(
            [
                subprocess.CompletedProcess([], 1, PRIVATE, PRIVATE),
                subprocess.CompletedProcess([], 0, completed().stdout, PRIVATE),
            ]
        )
        variants = {KEY}
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            value = encode(KEY.encode()).decode()
            variants.update((value, value.rstrip("=")))
        for value in variants:
            response = json.loads(completed().stdout.splitlines()[-1])
            response["output"]["response_text"] = value
            failures.append(completed(response))
        for index, failure in enumerate(failures):
            with self.subTest(failure_index=index):
                status, out, err, run = self.run_live(
                    {"ANTHROPIC_API_KEY": KEY, "ANTHROPIC_TEST_MODEL": MODEL},
                    return_value=failure,
                )
                self.assertEqual((status, out, err), (1, "", live.FAILED + "\n"))
                run.assert_called_once()

    def test_subprocess_exceptions_are_sanitized(self) -> None:
        """Timeout partial output and startup/decoding exceptions stay private."""
        for failure in (
            subprocess.TimeoutExpired(PRIVATE, 45, output=KEY, stderr=PRIVATE),
            OSError(PRIVATE),
            UnicodeError(PRIVATE),
        ):
            with self.subTest(exception_type=type(failure).__name__):
                status, out, err, run = self.run_live(
                    {"ANTHROPIC_API_KEY": KEY, "ANTHROPIC_TEST_MODEL": MODEL},
                    side_effect=failure,
                )
                self.assertEqual((status, out, err), (1, "", live.FAILED + "\n"))
                run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
