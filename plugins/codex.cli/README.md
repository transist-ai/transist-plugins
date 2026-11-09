# `codex.cli`

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
prompt or read as document content. Supply document content explicitly through
`context_files`. Kernel checks still own exact revision approval and authority.

Nested integer metadata accepts finite integral numbers such as `1.0`, with exact
bounds comparisons; booleans, fractions and nonfinite values fail. Identifiers
and digests must match the whole string, including no trailing newline (a stricter
semantic check than canonical schema `$` anchors). An invalid provided decision
with `review_request` returns `validation_error` before provider work, never
silently dropping feedback. Existing timestamp-reader behavior is unchanged.

## Managed document paths

`context_files` and `response_file` accept runtime-staged absolute document paths.
The provider cwd remains the runtime-selected canonical workspace. Absolute data
paths do not select a repository or grant additional filesystem access. Existing
workspace-relative paths, `repo://` under workspace `.transist/`, and private
`run://` paths remain supported. Traversal and symlinks are rejected; bounded
context reads require regular strict UTF-8 files and preserve their exact text
inside prompt headings, including CRLF and leading/trailing whitespace.

Use `history_mode: invocation` with managed responses. An out-of-workspace
absolute response is artifact-only history: no replay files or neighboring
sidecars are written. Legacy history for such a response uses the invocation's
artifact directory, never the document's parent. Staged paths expire at dispatch
cleanup; the runtime retains logical document revisions, not reusable path strings.
Pass prior response documents explicitly in `context_files` for revision work.
Full implicit feedback replay requires workspace-backed durable history; focused
and feedback-only modes do not load previous staging. `prompt_file` follows the
runtime prompt binding/captured-asset contract; there is no `system_prompt_file`.

## Opt-in lean CLI contract

Compact/invocation mode requires Codex's dedicated last-message artifact for an
authoritative answer. If it is unavailable, the result fails explicitly with
`protocol_error`; diagnostic stdout is retained as a log, not promoted to a draft.
The complete last message is authoritative, including full review reports.
`extra_args` cannot override answer transport in compact/invocation mode.

| Input | Values / default | Meaning |
|---|---|---|
| `output_mode` | `legacy` (default), `compact` | Compact removes raw `stdout` / `stderr` properties and adds `output_mode: compact` and a closed `diagnostics` object. The authoritative response, outcome, exit code, handoff path and APP usage remain. |
| `history_mode` | `legacy` (default), `invocation` | Invocation stores one `invocation.json` metadata artifact, referencing `stdout.txt`, `stderr.txt`, and `response.txt` by logical name. |
| `max_context_file_bytes` | Optional positive integer | UTF-8 byte limit for each prompt/context/draft/history file read. |
| `max_prompt_bytes` | Optional positive integer | UTF-8 byte limit for the fully composed prompt, including base task, skills, ignored-path instructions, gate feedback, draft, replay, headings and separators. Codex sends this bounded prompt on stdin independently of argv limits. |

The example adopts `compact` + `invocation`, focused replay, **65536 bytes per
file / 98304 bytes total**. Omitting the controls retains legacy output,
whitespace handling, and cumulative JSON/Markdown history. Limits use bytes,
not characters; exact limits pass. Strict reads are bounded and missing,
oversized or invalid UTF-8 inputs fail before provider launch. Failure messages
identify logical contributors and byte counts without private filenames or content.
Invocation metadata includes a sanitized `prompt_budget` manifest: section
labels/counts, joined UTF-8 total and configured limit. It contains no source paths.
Optional `context_files` entries may be empty/whitespace strings or null and are
skipped. Nonempty paths still require a readable UTF-8 file before provider launch.

`invocation` supports **artifact-only history** when `response_file` uses
`run://...`. The existing action resolver maps this to invocation-private
`InvokePaths.run_root`; the resolved target must remain inside that staging root.
The response is scratch, while metadata and logs are emitted once as APP hints.
No replay directory, history reads, cumulative exports, or workspace writes are
made, so a read-only workspace is sufficient. Scratch files do not survive
invocation cleanup. With revision feedback, `focused` and `feedback-only` include
only current gate feedback; they never reuse a scratch draft. `full` (including
the default) fails explicitly with feedback because durable replay is unavailable;
without feedback it works. `runtime://...` is not an action response-path scheme.

For **durable replay**, `invocation` keeps minimal records and separate answer files
under `<response-basename>.invocations/<plugin-and-root-hash>/`. Without
`response_file`, it uses `<workspace>/.transist/codex.cli/invocations/<hash>/`.
Grant exact workspace read/write authority for these destinations. A response
target must resolve inside the canonical workspace; `repo://...` resolves under
its `.transist/` directory. Out-of-workspace targets use artifact-only history.
These records follow workspace
retention, not staging retention. Metadata includes root/run/step/invoke IDs,
outcome and composed prompt once (including its task section); no separate task
copy or cumulative snapshot is exported.
Workspace replay records contain only feedback/outcome and a relative answer
filename, never raw logs or the composed prompt. `focused` reads only the latest
same-root answer and current gate feedback; `full` renders same-root selected
records under the prompt budget. Fresh roots do not replay previous-root drafts.
Unconfigured invocation replay file reads and full replay have a 256 KiB ceiling.

Raw logs are supplied once each as their existing APP artifact hints, retained
by runtime policy. Compact diagnostics list logical artifact labels,
`total_bytes`, `retained_bytes`, and `full_log_available`, never log contents.
The adapter allows **32 hints, 256 KiB per hint, 1 MiB contents per family**.
Opt-in results also enforce a **1 MiB serialized JSON envelope**, a package
policy rather than an APP transport limit; JSON escaping and the newline count.
Compact reserves complete answer/metadata artifacts before optional logs. Diagnostic
previews may be shortened or omitted with explicit byte accounting, preserving the
backend's successful or failed outcome. Oversized required non-log artifacts fail
with `protocol_error` and are unavailable rather than invalid truncated JSON.
Authoritative output is
never silently shortened; envelope failure returns empty failed output with a
diagnostic manifest. Full-log availability is false when complete logs cannot
fit. Early failures may have empty APP output; successful action output uses
closed conditional legacy/compact schema branches. Failed diagnostic output
permits `exit_code: null`; success requires an integer.
Each diagnostic entry also declares `retention`: `full`, `diagnostic_preview`,
or `unavailable`. Preview contents carry an explicit diagnostic-only label.
Escaping-related envelope pressure shortens only log previews first, preserving
a fitting authoritative answer and its original backend outcome. Metadata and replay
record failure only for backend failures or unrepresentable required data;
references to unavailable
artifacts are null. Retained hint contents remain strictly below 1 MiB in total.

Offline four-package contract matrix (fixtures only):

```sh
python3 -B -m unittest discover -s plugins/codex.cli/tests -p test_lean_cli_contract.py -v
```

### Environment accounting

| Variable | Classification / default | Read, forwarding and policy |
|---|---|---|
| `PATH` | Required inherited binary lookup | Runtime must allow an approved executable search path; forwarded to Codex. |
| `HOME` | Optional inherited, provider-owned home | Read by home expansion and inherited by Codex; approve exact account-state access separately. |
| `CODEX_HOME` | Optional inherited, default `~/.codex` | Read for best-effort session usage and forwarded; allow only approved provider state. |
| `CI`, `NO_COLOR` | Plugin-managed defaults `1` | Existing values are preserved, otherwise created for the child; optional policy allowance for overrides. |
| `TMPDIR` | Optional runtime-injected temporary directory | Inherited by the provider; runtime controls its invocation-private value. |

Other runtime-authorized environment values are inherited unchanged; the argv
guard counts them without logging keys or values. Credentials remain external
provider state, never prompt/history inputs.

Provider children inherit the runtime process group. The package-local runner
drains output and confirms child cleanup before a terminal APP result, including
on timeout or callback failure. Unconfirmed cleanup fails with nonretryable
`protocol_error`; progress and heartbeats run on the main thread.

`codex.cli` is a task-first `process` plugin for non-interactive Codex runs.

## Runtime Capabilities And Endpoints

- Adapter: `process`.
- Capability grants: adapter invoke, `plugin:codex.cli@0.1.0` load, and `action:codex.cli@0.1.0/exec_task` invoke.

The manifest requires `python3` and `codex` on `PATH`. Optional demo `model`
and `reasoning_effort` inputs may be omitted: workflow-materialized `null`
values leave the corresponding CLI flags unset. Explicit values still select
the documented model and effort.

Offline installed-demo regression (public Transist v0.1.0, disposable homes, mocked
provider commands; also covers Copilot and OpenCode):

Public-runtime both-platform results are [recorded separately](../../docs/validation.md).

```sh
python3 -B plugins/codex.cli/tests/e2e_optional_inputs.py
```
- Secrets: none declared by the plugin.
- Filesystem: grant the declared workspace plus the run artifact/evidence paths used by the workflow.
- External endpoints: none are fixed by this plugin. Allow the provider configured for Codex; OpenAI-backed setups commonly require `https://api.openai.com:443`.

## Actions

- `exec_task`

## Action Reference

### `exec_task`

Executes a non-interactive Codex agent task.

| Parameter | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `prompt` | string | Conditional | Inline task text. (One of `prompt` or `prompt_file` required) |
| `prompt_file` | string | Conditional | Path to a file containing the task text. |
| `context_files` | array[string] | No | Supporting file contents to inline into the prompt. |
| `ignore_paths` | array[string] | No | Repo-relative glob patterns the wrapper and agent should ignore. Defaults include `**/_build/**` and `**/deps/*/_build/**`. |
| `gate_decision` | object | Runtime | Runtime-injected `transist.gate_decision.v1` manual-gate decision. |
| `feedback_context_mode` | enum | No | Replay mode: `full`, `focused`, or `feedback-only`. |
| `response_file` | string | No | Stable workspace path to persist the final agent response. |
| `timeout_ms` | integer | No | Timeout in milliseconds. |
| `timeout_seconds` | integer | No | Timeout in seconds. |
| `skills_bundle` | object | No | Embedded structured skills bundle. |
| `skills_rendered` | string | No | Rendered workflow-skills text block. |
| `model` | string | No | Codex model to use for this step. |
| `reasoning_effort` | enum | No | Reasoning depth: `low`, `medium`, `high`, or `xhigh`. |
| `profile` | string | No | Codex profile to use. |
| `sandbox` | enum | No | Sandbox mode: `read-only`, `workspace-write`, or `danger-full-access`. |
| `skip_git_repo_check` | boolean | No | Skip the git repository check. |
| `extra_args` | array[string] | No | Pass-through of additional Codex CLI flags. |

## Feature Coverage

`codex.cli` covers the non-interactive Codex surface a workflow step typically
needs to drive Codex as a one-shot agent:

- one-shot task execution through `codex exec` with prompt or prompt-file input
- per-step model selection (`-m <model>`)
- reasoning-effort control (`-c model_reasoning_effort=<low|medium|high|xhigh>`)
- execution inside the runtime-owned canonical workspace
- context-file injection into the composed prompt
- repo ignore policy injection via `ignore_paths`, with Elixir build artifact defaults
- workflow-skills injection (`skills_bundle`, `skills_rendered`) with persisted
  `workflow_skills.md` and `workflow_skills.json` artifacts
- manual-gate feedback replay with `full` / `focused` / `feedback-only` modes
- stable response persistence through `response_file`
- clean timeout handling (`timeout_ms` / `timeout_seconds`) with canonical
  `timeout_error` reporting
- pass-through of additional Codex CLI flags via `extra_args`

Intentionally not covered: long-lived interactive Codex sessions, persistent
history management, or provider-admin operations. Codex provider authentication
is delegated to the operator's existing Codex login state.

The plugin writes the final agent message into the step artifacts directory and
returns that text as `response_text`. It also supports a Copilot-style wrapper
baseline for repo-scoped execution, context-file injection, timeout control,
live progress frames, and optional stable `response_file` persistence.

The action accepts exactly one prompt source:

- `prompt` for inline text
- `prompt_file` for a file path that Transist resolves before invocation

Failure reporting now uses canonical Transist APP errors. Missing local
`codex` binaries surface as `configuration_error`, and timeouts surface as
`timeout_error` while preserving partial stdout/stderr in the failed APP
result and artifact hints when available.

It may also receive workflow-owned skills context through:

- `skills_bundle` for the embedded structured bundle
- `skills_rendered` for the rendered workflow-skills text block

Optional wrapper controls:

- all execution and workspace-relative paths use the runtime-owned canonical workspace
- `context_files` to inline supporting file contents into the composed prompt
- `ignore_paths` to skip repo-relative files and directories such as build outputs
- `gate_decision` is injected by the runtime to replay a manual-gate revision
  decision; callers must not provide gate IDs or filesystem paths
- `feedback_context_mode` to control how much previous draft context is replayed
- `response_file` to persist the final agent response into a stable workspace path
- `timeout_ms` or `timeout_seconds` to fail long-running invocations cleanly
- `model` to select the Codex model for this step (free-form string, provider-owned)
- `reasoning_effort` to tune reasoning depth: `low`, `medium`, `high`, or `xhigh`
- `extra_args` to pass through additional Codex CLI flags when needed

## Why This Shape

This plugin uses the `process` adapter because Codex is being used here as a
local non-interactive CLI. That makes stdio APP the simplest and most direct
integration surface.

The plugin is prompt-first instead of exposing arbitrary CLI arguments:

- the workflow contract stays understandable
- schema validation remains useful
- it wraps an agent CLI without turning the plugin into a generic command
  trampoline

The implementation stores the final agent message in the step artifacts
directory because agent CLIs often produce mixed terminal output. Persisting the
last message gives workflows and humans a stable result artifact, and
`response_file` makes it easier for later workflow steps to consume the same
result without depending on step-output interpolation.

When `skills_rendered` is present, the plugin prepends that block to the main
prompt and persists `workflow_skills.md` plus `workflow_skills.json` artifacts.
When `context_files` are present, the wrapper injects each readable file into
the composed prompt before the final `# Task` section. While the underlying
`codex exec` command is running, the wrapper emits lightweight progress frames
so `transist workflows run` can keep the step visibly active.

When the runtime injects a `gate_decision` whose decision is `request_changes`,
the wrapper can also replay:

- the gate reason
- the current draft from `response_file`
- prior Codex interaction history for that artifact

You can tune that replay with `feedback_context_mode`:

- `full`: include the gate reason, current draft, and prior interaction history
- `focused`: include the gate reason and current draft, but omit prior
  interaction history
- `feedback-only`: include only the gate reason before the normal context files

`full` is the default for backwards compatibility. Revision-heavy workflows
should usually prefer `focused`.

## Agent Controls

These inputs are plugin-owned `with` fields. They belong in the step `with`
block and are not part of the `transist.flow.v1` schema syntax.

| Input | Type | Description |
|---|---|---|
| `model` | string | Codex model to use for this step. Free-form; model catalog is provider-owned. |
| `reasoning_effort` | enum | Reasoning depth. One of `low`, `medium`, `high`, `xhigh`. Maps to `codex exec -c model_reasoning_effort=<value>`. |

Different workflow steps can intentionally use different models or reasoning
levels. For example, a planning step might use `reasoning_effort: high` while a
summarization step uses `reasoning_effort: low`.

`temperature` is not yet supported.

## Prerequisites

- `codex` available in `PATH`
- authenticated Codex session or other configured provider
- installed `transist` binary from the main repo:
  [transist-ai/transist](https://github.com/transist-ai/transist)
- released binary downloads:
  [v0.1.0 release](https://github.com/transist-ai/transist/releases/tag/v0.1.0)

## Install

```bash
transist plugins install --path ./plugins/codex.cli
transist plugins inspect codex.cli
```

## Testing

Offline smoke test:

- does not require the `codex` binary
- does not require network or an authenticated session
- validates APP framing, progress updates, repo/context handling, file output, and missing-binary handling

```bash
python3 ./plugins/codex.cli/tests/smoke.py
```

Live integration test:

- requires `codex` in `PATH`
- requires a ready Codex login/configuration state
- runs a real non-interactive `codex exec` invocation

```bash
python3 ./plugins/codex.cli/tests/live.py
```

## Demo Workflow

```bash
transist workflows check --path ./plugins/codex.cli/workflow.yaml
transist workflows install --path ./plugins/codex.cli/workflow.yaml
transist workflows check --workflow codex_cli_demo@0.1.0 --inputs-file <inputs-file> --output readiness.json
transist workflows run codex-cli-demo --workflow codex_cli_demo@0.1.0 --inputs-file <inputs-file> --readiness-file readiness.json
```
