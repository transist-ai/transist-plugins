# `github.copilot.cli`

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
runtime-staged absolute document paths. File data never selects provider cwd or
repository authority: both cwd and `--add-dir` retain the runtime-selected canonical
workspace. Existing allowed absolute/workspace-relative paths, `repo://` under
workspace `.transist/`, and invocation-private `run://` remain supported. Reject
traversal, symlinks and nonregular bounded contexts. Bounded UTF-8 context bytes
are preserved inside prompt headings, including whitespace and CRLF.

Use `history_mode: invocation` for managed responses. Out-of-workspace absolute
responses have artifact-only history, with no replay/neighbor sidecars. Legacy
history for these outputs uses invocation artifacts. The runtime persists logical
document revisions; staged paths and `run://` expire after dispatch. Supply prior
documents explicitly in `context_files` for revisions. Full implicit feedback
replay needs workspace-backed durable history; focused/feedback-only never reuse
expired staging. `prompt_file` keeps the runtime prompt/captured-asset binding
contract; `system_prompt_file` is not an input. JSON answer transport, usage,
prompt/envelope budgets and child-process cleanup remain enforced.

## Opt-in lean CLI contract

Compact/invocation mode selects `--output-format json` and extracts the last
completed `assistant.message.data.content` without tool requests. Tool events,
message deltas, plain narration and stderr are diagnostics, never answer fallbacks.
An absent answer explicitly fails with `protocol_error`. Full review reports
remain whole; markdown heading heuristics apply only to legacy mode.
`extra_args` cannot override authoritative answer transport. This requires a
Copilot CLI supporting JSONL output; the offline fixture tests this event contract.
Before an opt-in task, an action-level `gh copilot -- --help` probe checks advertised
`--output-format json` and `--stream on` options, using the same final environment
and workspace. The probe sends no task prompt and is bounded to 30 seconds or the
configured shorter timeout. Missing options, probe failure or timeout return
`configuration_error` before task launch. Legacy mode skips this probe. This is
not an executable manifest prerequisite. The documented transport is described in
the [Copilot programmatic reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-programmatic-reference).

| Input | Values / default | Meaning |
|---|---|---|
| `output_mode` | `legacy` (default), `compact` | Forbids raw `stdout` / `stderr` properties and adds `output_mode: compact`, closed `diagnostics`, and `pr_body_text`. Keeps canonical response, outcome/exit code, feedback, summary/handoff paths and APP usage. |
| `history_mode` | `legacy` (default), `invocation` | One `invocation.json` artifact with IDs, outcome, composed prompt once (including its task section), and logical log/answer references. |
| `max_context_file_bytes` | Optional positive integer | UTF-8 byte limit per prompt/context/draft/history file read. |
| `max_prompt_bytes` | Optional positive integer | UTF-8 byte budget for all contributors, final headings and separators before launch. |

The example adopts compact output, invocation history, focused replay and
**65536 bytes per file / 98304 bytes total**. Omission preserves legacy output,
prompt/current-draft whitespace and cumulative history. Strict reads are bounded;
missing, oversized and invalid UTF-8 inputs fail before launch. Exact byte limits
pass. Errors contain logical labels/counts, not contents or private paths.
The argv check includes UTF-8 arguments, final child environment, NUL terminators,
pointer overhead and Linux per-string limits; config overrides are included.
Invocation metadata includes `prompt_budget`: sanitized section labels/counts,
joined UTF-8 total and configured limit, without source paths or contents.
Empty/whitespace or null optional `context_files` entries are skipped. Nonempty
paths still require a readable UTF-8 file before provider launch.

Invocation history is **artifact-only** with `response_file: run://...`. The
existing action resolver uses invocation-private `InvokePaths.run_root`, and
the resolved target must remain inside that staging root. Metadata/logs are
emitted once as APP hints. No replay directory, history reads, cumulative exports,
or workspace writes are made, supporting a read-only workspace. The response is
scratch and follows invocation cleanup. Revision feedback in `focused` or
`feedback-only` includes gate feedback only, never a scratch draft. `full` (also
the default) fails explicitly with feedback because durable replay is unavailable;
without feedback it works. `runtime://...` is not an action response-path scheme.

For **durable replay**, records and separate answers live under
`<response-basename>.invocations/<plugin-and-root-hash>/`, or
`<workspace>/.transist/github.copilot.cli/invocations/<hash>/` with no response
target. Approve exact read/write authority for these workspace destinations.
Workspace-relative and `repo://...` response paths must resolve inside the
canonical workspace (`repo://...` maps under `.transist/`). External targets use
artifact-only history; staging is not durable.
Minimal records store feedback/outcome and a relative answer filename. They do
not copy raw logs, tasks/composed prompts, or cumulative sidecar snapshots.
Metadata is emitted once as an APP artifact; logs/answer references are logical
artifact names, not phantom staging paths. `focused` reads only the latest
same-root answer plus current gate feedback. `full` renders selected same-root
records under the prompt budget; fresh roots cannot reuse earlier-root drafts.
Without configured limits, invocation replay file reads and full replay have a
256 KiB ceiling. Retention follows workspace cleanup.

Raw logs are supplied once each as existing stdout/stderr APP hints. Compact
`response.txt` retains the substantive canonical answer separately. Diagnostics
contain logical artifact labels, `total_bytes`, `retained_bytes`, and
`full_log_available`, never contents. Adapter caps are **32 hints / 256 KiB each /
1 MiB contents per family**. Opt-in packages also enforce a **1 MiB serialized
JSON result envelope**, including escaping/newline; this is package policy,
not an APP transport cap. Compact reserves complete answer/metadata artifacts
before optional logs. Diagnostic log previews may be shortened/omitted with byte
accounting, preserving the backend outcome. Oversized required non-log artifacts
fail with `protocol_error` and are unavailable, never invalid partial
JSON. Authoritative output is never silently truncated. Envelope failure returns
empty failed output with a diagnostic manifest. Complete-log availability is
false when full logs cannot fit. Closed legacy/compact branches require actual
log fields only for legacy; null exit code is allowed for failed diagnostics,
while success requires an integer. Early failures can have empty APP output.
Diagnostics declare `retention`: `full`, `diagnostic_preview`, or `unavailable`.
Previews include an explicit diagnostic-only label. Escaped-JSON envelope pressure
shortens only log previews first and preserves a fitting authoritative answer.
Metadata/replay preserve the backend outcome unless required data cannot fit;
unavailable artifact references
are null. Retained hint contents remain strictly below 1 MiB in total.

Offline four-package matrix:

```sh
python3 -B -m unittest discover -s plugins/codex.cli/tests -p test_lean_cli_contract.py -v
```

Additional environment controls: `PATH` is required inherited binary lookup;
policy must allow approved binaries. `HOME` is optional inherited provider home,
read for account/config expansion; grant exact provider-state access separately.
`CI` and `NO_COLOR` preserve optional inherited overrides, otherwise the plugin
creates child defaults `1`. Runtime-authorized environment is inherited unchanged
(including optional provider-owned credentials, which must be allowed/injected
and redacted by runtime policy); byte accounting never logs values. Temporary
and provider-state variables are classified in the environment table below.

Provider children inherit the runtime process group. The package-local runner
drains output and confirms child cleanup before a terminal APP result, including
on timeout or callback failure. Unconfirmed cleanup fails with nonretryable
`protocol_error`; progress and heartbeats run on the main thread.

`github.copilot.cli` is a prompt-aware `process` plugin for non-interactive
GitHub Copilot CLI runs through `gh copilot`.

## Runtime Capabilities And Endpoints

- Adapter: `process`.
- Capability grants: adapter invoke, `plugin:github.copilot.cli@0.1.0` load, and `action:github.copilot.cli@0.1.0/exec_task` invoke.

The manifest requires `python3` and `gh` on `PATH`, with `gh copilot` available.
Optional demo `model`
and `reasoning_effort` inputs may be omitted: workflow-materialized `null`
values leave the corresponding CLI flags unset. Explicit values retain their
documented behavior.

APP usage contains only canonical model/provider and token-count fields.
Provider-specific reasoning/cache details are retained in the `usage.json`
artifact hint, when usage is available.

The shared offline installed-demo regression targets required-only input
binding and explicit model/effort options through public Transist v0.1.0:
`python3 -B plugins/codex.cli/tests/e2e_optional_inputs.py`. It uses disposable
homes and mocks provider commands, without a live GitHub request.
Public-runtime both-platform results are [recorded separately](../../docs/validation.md).

- Secrets: none declared by the plugin; authentication stays in the operator's `gh` login state.
- Filesystem: grant the run artifact/evidence paths and any repository, context, prompt, or response paths used by the workflow. Plugin-managed Copilot configuration, cache, and temporary data use declared private runtime directories; they do not require workspace write access.
- External endpoints: GitHub/Copilot CLI commonly requires `https://api.githubcopilot.com:443`, `https://api.github.com:443`, and `https://github.com:443`.

## Action Reference

### `exec_task`

Runs a non-interactive task through `gh copilot`.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `prompt` | string | No* | Task instructions. Exactly one of `prompt` or `prompt_file` is required. |
| `prompt_file` | string | No* | Path to a file containing task instructions. |
| `context_files` | array | No | Supporting files to include in the prompt context. |
| `ignore_paths` | array | No | Repo-relative glob patterns the wrapper and agent should ignore. Defaults include `**/_build/**` and `**/deps/*/_build/**`. |
| `response_file` | string | No | Path to save the extracted markdown response. |
| `summary_title_file`| string | No | Path to save the extracted summary title. |
| `pr_body_file` | string | No | Path to save the extracted PR body. |
| `model` | string | No | Copilot model override (e.g. `gpt-4o`). |
| `reasoning_effort` | enum | No | `low`, `medium`, `high`, `xhigh`. |
| `available_tools` | array | No | Whitelist of tools Copilot is permitted to use. |
| `timeout_ms` | integer| No | Execution timeout in milliseconds. |
| `gate_decision` | object | Runtime | Runtime-injected `transist.gate_decision.v1` manual-gate decision. |
| `feedback_context_mode`| enum | No | `full`, `focused`, `feedback-only` (default: `full`). |

## Feature Coverage

`github.copilot.cli` covers the non-interactive `gh copilot` surface that agent
orchestration typically needs:

- one-shot prompt execution via `gh copilot -p ...` with streamed text output
- explicit `--allow-all-tools` and `--allow-all-paths` so Copilot can use the
  workflow's permitted tools and the runtime-provided private directories;
  the plugin deliberately does not use `--allow-all`, which also broadens
  Copilot URL permissions
- per-step model selection (`gh copilot -- --model <value>`)
- reasoning-effort control (`gh copilot -- --reasoning-effort <value>`)
- execution inside the runtime-owned canonical workspace
- context-file injection into the composed prompt
- repo ignore policy injection via `ignore_paths`, with Elixir build artifact defaults
- workflow-skills injection (`skills_bundle`, `skills_rendered`)
- manual-gate feedback replay with `full` / `focused` / `feedback-only` modes
- file outputs (`response_file`, `summary_title_file`,
  `pr_body_file`) with canonical markdown extraction from the Copilot transcript
- `available_tools`, `denied_tools`, and raw `extra_args` pass-through so autonomous
  runs can be constrained
- live progress frames (phase previews + stdout/stderr preview line) driving
  `transist workflows run`'s activity spinner
- clean timeout handling (`timeout_ms` / `timeout_seconds`) with canonical
  `timeout_error` reporting

Intentionally not covered: interactive Copilot sessions, slash-command UI,
Copilot Chat APIs outside the CLI, or billing/admin operations. GitHub
authentication is delegated to the operator's existing `gh auth login` state.

The action accepts either `prompt` or `prompt_file` plus optional `context_files`
at supported paths, including private staged managed-document inputs.
`ignore_paths` skips matching repo-relative context files and adds an explicit
ignore policy to the composed prompt. It can also read the latest manual-gate
feedback reason and execute `gh copilot` inside the canonical workspace. Use
typed `{{steps.<id>.out.<field>}}` values for JSON handoffs and bounded root managed
documents through supported process file fields for intermediate content.
Workspace output files remain an explicit supported choice for intended
deliverables, not an implicit runtime-managed handoff. The plugin forces Copilot
CLI prompt mode
into a non-interactive scripting shape and supports optional `timeout_ms` or
`timeout_seconds` inputs to avoid indefinite hangs. It also supports optional
`available_tools`, `denied_tools`, and raw `extra_args` passthrough so a workflow
can constrain what Copilot is permitted to do during autonomous runs. While the
underlying `gh copilot` command is running, the plugin also emits lightweight
progress frames so `transist workflows run` can drive an activity-based spinner. The
wrapper emits its own short phase previews first, such as prompt preparation,
repository selection, launch, and once-per-second heartbeat messages, then switches to
the latest stdout or stderr preview line once Copilot starts printing real
output. Transist now keeps elapsed time on the `Status:` line and renders the
preview line as either `github.copilot.cli: "Working..."` or
`github.copilot.cli: "<latest output line...>"`, so the wrapper status frames
are used to keep the run live without duplicating the timer in the preview
line. That preview is also clamped to the current terminal width so the
two-line live status block can be redrawn in place without leaving duplicate
status rows behind.
Legacy mode keeps Copilot in streamed text mode; compact/invocation mode uses
streamed JSONL. Both retain live progress and wrapper heartbeats.

Failure reporting now uses canonical Transist APP errors. Missing local `gh`
binaries surface as `configuration_error`, and timeouts surface as
`timeout_error` while preserving partial stdout/stderr in the failed APP
result and artifact hints when available.

## Path Schemes And Lifecycle

The file-oriented inputs such as `context_files`, `response_file`,
`summary_title_file`, and `pr_body_file` support:

- absolute paths
- `run://...` for invocation-local scratch under `InvokePaths.run_root`, private
  staging in Transist v0.1.0 that is removed after the invocation
- `repo://...` for files under the runtime-owned canonical workspace's
  `.transist/...`

Use root managed documents for workflow-owned drafts, summaries and PR bodies
needed across steps or manual-gate revisions. Supply prior content explicitly
through document-bound `context_files`, with `history_mode: invocation`; staged
paths and neighboring history sidecars are not durable document references.
See [managed document authoring](../../docs/authoring.md#path-lifecycle-and-cross-step-storage).
Workspace and `repo://...` targets remain supported for deliberately
repository-owned data and replay history, with repository-defined retention.
Action `run://...` files do not survive another invocation, including retry/resume.

Filesystem capability resources `run://artifacts` and `run://evidence` describe
runtime-controlled artifact/evidence authority, not plugin action URIs. Accepted
APP hints may be retained by the runtime according to policy, but that does not
retain arbitrary action scratch or make it available as a mutable draft.

When `response_file`, `summary_title_file`, or `pr_body_file` are set, the
wrapper now extracts the canonical markdown artifact from the Copilot
transcript instead of forwarding raw narration. For implementation and review
loops, that means:

- `response_file` keeps the cleaned title + markdown sections
- `summary_title_file` is derived from that cleaned title
- `pr_body_file` keeps the markdown body from `## What Changed` onward

The raw Copilot stdout is also emitted as `stdout.txt` and `response.txt` artifact
hints for runtime retention according to policy. Interaction-history sidecars
support later revisions only when their target files survive invocation cleanup.

When a manual gate sends `request_changes`, the wrapper can replay these into
the next Copilot turn, provided the draft and history files remain available:

- the gate reason
- the current draft from `response_file`
- prior Copilot interaction history for that artifact

You can tune that behavior with `feedback_context_mode`:

- `full`: include the gate reason, current draft, and prior interaction history
- `focused`: include the gate reason and current draft, but omit prior
  interaction history
- `feedback-only`: include only the gate reason before the normal context files

`full` remains the default for backwards compatibility. Workflows that expect
small, targeted revision turns should prefer `focused`.

That history is persisted next to the response artifact as:

- `<basename>.interaction.json` for the structured history
- `<basename>.interaction.md` for a readable transcript (the target's extension
  is replaced when present)

The structured sidecar accumulates entries at the selected target; the readable
sidecar and replay filter entries by root run ID (with a legacy fallback for
entries lacking that ID). This logical scoping does not determine file lifetime.
Workspace/`repo://...` sidecars can remain available for later revision turns;
action `run://...` sidecars are scratch and are removed after the invocation.
Without a configured file target, sidecars fall back to the invocation artifacts
directory: runtime-retained hints are inspection records, not automatic mutable
history for the next turn. Repository-local replay requires deliberately durable
draft/sidecar targets. Managed-document revision work instead passes the prior
content explicitly; staging does not publish adjacent sidecars.

## Why This Shape

This package keeps the workflow prompt-aware and skills-aware while using
GitHub Copilot CLI as the execution transport:

```bash
gh copilot -p "<composed prompt>"
```

This plugin intentionally does not expose a generic shell trampoline. The
workflow stays readable because each stage chooses:

- a prompt file
- a set of context files
- optional gate feedback
- optional output file targets

## Agent Controls

These inputs are plugin-owned `with` fields. They belong in the step `with`
block and are not part of the `transist.flow.v1` schema syntax.

| Input | Type | Description |
|---|---|---|
| `model` | string | Copilot model to use for this step. Free-form; model catalog is provider-owned. Maps to `gh copilot -- --model <value>`. |
| `reasoning_effort` | enum | Reasoning depth. One of `low`, `medium`, `high`, `xhigh`. Maps to `gh copilot -- --reasoning-effort <value>`. |

Different workflow steps can intentionally use different models or reasoning
levels.

`temperature` is not yet supported.

## Prerequisites

- `gh` available in `PATH`
- GitHub Copilot CLI available through `gh copilot`
- a ready GitHub/Copilot auth state
- installed `transist` binary from:
  <https://github.com/transist-ai/transist/releases/tag/v0.1.0>

## Environment Variables

| Variable | Classification | Purpose | Value | Policy handling |
|---|---|---|---|---|
| `COPILOT_HOME` | Runtime-managed | Isolates Copilot runtime state. | `runtime://temp/copilot` | Declared in `plugin.yaml`; no host value or operator policy grant. |
| `COPILOT_CONFIG_DIR` | Runtime-managed | Isolates Copilot configuration and session state. | `runtime://temp/copilot` | Declared in `plugin.yaml`; no host value or operator policy grant. |
| `COPILOT_SESSION_STATE_DIR` | Runtime-managed | Keeps Copilot session-state records isolated for usage extraction. | `runtime://temp/copilot/session-state` | Declared in `plugin.yaml`; no host value or operator policy grant. |
| `XDG_CACHE_HOME` | Runtime-managed | Keeps Copilot's bundled-package cache out of the host cache directory. | `runtime://temp/copilot/cache` | Declared in `plugin.yaml`; no host value or operator policy grant. |
| `TMPDIR` | Runtime-managed | Uses a runtime-owned temporary directory. | `runtime://temp/copilot/tmp` | Declared in `plugin.yaml`; no host value or operator policy grant. |

## Install

```bash
transist plugins install --path ./plugins/github.copilot.cli
transist plugins inspect github.copilot.cli
```

The current package version is `0.1.0`.
Install it in a clean standalone home when testing locally; do
not overwrite an immutable installed version. Governed installation requires
a fresh detached signature from an authorized publisher.

## Testing

Offline smoke test:

- does not require network or an authenticated Copilot session
- validates APP framing, prompt composition, feedback ingestion, and file output paths

```bash
python3 ./plugins/github.copilot.cli/tests/smoke.py
```

Real local Copilot startup regression test (requires an authenticated `gh
copilot` installation and network access):

```bash
python3 ./plugins/github.copilot.cli/tests/live.py
```

The live test starts `gh copilot` with a new private custom `TMPDIR` and
asserts that the directory is accepted. It is intentionally separate from the
offline suite because it invokes the real provider.

Unit + integration tests:

```bash
python3 -m unittest discover ./plugins/github.copilot.cli/tests -p "test_*.py" -v
```

## Migration Notes

- `available_tools` is now the canonical whitelist input mapped to
  `gh copilot -- --available-tools`.
- `allowed_tools` remains accepted in `0.1.0` for backward compatibility but is
  deprecated and emits a status preview warning; prefer `available_tools`.

Live integration validation is optional because the local Copilot CLI auth
surface is environment-specific.

## Demo Workflow

```bash
transist workflows check --path ./plugins/github.copilot.cli/workflow.yaml
transist workflows install --path ./plugins/github.copilot.cli/workflow.yaml
transist workflows check --workflow github_copilot_cli_demo@0.1.0 --inputs-file <inputs-file> --output readiness.json
transist workflows run github-copilot-cli-demo --workflow github_copilot_cli_demo@0.1.0 --inputs-file <inputs-file> --readiness-file readiness.json
```
