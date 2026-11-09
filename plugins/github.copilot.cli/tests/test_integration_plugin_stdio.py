#!/usr/bin/env python3
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class GithubCopilotCliIntegrationTest(unittest.TestCase):
    def test_stdio_flow_with_fake_gh_binary(self):
        temp_root = pathlib.Path(tempfile.mkdtemp(prefix="copilot-integration-"))
        bin_dir = temp_root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)

        args_file = temp_root / "gh-args.json"
        env_file = temp_root / "gh-env.json"
        gh_path = bin_dir / "gh"
        gh_script = textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json
            import os
            import sys
            import time

            args_file = os.environ.get("GH_FAKE_ARGS_FILE")
            if args_file:
                with open(args_file, "w", encoding="utf-8") as handle:
                    json.dump(sys.argv, handle)

            env_file = os.environ.get("GH_FAKE_ENV_FILE")
            if env_file:
                with open(env_file, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "COPILOT_CONFIG_DIR": os.environ.get("COPILOT_CONFIG_DIR"),
                            "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME"),
                            "XDG_CACHE_HOME_EXISTS": os.path.isdir(
                                os.environ.get("XDG_CACHE_HOME", "")
                            ),
                            "TMPDIR": os.environ.get("TMPDIR"),
                        },
                        handle,
                    )

            print("copilot started")
            sys.stdout.flush()
            time.sleep(3)
            print("Demo title\\n\\n## What Changed\\n\\nDemo body")
            sys.stdout.flush()
            sys.exit(0)
            """
        ).strip()
        gh_path.write_text(gh_script + "\n", encoding="utf-8")
        gh_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

        workspace_dir = temp_root / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = workspace_dir / "prompt.md"
        prompt_file.write_text("Find repositories.\n", encoding="utf-8")

        run_root = temp_root / "runs" / "run_demo"
        artifacts_dir = run_root / "artifacts"
        evidence_dir = run_root / "evidence"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        evidence_dir.mkdir(parents=True, exist_ok=True)

        invoke_request = {
            "type": "app.invoke.request.v1",
            "protocol_version": "app.v1",
            "invoke_id": "integration-invoke",
            "action": "exec_task",
            "input": {
                "prompt_file": str(prompt_file),
                "response_file": "run://copilot-output.md",
                "available_tools": ["shell(ls:*)", "read"],
                "timeout_seconds": 20,
            },
            "paths": {
                "workspace_dir": str(workspace_dir),
                "run_root": str(run_root),
                "artifacts_dir": str(artifacts_dir),
                "evidence_dir": str(evidence_dir),
            },
        }
        handshake_request = {
            "type": "app.handshake.request.v1",
            "protocol_version": "app.v1",
        }

        env = os.environ.copy()
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env["GH_FAKE_ARGS_FILE"] = str(args_file)
        env["GH_FAKE_ENV_FILE"] = str(env_file)
        runtime_env = {
            "COPILOT_HOME": str(temp_root / "runtime" / "copilot"),
            "COPILOT_CONFIG_DIR": str(temp_root / "runtime" / "copilot"),
            "COPILOT_SESSION_STATE_DIR": str(
                temp_root / "runtime" / "copilot" / "session-state"
            ),
            "XDG_CACHE_HOME": str(temp_root / "runtime" / "copilot" / "cache"),
            "TMPDIR": str(temp_root / "runtime" / "copilot" / "tmp"),
        }
        for value in runtime_env.values():
            pathlib.Path(value).mkdir(parents=True, exist_ok=True)
        env.update(runtime_env)

        process = subprocess.Popen(
            [sys.executable, str(ROOT / "plugin.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(ROOT),
            env=env,
        )
        payload = (
            json.dumps(handshake_request) + "\n" + json.dumps(invoke_request) + "\n"
        )
        stdout, stderr = process.communicate(payload, timeout=40)

        self.assertEqual(process.returncode, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(pathlib.Path(runtime_env["COPILOT_CONFIG_DIR"]).exists())

        frames = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertGreaterEqual(len(frames), 3)

        handshake = frames[0]
        self.assertEqual(handshake["type"], "app.handshake.response.v1")
        self.assertEqual(handshake["plugin"]["id"], "github.copilot.cli")
        self.assertEqual(handshake["plugin"]["version"], "0.1.0")

        result = frames[-1]
        self.assertEqual(result["type"], "app.invoke.result.v1")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"]["exit_code"], 0)

        status_previews = [
            frame.get("line_preview", "")
            for frame in frames
            if frame.get("type") == "app.invoke.progress.v1"
            and frame.get("stream") == "status"
        ]
        self.assertGreaterEqual(len(status_previews), 3)
        self.assertIn("Launching gh copilot...", status_previews)
        self.assertIn("Copilot finished, processing output...", status_previews)
        self.assertTrue(
            any("Copilot is still working..." in line for line in status_previews)
        )

        self.assertTrue(args_file.exists())
        gh_argv = json.loads(args_file.read_text(encoding="utf-8"))
        self.assertIn("copilot", gh_argv)
        self.assertIn("--allow-all-tools", gh_argv)
        self.assertIn("--allow-all-paths", gh_argv)
        self.assertNotIn("--allow-all", gh_argv)
        add_dir_values = [
            gh_argv[index + 1]
            for index, argument in enumerate(gh_argv[:-1])
            if argument == "--add-dir"
        ]
        self.assertEqual(add_dir_values[0], str(workspace_dir))
        self.assertEqual(len(add_dir_values), 1)
        self.assertIn("--available-tools", gh_argv)
        available_index = gh_argv.index("--available-tools")
        self.assertEqual(gh_argv[available_index + 1], "shell(ls:*),read")

        gh_env = json.loads(env_file.read_text(encoding="utf-8"))
        self.assertEqual(
            gh_env["COPILOT_CONFIG_DIR"], runtime_env["COPILOT_CONFIG_DIR"]
        )
        self.assertEqual(gh_env["XDG_CACHE_HOME"], runtime_env["XDG_CACHE_HOME"])
        self.assertTrue(gh_env["XDG_CACHE_HOME_EXISTS"])
        self.assertEqual(gh_env["TMPDIR"], runtime_env["TMPDIR"])

        response_file = run_root / "copilot-output.md"
        history_json = artifacts_dir / "copilot_response.interaction.json"
        history_markdown = artifacts_dir / "copilot_response.interaction.md"
        self.assertTrue(response_file.exists())
        self.assertTrue(history_json.exists())
        self.assertTrue(history_markdown.exists())

        response_text = response_file.read_text(encoding="utf-8").strip()
        self.assertEqual(response_text, "Demo title\n\n## What Changed\n\nDemo body")

        history_payload = json.loads(history_json.read_text(encoding="utf-8"))
        self.assertEqual(len(history_payload), 1)
        self.assertEqual(history_payload[0]["status"], "success")


if __name__ == "__main__":
    unittest.main()
