# `openspec`

Package version: `0.1.0`.

## Gate decision metadata

`generate_plan` accepts optional runtime-injected `gate_decision.review_request`
for document gates. The closed request contains run/gate/step IDs, positive gate
execution and nonempty typed document revisions (producer, revision/blob digests,
kind, sizes and counts). Initialization revisions may have execution zero.
The selected local `schemas/gate-decision.schema.json` bundles the canonical
runtime definitions. The action retains required `decided_at_ms` even though
the runtime schema permits omission. Legacy decisions without `review_request`
remain valid. Existing request-changes feedback preserves reason and actor/source
attribution; metadata itself is never appended to the backend prompt or read as
content. Supply content through `context_files`. Exact revision approval and
authority remain kernel checks.

Nested integer metadata accepts finite integral numbers such as `1.0`, with exact
bounds comparisons; booleans, fractions and nonfinite values fail. Identifiers
and digests must match the whole string, including no trailing newline (a stricter
semantic check than canonical schema `$` anchors). An invalid provided decision
with `review_request` returns `validation_error` before provider work, never
silently dropping feedback. Existing timestamp-reader behavior is unchanged.

## Managed document paths

`context_files`, generated `response_file`, and verification `report_file`
accept runtime-staged absolute file paths. File data does not become another
workspace: OpenSpec source changes and normal repository context remain in the
canonical runtime workspace. Optional approved/canonical plan directories are
explicit bundle data, with their existing validation/staging behavior.

The resolver also supports allowed absolute paths, workspace-relative paths,
`repo://` under workspace `.transist/`, and invocation-private `run://`. Traversal
and symlinks are rejected; bounded context reads require regular UTF-8 files and
preserve exact text/CRLF within prompt framing. Continue supplying positive
`max_context_file_bytes` and `max_prompt_bytes` to bound the whole composed request
before provider launch. Existing compact output and subprocess lifecycle apply.
`prompt_file` is the runtime prompt/captured-asset binding, not a document binding;
no `system_prompt_file` input exists. Runtime document revisions persist logical
handoffs, while staged path strings and `run://` expire after the invocation.

`openspec` is a prompt-aware `process` plugin that wraps the public OpenSpec CLI
for two workflow tasks:

- `generate_plan`
- `verify`

It uses a selected AI backend only as a text generator, while the plugin owns
all file writes for OpenSpec change artifacts and validation reports.

## Runtime Capabilities And Endpoints

- Adapter: `process`.
- Capability grants: adapter invoke, `plugin:openspec@0.1.0` load, and selected `action:openspec@0.1.0/{generate_plan|verify}` invoke.
- Secrets: none declared by the plugin.
- Filesystem: declare managed document bindings for workflow-owned plans,
  metadata, bundles and reports; grant only the exact repository paths used for
  OpenSpec source changes. Artifact/evidence capabilities do not preserve arbitrary
  action scratch for later steps. Copilot configuration and
  temporary data use declared private runtime directories. With `backend: cursor`
  and CLI login, Cursor writes per-workspace state to `~/.cursor/projects` outside
  the repository. Request `write_allow_paths: [~/.cursor/projects]` in the
  workflow's `filesystem_policy` and authorize the resulting exact external
  filesystem capability in governed homes. Do not grant the entire home or use
  runtime-temporary storage for durable login state.
- External endpoints: none are fixed by the plugin. `generate_plan` delegates model access to the selected backend (`gh-copilot`, Codex, or Cursor), so allow that backend's configured endpoint.

## Command lifecycle

All backends and `openspec validate` use the packaged `process_runner.py` and
`process_lifecycle.py` helpers, retaining the existing command timeout and
environment/input behavior. Safe main-thread status heartbeats cover execution
and finalization without previewing command arguments or environment values.
Terminal results follow inherited-process-group TERM/KILL cleanup and bounded
pipe-reader finalization (five seconds); no process groups are created.
Unconfirmed cleanup returns nonretryable `protocol_error` with valid captured
stdout/stderr hints; unexpected runner failures return safe `adapter_internal_error`.

## Environment And Policy Setup

Process-plugin policy must allow required inherited environment variables. If
an optional variable is configured on the host and the plugin should consume
it, allow that variable as well.

| Variable | Requirement | Purpose |
|---|---|---|
| `PATH` | Required | Locates `openspec` and the selected `gh`, `codex`, or `cursor-agent` backend. |
| `HOME` | Required in normal installations | Lets backend CLIs find their authenticated user configuration and default session storage. |
| `COPILOT_HOME`, `COPILOT_CONFIG_DIR` | Runtime-managed | Point GitHub Copilot at `runtime://temp/openspec/copilot`. Host values do not need policy allowance. |
| `COPILOT_SESSION_STATE_DIR` | Runtime-managed | Points usage extraction at `runtime://temp/openspec/copilot/session-state`. Host values do not need policy allowance. |
| `XDG_CACHE_HOME` | Runtime-managed | Uses `runtime://temp/openspec/copilot/cache` instead of the host cache directory. |
| `TMPDIR` | Runtime-managed | Uses `runtime://temp/openspec/tmp` instead of a host temporary directory. |
| `CODEX_HOME` | Optional | Overrides Codex configuration and session lookup. Allow it only when configured. |
| `SSL_CERT_FILE`, `SSL_CERT_DIR` | Optional | Makes a host-configured custom CA bundle available to backend CLIs. Allow them only when required by the environment. |

The plugin manages `CI` and `NO_COLOR`. Transist resolves the package's
declared `TMPDIR`, `COPILOT_HOME`, `COPILOT_CONFIG_DIR`,
`COPILOT_SESSION_STATE_DIR`, and `XDG_CACHE_HOME` values before the plugin
starts. In particular, `gh copilot` receives isolated configuration, cache,
temporary, and session-state directories without operators exposing host
values for them.

Backend authentication remains owned by the selected CLI. The plugin declares
no secret inputs and does not request raw credentials from Transist.

## Feature Coverage

`openspec` currently covers the OpenSpec change-management surface that a
plan-and-verify workflow typically needs:

- generating a complete OpenSpec change bundle (`proposal.md`, `tasks.md`,
  `design.md`, structured spec deltas rendered to `specs/<capability>/spec.md`,
  and a human-facing `plan.md` summary)
- running `openspec validate <slug>` against the generated change, either
  in-repo or inside a transient validation workspace
- exporting a canonical plan bundle to `canonical_plan_dir` when requested
- verifying an implementation against an OpenSpec change or approved prose
  bundle (`verify`) with an optional strict-fail mode (`fail_on_mismatch`)
- repo-context controls for planning (`repo_context_mode=none|paths|full`,
  `repo_context_paths`) so planning runs can be isolated from ambient repo content
- repo ignore policy via `ignore_paths`, with Elixir build artifact defaults
- manual-gate feedback replay with `full` / `focused` / `feedback-only` modes
- per-step `model` and `reasoning_effort` passed through to the selected backend
- pluggable AI backends used as text generators:
  - `gh-copilot` via `gh copilot` (maps `model` and `reasoning_effort` to the
    `--model` and `--reasoning-effort` flags; optional repo context via `--add-dir`)
  - `codex` via `codex exec` (maps reasoning effort to
    `-c model_reasoning_effort=<value>`; runs in an isolated working directory by default)
  - `cursor` via `cursor-agent --print` (maps `model` to `--model <value>`;
    `reasoning_effort` is ignored because Cursor encodes reasoning in the model
    slug, e.g. `claude-opus-4-7-thinking-high`; optional repo context via
    `--add-dir`)
- stable artifact contract: always writes `proposal.md`, `tasks.md`, `design.md`,
  and at least one spec file per run; returns `proposal_file`, `tasks_file`,
  `design_file`, and `spec_files` in action output

Intentionally not covered: OpenSpec "archive" / "list" / "diff" CLI flows,
multi-repo changesets, backends other than `gh-copilot` / `codex` / `cursor`,
and `temperature` tuning.

## Action Reference

### `generate_plan`

Generates a complete OpenSpec change bundle.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `change_name` | string | **Yes** | Slug or name for the change (e.g. `feat-auth`). |
| `prompt` | string | No* | Planning instructions. Exactly one of `prompt` or `prompt_file` required. |
| `ignore_paths` | array[string] | No | Repo-relative glob patterns the wrapper and backend should ignore. Defaults include `**/_build/**` and `**/deps/*/_build/**`. |
| `repo_context_mode`| enum | No | `none`, `full`, `paths` (default: `none`). |
| `canonical_plan_dir`| string | No | Export a stable plan bundle to this directory. |
| `backend` | enum | No | `gh-copilot`, `codex`, or `cursor` (default: `gh-copilot`). |
| `model` | string | No | Backend model override. |
| `reasoning_effort` | enum | No | `low`, `medium`, `high`, `xhigh`. |

### `verify`

Verifies an implementation against an OpenSpec change or prose bundle.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `change_name` | string | **Yes** | Name of the change to verify against. |
| `prompt` | string | **Yes** | Verification instructions. |
| `ignore_paths` | array[string] | No | Repo-relative glob patterns the wrapper and backend should ignore. Defaults include `**/_build/**` and `**/deps/*/_build/**`. |
| `approved_plan_dir` | string | No | Path to an external approved plan bundle. |
| `fail_on_mismatch` | boolean | No | Hard-fail the action if verification fails (default: `false`). |

## Path Schemes And Lifecycle

File-oriented fields such as `context_files`, `response_file`,
`change_metadata_file`, `canonical_plan_dir`, `approved_plan_dir`, and `report_file`
support workspace-relative paths, absolute paths, `repo://...` beneath
`<workspace>/.transist/...`, and `run://...` beneath `InvokePaths.run_root`.
Absolute paths require exact approved filesystem access. In Transist v0.1.0,
`run_root` is private invocation staging; action `run://...` files are removed
after the invocation and do not survive into later steps, retries, or resume.

For revised workflow-owned handoffs, bind summaries, metadata and reports to
root managed file documents and canonical plan bundles to managed directory
documents. Rebind `canonical_plan_dir` → `approved_plan_dir` by document name,
with producer write and consumer read access; never forward a previous staged
absolute path. Use `context_files` to supply prior document content explicitly.
See [managed document authoring](../../docs/authoring.md#path-lifecycle-and-cross-step-storage).
Workspace and `repo://...` targets remain supported for deliberately
repository-owned files; their lifetime follows repository retention and cleanup.

Default in-repository changes under `openspec/changes/<slug>/` remain repository
files. Transient validation workspaces and provider `runtime://temp` directories
are invocation-local scratch; canonical export is durable only when its selected
destination survives invocation cleanup.

Filesystem capability resources `run://artifacts` and `run://evidence` describe
runtime-controlled artifact/evidence authority, not plugin action URIs. Accepted
APP hints may be retained according to runtime policy, but arbitrary action
scratch files are not retained or automatically rebound as approved plan inputs.
Grant only the exact resolved paths needed by the workflow.

The workflow's first-class `prompt_file` is a contained package asset captured
and injected by the runtime, not a mutable operational-file destination.

## Current-runtime result contract

### Byte budgets and lean outputs

Both actions accept these plugin-owned `with` fields:

| Field | Contract | Default |
| --- | --- | --- |
| `output_mode` | `legacy` or `compact`; null/empty values are invalid | `legacy` |
| `max_prompt_bytes` | Optional positive integer, raw UTF-8 bytes | No caller limit |
| `max_context_file_bytes` | Optional positive integer, raw UTF-8 bytes per replay/context file | No caller limit |

The demo explicitly selects `compact`, 98,304 total prompt bytes and 65,536
bytes per context file. The total includes the template, section headers,
separators, skills, task, gate feedback, draft, explicit context, automatically
replayed existing changes/canonical plans, and approved verification bundles.
Each file read uses at most the smaller remaining/per-file budget plus one byte;
overflow fails with `validation_error` before **any** provider or validation CLI
launch. Invalid UTF-8 fails rather than being replaced. Ambient repository access
(`repo_context_mode`/`target_repo_path`) is separate from composed prompt bytes.
`prompt-composition.json` records section labels and byte counts, never contents,
and is available on composition-budget failures as well as successful composition.

Supplying either byte control also selects strict contributor handling: every
nonempty, nonignored `context_files` entry and every selected current draft must
be readable before launch. Missing/unreadable contributors fail with sanitized
field/index labels and known raw byte counts, without paths or content. Empty
optional entries explicitly select nothing. Draft replay is selected only for
`request_changes` in `full`/`focused` mode without `redo:`; a fresh generation's
output path is not a required prior draft. Automatically replayed existing or
approved plan files also fail closed. Missing transient change staging is optional
when replaying a canonical plan, but the selected existing canonical plan itself
is required. Omitting both controls retains legacy unavailable-file skipping.

Compact output keeps the complete summary/report text, verdict, findings array
and count (verify), and every existing control/file path. It sets
`output_mode: compact` and adds `diagnostics.logs` entries containing logical
artifact `name`, original UTF-8 `bytes`, and `truncated`. It **omits** `stdout`
and `stderr`; the closed conditional schemas reject empty-string substitutes.
Legacy output retains complete inline stdout/stderr and its existing fields.
Each log also declares `full_log_storage: invocation_private` and
`full_log_hint_offered`: the latter is true only when the complete log is actually
offered inline in this result, not when merely written to private staging. Offering
a hint does not establish its acceptance or durable retention by the runtime.

Each command's full logs are saved once as `backend-{stdout,stderr}.txt` or
`validate-{stdout,stderr}.txt` under the invocation artifact directory. Compact
does not repeat raw backend JSON as a response hint. Log hints may use a bounded
UTF-8 prefix named `*.truncated.txt`; metadata always names the complete log and
reports its original byte count. No business plan/report text is shortened.
The complete APP result is conservatively limited to 1 MiB including JSON
escaping, in addition to the per-hint/count/aggregate limits below. Business
output that cannot fit returns explicit `validation_error`, preserving written
plans/reports, full artifact files, and `produced-evidence.json` inventory;
`produced-output.json` retains the full output if it cannot travel inline.
Already-produced files are not rolled back. Full log/artifact copies written to
invocation-private staging may disappear after the invocation. The inventory
labels that storage and `retention: not_guaranteed`; only actual emitted hints
are offered for runtime acceptance. A bounded hint is not a promise that oversized
raw files are automatically promoted or durably retained by the adapter.

`lean_io.py` and `schemas/diagnostics.schema.json` are selected runtime payload.
They do not add provider-history lookup or alter the shared lifecycle/runner.

- Omitted workflow model/reasoning inputs materialize as `null`. Both actions
  accept null as no override and omit the corresponding backend flags; no empty
  enum defaults are required.
- Nested artifact paths use safe flat filenames with a full SHA-256 path suffix.
  `artifact-paths.json` maps emitted filenames back to their original paths.
- Inline hints are limited to 32 files (including metadata), 256 KiB per file,
  and 1 MiB total. Exceeding a limit returns a bounded `validation_error`, rather
  than a success the adapter cannot persist. Files already written to the
  workspace are not rolled back on this error.
- APP `usage` includes only provider/model and input/output/total tokens.
  `usage.json` retains backend-specific reasoning/cache counters, including on
  failed actions. Absent usage and error fields are omitted.

Offline verification uses `env -u PYTHONPATH python3` with
`plugins/openspec/tests/smoke.py` or
`-m unittest discover -s plugins/openspec/tests -p 'test_*.py'`.
Unit checks require the pinned development dependencies. For installed standalone
validation with fake CLIs, use the root `scripts/check_installed.py` inventory
with `--package openspec`, explicit matching `--transist-bin`/`--admin-bin` and
an existing allowed `--fixture-base`. A direct `plugins/openspec/tests/e2e.py`
invocation requires `TRANSIST_BIN`, `TRANSIST_ADMIN_BIN` and
`TRANSIST_FIXTURE_BASE`, plus a working native sandbox. See
[Contributing](../../CONTRIBUTING.md#static-ci-and-local-validation).
No sibling source-checkout binary is required;
it covers the demo's omitted overrides and a canonical plan export without
provider calls. Runtime-persisted hint filenames carry an indexed prefix in
addition to the names recorded in `artifact-paths.json`.

## Behavior

- normalizes `change_name` into `openspec/changes/<slug>/`
- defaults to `repo_context_mode=none`, so plan generation does not receive repo
  ambient context unless the workflow opts in explicitly
- supports repo context controls for planning:
  - `none`: do not expose repo files to the backend outside explicit
    `context_files`
  - `full`: preserve the current repo-wide planning context behavior
  - `paths`: expose only the listed `repo_context_paths` by staging those
    repo-relative files or directories into a focused planning context
- skips ignored paths when reading `context_files` and when staging `paths` mode
  repo context or copied target repositories; `full` mode remains prompt-only
  because the backend sees the original repository root
- supports an optional `target_repo_path` (absolute path to a separate target
  reference repository). When set, the chosen backend can read it as
  read-only context alongside any planning-repo context:
  - `gh-copilot`: passed as an additional `--add-dir <target_repo_path>`
  - `codex`: a copy of the target repo is staged at
    `<working_dir>/__target_repo__/` so the read-only sandbox can see it.
    Combining `target_repo_path` with `repo_context_mode=full` is rejected
    for `codex` because the planning repo is also the working directory in
    that mode and staging into it would mutate the planning tree.
  - `cursor`: passed as an additional `--add-dir <target_repo_path>` (no copy).
- uses the chosen backend to generate strict JSON for:
  - `proposal.md`
  - `tasks.md`
  - `design.md`
  - one or more structured spec deltas that the plugin renders into `specs/<capability>/spec.md`
  - a human-facing `plan.md` summary
- requires a non-empty `design_markdown` response field and always materializes `design.md`
- requires at least one delta-bearing spec so `openspec validate` can succeed
- when `canonical_plan_dir` is unset:
  - requires an already initialized `openspec/` directory in the target repo
  - writes the OpenSpec change into `openspec/changes/<slug>/`
  - runs `openspec validate <slug>` after writing the change
- when `canonical_plan_dir` is set:
  - writes the OpenSpec change into an invocation-local transient validation workspace
  - runs `openspec validate <slug>` in that transient workspace
  - exports a canonical plan bundle with `README.md`, `proposal.md`, `tasks.md`,
    `design.md`, and `specs/<capability>/spec.md`
- when the runtime injects a `gate_decision` with a `request_changes` decision,
  the wrapper can replay the operator feedback and optionally the current plan
  draft plus existing OpenSpec files

Successful `generate_plan` runs therefore have a stable artifact contract:

- always writes `proposal.md`, `tasks.md`, `design.md`, and at least one `specs/<capability>/spec.md` file into the generated OpenSpec change directory
- always returns `proposal_file`, `tasks_file`, `design_file`, and `spec_files` in the action output
- writes the human-facing summary only when `response_file` is configured
- writes the metadata JSON only when `change_metadata_file` is configured
- exports `README.md`, `proposal.md`, `tasks.md`, `design.md`, and `specs/<capability>/spec.md` into `canonical_plan_dir` when canonical export is enabled

For simple changes, keep `design.md` brief instead of omitting it.

`feedback_context_mode` controls that replay:

- `full`: include operator feedback, the current draft, and existing OpenSpec
  files for the target change
- `focused`: include operator feedback and the current draft only; do not replay
  existing OpenSpec change files automatically
- `feedback-only`: include only the operator feedback before the normal context
  files

`full` is the default for backwards compatibility. `redo:` feedback still
forces a clean restart regardless of mode.

Recommended planning usage for noisy ticket artifacts:

- persist the raw ticket or audit artifacts separately if needed
- pass only a normalized planning brief through `context_files`
- leave `repo_context_mode` unset, or set it to `none`
- use `repo_context_mode=paths` only when a workflow has a narrow,
  well-justified need for specific repo files during planning

### `verify`

Requires:

- a runtime-declared workspace containing the OpenSpec change
- `change_name`
- exactly one of `prompt` or `prompt_file`

Optional inputs:

- `context_files`
- `report_file`
- `approved_plan_dir`
- `approved_plan_mode`
- `backend`
- `model`
- `reasoning_effort`
- `timeout_ms`
- `fail_on_mismatch`
- workflow skill bindings

Behavior:

- when `approved_plan_dir` is unset:
  - requires an existing OpenSpec change directory for the normalized change name
  - runs `openspec validate <slug>` before backend verification
- when `approved_plan_dir` is set:
  - reads the approved plan bundle from that directory
  - stages it into an invocation-local transient validation workspace
  - when `approved_plan_mode` is unset or `openspec_change`, runs `openspec validate <slug>` in that transient workspace before backend verification
  - when `approved_plan_mode=prose_bundle`, skips `openspec validate` and treats the approved bundle as prose planning artifacts instead of a strict OpenSpec delta tree
- writes `validation_report.md`
- can hard-fail the workflow when the implementation does not satisfy the plan

## Backends

Supported values:

- `gh-copilot`
- `codex`
- `cursor`

The backend is used only to produce structured text. It is not the primary
implementation executor.

## Agent Controls

These inputs are plugin-owned `with` fields. They apply to both `generate_plan`
and `verify` actions and belong in each step's `with` block.

| Input | Type | Description |
|---|---|---|
| `model` | string | Model to use for this step. Free-form; model catalog is provider-owned. |
| `reasoning_effort` | enum | Reasoning depth. One of `low`, `medium`, `high`, `xhigh`. |

Backend mapping:

- `backend=gh-copilot`: `model` maps to `--model <value>`, `reasoning_effort` maps to `--reasoning-effort <value>`
- `backend=codex`: `model` maps to `--model <value>`, `reasoning_effort` maps to `-c model_reasoning_effort=<value>`
- `backend=cursor`: `model` maps to `--model <value>`. `reasoning_effort` is
  ignored — Cursor encodes reasoning in the model slug itself
  (e.g. `claude-opus-4-7-thinking-high`, `claude-opus-4-7-low`,
  `gpt-5.5-extra-high`). Use `cursor-agent --list-models` to see the available
  catalog.

For `generate_plan`, repo context is also backend-controlled:

- `gh-copilot` receives repo context through `--add-dir` only when planning
  opts into `repo_context_mode=full` or `repo_context_mode=paths`. When
  `target_repo_path` is set, it is exposed via an additional `--add-dir`.
- `codex` planning runs from an isolated working directory by default and only
  receives staged repo context when planning opts into `full` or `paths`. When
  `target_repo_path` is set, a copy of the target repo is staged into that
  working directory so the read-only sandbox can read it.
- `cursor` mirrors the `gh-copilot` pattern: repo context is passed via
  `--add-dir` for any opted-in `repo_context_mode`, and `target_repo_path` is
  exposed via an additional `--add-dir` (no copy). The cursor invocation runs
  with `--print --output-format text --workspace <dir> --force` so it is
  non-interactive and auto-approves tool use.

Steps in the same workflow can use different models or reasoning levels. For
example, the planning step can use a high-reasoning model while the verification
step uses a lighter one:

```yaml
with:
  model: "{{inputs.plan_model}}"
  reasoning_effort: "{{inputs.plan_reasoning_effort}}"
```

`temperature` is not yet supported.

## Token usage reporting

The plugin reports `usage` on the invoke result whenever the chosen backend
exposes token counts for its last session.

- `backend=gh-copilot` extracts usage from the Copilot session-state events
  (`~/.copilot/session-state/<id>/events.jsonl`) by matching the session whose
  `context.cwd` / `context.gitRoot` equals the directory where `gh copilot` was
  invoked, and falling back to parsing token totals from stdout when no
  matching session state is found. Honors `COPILOT_CONFIG_DIR` and
  `COPILOT_SESSION_STATE_DIR` env overrides.
- `backend=codex` extracts usage from the most recent matching Codex rollout
  under `$CODEX_HOME/sessions/YYYY/MM/DD/` (default `~/.codex`).
- `backend=cursor` does not currently report token usage; the invoke result
  carries `usage: null`. Switch to the `cursor.cli` plugin or use Cursor's own
  session telemetry if usage tracking is required.

When usage is available the plugin:

- returns it on `invoke_result.usage` (keys: `input_tokens`, `output_tokens`,
  `total_tokens`, optional `reasoning_tokens` / `cache_read_tokens` /
  `cache_write_tokens` / `model`)
- writes a `usage.json` artifact hint in the step's artifacts directory

Usage extraction is best-effort and never fails the primary action: if the
backend session files are missing or unreadable, the result is still produced
with `usage: null`.

## Prerequisites

- `openspec` available in `PATH`
- backend CLI installed and authenticated:
  - `gh copilot` for `gh-copilot`
  - `codex` for `codex`
  - `cursor-agent` for `cursor`
- installed `transist` binary from:
  <https://github.com/transist-ai/transist/releases/tag/v0.1.0>

Repo-local `openspec/` initialization is required only for the default
repo-local change mode. Canonical export and approved-bundle verification use a
transient validation workspace instead.

## Install

```bash
transist plugins install --path ./plugins/openspec/plugin.yaml
transist plugins inspect openspec
```

Validate changed selected bytes in a fresh disposable home with matching digest
and signature where governed trust is required. Preserve older same-version
installs and runs; do not overwrite them to adopt the current wrapper.

## Testing

Offline smoke test:

```bash
python3 ./plugins/openspec/tests/smoke.py
```

## Demo Workflow

The bundled demo retains legacy `artifacts` bindings for plan, metadata and
report files, plus a workflow-owned skill. It is not a migrated managed-document
handoff fixture. The revised authoring guidance above and
[historical Step 12 scope note](../../docs/history/revised-workflow-authoring-step-12.md#documentation-and-adoption-boundary)
describe that boundary; changing captured workflow assets needs separate review
and validation.

```bash
transist workflows check --path ./plugins/openspec/workflow.yaml
```
