# `git.cli`

## Managed file data (Transist v0.1.0)

`commit_worktree.summary_file` and `sync_branch_from_pr.pr_file` accept
runtime-staged absolute document paths. Their resolver also supports allowed
absolute paths, workspace-relative paths, `repo://` under workspace `.transist/`,
and invocation-private `run://`. These are data inputs: every Git subprocess
continues to use the runtime-selected canonical workspace as cwd. A file path or
`repo_path` value cannot select another Git repository.

Summary reads are bounded to 65,536 UTF-8 bytes (first nonempty title line);
PR JSON reads to 5 MiB. Invalid UTF-8, traversal, symlinks and nonregular inputs
fail with `validation_error`. The runtime validates any supplied `repo_path`
against the selected workspace. Use `$document` read bindings for cross-step data;
staged paths and `run://` expire after each invocation. Source/worktree operations
remain repository deliverables, with existing compact output/capture/lifecycle
contracts. Managed documents outside the repository are not staged by `add_all`.

Package version: `0.1.0`.

`git.cli` is a narrow `process` plugin for git branch, commit, and push
operations without exposing a generic shell passthrough.

## Runtime Capabilities And Endpoints

- Adapter: `process`.
- Capability grants: adapter invoke, `plugin:git.cli@0.1.0` load, and the selected action invoke grants.

The manifest requires `python3` and `git` on `PATH`.

- Secrets: none declared by the plugin.
- Filesystem: grant the target repository and exact resolved handoff paths used by the workflow; action path URIs do not grant authority.
- External endpoints: local actions need none. `push_current_branch`, `pull_current_branch`, and `sync_branch_from_pr` use the configured Git remote, so grant its resolved HTTPS or SSH endpoint rather than a fixed host.

## Command lifecycle

Commands use the packaged `process_runner.py` and `process_lifecycle.py` helpers.
The terminal result follows inherited-process-group TERM/KILL cleanup and bounded
pipe-reader finalization (five seconds); the wrapper creates no process groups.
Safe status heartbeats keep quiet commands active without previewing arguments or
environment values. Commands retain no wrapper timeout (`timeout=None`).
Unconfirmed cleanup returns nonretryable `protocol_error` with valid captured
stdout/stderr hints; unexpected runner failures return safe `adapter_internal_error`.

## Action Reference

### `scan_instruction_comments`

Read-only tracked-source scan ported from `scan_tst_comments.py` in both delivery
examples in [`transist-workflows` at `88e6871`](https://github.com/transist-ai/transist-workflows/tree/88e6871cd8155b2d021e8fc1420685b273b7e5ea/examples/hotfix_ticket_delivery/modules/scripts).
The original helper copies were byte-identical at that historical port checkpoint.
`instruction_comments.py` is selected in this package; behavioral tests live in
`tests/test_instruction_comments.py`. Both maintained delivery workflows now call
this action instead of keeping scanner copies. Historical development evidence is in
the [Step 12 record](../../docs/history/revised-workflow-authoring-step-12.md).
The current public-runtime both-platform gate is [recorded separately](../../docs/validation.md).

Required input: `output_file`; optional `output_mode` and
`max_diagnostic_bytes`. Output: Boolean `ok`, integer `count`, Boolean
`has_comments`; compact additionally reports diagnostics/byte metrics. The file
contains the helper's exact located Markdown requests, not an APP envelope.
There is no model call, usage claim or duplicated context artifact hint.

Source always comes from `paths.workspace_dir`, the canonical runtime-selected
workspace, using bounded `git ls-files -z` and its Git-confirmed deletions. No
action input selects a repository. Output is data: staged/allowed absolute paths,
workspace-relative paths, `repo://` beneath `.transist/`, and invocation-private
`run://` are supported by the same managed-file resolver. Output cannot widen
source authority. Nonsymlink output parents may be created. Use a `$document`
write/update binding for durable handoff; scratch paths expire after dispatch.

Preserved scanner rules:

- Only tracked source; ignore `.transist` components, `*.interaction.json`,
  `*.interaction.md`, the output itself, and recognized binary content in an
  8-KiB probe, regardless of extension: NUL, complete raw Netpbm P4/P5/P6 headers
  (positive dimensions, P5/P6 maxval 1–65535), PNG/JPEG/GIF/TIFF, PDF, ZIP,
  gzip/bzip2/xz/Zstandard, ELF and WOFF/WOFF2 signatures. Netpbm header comments
  and raster bytes are never reviewer requests, even when valid UTF-8.
  Git-confirmed worktree deletions are excluded; unreadable/racing missing files,
  symlink replacements, ancestor links, special files and unsafe names fail.
- Leading, comment-only, case-insensitive `tst:` after `#`, `//`, `--`, `/*` or
  `<!--`; nonempty bodies only. Quoted examples and trailing code are not requests.
  Python uses `tokenize`; the helper's conservative lexical tracking covers
  JS/TS/JSX/TSX/MJS/CJS, Elixir, SQL, Go, Java, Kotlin, C/C++/headers, Ruby and Swift.
  This is the existing lexical scan, not a new language parser.
- Bounds: 8 MiB/file, 64 KiB/line, 4 KiB/comment, 32 KiB/Markdown, 10,000 filenames
  and scanned files, 64 MiB aggregate source (including binary bytes/growth),
  2 MiB per Git filename capture, 30 seconds/Git query, 60 seconds/scan budget.
  Overflow/malformed source fails before output publication, never truncates a
  critical request or reports a false-clean result.
- Unrecognized content remains strict UTF-8 text, including Python source with
  coding cookies. No replacement decoding or decode-error-based binary fallback
  is used. Incomplete/unrecognized binary headers outside the fixed probe may
  therefore fail closed. Binary identification is not full asset validation;
  binary files still consume file-count/size/growth budgets before exclusion.
- Expected source failures return `validation_error` with a controlled actionable
  reason and, when representable, a repository-relative path (at most 240 ASCII
  characters; unsafe/long names are omitted). Messages stay below 512 bytes and
  contain no source bytes, raw exceptions or absolute host paths. Unexpected
  failures retain the generic safe error. No source failure publishes partial
  requests or overwrites prior output.

The enumeration retains the helper's bounded streaming subprocess and fixed
deadline/kill behavior; it emits no raw Git diagnostics. Other Git commands keep
the existing packaged process runner. Descriptor traversal uses search-only
directory handles where supported so native runtime staging needs no parent
directory listing grant. Environment requirements are the existing Python/Git
environment documented below; no new environment selectors or secrets are read.

```yaml
- id: inline_requests
  uses: process:git.cli/scan_instruction_comments
  with:
    output_file: {$document: inline_requests, access: write}
    output_mode: compact
```

`steps.inline_requests.out.has_comments` is a native Boolean. This action owns
only scanning; gates, reviewer ledgers, remediation and delivery orchestration
stay in the workflow. For targeted fake-gh/fixture-Git installed validation use
`scripts/check_installed.py --package review-actions` with its required explicit
binary/storage arguments, as shown in the
[Step 7 evidence](../../docs/history/revised-workflow-authoring-step-7.md).

### Output modes and byte budgets (all actions)

`output_mode` is `legacy` (default) or explicit `compact`. Legacy result shape
and hints are unchanged. Compact returns `output_mode: compact`, all existing
business/control fields (including branches, commit SHA/message, worktree paths,
no-op flags and exit codes), and a closed `diagnostics` descriptor instead of
inline `stdout`/`stderr`. One `diagnostics.json` hint records each command's two
streams and exit code once, including probes and intermediate commands.

`max_diagnostic_bytes` optionally lowers the 256 KiB hint cap. The descriptor
reports serialized `bytes`, per-stream UTF-8 byte counts, and `retained`; when
over cap it has an empty artifact reference and `retained: false`. Business
fields are never truncated. Error previews are UTF-8-safe and bounded to 2 KiB.
`max_capture_bytes` is an optional positive aggregate stream budget checked after
each command finishes; exceeding it fails explicitly as incomplete. It is not a
streaming memory cap, and a mutating command may already have completed.
Budget failures before mutation remain `retryability: safe`. Once a mutating
command is dispatched, safe failures become `retryability: operator`: inspect
effects before retrying. Capture failures retain available commit SHA/message
and branch/remote metadata; optional diagnostic omission never enables retry.

Compact `metrics.input_bytes` and `output_bytes` measure JSON serialization
(including escaping); `result_bytes` measures the final complete APP result,
including hints/error/metrics, excluding its framing newline. Raw stream counts
measure UTF-8 text, not the escaped representation. Serialization failure is an
explicit failure rather than a success with dropped fields.

Safe read-only example:

```yaml
plugin: git.cli@0.1.0
action: current_branch
with:
  output_mode: compact
```

Offline regression command: `python3 -B -m unittest discover -s plugins/git.cli/tests`.

### `ensure_branch`

Ensures a feature branch exists and is checked out. Prefers `name` if provided, otherwise falls back to `default_name`.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `name` | string | No | The preferred branch name. |
| `default_name` | string | **Yes** | Fallback branch name (e.g. Jira ticket ID). |
| `start_point` | string | No | Git ref to start the branch from (e.g. `origin/main`). |

### `commit_worktree`

Adds and commits changes in the working tree.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `message` | string | No | Commit message. |
| `jira_ticket_id` | string | No | Used to derive message if `message` is omitted. |
| `summary_file` | string | No | Path to a file containing the commit body. |
| `add_all` | boolean | No | Run `git add .` before committing (default: `false`). |

### `push_current_branch`

Pushes the active branch to a remote.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `remote` | string | No | Remote name (default: `origin`). |
| `set_upstream` | boolean | No | Run with `-u` (default: `true`). |
| `force_with_lease` | boolean | No | Use `--force-with-lease` (default: `false`). |

### `pull_current_branch`

Refreshes the active branch from a remote. By default rebases local
commits on top of the remote tip and autostashes uncommitted changes.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `remote` | string | No | Remote name (default: `origin`). |
| `branch` | string | No | Branch to pull (default: current branch via `git branch --show-current`). |
| `rebase` | boolean | No | Use `--rebase` (default: `true`). Mutually exclusive with `ff_only`. |
| `autostash` | boolean | No | Pass `--autostash` (default: `true`). |
| `ff_only` | boolean | No | Use `--ff-only` instead of rebase (default: `false`). |

### `sync_branch_from_pr`

Syncs the local environment to a PR's base branch.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `pr_file` | string | **Yes** | Path to the PR handoff JSON (from `github.cli`). |
| `remote` | string | **Yes** | Remote name to fetch from. |

## Why This Shape

This plugin uses the `process` adapter because `git` is already a local CLI.
There is no reason to wrap git behind an HTTP or MCP boundary just to perform
branch operations.

The action set is intentionally narrow:

- it shows how to model a tool as named operations instead of a shell passthrough
- it keeps workflow inputs predictable and schema-friendly
- it avoids turning the plugin into an unrestricted command runner

The implementation assumes the runtime-owned canonical workspace is a git repository
and focuses on the small git surface that a ticket-delivery workflow typically
needs. It does not try to model the full git CLI.

## Path Schemes And Lifecycle

When an action needs to read a workflow handoff file such as `summary_file`,
the path resolver supports four forms:

- workspace-relative paths
- absolute paths
- `run://...` for invocation-local scratch under `InvokePaths.run_root`, private
  staging in Transist v0.1.0 that is removed after the invocation
- `repo://...` for files under the runtime-owned canonical workspace's
  `.transist/...`

Use root managed documents for workflow-owned handoffs read by later steps,
retries or resume. Bind file consumers such as `summary_file` by document name
with read access, rather than forwarding an earlier staged path. See
[managed document authoring](../../docs/authoring.md#path-lifecycle-and-cross-step-storage).
Workspace and `repo://...` files remain supported for deliberately
repository-owned data, with repository-defined retention and cleanup.
`run://...` action paths cannot provide cross-invocation handoffs.

Filesystem capability resources `run://artifacts` and `run://evidence` are
distinct from action path URIs. Runtime retention of accepted APP hints does
not preserve arbitrary action `run://...` files or grant access to other paths.

`sync_branch_from_pr` reads `pull_request.baseRefName` from a PR handoff file,
switches to that base branch, and pulls it from the requested remote. This
keeps the workflow on a named git operation instead of a shell step.

`delete_branch` resolves the target branch the same way `ensure_branch` does,
deletes it locally with `git branch -D` when `force=true`, and can treat a
missing branch as a successful no-op when `allow_missing=true`.

## Prerequisites

- `git` available in `PATH`
- declare a git repository as the workflow workspace
- installed `transist` binary from:
  <https://github.com/transist-ai/transist/releases/tag/v0.1.0>

## Install

```bash
transist plugins install --path ./plugins/git.cli
transist plugins inspect git.cli
```

## Testing

Offline smoke test:

- does not require the `git` binary
- does not require repo mutations
- validates APP framing, schema-shaped output, PR-based branch sync, and safe
  branch cleanup paths with mocked command execution

```bash
python3 ./plugins/git.cli/tests/smoke.py
python3 ./plugins/git.cli/tests/smoke.py --compact
```

Live integration test:

- requires `git` in `PATH`
- runs the real plugin against the current git worktree

```bash
python3 ./plugins/git.cli/tests/live.py
```

## Demo Workflow

```bash
transist workflows check --path ./plugins/git.cli/workflow.yaml
transist workflows install --path ./plugins/git.cli/workflow.yaml
transist workflows check --workflow git_cli_demo@0.1.0 --output readiness.json
transist workflows run git-cli-demo --workflow git_cli_demo@0.1.0 --readiness-file readiness.json
```
