# Authoring companion plugins

Start with the canonical
[Plugin Authoring Handbook](https://github.com/transist-ai/transist/blob/v0.1.0/docs/public/handbook/06-architecture-and-extension/03-plugin-authoring.md),
[plugin guides](https://github.com/transist-ai/transist/tree/v0.1.0/docs/public/guides/plugins),
and [manifest reference](https://github.com/transist-ai/transist/blob/v0.1.0/docs/public/reference/plugin-manifest.md).
Use the runtime sources of truth and tested revision in
[compatibility](compatibility.md). This page adds companion repository conventions.

Initial publication is unsigned source-only at `0.1.0`. Subsequent plugins are
versioned independently under the [version policy](#plugin-version-policy).
The final public-runtime
macOS/Linux gate is [recorded separately](validation.md); historical development results do
not establish acceptance. Required tests use fixtures without live credentials.

## Layout and finite package contract

Public source packages belong under `plugins/<id>/`, with `plugin.yaml`, action
schemas, selected entrypoint/assets, a standalone README, and package-local tests.
A demo `workflow.yaml` is an explicit workflow example, not an implicit agent loop.

The required companion source layout is:

```text
plugins/<plugin-id>/
  plugin.yaml
  README.md
  workflow.yaml
  schemas/                  # action input/output JSON schemas
  tests/                    # package-local tests or smoke runners
  <implementation/assets>   # adapter- and language-specific files
```

Check it without installing or executing a plugin:

```sh
env -u PYTHONPATH python3 -B scripts/check_plugin_structure.py
```

The checker discovers tracked and nonignored new source packages, including
incomplete directories; it does not require a fixed package count. It checks
YAML/JSON syntax, manifest identity/adapter/actions, finite payload paths,
entrypoint selection, and Draft 2020-12 action schemas with contained local
references. Neither schema retrieval nor plugin code execution is performed.
Implementation filenames vary: Python process packages use `plugin.py`, Git MCP
uses `server.py`, text HTTP uses `service.py`, and Ollama uses Go sources.
Ollama's selected `bin/ollama.cli` is a documented build output, not a committed
source file; the static gate requires `go.mod` and `cmd/plugin/main.go` instead.
Generated binaries, ignored local packages, and caches are not public sources.

- Use `transist.plugin.v1`, a deliberate id/version, and non-empty `package.files`.
  The manifest is implicit; list every runtime dependency explicitly. No globs,
  parent traversal, symlink escapes, or remote schema references.
- Inputs and outputs use JSON Schema Draft 2020-12. Validate successful output
  as well as input; compile both schemas during installation.
- Declare `provider_kind` or `default_provider_kind` deliberately; absent locality
  defaults to external. Declare unconditional binary/env/path prerequisites.
- Changing selected bytes or the file list changes the digest and requires a
  matching new signature for governed use. After initial publication, the
  [version policy](#plugin-version-policy) also requires a new plugin version;
  validate changed bytes in a fresh home.
  Unselected docs, tests, and workflows are exported public assets, not payload.
- Keep tests self-contained; do not require a private checkout or provider login
  for offline checks. Keep new offline checks discoverable by the local root
  runner and new approved publication assets compatible with the snapshot gate.
  Neither behavioral runner nor publication exporter runs in [static CI](ci.md).

## Choose the actual adapter

| Adapter | Reference | Boundary |
| --- | --- | --- |
| `process` | [shell](../plugins/shell/README.md) | Adapter-launched executable exchanges APP frames on stdio |
| `mcp` | [git.mcp](../plugins/git.mcp/README.md) | Local MCP server exposes explicitly bound tools; declare `bindings.tool_name` |
| `http` | [text.http](../plugins/text.http/README.md) | Operator starts/manages service separately; adapter reaches configured APP URL |

An integration's name or remote API does not determine its adapter: the outbound
`http` package is `process`. HTTP network policy covers the runtime-to-service
boundary, not arbitrary service-side downstream traffic. Describe endpoints and
authority at the boundary actually enforced.

## Environment, secrets, paths, and policy

Every README must list variables read, inherited, forwarded, or created for child
processes: required/optional/plugin-managed, purpose, safe default/example, and
runtime allow/inject/redact handling. Include `PATH`, temporary-directory and
provider-state controls as well as credentials. Do not commit real secrets or
use broad operator-home grants to recover host authentication.

Use declared invoke-step `with.secret_refs` and adapter-specific policy for raw
delivery. Keep secret values out of prompts, fixtures, logs, hints, usage, and
persisted output. Dummy-secret confinement tests complement transport tests.
Model-backed participants should report canonical usage when the backend supplies
it; do not assume missing metrics are zero usage.

Process/MCP provider temporary directories use complete `entrypoint.env` values
of `runtime://temp` or `runtime://temp/<relative-path>`. Runtime injects `TMPDIR`
last. Temporary provider state is not durable workspace storage.

Document every accepted path scheme, resolver, companion input, lifecycle, and
containment boundary. `repo://` and `source://` are package conventions, not
generic runtime capability schemes. Workflow-owned helper assets are separate
from `package.files`; do not depend on absolute or parent-traversing helper paths.

### Path lifecycle and cross-step storage

For the public v0.1.0 authoring contract, declare root `documents` and bind
process action data using `{$document: name, access: read|write|update}`. The
runtime stages absolute private paths, atomically publishes bounded revisions
after success, and persists logical references. File consumers must accept those
paths without interpreting them as repository/workspace selectors. A path is
data, never authority. Modules inherit root document declarations.

The maintained CLI model, Jira, Git/GitHub, OpenSpec, shell `read_file` and
OpenAI-compatible contracts support this boundary. Read each package's exact
fields, limits, supported schemes and file semantics. Document bindings are
process-only and cannot replace workflow `prompt_file` or authority-bearing
fields. Relative members inside attachment trees resolve from their own manifest
parent and remain valid when a tree is relocated. Runtime-produced staged strings
must never be saved inside a business file for a subsequent invocation.

In Transist v0.1.0, `InvokePaths.run_root` is private invocation staging, not the
durable root run folder in the effective home. Companion action `run://...`
paths resolve into this staging and are removed after the invocation; do not
use them for handoffs across steps, retries, manual-gate revisions, or resume.
Provider `runtime://temp` directories are also invocation-private scratch.

Use managed documents for revised workflow-owned cross-step files and directory
trees. Bind each later read/update by document name rather than saving a staged
absolute path or copying it from an action result. Failed invocations do not
publish a new managed revision. Directory members must use relocatable relative
references, not absolute paths from a previous staging directory.

Workspace-relative paths and `repo://...` remain supported package conventions
for deliberately repository-owned files. The shell resolver requires `repo_path`
for `repo://` and uses `<repo_path>/.transist/...`; bind it to the declared
repository workspace. Their lifetime follows repository retention and cleanup,
not managed-document publication. These schemes are not universal runtime
authority; accepted fields and resolvers remain plugin-specific.

Filesystem capability resources `run://artifacts` and `run://evidence` name
runtime-controlled artifact/evidence authority. They are distinct from plugin
action path URIs even when the spelling overlaps. The runtime may retain accepted
APP artifact/evidence hints according to storage and redaction policy; that does
not preserve arbitrary invocation scratch or make retained records mutable
handoff files. Document durable handoff destinations separately from hints.

### Review gates, prompts, skills, and modules

For document approval, a manual gate declares `review_documents`; a consuming
document binding uses `reviewed_by` to require the approved revision. The gate
reviews exact committed revisions, not whichever bytes happen to occupy a
workspace filename later. Runtime-injected `gate_decision.review_request` is
typed review metadata, not document content or execution authority. Pass content
explicitly through the action's document-bound fields such as `context_files`.

Use first-class step `prompt`/`prompt_file` and `skills` with actions declaring
the matching `transist_prompt_v1` and `transist_skills_v1` bindings. Prompt and
skill files are workflow-owned captured assets, separate from the plugin's
`package.files`; being unselected by a plugin does not make a referenced skill
irrelevant to workflow package identity. Package-local modules inherit the root
document namespace, and their detached assets resolve from the declaring module.
Keep module/helper files regular and contained; do not use parent traversal or
symlinks. The current
[workflow schema](https://github.com/transist-ai/transist/blob/v0.1.0/docs/public/reference/workflow-schema.md)
defines these bindings and review semantics. The [Step 12 evidence](history/revised-workflow-authoring-step-12.md)
retains its original development-runtime provenance.

## Evidence before acceptance

Use the requirements-installed venv and explicitly unset `PYTHONPATH` for Python
validation commands (`env -u PYTHONPATH python3 ...`). `PYTHONNOUSERSITE` alone
does not prevent inherited import-path overrides. Record imported package
versions and module locations before claiming an isolated pinned environment.

1. Run offline unit/schema/protocol tests and APP smoke without live accounts.
2. Install, list, and inspect the exact selected payload with the verified runtime.
3. Check the authored workflow, install it in a disposable home, bind actual
   inputs, and check installed readiness. Authorize only reported missing authority.
4. Run through the installed adapter, poll terminal state, and assert real outputs,
   artifact/evidence behavior, policy denial, and secret non-persistence.
5. Test signing/trust separately for governed claims, and live interoperability
   separately when required. A standalone or mocked pass does not prove either.

Use [Contributing](../CONTRIBUTING.md) for exact root commands and scoped lint.
Record runtime revision, platform, tested package bytes, observed results, and
pending checks. Read [publication](publication.md) before exporting source packages.

## Plugin version policy

The initial publication baseline is `0.1.0` for all plugins and demos. After
publication, version each plugin independently using SemVer 2.0.0. Leave unchanged
plugins at their existing versions; do not move a published tag or replace a
published payload under the same id/version.

| Change after publication | Version change |
| --- | --- |
| Compatible bug fix or internal selected-payload change | Patch, e.g. `0.1.0` to `0.1.1` |
| Backward-compatible action or optional input | Minor, e.g. `0.1.1` to `0.2.0` |
| Breaking inputs, outputs, actions, defaults or required authority | Next minor before `1.0.0`; next major thereafter, with migration notes |
| New plugin | Start at `0.1.0` |
| Docs/tests outside the selected payload | No plugin bump |

Any change to `plugin.yaml`, selected bytes or `package.files` requires a new
plugin version after publication. Keep APP/MCP discovery and other reported
versions in sync with the manifest; update affected package-local tests and guides.
The static checker validates exact semantic versions (including prerelease/build
suffixes), not version ranges, and requires every demo dependency pin to match its
own local plugin manifest. Reported versions are checked separately by behavioral
tests; static CI does not execute plugins or prove bump correctness against history.

Workflow releases evolve independently too. Bump a demo's workflow version when
its packaged behavior or dependency pins change; do not change unrelated demos.
Schema identifiers such as `transist.plugin.v1`, `transist.flow.v1`, and `app.v1`
are contracts, not release versions, and remain unchanged. Repository snapshot
tags and runtime compatibility versions are separate from plugin/workflow
versions; see [publication](publication.md#subsequent-repository-releases).
Dated validation records retain observed historical versions rather than claiming
that old checks verified new bytes.

A version string is not a content identity: selected bytes and
`package.files` determine `plugin_dir_sha256`, and a signature approves that
specific digest. Recompute and sign changed selected bytes before governed use.
Validate them in a new disposable home; preserve older homes and their locked
payloads. A version bump does not replace digest/signature verification.
Do not overwrite an existing same-version install, edit its files,
reuse an old signature, or delete runs to bypass asset locks. A fresh run in a
fresh home is the adoption path. Rollback selects the preserved old home and
its original workflow/package bytes, subject to normal runtime readiness checks.
This companion policy does not change runtime-bundled package releases.

## Lean output and history contracts

These are plugin-owned opt-ins. Direct callers omitting the controls keep legacy
defaults. Updated model/OpenSpec/Ollama demo templates explicitly select compact
behavior; input defaults do not silently migrate historical runs. Review workflow
conditions, parent/subflow forwarding, summary extraction and downstream field
accesses before adoption. Forward complete business/control fields, not diagnostic
transcripts; do not turn a pointer, unavailable log, or preview into a business
answer. No runtime schema, persistence layout, retention, cleanup, provider
authority or shared process-helper contract is changed by these controls.

| Scope | Controls and limits | Business output versus diagnostics |
| --- | --- | --- |
| `codex.cli`, `cursor.cli`, `github.copilot.cli`, `opencode.cli` | Independent `output_mode: legacy/compact`, `history_mode: legacy/invocation`; optional positive `max_context_file_bytes` and `max_prompt_bytes` | Compact retains complete authoritative answer and control fields; omits inline stdout/stderr. Codex uses last-message output, Cursor/OpenCode structured answer events, Copilot assistant-message events; tool narration is diagnostic. Invocation history writes one metadata hint and minimal root-scoped workspace replay records, not cumulative transcript snapshots. |
| `openspec` generate/verify | `output_mode`; optional positive prompt/per-context byte budgets | Compact preserves complete plan/report, verdict, findings and paths; removes inline backend/validation logs. Counts/labels describe full logs or explicitly truncated previews. |
| `ollama.cli` generate | `output_mode`, optional positive `max_prompt_bytes`; `max_output_bytes` aggregate capture, at most 4 MiB | Compact preserves complete response and exit status, omits duplicated stdout/stderr/response hints. Capture overflow fails; a captured prefix is not a response. |
| `git.cli` | `output_mode`; optional positive `max_capture_bytes`; `max_diagnostic_bytes` at most 256 KiB | Compact preserves normalized branch/worktree/status and control fields; one capped command diagnostic hint replaces inline logs. Capture accounting occurs after each command, not as a streaming memory bound. |
| `github.cli` | `output_mode`; action-specific response, total-response, page/comment, plan/reply limits; `max_diagnostic_bytes` at most 256 KiB | Compact preserves complete normalized PR/review/business records and pagination controls. A diagnostic hint is omitted, with `retained: false`, when over its cap; business records are not silently truncated. |
| `jira.rest` | `output_mode`; action-specific `max_response_bytes`, attachment `max_bytes`, diagnostic cap at most 256 KiB | Compact preserves normalized issue/attachment/control data while removing duplicate `raw`. Diagnostic retention is explicit; download limits retain their existing per-action semantics, including zero where allowed. |
| `shell` all actions | `output_mode`; no new stdout budget/truncation control | Stdout and stderr remain exact semantic output. Compact removes duplicate hints, not business stdout; promoted guard fields remain usable. Output schemas stay open for arbitrary business keys; diagnostic metadata is closed. |
| `http`, `anthropic`, `openai.compatible`, `git.mcp`, `text.http`, `tika.http` | Existing action-specific response/token/input/document caps; no speculative compact switches | Existing bounded/low-output contracts remain governed by their own schemas and handlers. They still run the normal offline checks. |
| Canonical private packages | Follow their own schemas, selected payloads and explicit acceptance records | Private integrations are not public publication assets or evidence for public-package acceptance. |

Use the specific action schema and package guide for allowed fields; controls
are not universal runtime knobs. Modes are closed enums; unsupported/null modes,
noninteger byte budgets and invalid UTF-8 fail explicitly in bounded contracts.
Prompt/context limits
count raw UTF-8 bytes, including templates, headings, separators, feedback, draft
and replay. Bounded file reads use a ceiling plus one byte and overflow fails
before provider launch. Explicit repository-context authority is separate from
composed prompt size. Demo defaults are 65,536 bytes per context file and 98,304
bytes for the total composed prompt, where supported.

Lean output handling in the four CLI handlers and OpenSpec uses 32-hint,
256-KiB-per-hint and 1-MiB aggregate hint limits; their lean envelopes cap serialized
APP results at 1 MiB including JSON escaping. Ollama has a 256-KiB per-hint and
1-MiB result cap plus its separate aggregate/per-line capture guards. Business
answers are never shortened to fit: explicit failure replaces a misleading
success. Full written logs and diagnostic previews are distinct, and staging
files are not guaranteed durable merely because they exist. Retention metadata
must honestly say full, preview, or unavailable. Shell's existing 16-file/256-KiB
evidence caps, secret handling and lifecycle failures still apply. GitHub/Jira
retrieval defaults (5 MiB per response; GitHub 20 MiB total where supported) are
not streaming memory guarantees. Consult each handler's documented cap semantics.

## Storage inventory and benchmark

Inventory requires an explicit durable run directory and optionally a selected
workspace directory. It does not use `TRANSIST_HOME`, search default homes, parse
run payloads, prune, repair, migrate, or write run files:

```sh
python3 -B scripts/inspect_run_storage.py --run-root "$RUN_ROOT"
python3 -B scripts/inspect_run_storage.py --run-root "$RUN_ROOT" \
  --workspace "$SELECTED_WORKSPACE" --list-largest 20
python3 -B scripts/inspect_run_storage.py --run-root "$RUN_ROOT" \
  --hash-duplicates --max-hash-file-bytes 67108864 \
  --max-hash-total-bytes 268435456
```

Default output is aggregate counts, logical bytes (`st_size`) and allocated
bytes (`st_blocks * 512`, null if unsupported). Every unique regular inode counts
once across overlapping roots/hardlinks; independent files with the same basename
still count separately. Directories are excluded from byte totals. Categories
cover immutable executions, latest state projections, artifacts, evidence,
recipe/assets, gates, events/failures, context, provenance and workspace files,
including recursively contained runs. JSON files under artifact/evidence families
have distinct descriptor-candidate categories: without reading payloads this
cannot distinguish an actual descriptor from a JSON business artifact.

Root symlinks, symlink ancestors and parent traversal are rejected; contained
symlinks/nonregular entries are skipped without following them. Unix descriptor-
relative traversal and hashing prevent symlink redirection. Default traversal
budgets are 100,000 entries and 128 directory levels; `truncated` and skipped
counts identify incomplete observations. Live scans are not atomic snapshots.
Allocated counts do not deduplicate filesystem clones/shared extents or account
for filesystem compression. Categories are layout heuristics, not schema validation.

Payload reads require `--hash-duplicates`. Only equal-size candidates within
per-file/total budgets are streamed in chunks of at most 64 KiB; changed files
are rejected. Reserved hash budget includes the one-byte growth probe even on
failure; successfully hashed bytes are reported separately. Identifiers are
per-report salted hashes; largest-file listings and duplicate groups return no
raw names, paths, contents or content digests. Treat even sizes/counts as private
operational metadata; normal OS read access may update filesystem access times.

```sh
env -u PYTHONPATH python3 -B scripts/benchmark_run_persistence.py --iterations 8 --depth 3 \
  > /tmp/lean-benchmark-synthetic.log 2>&1
# Optional installed fake-provider smoke: explicit executable and fixture base.
env -u PYTHONPATH python3 -B scripts/benchmark_run_persistence.py \
  --transist "$TRANSIST_BIN" --fixture-base "$FIXTURE_BASE" \
  > /tmp/lean-benchmark-installed.log 2>&1
```

The benchmark imports canonical Codex contract fixtures and invokes real legacy
and compact handlers with mocked provider outputs and output-schema validation.
It models immutable execution records and **latest-only** state projections over
root plus contained depth, explicitly assuming complete child-output forwarding.
Reported hint payloads/descriptors, cumulative legacy history artifacts, workspace
history files, and minimal invocation replay all contribute once to totals.
Component subtotals such as cumulative legacy artifacts are labelled subsets,
not extra bytes. It asserts at least 75% savings for the transcript-heavy fixture
and near-2x compact history growth at double iterations. These are **synthetic
serialized storage**, not runtime disk use, write amplification or universal savings;
runtime evidence, recipe/assets, provenance, gates and allocation are excluded.
The legacy model accumulates all reported cumulative hints without applying
installed-adapter retention caps; oversized legacy hints need not be accepted by
a real runtime. Synthetic savings therefore do not establish installed acceptance
of the repeated legacy workload.

Optional installed smoke copies selected bytes into a disposable package/home,
substitutes only fake-provider transport, denies network and exercises two single
invocations through the real CLI/adapter. Its inventories are actual run metadata,
reported separately; it does not prove nested/iteration disk savings, live provider
interoperability or governed signatures. Missing prerequisites or a failed run fail
the command rather than reporting acceptance. Capture exact results and binary
identity using the [current validation requirements](validation.md). Earlier
measurements remain [historical](history/compatibility.md#lean-storage-acceptance).
