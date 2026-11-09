"""Credential scanning and native sandbox plans; no mocked Linux acceptance."""

import base64
import copy
import json
from pathlib import Path
import unittest

from e2e import (
    E2E,
    failure_details,
    linux_probe_command,
    sandbox_backend,
    sandbox_executable,
    variants,
    verify_sandbox_readiness,
)


class CredentialScanTests(unittest.TestCase):
    """Ensure a token without Bearer and encoded variants cannot evade the scan."""

    def test_prefix_free_token_variants_are_rejected(self) -> None:
        """Test both padding alphabets and raw token, independently of full header."""
        token = "fixture-only-token-" + "x" * 33
        authorization = "Bearer " + token
        runner = E2E.__new__(E2E)
        runner.needles = variants(authorization) | variants(token)
        for value in (authorization, token):
            raw = value.encode()
            for encoded in (raw, base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
                for candidate in (encoded, encoded.rstrip(b"=")):
                    with self.subTest(length=len(candidate)):
                        with self.assertRaisesRegex(AssertionError, "value withheld"):
                            runner.scan_bytes(b"prefix " + candidate + b" suffix")

    def test_safe_records_are_accepted(self) -> None:
        """Allow logical secret refs and redacted records without false leakage."""
        runner = E2E.__new__(E2E)
        runner.needles = variants("Bearer " + "x" * 32)
        runner.scan_bytes(
            b'{"secret_refs":["TIKA_AUTHORIZATION"],"value":"[REDACTED]"}'
        )

    def test_negative_causes_are_not_matched_from_recipe(self) -> None:
        """A TLS policy or retryability:none cannot prove a TLS/network denial."""
        report = {
            "recipe": {"network": {"tls_required": True, "mode": "none"}},
            "steps": {
                "text": {
                    "last_result": {
                        "status": "failed",
                        "error": {
                            "code": "validation_error",
                            "message": "Invalid input",
                            "retryability": "none",
                        },
                    }
                }
            },
        }
        findings = " ".join(failure_details(report))
        self.assertIn("validation_error", findings)
        self.assertNotIn("tls", findings)
        self.assertNotIn("network", findings)


class SandboxPlanTests(unittest.TestCase):
    """Check fail-closed platform contracts without claiming native Linux exec."""

    def test_only_native_supported_executable_is_accepted(self) -> None:
        """Foreign tools cannot substitute for missing host support; no skips."""
        candidates = {
            "sandbox-exec": "/usr/bin/sandbox-exec",
            "bwrap": "/usr/bin/bwrap",
        }
        for host, backend in (("darwin", "sandbox-exec"), ("linux", "bwrap")):
            self.assertEqual(sandbox_backend(host), backend)
            self.assertEqual(sandbox_executable(host, candidates), candidates[backend])
            for unavailable in ({}, {**candidates, backend: None}):
                with self.subTest(host=host), self.assertRaisesRegex(
                    RuntimeError, backend
                ):
                    sandbox_executable(host, unavailable)
        for host in ("win32", "freebsd", "linux2", "unknown"):
            with self.subTest(host=host), self.assertRaisesRegex(
                RuntimeError, "unsupported"
            ):
                sandbox_executable(host, candidates)

    def test_linux_plan_pins_readonly_namespace_and_actual_launcher(self) -> None:
        """No root/socket bind or weaker launch path can satisfy the planned probe."""
        namespaces = {name: name + ":[1]" for name in ("mnt", "net", "user")}
        command = linux_probe_command(
            "/usr/bin/bwrap",
            Path("/usr/bin/python3.11"),
            Path("/trusted/transist-sandbox-launcher"),
            Path("/fixture"),
            namespaces,
        )
        options, program = (
            command[: command.index("--")],
            command[command.index("--") + 1 :],
        )
        for flag in ("--die-with-parent", "--unshare-net", "--unshare-user"):
            self.assertIn(flag, options)
        capabilities = [
            options[i + 1] for i, flag in enumerate(options) if flag == "--cap-add"
        ]
        self.assertEqual(
            set(capabilities),
            {"CAP_NET_ADMIN", "CAP_SETPCAP", "CAP_SETGID", "CAP_SETUID"},
        )
        mounts = {
            flag: [
                tuple(options[i + 1 : i + 3])
                for i, value in enumerate(options)
                if value == flag
            ]
            for flag in ("--bind", "--ro-bind")
        }
        self.assertEqual(mounts["--bind"], [("/fixture/sandbox-write", "/probe/write")])
        self.assertIn(("/fixture/sandbox-read", "/probe/read"), mounts["--ro-bind"])
        self.assertIn(
            ("/trusted/transist-sandbox-launcher", "/tmp/transist-sandbox-launcher"),
            mounts["--ro-bind"],
        )
        self.assertNotIn(("/", "/"), mounts["--ro-bind"])
        self.assertFalse(
            any(
                "docker.sock" in path
                for pairs in mounts.values()
                for pair in pairs
                for path in pair
            )
        )
        self.assertEqual(
            program[:3], ["/tmp/transist-sandbox-launcher", "--network-mode", "none"]
        )
        self.assertEqual(program[5:9], ["/usr/bin/python3.11", "-I", "-B", "-c"])
        self.assertEqual(json.loads(program[-3]), namespaces)
        # Assertions execute only after successful namespace/launcher startup;
        # a generic failed command is never treated as read-only enforcement.
        compile(program[9], "native-linux-sandbox-probe", "exec")
        self.assertIn("errno.EROFS", program[9])
        self.assertIn("NoNewPrivs", program[9])
        self.assertIn("CapBnd", program[9])

    def test_linux_plan_missing_prerequisites_fail(self) -> None:
        """Absent bwrap or incomplete namespace evidence cannot yield a plan."""
        args = (
            Path("/usr/bin/python3.11"),
            Path("/trusted/launcher"),
            Path("/fixture"),
        )
        namespaces = {name: name + ":[1]" for name in ("mnt", "net", "user")}
        with self.assertRaisesRegex(RuntimeError, "bwrap"):
            linux_probe_command(None, *args, namespaces)
        for invalid in ({}, {"net": "net:[1]"}, {**namespaces, "user": ""}):
            with self.assertRaisesRegex(RuntimeError, "namespace"):
                linux_probe_command("/usr/bin/bwrap", *args, invalid)
        with self.assertRaisesRegex(RuntimeError, "Python"):
            linux_probe_command(
                "/usr/bin/bwrap", Path("/unmounted/python"), *args[1:], namespaces
            )

    def readiness(self, backend: str) -> dict:
        """Build the current CLI shape with distinct checks for both actions."""
        return {
            "status": "ready",
            "checks": [
                {
                    "code": "filesystem_workspace_boundary",
                    "status": "passed",
                    "step_path": step,
                    "message": (
                        "workspace-scoped filesystem sandbox backend "
                        f"'{backend}' can be constructed"
                    ),
                }
                for step in ("text", "metadata")
            ],
        }

    def test_readiness_requires_each_platforms_exact_backend(self) -> None:
        """Both actual backend labels are accepted only on their respective host."""
        for host, backend in (("darwin", "sandbox-exec"), ("linux", "bwrap")):
            self.assertEqual(
                verify_sandbox_readiness(self.readiness(backend), host), backend
            )
            for foreign in {"sandbox-exec", "bwrap", "none", "weak"} - {backend}:
                with self.subTest(host=host, backend=foreign), self.assertRaises(
                    AssertionError
                ):
                    verify_sandbox_readiness(self.readiness(foreign), host)

    def test_generic_ready_missing_failed_or_duplicate_checks_fail(self) -> None:
        """No generic ready status, substring match or duplicate action can pass."""
        for host, backend in (("darwin", "sandbox-exec"), ("linux", "bwrap")):
            valid = self.readiness(backend)
            invalid = [
                {"status": "ready"},
                {**valid, "status": "not_ready"},
                {**valid, "checks": valid["checks"][:1]},
                {**valid, "checks": [valid["checks"][0]] * 2},
                {**valid, "checks": valid["checks"] * 2},
            ]
            for field, value in (
                ("status", "failed"),
                ("status", "deferred_to_dispatch"),
                ("message", "not " + valid["checks"][0]["message"]),
                ("code", "other"),
            ):
                changed = copy.deepcopy(valid)
                changed["checks"][1][field] = value
                invalid.append(changed)
            for report in invalid:
                with self.subTest(host=host, report=report), self.assertRaises(
                    AssertionError
                ):
                    verify_sandbox_readiness(report, host)


if __name__ == "__main__":
    unittest.main(verbosity=2)
