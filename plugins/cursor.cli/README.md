# `cursor.cli`

Package version: `0.1.0`.

## Gate decision metadata

Both `exec_task` and `exec_task_cli` accept optional runtime-injected
`gate_decision.review_request` for document gates through their shared input
schema. The closed request contains run/gate/step IDs, positive gate execution
and nonempty typed document revisions (producer, revision/blob digests, kind,
sizes and counts). Initialization revisions may have execution zero.
The selected local `schemas/gate-decision.schema.json` bundles the canonical
runtime definitions. Both actions retain required `decided_at_ms` even though
the runtime schema permits omission. Legacy decisions without `review_request`
remain valid. Request-changes reason, actor and actor-source attribution reach
the existing feedback prompt; metadata itself is never appended or dereferenced.
Supply content through `context_files`. Exact revision approval and authority
remain kernel checks.

Nested integer metadata accepts finite integral numbers such as `1.0`, with exact
bounds comparisons; booleans, fractions and nonfinite values fail. Identifiers
and digests must match the whole string, including no trailing newline (a stricter
semantic check than canonical schema `$` anchors). An invalid provided decision
with `review_request` returns `validation_error` before provider work, never
silently dropping feedback. Existing timestamp-reader behavior is unchanged.

## Managed document paths

Both `exec_task` and `exec_task_cli` accept runtime-staged absolute paths for
`context_files`, `response_file`, `summary_title_file` and `pr_body_file`. Paths
are file data; provider cwd and `--workspace` remain the runtime-selected canonical
workspace. Existing allowed absolute/workspace-relative paths, `repo://` under
workspace `.transist/`, and private `run://` are supported. Reject traversal,
symlinks and nonregular bounded context files. Bounded UTF-8 contexts reach the
composed prompt exactly, with their whitespace/CRLF inside the existing headings.

With `history_mode: invocation`, out-of-workspace absolute responses have
artifact-only history and no replay/neighbor sidecars. Legacy history for these
responses uses the invocation artifact directory. The runtime persists document
revisions; staged paths and `run://` expire after dispatch. Pass previous document
content through `context_files` for revision work. Full implicit feedback replay
requires workspace-backed history; focused/feedback-only never reuse expired
staging. `prompt_file` retains the runtime prompt/captured-asset binding contract;
there is no `system_prompt_file` input. Compact output, usage, argv/prompt budgets,
stream handling and cancellation retain their existing contracts.

## Opt-in lean CLI contract

Complete answer events are authoritative in compact/invocation mode; markdown
headings inside full review reports cannot discard earlier findings. Missing
answer events fail explicitly. `extra_args` cannot override answer transport.

| Input | Values / default | Meaning |
|---|---|---|
| `output_mode` | `legacy` (default), `compact` | Removes raw `stdout` / `stderr` properties. Retains canonical response, outcome/exit code, feedback, session/share fields, summary/handoff paths and APP usage; adds `pr_body_text`, `output_mode: compact` and closed `diagnostics`. |
| `history_mode` | `legacy` (default), `invocation` | One `invocation.json` artifact containing IDs, outcome and composed prompt once (including its task section); references log/answer artifacts by logical name. |
| `max_context_file_bytes` | Optional positive integer | UTF-8 byte limit for each prompt/context/draft/history file read. |
| `max_prompt_bytes` | Optional positive integer | UTF-8 byte limit for all composed sections, final headings and separators before launch. |

The demo adopts compact output, invocation history, focused replay, **65536 bytes
per file / 98304 bytes total**. Omitted controls preserve legacy whitespace,
output and cumulative JSON/Markdown history. Exact byte limits pass; bounded
reads reject missing, oversized and invalid UTF-8 inputs before provider launch.
Budget errors identify logical contributor labels/counts without contents or
private filenames. The final argv guard includes encoded arguments, actual
child environment, NUL terminators and pointer overhead, plus Linux per-string
limits. It accounts for authentication isolation and final environment overrides.
Invocation metadata includes `prompt_budget`: sanitized section labels/counts,
joined UTF-8 total and configured limit, without source paths or contents.
Empty/whitespace or null optional `context_files` entries are skipped, including
an absent initial validation report. Nonempty missing paths still fail before launch.

Invocation history is **artifact-only** for `response_file: run://...`. The
existing action resolver uses invocation-private `InvokePaths.run_root`; the
resolved target must remain inside that staging root. Metadata/log APP hints are
emitted once, with no replay directory, history reads, cumulative exports, or
workspace writes. This supports read-only workspaces. The response is scratch
and is removed with staging. With revision feedback, `focused`/`feedback-only`
include current gate feedback only, never a scratch draft. `full` (also the
default) explicitly fails with feedback when durable replay is unavailable;
without feedback it works. `runtime://...` is not an action response-path scheme.

**Durable replay** uses immutable records and separate answer files under
`<response-basename>.invocations/<plugin-and-root-hash>/`, or
`<workspace>/.transist/cursor.cli/invocations/<hash>/` without a response target.
Approve exact workspace read/write paths. Workspace-relative and `repo://...`
response targets must resolve inside the canonical workspace; `repo://...` uses
its `.transist/` directory. External targets use artifact-only history.
No staging paths are advertised as durable.
Records contain feedback/outcome and a relative answer filename, never raw logs,
task/composed-prompt snapshots or cumulative exports. Metadata is retained once
in its APP hint. `focused` reads only the latest same-root answer plus current
gate feedback; `full` renders selected same-root records under the prompt
budget. A fresh root cannot read the previous root's draft. Unconfigured
invocation replay file reads and full replay have a 256 KiB ceiling.

Raw stdout/stderr are supplied once each as existing APP artifact hints;
retention follows runtime policy. Compact diagnostics contain logical artifact
labels, `total_bytes`, `retained_bytes`, and `full_log_available`, without content.
Adapter limits are **32 hints / 256 KiB each / 1 MiB contents per family**.
Opt-in packages additionally enforce a **1 MiB serialized JSON result envelope**,
including escaping/newline; this is package policy, not an APP transport cap.
Compact reserves complete answer/metadata artifacts before optional logs. Diagnostic
log previews may be shortened/omitted with byte accounting while preserving the
backend outcome. Oversized required non-log artifacts fail with `protocol_error`
and are unavailable rather than invalid partial JSON. Authoritative output is never
silently shortened; envelope failure returns empty failed output and a manifest.
Full-log availability is false when complete logs cannot fit. Closed conditional
schema branches require actual log properties for legacy and forbid them for
compact. Failed diagnostic output allows null exit code; success requires an
integer. Early APP failures can have empty output. Partial structured timeout
logs are parsed for answer events rather than promoted to response text.
Diagnostic entries declare `retention`: `full`, `diagnostic_preview`, or
`unavailable`. Preview contents are explicitly labeled diagnostic-only.
Escaping-related envelope pressure shortens only log previews before failing
an unrepresentable authoritative output. A fitting answer remains whole.
Metadata/replay preserve the backend outcome unless required data cannot fit;
unavailable artifact references
are null. Retained hint contents remain strictly below 1 MiB in total.

Offline matrix, including both Cursor terminal outcomes and legacy regressions:

```sh
python3 -B -m unittest discover -s plugins/codex.cli/tests -p test_lean_cli_contract.py -v
```

Additional child environment accounting: `PATH` is required inherited binary
lookup (policy must allow approved binaries); `CI` and `NO_COLOR` are optional
inherited overrides with plugin-created default `1`; `CURSOR_CLIENT` is optional
inherited with plugin-created default `cursor.cli`. Existing runtime-authorized
values are forwarded; the byte guard counts all final environment values
without logging their names/content. Authentication and temporary-directory
variables retain the classifications in the environment tables below.

`cursor.cli` is a prompt-aware `process` plugin for non-interactive Cursor
Agent runs through the `agent` (or `cursor-agent`) CLI.

It is designed as a drop-in replacement for `opencode.cli`. Existing
`opencode.cli` workflows can usually switch to `cursor.cli` by updating the
exact `plugins` dependency, the `uses` selector, and `with.model`.

## Runtime Capabilities And Endpoints

- Adapter: `process`.
- Capability grants: adapter invoke, `plugin:cursor.cli@0.1.0` load, and the selected `exec_task` or `exec_task_cli` action invoke.

The manifest requires `python3` on `PATH`. The provider executable is resolved
from `CURSOR_AGENT_PATH`, `agent`, or `cursor-agent`, so it is checked by the
action rather than a single fixed static binary prerequisite.
The lifecycle helper requires Python 3.9+; verify the interpreter selected
by both runtime preflight and execution, not only the caller's shell.

- Authentication: `exec_task` requires a `CURSOR_API_KEY` secret binding; `exec_task_cli` delegates to the operator's existing Cursor CLI login without a secret binding.
- Filesystem: grant the run artifact/evidence paths and any repository, context, prompt, or response paths used by the workflow. CLI login may require exact external access to its provider-owned account state; validate that access in the governed sandbox. Never grant the entire operator home as a shortcut.
- External endpoints: provider-managed. Current Cursor Agent installations commonly require `https://api2.cursor.sh:443` and `https://agentn.us.api5.cursor.sh:443`; verify the installed CLI/provider before narrowing the allowlist.

## Actions

- `exec_task` (required `CURSOR_API_KEY`)
- `exec_task_cli` (existing Cursor CLI login)

## Runtime Environment

| Variable | Classification | Purpose | Value and policy handling |
| --- | --- | --- | --- |
| `CURSOR_AGENT_PATH` | Optional inherited | Overrides the `agent` / `cursor-agent` executable lookup. | Set only to an approved local executable; no secret value is expected. |
| `CURSOR_API_KEY` | Required for `exec_task` only | Authenticates API-key runs. | Declare `with.secret_refs: ["CURSOR_API_KEY"]` for `exec_task`; `exec_task_cli` removes this variable from its child environment. |
| `CURSOR_CONFIG_DIR` | Plugin-managed | Isolates Cursor configuration for API-key runs. | `runtime://temp/cursor/config`; removed from the `exec_task_cli` child environment. |
| `CURSOR_DATA_DIR` | Plugin-managed | Isolates Cursor provider data for API-key runs. | `runtime://temp/cursor/data`; removed from the `exec_task_cli` child environment. |
| `HOME` | Child-process-only for API-key runs | Isolates API-key mode from the operator's CLI login. | Set to the invocation-private Cursor config directory for `exec_task`; unset for `exec_task_cli`, leaving account lookup to Cursor CLI. |
| `TMPDIR` | Plugin-managed | Isolates transient provider files. | `runtime://temp/cursor/tmp`; created and injected by the runtime. |

`TMPDIR` remains invocation-private in both actions. The CLI-login action rejects
`--api-key` and custom auth headers in `extra_args`, does not copy or persist
credentials, and relies on the existing Cursor CLI login and
only on filesystem access explicitly approved for the workflow. Readiness does
not establish that a CLI login is active; verify it with a separately approved
governed live invocation.

## Action Reference

### `exec_task` and `exec_task_cli`

Executes a non-interactive Cursor Agent task.

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
| `allowed_tools` | array[string] | No | Accepted for drop-in compatibility; ignored by Cursor Agent. |
| `denied_tools` | array[string] | No | Accepted for drop-in compatibility; ignored by Cursor Agent. |
| `timeout_ms` | integer | No | Timeout in milliseconds. |
| `timeout_seconds` | integer | No | Timeout in seconds. |
| `model` | string | No | Cursor model to use for this step. |
| `reasoning_effort` | enum | No | Accepted for drop-in compatibility; ignored. |
| `variant` | string | No | Accepted for drop-in compatibility; ignored. |
| `agent` | string | No | Accepted for drop-in compatibility; ignored. |
| `session_title` | string | No | Accepted for drop-in compatibility; ignored. |
| `share` | boolean | No | Accepted for drop-in compatibility; ignored. |
| `dangerously_skip_permissions` | boolean | No | Alias for `force`. Defaults to `true`. |
| `permission` | object | No | Accepted for drop-in compatibility; ignored. |
| `extra_args` | array[string] | No | Pass-through of additional Cursor Agent CLI flags. |
| `skills_bundle` | object | No | Embedded structured skills bundle. |
| `skills_rendered` | string | No | Rendered workflow-skills text block. |
| `secret_refs` | array[string] | `exec_task` only | Include `CURSOR_API_KEY` for the key-backed action. Omit for `exec_task_cli`; raw values are never included in action input. |
| `mode` | enum | No | Cursor execution mode: `plan` or `ask`. |
| `force` | boolean | No | Auto-approve non-denied Cursor Agent tools. Defaults to `true`. |
| `approve_mcps` | boolean | No | Automatically approve all MCP servers. |
| `sandbox` | enum | No | Explicitly enable or disable sandbox mode: `enabled` or `disabled`. |

## Feature Coverage

`cursor.cli` covers the non-interactive Cursor Agent surface that prompt-driven
automation typically needs:

- one-shot prompt execution from inline prompts or prompt files
- per-step `model` selection
- `mode` control (`plan` / `ask`)
- autonomous execution with `force` defaulting to `true`
- `--approve-mcps` and `--sandbox` pass-through
- stream-json event parsing with:
  - final assistant text extracted from `result` and `assistant` events
  - token usage extracted from `result.usage` (written to `usage.json` artifact;
    APP `usage` contains only canonical model/provider and input/output/total
    token counts, with cache details retained in the artifact)
- workflow-skills injection prepended to the prompt
- repo ignore policy injection via `ignore_paths`, with Elixir build artifact defaults
- manual-gate feedback replay with `full` / `focused` / `feedback-only` modes
- stable artifact persistence (`response_file`, `summary_title_file`,
  `pr_body_file`) with canonical markdown extraction
- clean timeout handling (`timeout_ms` / `timeout_seconds`) with canonical
  `timeout_error` reporting
- pass-through of additional Cursor Agent CLI flags via `extra_args`

Intentionally not covered: interactive Cursor sessions, Cursor web UI,
plugin/extension management, or provider-admin operations.

The action preserves a prompt-aware workflow contract: prompt composition,
workflow skills injection, manual-gate feedback replay, stable
`response_file` persistence, and canonical markdown extraction for summary
and PR-body artifacts.

The action accepts exactly one prompt source:

- `prompt` for inline text
- `prompt_file` for a file path that the wrapper reads before invocation

Optional wrapper controls:

- Cursor Agent runs inside the runtime-owned canonical workspace
- `context_files` to inline supporting file contents into the composed prompt
- `ignore_paths` to skip repo-relative files and directories such as build outputs
- `gate_decision` is injected by the runtime to replay a manual-gate revision
  decision; callers must not provide gate IDs or filesystem paths
- `feedback_context_mode` to control how much previous draft context is replayed
- `response_file`, `summary_title_file`, and `pr_body_file` for stable artifacts
- `timeout_ms` or `timeout_seconds` to fail long-running invocations cleanly
- `model` to select the Cursor model for this step
- `mode` to choose `plan` (read-only) or `ask` mode
- `force` to auto-approve Cursor Agent tool calls (defaults to `true` for
  autonomous workflows)
- `dangerously_skip_permissions` as an `opencode.cli` compatibility alias for
  `force`
- `approve_mcps` to automatically approve all MCP servers
- `sandbox` to explicitly enable or disable sandbox mode
- `extra_args` to pass through additional Cursor Agent CLI flags when needed

## Path Schemes And Lifecycle

File-oriented inputs and outputs such as `context_files`, `response_file`,
`summary_title_file`, and `pr_body_file` support:

- workspace-relative paths under the runtime-declared canonical workspace
- absolute paths subject to exact approved filesystem access
- `repo://...` under `<workspace>/.transist/...`
- `run://...` under `InvokePaths.run_root`, which is private invocation staging
  in Transist v0.1.0 and is removed after the invocation

Use root managed documents for workflow-owned drafts and handoffs needed across
steps, manual-gate revisions, retries or resume. Supply prior revisions explicitly
through document-bound `context_files`, with `history_mode: invocation`; do not
reuse staged paths or assume neighboring history files are published documents.
See [managed document authoring](../../docs/authoring.md#path-lifecycle-and-cross-step-storage).
Workspace and `repo://...` targets remain supported for deliberately
repository-owned files and replay history, with repository-defined retention.
Action `run://...` and provider `runtime://temp` are invocation scratch.

Filesystem capability resources `run://artifacts` and `run://evidence` describe
runtime-controlled artifact/evidence authority, not plugin action URIs. The
runtime may retain accepted APP hints according to policy, but that does not
preserve arbitrary scratch files or turn them into mutable cross-step drafts.
Path resolution and provider flags do not grant additional filesystem authority.

The workflow's first-class `prompt_file` is a separate contained package asset,
captured and injected by the runtime. Do not use a mutable working-file URI as
that authored asset path.

## Drop-in Compatibility Notes

The following `opencode.cli` input fields are accepted for compatibility but
have no effect with Cursor Agent because the underlying CLI does not expose
matching controls:

- `reasoning_effort`, `variant`
- `agent`, `session_title`
- `share` (`share_url` in output is always empty)
- `allowed_tools`, `denied_tools`
- `permission`

## Permission / Autonomous Defaults

Cursor Agent supports coarse permission control only: `--force` (also aliased
as `--yolo`) auto-approves non-denied tool calls. The plugin defaults `force` to
`true` so Transist workflows execute autonomously. Set `force: false` (or
`dangerously_skip_permissions: false`) to require manual approval.

## Prerequisites

- `agent` or `cursor-agent` available in `PATH`, or `CURSOR_AGENT_PATH` set
- for `exec_task`, a `CURSOR_API_KEY` available through the runtime secret store and permitted for process delivery
- for `exec_task_cli`, a working Cursor CLI login reachable under the exact approved governed filesystem policy
- installed `transist` binary from the main repo:
  [transist-ai/transist](https://github.com/transist-ai/transist)
- released binary downloads:
  [v0.1.0 release](https://github.com/transist-ai/transist/releases/tag/v0.1.0)

## Runtime-Owned Invocation Environment

The package declares these runtime-owned directories in `plugin.yaml`. Transist
resolves them for each invocation; operators do not configure or policy-allow
host values for them.

| Variable | Declared value | Purpose |
|---|---|---|
| `CURSOR_CONFIG_DIR` | `runtime://temp/cursor/config` | Isolates Cursor configuration. |
| `CURSOR_DATA_DIR` | `runtime://temp/cursor/data` | Isolates Cursor data. |
| `HOME` | private Cursor config directory for `exec_task`; unset for `exec_task_cli` | Separates key-backed execution from CLI login. |
| `TMPDIR` | `runtime://temp/cursor/tmp` | Provides a runtime-owned temporary directory. |
| `CURSOR_API_KEY` | Logical `CURSOR_API_KEY` secret reference, only for `exec_task` | Authenticates Cursor Agent without interactive login. |

## Install

```bash
transist plugins install --path ./plugins/cursor.cli
transist plugins inspect cursor.cli
```

## Process Cleanup Contract

The plugin preserves the runtime-owned invocation process group: it never starts
a new session or group. Before launching Cursor it protects its own PID, the
entire ancestor chain (including a sandbox supervisor), and existing group
members. On success, nonzero exit, timeout, or unexpected exceptions, it rescans
for live remaining invocation-group members, verifies birth identity and group
before each individual signal, sends TERM with a **two-second total** budget,
then KILL to verified survivors with a **two-second total** budget, and rescans
to confirm no live owned members remain. Zombies/exited processes are complete.
The runtime must provide an invocation-private group; escaped-group processes
remain the runtime's containment/cancellation responsibility.

Linux uses `/proc` metadata and pidfd signaling (requires Python/OS pidfd
support); macOS uses libproc metadata through ctypes with birth/group checks
immediately before signaling. macOS has no pidfd, so its check and signal cannot
be atomic. System ancestors with privileged birth metadata use libproc's short
parent metadata solely to protect their PIDs; cleanup candidates always require
full birth metadata. No `ps`, third-party runtime package, command-line inspection, or
credential/account inspection is used. Permission, observation, or identity
uncertainty fails closed rather than authorizing an unsafe signal.

Interruptible pipe readers stop/join within **one second total**, giving a
five-second cleanup/wait budget. There is no blocking drain of inherited pipes.
Heartbeat and progress emission run only on the main thread and continue while
provider cleanup is underway. Captured output is frozen before the single
terminal APP result; readers cannot emit frames or mutate that output. Native
system calls and APP transport writes still depend on OS/transport availability.
If cleanup cannot be confirmed, the result is `protocol_error` with message
`Cursor process cleanup could not be confirmed` and captured ordinary
response/stdout/stderr artifact hints. Successful cleanup preserves the primary
provider outcome, session, response, usage, authentication isolation, and existing
timeout interpretation. Kernel cancellation precedence remains with the runtime.

## Testing

Installed lifecycle regression (stdlib only; real runtime and native sandbox):

```bash
python3 -B plugins/cursor.cli/tests/e2e_lifecycle.py \
  --transist /absolute/path/to/candidate/transist --candidate-lifecycle \
  --baseline-transist /absolute/path/to/baseline/transist \
  --fixture-base /absolute/path/to/allowed/projects/tmp
```

`--transist` is required (`TRANSIST_BIN` is also accepted); a missing binary is
a failure, with no PATH lookup or fallback. Both binaries must advertise 0.1.0.
`--candidate-lifecycle` explicitly selects the Step-1 post-terminal contract;
version strings never select behavior. The optional baseline runs independently
in fresh homes and expects the old generic lingering-boundary timeout/exit 124.
Use repeatable `--case leader --case orphan` to investigate just those generic
APP cases; a partial matrix does not establish Cursor acceptance.

The runner installs the exact unmodified selected `cursor.cli@0.1.0` payload,
including `process_lifecycle.py`, and compares installed bytes with source. It
uses CLI workflow installation/readiness/run and bounded `runs show` polling,
not direct plugin calls. Ten cases cover clean success, leftover child, inherited
pipes, TERM resistance, provider timeout/failure, runtime cancellation precedence,
API-key isolation/redaction, and two separate generic APP lingering leader/child
fixtures. Success checks response/stdout/usage artifacts. The generic candidate
checks `protocol_error`, empty execution output and confirmed lifecycle evidence.

The fake Cursor executable lives inside a disposable canonical workspace. A
separate disposable standalone home permits only `PATH` and `CURSOR_AGENT_PATH`
as inherited child variables, enables process secret delivery, and receives a
fake key only through the declared `exec_task` binding. CLI-login fixtures require
the key/config/data/HOME variables to be absent and never read account state.
Network is `none`; a host loopback listener must receive zero connections. Raw
and base64 key sentinels must be absent from CLI output and persisted home files,
with redaction checked in both stdout and stderr artifacts. Reports print only
case names and safe errors. Native birth-identity checks verify no live owned PID
at terminal state; cleanup always cancels outstanding runs and signals only
verified fixture identities before removing homes/workspaces.
The detached runner's announced PID is tracked by birth identity and also
confirmed exited (or cleaned up after a bounded wait) before fixture removal.

Provide an **existing** sandbox-allowed fixture base outside the runtime home and
system temporary parent. Paths are canonicalized (`/tmp` is `/private/tmp` on
macOS); macOS fixtures beneath `/private/tmp` are rejected. Native sandbox
failure is a failure, never a skip. No Go, requests, provider credentials or
publisher keys are needed. Governed signing and live-provider validation
remain separate acceptance gates.

Fixtures use three disjoint siblings: `source/` for authored workflows, `home/`
for runtime state/staging, and `workspace/` for the fake provider and outputs.
Never nest the home or workspace beneath the recipe source directory: on Linux
its read-only mount can mask writable runtime staging. Runtime homes also must
not be nested inside the workspace. The driver regression checks these path
boundaries and actual workflow placement. Use a **native Linux filesystem** for
fixture state (for example an explicitly mounted disposable tmpfs); host-backed
FUSE shares may not preserve the stable file identity required by home
publication. This fixture layout requires no mount-policy changes or wider grants.

Historical development fixture-layout checks passed all ten candidate cases on native Linux
tmpfs with bubblewrap, and all ten cases per binary on macOS with sandbox-exec.
Linux evidence is candidate-only; no Linux baseline binary was tested. See the
compatibility record for exact runtime identity and logs.

On macOS, preflight forces the system PATH and selects Python 3.9.6 on the
tested host. The helper uses postponed annotations so modern union type hints
are not evaluated during import on Python 3.9. The focused driver test imports
both selected Python files through `/usr/bin/python3 -I -B`, without invoking
Cursor or reading account state. This repairs the initial preflight import
failure without changing process protection, cleanup budgets, signals, APP
framing or authentication. The source declares `cursor.cli@0.1.0`; each
fixture installs those exact package bytes, never patches an installed payload.
Both historical installed matrices passed; see the
[archived compatibility evidence](../../docs/history/compatibility.md).
These results do not establish public v0.1.0 acceptance; the final
both-platform gate is [recorded separately](../../docs/validation.md).

Actual-subprocess lifecycle tests (no provider credentials/network required):

```bash
python3 -B -m unittest discover -s plugins/cursor.cli/tests -p test_lifecycle.py
```

Offline smoke test:

- does not require the `agent` binary
- does not require network or a configured provider
- validates APP framing, prompt composition, file output, usage extraction,
  command-line limits, and missing-binary handling

```bash
python3 ./plugins/cursor.cli/tests/smoke.py
```

Live integration test:

- requires `agent` or `cursor-agent` in `PATH`, or `CURSOR_AGENT_PATH`
- requires a usable Cursor Agent runtime and `CURSOR_API_KEY` for the key-backed test
- runs a real non-interactive Cursor Agent invocation

```bash
python3 ./plugins/cursor.cli/tests/live.py
```

## Demo Workflow

```bash
transist workflows check --path ./plugins/cursor.cli/workflow.yaml
transist workflows install --path ./plugins/cursor.cli/workflow.yaml
transist workflows check --workflow cursor_cli_demo@0.1.0 --inputs-file <inputs-file> --output readiness.json
transist workflows run cursor-cli-demo --workflow cursor_cli_demo@0.1.0 --inputs-file <inputs-file> --readiness-file readiness.json
```
