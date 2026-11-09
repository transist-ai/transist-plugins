# `opencode.cli`

Package version: `0.1.0`.

## Gate decision metadata

`exec_task` accepts optional runtime-injected `gate_decision.review_request` for
document gates. The closed request contains run/gate/step IDs, positive gate
execution and nonempty typed document revisions (producer, revision/blob digests,
kind, sizes and counts). Initialization revisions may have execution zero.
The selected local `schemas/gate-decision.schema.json` bundles the canonical
runtime definitions; this action retains its existing required `decided_at_ms`,
although the runtime schema makes that timestamp optional. Legacy decisions
without `review_request` remain valid. Request-changes feedback preserves reason,
actor and actor-source attribution; metadata is validated, never appended to the
prompt or read as document content. Supply content through `context_files`.
Kernel checks still own exact revision approval and authority.

Nested integer metadata accepts finite integral numbers such as `1.0`, with exact
bounds comparisons; booleans, fractions and nonfinite values fail. Identifiers
and digests must match the whole string, including no trailing newline (a stricter
semantic check than canonical schema `$` anchors). An invalid provided decision
with `review_request` returns `validation_error` before provider work, never
silently dropping feedback. Existing timestamp-reader behavior is unchanged.

## Managed document paths

`context_files`, `response_file`, `summary_title_file` and `pr_body_file` accept
runtime-staged absolute document paths as data. They never select provider cwd:
the canonical runtime workspace stays authoritative. Existing allowed absolute
paths, workspace-relative paths, `repo://` under workspace `.transist/`, and
invocation-private `run://` remain supported. Reject traversal, symlinks and
nonregular bounded context files. Bounded UTF-8 context text is preserved exactly
inside prompt headings, including whitespace and CRLF.

With `history_mode: invocation`, an out-of-workspace absolute response uses
artifact-only history, without replay/neighbor sidecars. Legacy history for such
outputs uses invocation artifacts. The runtime persists logical document
revisions; staged paths and `run://` expire after dispatch. Supply prior documents
in `context_files` for revision tasks. Full implicit feedback replay requires
workspace-backed history; focused/feedback-only never reuse expired staging.
`prompt_file` retains the runtime prompt/captured-asset binding contract; there
is no `system_prompt_file` input. Compact answer extraction, usage, byte budgets
and subprocess lifecycle remain enforced.

## Opt-in lean CLI contract

Compact/invocation mode uses authoritative answer events or session export.
If neither provides an answer, the result explicitly fails with `protocol_error`;
diagnostic stdout is retained as a log, not promoted to a draft.
Complete answer text remains authoritative, including all review findings before
markdown headings. `extra_args` cannot override answer transport in these modes.

| Input | Values / default | Meaning |
|---|---|---|
| `output_mode` | `legacy` (default), `compact` | Removes raw `stdout` / `stderr` properties. Keeps canonical response, outcome/exit code, feedback, session/share, summary/handoff paths and APP usage; adds `pr_body_text`, `output_mode: compact` and closed diagnostics. |
| `history_mode` | `legacy` (default), `invocation` | One `invocation.json` artifact with IDs, outcome, composed prompt once (including its task section), and logical log/answer references. |
| `max_context_file_bytes` | Optional positive integer | UTF-8 byte ceiling for each prompt/context/draft/history file read. |
| `max_prompt_bytes` | Optional positive integer | UTF-8 byte budget for all contributors, headings and separators before launch. |

The example adopts compact output, invocation history, focused replay and
**65536 bytes per file / 98304 bytes total**. Omitted controls retain legacy
whitespace/output and cumulative history. Exact byte limits pass. Strict reads
are bounded; missing, oversized and invalid UTF-8 sources fail before launch.
Diagnostic errors use logical contributor labels/counts without private paths
or content. The argv guard includes UTF-8 arguments, final environment (including
merged permission config), NUL terminators, pointers and Linux per-string limits.
Invocation metadata includes `prompt_budget`: sanitized section labels/counts,
joined UTF-8 total and configured limit, without source paths or contents.
Empty/whitespace or null optional `context_files` entries are skipped. Nonempty
paths still require a readable UTF-8 file before provider launch.

Invocation history is **artifact-only** with `response_file: run://...`. The
existing action resolver uses invocation-private `InvokePaths.run_root`; the
resolved target must remain inside that staging root. Metadata/log APP hints
are emitted once, with no replay directory, history reads, cumulative exports,
or workspace writes. A read-only workspace is sufficient. The response is
scratch and follows invocation cleanup. With revision feedback, `focused` and
`feedback-only` include gate feedback only, never a scratch draft. `full` (also
the default) explicitly fails with feedback when durable replay is unavailable;
without feedback it works. `runtime://...` is not an action response-path scheme.

For **durable replay**, records and separate answer files use
`<response-basename>.invocations/<plugin-and-root-hash>/`, or
`<workspace>/.transist/opencode.cli/invocations/<hash>/` without a response target.
Approve exact workspace read/write authority. Workspace-relative and
`repo://...` targets must resolve inside the canonical workspace (`repo://...`
maps under `.transist/`). Out-of-workspace targets use artifact-only history;
staging paths are never presented as durable.
Records contain feedback/outcome and a relative answer filename, never raw logs
or task/composed-prompt snapshots. Metadata is emitted once as its APP hint; no
cumulative history export is generated. `focused` reads the latest same-root
answer plus current gate feedback, without full history. `full` renders selected
same-root records under the prompt budget; fresh roots cannot read prior-root
drafts. Unconfigured invocation replay file reads and full replay have a 256 KiB
ceiling. Lifetime follows workspace retention/cleanup, not invocation staging.

Full logs are supplied once each as existing stdout/stderr APP hints, subject
to runtime retention. Compact diagnostics contain logical artifact labels,
`total_bytes`, `retained_bytes`, and `full_log_available`, never contents.
Adapter limits are **32 hints / 256 KiB each / 1 MiB contents per family**.
Opt-in packages additionally enforce a **1 MiB serialized JSON result envelope**,
including escaping/newline; this is package policy, not an APP transport cap.
Compact reserves complete answer/metadata artifacts before optional logs. Diagnostic
previews may be shortened/omitted with byte accounting, preserving the backend
outcome. Oversized required non-log artifacts fail with `protocol_error` and are
unavailable rather than invalid truncated JSON. Authoritative output is never
silently shortened. Envelope failure returns empty failed output with a manifest;
complete-log availability is false when full logs cannot fit. Closed conditional
legacy/compact schema branches require actual log fields only for legacy.
Failed diagnostic output permits a null exit code; success requires an integer.
Early APP failures may have empty output. Structured timeout logs contribute
only extracted answer events, never raw JSON-event narration as response text.
Diagnostic entries declare `retention`: `full`, `diagnostic_preview`, or
`unavailable`. Preview contents have an explicit diagnostic-only label.
Escaping-related envelope pressure shortens only log previews first; a fitting
authoritative answer stays whole. Metadata/replay preserve the backend outcome
unless required data cannot fit, with null references for unavailable artifacts.
Retained hint contents
remain strictly below 1 MiB in total.

Offline four-package matrix:

```sh
python3 -B -m unittest discover -s plugins/codex.cli/tests -p test_lean_cli_contract.py -v
```

Additional environment accounting: `PATH` is required inherited binary lookup
(allow approved executables); `HOME` is optional inherited provider-owned state
(approve exact account/config paths). `CI` and `NO_COLOR` preserve optional
inherited overrides or create child defaults `1`. `OPENCODE_CLIENT` is optional
inherited with default `opencode.cli`. `OPENCODE_CONFIG_CONTENT` is optional
inherited JSON, default absent, read and merged with permission controls before
forwarding; policy must allow approved non-secret overrides. Other
runtime-authorized values are inherited unchanged, counted without logging
keys/values. Provider-state, credentials and temporary-directory variables retain
their classifications in the environment table below.

Provider children inherit the runtime process group. The package-local runner
drains output and confirms child cleanup before a terminal APP result, including
on timeout or callback failure. Unconfirmed cleanup fails with nonretryable
`protocol_error`, including session export; parsing, progress, and heartbeats
run on the main thread.

`opencode.cli` is a prompt-aware `process` plugin for non-interactive
OpenCode runs through `opencode run`.

## Runtime Capabilities And Endpoints

- Adapter: `process`.
- Capability grants: adapter invoke, `plugin:opencode.cli@0.1.0` load, and `action:opencode.cli@0.1.0/exec_task` invoke.

The manifest requires `python3` and `opencode` on `PATH`. Optional demo `model`,
`reasoning_effort`, `variant`, and `agent` inputs may be omitted:
workflow-materialized `null` values leave the corresponding CLI flags unset.
Explicit variants still override the reasoning-effort alias.

The shared offline installed-demo regression targets required-only input
binding and explicit variant precedence through public Transist v0.1.0:
`python3 -B plugins/codex.cli/tests/e2e_optional_inputs.py`. It uses disposable
homes and mocks provider commands, without a live provider request.
Public-runtime both-platform results are [recorded separately](../../docs/validation.md).
- Secrets: optional `OPENROUTER_KEY_PERSONAL`, delivered only when a workflow
  declares it in `with.secret_refs`. The process adapter binds it to the
  OpenCode child as `OPENROUTER_API_KEY`; it is not persisted. Other provider
  authentication remains in the local OpenCode setup.
- Filesystem: grant the run artifact/evidence paths and any repository, context, prompt, or response paths used by the workflow. OpenCode configuration and temporary data use declared private runtime directories; they do not require workspace write access.
- External endpoints: provider-dependent, not fixed by the plugin. Common configured endpoints include `https://opencode.ai:443`, `https://api.openai.com:443`, or `https://openrouter.ai:443`.

## Actions

- `exec_task`

## Action Reference

### `exec_task`

Executes a non-interactive OpenCode task.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `prompt` | string | Conditional | Inline task text. (One of `prompt` or `prompt_file` required) |
| `prompt_file` | string | Conditional | Path to a file containing the task text. |
| `context_files` | array[string] | No | Supporting file contents to inline into the prompt. |
| `ignore_paths` | array[string] | No | Repo-relative glob patterns the wrapper and agent should ignore. Defaults include `**/_build/**` and `**/deps/*/_build/**`. |
| `gate_decision` | object | Runtime | Runtime-injected `transist.gate_decision.v1` manual-gate decision. |
| `feedback_context_mode` | enum | No | Replay mode: `full`, `focused`, or `feedback-only`. |
| `response_file` | string | No | Stable workspace path to persist the final assistant response. |
| `summary_title_file` | string | No | Stable path for extracted summary title. |
| `pr_body_file` | string | No | Stable path for extracted PR body. |
| `allowed_tools` | array[string] | No | Compatibility shims for allowed shell commands. |
| `denied_tools` | array[string] | No | Compatibility shims for denied shell commands. |
| `timeout_ms` | integer | No | Timeout in milliseconds. |
| `timeout_seconds` | integer | No | Timeout in seconds. |
| `model` | string | No | OpenCode model to use for this step. |
| `reasoning_effort` | enum | No | Compatibility alias for `variant`: `low`, `medium`, `high`, `xhigh`. |
| `variant` | string | No | Native OpenCode variant (e.g., `minimal`, `high`, `max`). |
| `agent` | string | No | OpenCode primary agent to select. |
| `session_title` | string | No | OpenCode session title. |
| `share` | boolean | No | Request a shared session URL. |
| `dangerously_skip_permissions` | boolean | No | Auto-approve non-denied permissions. |
| `permission` | object | No | Native OpenCode permission overrides. |
| `extra_args` | array[string] | No | Pass-through of additional OpenCode CLI flags. |
| `skills_bundle` | object | No | Embedded structured skills bundle. |
| `skills_rendered` | string | No | Rendered workflow-skills text block. |
| `secret_refs` | array[string] | No | Logical runtime secret references. Use `OPENROUTER_KEY_PERSONAL` for OpenRouter; raw values are never included in the action input. |

## Feature Coverage

`opencode.cli` covers the non-interactive `opencode run` surface that
prompt-driven automation typically needs:

- one-shot prompt execution from inline prompts or prompt files
- per-step `model` selection
- native OpenCode `variant` control (`minimal` / `high` / `max`) plus a
  `reasoning_effort` compatibility alias
- primary `agent` selection and `session_title` control
- optional shared-session URL capture (`share`)
- native `permission` overrides plus shell-rule compatibility shims derived from
  `allowed_tools` / `denied_tools`
- `dangerously_skip_permissions` for permissive automation runs
- JSON event-mode streaming with:
  - final assistant text extracted from streamed `text` events
  - token usage extracted from `step_finish.part.tokens` (written to
    `usage.json` artifact and APP `usage`)
  - share URL captured from OpenCode session output or export fallback
- workflow-skills injection prepended to the prompt
- repo ignore policy injection via `ignore_paths`, with Elixir build artifact defaults
- manual-gate feedback replay with `full` / `focused` / `feedback-only` modes
- stable artifact persistence (`response_file`, `summary_title_file`,
  `pr_body_file`) with canonical markdown extraction
- clean timeout handling (`timeout_ms` / `timeout_seconds`) with canonical
  `timeout_error` reporting
- pass-through of additional OpenCode CLI flags via `extra_args`

Intentionally not covered: interactive OpenCode sessions, OpenCode web UI,
plugin/extension management, or provider-admin operations. OpenCode provider
configuration and auth is delegated to the operator's local setup.

The action preserves a prompt-aware workflow contract: prompt composition,
workflow skills injection, manual-gate feedback replay, stable
`response_file` persistence, and canonical markdown extraction for summary
and PR-body artifacts.

The action accepts exactly one prompt source:

- `prompt` for inline text
- `prompt_file` for a file path that the wrapper reads before invocation

Optional wrapper controls:

- OpenCode runs inside the runtime-owned canonical workspace
- `context_files` to inline supporting file contents into the composed prompt
- `ignore_paths` to skip repo-relative files and directories such as build outputs
- `gate_decision` is injected by the runtime to replay a manual-gate revision
  decision; callers must not provide gate IDs or filesystem paths
- `feedback_context_mode` to control how much previous draft context is replayed
- `response_file`, `summary_title_file`, and `pr_body_file` for stable artifacts
- `timeout_ms` or `timeout_seconds` to fail long-running invocations cleanly
- `model` to select the OpenCode model for this step
- `reasoning_effort` as a compatibility alias that maps to OpenCode `variant`
- `variant` for provider-native reasoning / effort control
- `agent` to select the OpenCode primary agent
- `session_title` to control the OpenCode session title
- `share` to request a shared OpenCode session URL
- `dangerously_skip_permissions` to auto-approve permissions that are not denied
- `permission` for native OpenCode permission overrides
- `allowed_tools` and `denied_tools` as compatibility shims for shell command rules
- `extra_args` to pass through additional OpenCode CLI flags when needed

The wrapper runs OpenCode in JSON event mode and extracts:

- the final assistant text from streamed `text` events
- token usage from `step_finish.part.tokens`
- optional share URLs from the OpenCode session output or export fallback

When usage is available, the plugin also writes a `usage.json` artifact under
the step artifacts directory. APP `usage` projects only canonical model/provider
and input/output/total token counts; reasoning/cache details remain in `usage.json`.

Failure reporting uses canonical Transist APP errors. Missing local `opencode`
binaries surface as `configuration_error`, wrapper validation problems surface
as `validation_error`, and runtime / provider failures are normalized to the
closest canonical APP error.

## Why This Shape

This plugin keeps the workflow contract predictable so common prompt-driven
automation patterns remain easy to reuse:

- prompt files and inline prompts behave the same way
- workflow-owned skills are prepended to the prompt
- manual-gate feedback can replay the current draft and prior interaction history
- later steps can consume stable files instead of fragile stdout parsing

OpenCode also exposes richer native controls, so this plugin adds optional
`agent`, `variant`, and `permission` inputs instead of trying to force
everything into generic compatibility fields.

## Agent Controls

These inputs are plugin-owned `with` fields. They belong in the step `with`
block and are not part of the `transist.flow.v1` schema syntax.

| Input | Type | Description |
|---|---|---|
| `model` | string | OpenCode model to use for this step. Free-form; provider catalogs remain provider-owned. |
| `reasoning_effort` | enum | Compatibility alias for OpenCode `variant`. Maps `low -> minimal`, `high -> high`, `xhigh -> max`, and leaves `medium` unset. |
| `variant` | string | Native OpenCode model variant, such as `minimal`, `high`, or `max`. Overrides `reasoning_effort` when both are set. |
| `agent` | string | OpenCode primary agent to use for the run. |

## Permission Compatibility

The wrapper translates supported `allowed_tools` and `denied_tools` entries
into OpenCode `permission.bash` rules. Supported forms are:

- `shell(...)`
- `bash(...)`
- plain bash command patterns such as `git status *`

Unsupported non-shell descriptors are rejected with `validation_error` so the
workflow contract fails clearly instead of silently broadening permissions.

Native `permission` input remains authoritative. The wrapper builds shell-rule
shims first, then overlays the explicit `permission` field through
`OPENCODE_CONFIG_CONTENT`.

## Prerequisites

- `opencode` available in `PATH`
- a configured OpenCode provider / auth state for live runs, or an
  `OPENROUTER_KEY_PERSONAL` secret reference for OpenRouter models
- installed `transist` binary from the main repo:
  [transist-ai/transist](https://github.com/transist-ai/transist)
- released binary downloads:
  [v0.1.0 release](https://github.com/transist-ai/transist/releases/tag/v0.1.0)

## Runtime Environment

The package declares these runtime-owned directories in `plugin.yaml`. Transist
resolves them for each invocation; operators do not configure or policy-allow
host values for them. The optional OpenRouter binding is injected only into the
OpenCode child process when the workflow explicitly requests the logical
secret reference.

| Variable | Source | Classification | Purpose and policy |
|---|---|---|---|
| `XDG_DATA_HOME` | `runtime://temp/opencode/data` | Plugin-managed | Isolates OpenCode data; no host-policy grant required. |
| `XDG_STATE_HOME` | `runtime://temp/opencode/state` | Plugin-managed | Isolates OpenCode state; no host-policy grant required. |
| `XDG_CACHE_HOME` | `runtime://temp/opencode/cache` | Plugin-managed | Keeps caches out of the host cache directory; no host-policy grant required. |
| `TMPDIR` | `runtime://temp/opencode/tmp` | Plugin-managed | Provides a runtime-owned temporary directory; no host-policy grant required. |
| `OPENROUTER_API_KEY` | Logical `OPENROUTER_KEY_PERSONAL` secret reference | Optional child-process secret | Authenticates OpenRouter models. A workflow must declare `with.secret_refs: ["OPENROUTER_KEY_PERSONAL"]`; runtime secret policy must permit process delivery and redact the value. |

## Install

```bash
transist plugins install --path ./plugins/opencode.cli
transist plugins inspect opencode.cli
```

## Testing

Offline smoke test:

- does not require the `opencode` binary
- does not require network or a configured provider
- validates APP framing, prompt composition, permission shims, file output,
  usage extraction, export fallback, and missing-binary handling

```bash
python3 ./plugins/opencode.cli/tests/smoke.py
```

Live integration test:

- requires `opencode` in `PATH`
- requires a usable OpenCode runtime and provider configuration
- skips cleanly when the local environment blocks OpenCode session storage or
  lacks provider credentials

```bash
python3 ./plugins/opencode.cli/tests/live.py
```

## Demo Workflow

```bash
transist workflows check --path ./plugins/opencode.cli/workflow.yaml
transist workflows install --path ./plugins/opencode.cli/workflow.yaml
transist workflows check --workflow opencode_cli_demo@0.1.0 --inputs-file <inputs-file> --output readiness.json
transist workflows run opencode-cli-demo --workflow opencode_cli_demo@0.1.0 --inputs-file <inputs-file> --readiness-file readiness.json
```
