# `jira.rest`

Package version: `0.1.0`.

`jira.rest` is a `process` APP plugin that proxies live Jira REST operations.
Despite the `.rest` suffix (which refers to the upstream Jira REST API it
calls, not its adapter), the plugin no longer runs a local HTTP server: each
invocation spawns `plugin.py` as a short-lived subprocess that speaks APP
over stdio.

## Runtime Capabilities And Endpoints

- Adapter: `process`.
- Capability grants: adapter invoke, `plugin:jira.rest@0.1.0` load, and selected action invoke grants for `get_issue`, `search_issues`, `create_issue`, `transition_issue`, or `download_attachments`.

The manifest requires `python3` on `PATH`.

- Secrets: required `JIRA_AUTH_HEADER`, bound to the process environment.
- Filesystem: grant exact resolved issue and attachment paths used by the workflow; action path URIs do not grant authority.
- External endpoints: `https://<site_url>/rest/api/3`; `site_url` is supplied per invocation or through `JIRA_SITE_URL` (for example `https://example.atlassian.net:443`).

## Action Reference

### Managed issue files and attachment trees

`get_issue.output_file`, `download_attachments.jira_issue_file`, `output_dir`, and
`manifest_file` accept runtime-staged absolute file/tree paths as ordinary data.
They also accept policy-allowed absolute paths, workspace-relative paths,
`repo://` under workspace `.transist/`, and invocation-private `run://`. They never
select another workspace. Source JSON is regular, strict UTF-8 and bounded to
5 MiB. Traversal, symlinks (including ancestors) and special files are rejected.

Omit `manifest_file` to write `manifest.json` inside `output_dir`. An explicit
manifest must be **directly inside that directory**. Each `downloaded.local_path`
is a regular member path **relative to that manifest's parent**, including in the
action result. Filenames and provider IDs are sanitized; collisions get distinct
names. No manifest contains staging absolute paths. Relocate the complete tree
and resolve each member against its own manifest parent; do not resolve it against
the repository or reuse a prior invocation's output directory string.

```yaml
with:
  jira_issue_file: {$document: issue, access: read}
  output_dir: {$document: attachments, access: write}
  secret_refs: [JIRA_AUTH_HEADER]
```

The runtime owns revision persistence and aggregate document limits. Its staged
paths and `run://` expire after invocation. Workspace files retain their explicitly
selected workspace lifecycle. `max_bytes` remains a per-attachment bound (default
10 MiB; zero retains the action's existing default); skip records remain explicit.

### Output modes and bounded JSON reads

All actions accept `output_mode: legacy|compact` (default `legacy`). Legacy
output shapes remain unchanged. Compact emits `output_mode: compact`, drops
duplicative `raw`, and preserves normalized description, attachment metadata,
issue identifiers/status, download manifests and pagination tokens. A closed
`diagnostics` descriptor references a single `response.json` hint, with serialized
`bytes` and `retained`; an over-cap response has no hint and an empty reference.
`max_diagnostic_bytes` optionally lowers the 256 KiB hint cap. Normalized business
data is never truncated to fit it. Compact handoff files contain the same
normalized payload; search handoffs include `next_page_token` and `is_last`.
Legacy search handoffs continue to contain the raw Jira search response.

REST actions accept positive `max_response_bytes` (default 5 MiB). JSON bodies
are read in bounded chunks with at most one sentinel byte beyond the cap, before
UTF-8 decoding or parsing. Oversize replies fail explicitly; a write request may
already have completed remotely. After issue-create/transition dispatch, failures
otherwise marked safe use `retryability: operator`; inspect remote effects before
retrying. Read-only retrieval, including the search POST and transition reads
before mutation, retains safe retryability. Available issue identifiers and the
last observed transition checkpoint are retained; truncated JSON is not parsed
to invent a created key. A truncated issue/search/transition response
never overwrites a handoff or triggers a subsequent transition. Empty 204
responses remain valid. Search remains one page per invoke, with its continuation
token preserved; callers must request subsequent pages explicitly.

Compact metrics count serialized `input_bytes`, `output_bytes`, and complete
final APP `result_bytes`, including hints and errors, excluding the newline.
JSON escaping is included; serialization failures are explicit failures.
Attachment downloads retain the existing 10 MiB per-file cap and explicit skip
records; aggregate attachment-count/byte knobs are not provided.

Safe normalized handoff example:

```yaml
plugin: jira.rest@0.1.0
action: get_issue
with:
  issue_key: PROJ-123
  output_mode: compact
  output_file: repo://tickets/PROJ-123/jira_issue.json
```

This handoff remains consumable by `download_attachments` and GitHub's
Jira-derived PR-title input. Offline regression command:
`python3 -B -m unittest discover -s plugins/jira.rest/tests`.

### `get_issue`

Fetches a single Jira issue by key.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `site_url` | string | No | Jira site URL override. Falls back to `JIRA_SITE_URL` when omitted. |
| `issue_key` | string | **Yes** | Jira issue key (e.g. `PROJ-123`). |
| `fields` | array | No | Specific fields to fetch. |
| `include_attachments` | boolean | No | Include attachment metadata (default: `true`). |
| `output_file` | string | No | Path to persist normalized issue JSON. Supports `run://` and `repo://`. |

### `create_issue`

Creates a new Jira issue.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `site_url` | string | No | Jira site URL override. Falls back to `JIRA_SITE_URL` when omitted. |
| `project_key` | string | **Yes** | Jira project key. |
| `issue_type` | string | **Yes** | e.g. `Story`, `Bug`, `Task`. |
| `summary` | string | No* | Ticket title. One of `summary` or `summary_file` required. |
| `summary_file` | string | No | Path to file containing ticket title. |
| `description` | string | No | Ticket body (Markdown/ADF). |
| `description_file` | string | No | Path to file containing ticket body (used when `description` is empty). |
| `parent` | string | No | Parent issue key for child/sub-task creation. |
| `labels` | array/string | No | Labels as array or comma-separated string. |
| `assignee_account_id` | string | No | Jira Cloud accountId assignee. |
| `output_file` | string | No | Path to persist created issue payload. Supports `run://` and `repo://`. |

### `search_issues`

| Parameter | Type | Required | Description |
|---|---|---|---|
| `site_url` | string | No | Jira site URL override. Falls back to `JIRA_SITE_URL` when omitted. |
| `jql` | string | **Yes** | Jira Query Language string. |
| `max_results` | integer | No | Maximum issues to return (default: `50`). |

## Feature Coverage

`jira.rest` currently covers a small Jira Cloud REST slice:

- fetching a single issue by key, including normalized attachment metadata
  (`get_issue`)
- JQL-based search with pagination (`search_issues`)
- creating issues with a minimal Jira Cloud-style payload and optional ADF
  description (`create_issue`)
- transitioning an issue either by exact transition name or unambiguous target
  status name (`transition_issue`)
- bulk downloading attachment bytes into a workspace directory with a
  configurable extension/MIME/size policy and a JSON manifest
  (`download_attachments`)
- per-invocation `site_url` override with `JIRA_SITE_URL` fallback
- auth via a `JIRA_AUTH_HEADER` secret bound to an environment variable

Intentionally not covered: comments, worklogs, sprint / board /
backlog management, Jira admin APIs, or arbitrary REST passthrough.

## Why This Shape

This plugin uses the `process` adapter. Each invocation Transist makes spawns
`plugin.py` as a child process, exchanges an APP handshake and one
`app.invoke.request.v1` over stdin/stdout, and exits. Because there is no
long-lived local server:

- multiple workflows can use the plugin concurrently on the same host without
  port collisions
- per-invocation isolation is automatic — no shared state across runs
- there is nothing to start or supervise outside of Transist itself

The plugin still talks to upstream Jira via plain HTTPS (`urllib`), so the
"REST" in the name reflects the upstream protocol, not the Transist↔plugin
transport.

The plugin assumes:

- Jira base URL is provided per invocation as `site_url` or via `JIRA_SITE_URL`
- auth is supplied as the `JIRA_AUTH_HEADER` environment variable, prepared in
  the form Jira expects in the `Authorization` header (e.g. `Basic …` or
  `Bearer …`)
- issue creation uses a minimal Jira Cloud-style payload with ADF description
  support

## `download_attachments`

Reads attachment metadata from a previously persisted `jira_issue.json`,
filters by policy, fetches each allowed attachment with the same
`JIRA_AUTH_HEADER`, and writes bytes plus a JSON manifest to the workspace.

Inputs:

- `jira_issue_file`: path to the JSON written by an earlier `get_issue` step
  (supports workspace-relative, `repo://`, absolute, and invocation-local `run://`)
- `output_dir`: directory to write attachment files into (same path schemes)
- `manifest_file`: optional; defaults to `<output_dir>/manifest.json`
- `repo://...` paths resolve beneath `<workspace>/.transist/`
- `allowed_extensions`: lowercase filename extensions (with or without leading
  dot). Default: `["md","txt","log","json","yaml","yml","csv","pdf"]`. Empty
  array disables the extension filter.
- `allowed_mime_prefixes`: MIME-type prefixes. Default: `["text/",
  "application/json","application/yaml","application/pdf"]`. Empty array
  disables the mime filter.
- `max_bytes`: per-attachment cap (default 10 MiB)

Filter semantics: an attachment is accepted when either the extension filter
**or** the mime prefix filter accepts it (OR). When both lists are empty, all
attachments within `max_bytes` are downloaded. Size exceeding `max_bytes` or a
fetch error always skip the attachment.

Output:

- `downloaded[]`: `{id, filename, local_path, mime_type, size}`
- `skipped[]`: `{id, filename, mime_type, size, reason}` where `reason` is one
  of `extension_not_allowed`, `mime_not_allowed`,
  `extension_and_mime_not_allowed`, `size_exceeds_max`, `fetch_failed`
- `total_downloaded`, `total_skipped`, `manifest_path`

## Path Schemes And Lifecycle

In Transist v0.1.0, `InvokePaths.run_root` is private invocation staging.
Action `run://...` paths resolve there and are removed after the invocation;
they are not durable across steps, retries, or resume. Workspace-relative paths
resolve under the declared workspace; `repo://...` resolves under its `.transist/`
directory. Absolute paths require their own exact approved filesystem access.

For revised `get_issue` → `download_attachments` handoffs, bind
`get_issue.output_file` to a root managed file document with write access and
`jira_issue_file` to that same document with read access. Bind the attachment tree
as a managed directory document; its manifest members must remain relative to
their manifest parent so relocation is safe. Rebind later consumers by document
name instead of forwarding a staged absolute path. See
[managed document authoring](../../docs/authoring.md#path-lifecycle-and-cross-step-storage).

Workspace and `repo://...` remain supported for deliberately repository-owned
data, with repository-defined retention. Do not use `run://` for a cross-step
issue/attachment handoff: its files are removed after the invocation.
Filesystem capability resources `run://artifacts` and `run://evidence` are
distinct from these action URIs; runtime retention of accepted APP hints does
not preserve arbitrary scratch files for a later action.

## Prerequisites

- Python 3
- `JIRA_AUTH_HEADER` available for live calls (delivered by Transist as the
  process secret binding)
- installed `transist` binary from:
  <https://github.com/transist-ai/transist/releases/tag/v0.1.0>

## Install

```bash
transist plugins install --path ./plugins/jira.rest
transist plugins inspect jira.rest
```

## Testing

Offline smoke test:

- validates manifest, schemas, and the process APP frame contract
- exercises handler logic with mocked `urllib` so no network is required
- validates auth-error behavior when `JIRA_AUTH_HEADER` is missing

```bash
python3 ./plugins/jira.rest/tests/smoke.py
python3 ./plugins/jira.rest/tests/smoke.py --compact
```

Live integration validation is currently manual:

- requires `JIRA_AUTH_HEADER`
- requires a reachable Jira instance

## Demo Workflow

```bash
transist workflows check --path ./plugins/jira.rest/workflow.yaml
transist workflows install --path ./plugins/jira.rest/workflow.yaml
transist workflows check --workflow jira_rest_demo@0.1.0 --output readiness.json
transist workflows run jira-rest-demo --workflow jira_rest_demo@0.1.0 --readiness-file readiness.json
```
