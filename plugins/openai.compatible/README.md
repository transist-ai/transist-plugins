# `openai.compatible` (0.1.0)

Reference `process` plugin for non-streaming OpenAI-compatible Chat Completions.
The action `chat_complete` is explicitly **external**. Its `base_url` is the
provider's API root: trailing slashes are removed and `/chat/completions` is
appended once, without adding `/v1`. For OpenRouter use
`https://openrouter.ai/api/v1`; for another provider change both the URL and
the workflow's step-scoped network allowlist. HTTP is restricted to loopback.
No ambient proxy or redirects are used; DNS answers are checked and the socket
is pinned to one validated address, with standard TLS certificate verification.
No retries are made.

`model` and exactly one of ordered `messages` (system/user/assistant text only)
or `user_text_file` are required. With `user_text_file`, optional `system_prompt`
adds one system message before the user message; it is invalid with `messages`.
File bytes decode as strict UTF-8 without trimming or newline translation.
Optional `response_file` receives exact UTF-8 answer bytes with no added newline.
It does not appear in output metadata; there is no plugin-managed conversation
history. `messages` callers continue to control their explicit ordered history.

`max_prompt_bytes` bounds the aggregate UTF-8 content before network transport
(default and maximum 262,144), including the optional system prompt. Each message
and the user file retain the 65,536-byte ceiling; file reads use the smaller
prompt/message ceiling plus one byte. The serialized request independently
must fit 256 KiB including JSON escaping; responses remain capped at 1 MiB.
Overflow, invalid UTF-8, nonregular files, symlinks and traversal fail explicitly.

File data accepts runtime-staged absolute paths, policy-allowed absolute paths,
workspace-relative paths, `repo://` under workspace `.transist/`, and invocation
`run://` scratch. No file path selects a workspace or expands filesystem authority.
Reads/writes use descriptor-relative no-follow opens. Use root `$document`
declarations and process bindings for durable handoffs; staged paths and `run://`
expire after invocation. No `system_prompt_file` field is supported.

```yaml
with:
  base_url: https://example.org/v1
  model: chosen-model
  user_text_file: {$document: diagnostic, access: read}
  system_prompt: Give a concise advisory.
  response_file: {$document: advisory, access: write}
  secret_refs: [OPENAI_COMPATIBLE_API_KEY]
```

Workflows also pass `secret_refs: [OPENAI_COMPATIBLE_API_KEY]` for runtime
binding; this metadata is not sent to the provider.
`max_tokens` defaults to 256 (maximum 4096); `timeout_ms` defaults to 30000
(maximum 120000). At most 64 messages, 64 KiB per message, 256 KiB
serialized request and 1 MiB response are accepted. Unknown fields fail.
Success returns `response_text`, optionally `finish_reason`, `response_id`,
and `response_model`. Provider token counts, when complete, appear in APP
`usage` and a `usage.json` artifact hint. Missing token counts are omitted.
Errors use canonical APP codes: secret_resolution, configuration, validation,
transport, timeout, auth, rate_limit, tool_execution, and protocol errors.
Diagnostics contain fixed messages/status only, never provider response bodies.

## Environment and policy

| Variable | Classification / purpose | Source and policy |
| --- | --- | --- |
| `OPENAI_COMPATIBLE_API_KEY` | Required resolved provider credential; no safe default. | Runtime injects into the process via declared `secret_inputs` / workflow `with.secret_refs: [OPENAI_COMPATIBLE_API_KEY]`; allow secret resolution and redact; never put it in an input or file. |

No other environment variables are read or forwarded by this plugin. It uses
no child process or provider scratch allocation; only explicitly selected file
data is read/written. Runtime-managed environment variables (including `TMPDIR`)
are not consumed. `HTTP_PROXY`, `HTTPS_PROXY`,
and `NO_PROXY` are deliberately ignored: sockets connect directly.

Configure `policy.remote_secret_delivery.process: true` for governed or
policy-enforced runs, with explicit permission for the credential. The root
workflow can deny network by default; scope an effective `allowlist` to the
external action (matching host and port, TLS required). A matching `loopback`
mode also works for loopback URLs. Other modes fail closed. The process
adapter's allowlist is **cooperatively enforced by the plugin**, not an OS
egress boundary. Use additional network isolation where required.

## Installation

The manifest requires `python3` on `PATH`. Lifecycle and offline E2E commands
target public Transist v0.1.0. Final both-platform release validation is
[recorded separately](../../docs/validation.md).

```sh
transist plugins install --path plugins/openai.compatible/plugin.yaml
transist plugins list
transist plugins inspect openai.compatible
env -u PYTHONPATH python3 -B plugins/openai.compatible/tests/smoke.py
```

The published source is unsigned for standalone development. For governed
deployment, sign this exact package, import your approving trust root, and
install the detached signature with the public plugin lifecycle. No signing
key or provider credential ships with the example.

## Supplied workflow

`workflow.yaml` supplies `openai_compatible_demo@0.1.0`, pinned to plugin 0.1.0.
Only `model` and `prompt` are required. The default endpoint is OpenRouter;
when overriding `base_url`, edit the demo's step-scoped network allowlist to
match the provider host and port. Resolve `OPENAI_COMPATIBLE_API_KEY` through runtime
secret configuration before running. Use a disposable standalone home:

```sh
DEMO_HOME="$(mktemp -d)/home"
transist --home "$DEMO_HOME" home init --mode standalone
transist --home "$DEMO_HOME" plugins install --path plugins/openai.compatible/plugin.yaml
transist --home "$DEMO_HOME" workflows check --path plugins/openai.compatible/workflow.yaml
transist --home "$DEMO_HOME" workflows install --path plugins/openai.compatible/workflow.yaml --approve
transist --home "$DEMO_HOME" workflows check openai_compatible_demo@0.1.0 --input model=your-model --input prompt=hello --live
transist --home "$DEMO_HOME" workflows run openai_compatible_demo@0.1.0 --input model=your-model --input prompt=hello
```

Enable process secret delivery in that home's `config.yaml` before readiness
or execution. The final command makes a live provider request; the offline E2E
recipe instead uses a loopback fixture and a dummy credential:

```sh
env -u PYTHONPATH python3 -B -m unittest discover -s plugins/openai.compatible/tests -p 'test_*.py'
env -u PYTHONPATH python3 -B scripts/check_installed.py \
  --transist-bin "$TRANSIST_BIN" --admin-bin "$TRANSIST_ADMIN_BIN" \
  --fixture-base "$FIXTURE_BASE" --package openai.compatible
```

Use the interpreter from the `tests/requirements.txt` venv. The selected installed
runner covers native provider cases, exact managed-file bytes, prompt overflow,
and cancellation of a held file-mode request before document publication. The
cancellation case checks unchanged prior document heads, no false success/usage,
closed provider connection, stopped runner and isolation of the next run.

For an optional live OpenRouter smoke, set `OPENROUTER_KEY_PERSONAL` and
`OPENROUTER_MODEL`, then run `python3 -B plugins/openai.compatible/tests/live.py`.
Absent credentials skip locally; set `REQUIRE_LIVE_PROVIDER=1` to require the
live gate. GitHub Actions runs static checks only, with no live workflow or
provider credentials. Other compatible providers are expected, not live-certified.
