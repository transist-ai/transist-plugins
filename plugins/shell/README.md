# `shell`

Package version: `0.1.0`.

`shell` is the baseline `process` plugin for simple local command execution.
It is the smallest package in this repository and is useful for quickstart
installs, compatibility checks, and simple workflow examples.

## Runtime Capabilities And Endpoints

- Adapter: `process`.
- Capability grants: adapter invoke, `plugin:shell@0.1.0` load, and selected `action:shell@0.1.0/{exec|exec_with_secret_env|echo|read_file}` invoke. The manifest declares `default_provider_kind: local` for local-only execution.

The manifest requires `python3` on `PATH`. Commands selected by the workflow
must also be installed and allowed by runtime policy.

- Secrets: `exec_with_secret_env` declares required `MY_API_TOKEN` delivered as `API_TOKEN`; other actions declare none.
- Filesystem: grant the declared workspace and any explicit external paths used by the command. `source://` resolves a contained captured workflow asset and does not require a broad source-tree grant.
- External endpoints: none are fixed by the plugin. `exec` can launch another program, so any network access must be granted for that command's declared workflow policy.

## Command lifecycle

`exec` and `exec_with_secret_env` use packaged `process_runner.py` and
`process_lifecycle.py`; `echo` and `read_file` stay in-process. Safe status heartbeats
keep quiet commands active without previewing arguments or environment values.
Commands retain no wrapper timeout (`timeout=None`). Terminal results follow
inherited-process-group TERM/KILL cleanup and bounded pipe-reader finalization
(five seconds); the wrapper creates no process groups. Unconfirmed cleanup
returns nonretryable `protocol_error` with valid captured stdout/stderr hints;
unexpected runner failures return safe `adapter_internal_error`.

## Action Reference

### `read_file` — bounded workspace text

Inputs: required `path`, optional integer `max_bytes` (default 65,536; range
1–1,048,576), `output_file`, and `output_mode: legacy|compact`. Returns exact
UTF-8 `text` and raw `bytes`; no trimming, CRLF translation, duplicate hints or
output-path metadata. Both modes retain the complete text.

The source is a regular file beneath the runtime-selected `workspace_dir`,
addressed by a relative path or a contained absolute path. URI sources, parent
traversal, symlinks (including ancestors), FIFOs/devices, invalid UTF-8 and overflow
fail with `validation_error`. Descriptor-relative no-follow opens anchor the
source even if an ancestor is replaced. `repo_path` is not an input to this action.

`output_file` is file data: a runtime-staged absolute document path, absolute path
allowed by policy, workspace-relative path, `repo://` beneath workspace `.transist/`,
or invocation-private `run://`. Output also rejects traversal and symlinks, and is
written byte-for-byte only after source validation. It cannot change source scope
or working directory. New destinations' parents are created; empty files are valid.
Use `$document` process bindings for cross-invocation persistence:

```yaml
documents:
  diagnostic: {kind: file, max_bytes: 65536}
steps:
  - id: read_diagnostic
    uses: process:shell/read_file
    with:
      path: diagnostic.txt
      output_file: {$document: diagnostic, access: write}
```

Runtime document bindings persist revisions and logical references. A staged path
and `run://` scratch expire after dispatch; never save their strings for reuse.
The existing `exec` family still resolves arguments and executes in the selected
workspace. Bind its required `repo_path` to that same canonical workspace.

### Explicit compact output (all actions)

`output_mode` accepts `legacy` (default) or `compact`. Legacy output and hints
are unchanged. Compact **keeps stdout exactly**, including whitespace, booleans,
multibyte text and arbitrary business JSON. Promoted JSON guard fields remain
available; fixed `ok`, `stdout`, `stderr`, `exit_code` and compact metadata take
precedence over promoted keys. It keeps stderr once in `output.stderr` and
suppresses duplicate stdout/stderr artifact hints. The closed `diagnostics`
metadata references those exact output fields and gives their UTF-8 byte counts.
It does not replace stdout with an artifact pointer or a preview.

Command action output schemas intentionally stay open for arbitrary promoted
business fields, including legacy metadata-name collisions; compact diagnostic
metadata has its own closed schema. `read_file` has a closed `text`/`bytes` output
and its explicit source bound. There is no command stdout truncation/budget knob.
Evidence collection, secret bindings, gate-decision handling and the existing
16-file/256-KiB evidence caps are preserved. Cleanup failures still retain their
bounded captured-output hints when no semantic output exists.

Compact metrics count JSON-serialized `input_bytes`, `output_bytes`, and final
complete APP `result_bytes` (including escapes, hints and errors, no newline).
Serialization failures produce explicit failures. An example preserving the
business boolean text is:

```yaml
plugin: shell@0.1.0
action: echo
with:
  args: [true]
  output_mode: compact
```

`out.stdout` remains exactly `"true"`. Offline compact regression command:
`python3 -B -m unittest discover -s plugins/shell/tests -p 'test_compact.py'`.

### `exec`

Runs a named local program with explicit positional arguments.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `command` | string | **Yes** | The executable name or path to run (e.g. `python3`, `git`, `/usr/bin/ls`). |
| `args` | array | No | Positional arguments. Items can be strings, numbers, or booleans. |
| `repo_path` | string | For `repo://` | Root for repository-local `.transist/` paths. |

When a workflow declares `gate_decision` on an `exec` step, the runtime injects
the validated record and the plugin forwards its canonical JSON to the child as
`TRANSIST_GATE_DECISION_JSON`. The variable is plugin-managed, is absent on the
initial iteration, contains no secret material, and does not require an
operator environment grant. Commands should validate the schema and decision
before using its optional reason or actor fields.

For document gates, `exec` also accepts optional `gate_decision.review_request`:
a closed run/gate/step identity, positive gate execution and nonempty typed
document revision list with producer, kind, revision/blob digests, sizes and
counts. Initialization document revisions can have execution zero. The selected
local `schemas/gate-decision.schema.json` bundles the canonical runtime shapes;
`exec` retains its existing required `decided_at_ms` although runtime records
permit omission. Legacy decisions without review metadata still work. The child
receives the entire JSON record, including review metadata, without dropping
fields. It is data; exact revision approval and authority remain kernel checks.

If no decision is injected, the plugin removes any stale inherited
`TRANSIST_GATE_DECISION_JSON` before running the child. Commands that emit a
JSON object on stdout also expose its keys as workflow output fields; fixed
result fields take precedence. Regular UTF-8 files written to
`$TRANSIST_EVIDENCE_DIR` become evidence hints, up to 16 files of 256 KiB each.

### `exec_with_secret_env`

Runs a command with `MY_API_TOKEN` supplied as `API_TOKEN` by the runtime.
Its `command` and `repo_path` behavior matches `exec`; its `args` are strings.
It does not accept a gate-decision binding.

### `echo`

A deterministic in-process echo for validation and debugging.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `args` | array | No | Items to be joined and returned in the action output. |

## Why This Shape

Unlike most packages in this repository, `shell` intentionally exposes a small
generic command runner. It exists as the baseline `process` package and keeps
the contract minimal:

- `echo` is deterministic and self-contained for simple validation flows
- `exec` runs a named local program plus explicit `args`
- command execution happens inside the Transist workspace directory
- `source://...` commands and args resolve inside the staged workflow source root

The plugin does not build a shell command line or parse pipelines. Workflows
must pass the executable name in `command` and individual arguments in `args`.

## Path Schemes And Lifecycle

Supported path forms inside `args`:

- `run://...` for invocation-local scratch under `InvokePaths.run_root`, which
  is private staging in Transist v0.1.0 and is removed after the invocation.
  It is not durable storage across steps, retries, or resume.
- `repo://...` for files under `<repo_path>/.transist/...`; `repo_path` is required
  and should identify the declared repository workspace.
- `source://...` for files relative to the compiled workflow package directory

`args` items may be JSON strings, numbers, or booleans. Numeric and boolean
items are stringified by the plugin (numbers via their JSON form; booleans
as `true`/`false`) before being handed to the OS, so workflows can forward
typed inputs directly without manually coercing them to strings.

The runtime captures `source://` helpers at run birth for snapshot-backed
runs. The plugin rejects traversal, missing files, and symlink escapes while
resolving a staged helper. `repo://` requires `repo_path` and maps to
`<repo_path>/.transist/<path>`; `run://` resolves from the invocation staging root.

Maintained delivery workflows use bounded root managed documents for operational
cross-step handoffs outside Git. Bind supported process path fields with
`{$document: name, access: read|write|update}` rather than sharing invocation
scratch or ticket-keyed `.transist/` files. Source/OpenSpec deliverables remain in
the workspace. Other workflows may explicitly use mutable workspace files;
their retention is not automatic capture, revision review or rollback. Grant
only the exact authority needed by the declared workspace and action paths.

Filesystem capability resources `run://artifacts` and `run://evidence` describe
runtime-controlled artifact/evidence authority, not these plugin action URIs.
The runtime may retain accepted APP artifact/evidence hints according to policy;
that does not retain arbitrary files written through action `run://...` paths.

## Runtime environment

The runtime injects `TRANSIST_WORKSPACE_DIR` for the declared canonical workspace,
and `TRANSIST_ARTIFACTS_DIR`, `TRANSIST_EVIDENCE_DIR`, and optionally
`TRANSIST_RUNTIME_METADATA_FILE` for invocation-private staging. It forces
`TMPDIR` to the private temporary root after plugin and secret environment
processing; on macOS, use an explicit
`mktemp` template under `$TMPDIR`. `TRANSIST_GATE_DECISION_JSON` is
plugin-managed and passed to `exec` children only when a decision was injected.
`API_TOKEN` is a required secret only for `exec_with_secret_env` and must be
injected by runtime policy, never stored in workflow files. Workflow policy
must grant the exact filesystem and secret authority a command needs; none of
these environment variables grants additional authority by itself.

## Prerequisites

- installed `transist` binary from:
  <https://github.com/transist-ai/transist/releases/tag/v0.1.0>
- a Python 3 runtime for the package entrypoint
- any local command you intend to run through `shell.exec` available in `PATH`

## Install

```bash
transist plugins install --path ./plugins/shell/plugin.yaml
transist plugins inspect shell
```

## Testing

Offline smoke test:

- does not require external CLIs
- validates APP framing, `echo`, `exec`, and validation failures with mocked subprocess execution

```bash
python3 ./plugins/shell/tests/smoke.py
python3 ./plugins/shell/tests/smoke.py --compact
```

Live integration test:

- requires `python3` in `PATH`
- runs the real plugin process and executes a tiny child Python command through `shell.exec`

```bash
python3 ./plugins/shell/tests/live.py
```

## Demo Workflow

```bash
transist workflows check --path ./plugins/shell/workflow.yaml
transist workflows install --path ./plugins/shell/workflow.yaml
transist workflows run shell-demo --workflow shell_demo@0.1.0
```

## Related

- [Repository overview](../../README.md)

## License

This shell package is distributed under [Apache-2.0](../../LICENSE), like every
plugin package in this repository.
