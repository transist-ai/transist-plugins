# `github.cli`

## Managed file data (Transist v0.1.0)

PR title/Jira/body inputs, `response_plan_file`, `state_file` and `output_file`
accept runtime-staged absolute document paths. Allowed absolute paths,
workspace-relative paths, `repo://` under workspace `.transist/`, and invocation
`run://` remain supported. Paths are data and never become Git/GitHub cwd; the
runtime-selected canonical workspace stays authoritative, including validation
of a supplied `repo_path`.

Read regular UTF-8 text/JSON within `max_plan_bytes` where supported (default
5 MiB; title/Jira inputs use that fixed default). Reject traversal, symlinks,
special files, invalid UTF-8 and overflow rather than posting a partial value.
A PR body file without `body_prefix` is forwarded exactly, preserving whitespace
and CRLF; an explicit prefix retains the existing normalized prefix semantics.
Review plan and state JSON use the publication reconciliation rules below.

Use `$document` read/write/update bindings for durable cross-invocation handoffs;
`state_file` can be a write binding initially and update on the next invocation.
Persist relative business members, never a staged absolute string for later
reuse. Runtime-staged paths and `run://` scratch expire after dispatch. Compact
business output, capture bounds and subprocess cleanup contracts remain in force.

Package version: `0.1.0`.

`github.cli` is a `process` plugin focused on pull-request workflows through the
GitHub CLI.

## Runtime Capabilities And Endpoints

- Adapter: `process`.
- Capability grants: adapter invoke, `plugin:github.cli@0.1.0` load, and the selected GitHub action invoke grants.

The manifest requires `python3` and `gh` on `PATH`.

- Secrets: none declared by the plugin; authentication stays in the operator's `gh` login state.
- Filesystem: grant the target repository and exact resolved handoff paths used by the workflow; action path URIs do not grant authority.
- External endpoints: GitHub CLI commonly uses `https://api.github.com:443` and may use `https://github.com:443` or the repository's configured remote for checkout and push operations.

## Command lifecycle

Commands use the packaged `process_runner.py` and `process_lifecycle.py` helpers.
The terminal result follows inherited-process-group TERM/KILL cleanup and bounded
pipe-reader finalization (five seconds); the wrapper creates no process groups.
Safe status heartbeats keep quiet commands active without previewing arguments or
environment values. Commands retain no wrapper timeout (`timeout=None`).
Unconfirmed cleanup returns nonretryable `protocol_error` with valid captured
stdout/stderr hints; unexpected runner failures return safe `adapter_internal_error`.

## Action Reference

### Opt-in compact output and retrieval budgets

All actions accept `output_mode: legacy|compact`, defaulting to `legacy` with
unchanged result shape and hints. Compact emits `output_mode: compact`, retains
complete structured PR/review data and controls, and replaces inline raw streams
with a closed `diagnostics` descriptor and one `diagnostics.json` hint. Review
comment bodies, diff hunks, thread/database IDs, counts, approval/merge flags and
timestamps are complete. Reply hashes/state and snapshot checkpoints keep their
existing semantics. Compact `output_file` contains that same structured payload;
the file is still written, but its duplicative JSON artifact hint is suppressed.

Positive read budgets: `max_response_bytes` (5 MiB per command) and
`max_total_response_bytes` (20 MiB per invoke) are checked on captured UTF-8
stdout+stderr before JSON parsing. These are post-capture retrieval checks, not
streaming memory caps. Mutation responses are not cut off before checkpointing.
`max_comments` (10,000) bounds review retrieval/upsert search; upsert additionally
accepts `max_comment_pages` (100). Exhausted search fails **before** POST/PATCH,
even if a matching marker was seen on an earlier page. Review GraphQL connections
request page information and fail explicitly when any requested connection is
incomplete; this action does not paginate past its 100-node connections. Partial
GraphQL errors also fail without overwriting the last successful snapshot.

Reply publishing accepts `max_plan_bytes` (5 MiB, bounded file read) and
`max_replies` (1,000). Exceeding either rejects the entire plan before any reply.
No requests are silently discarded to satisfy a budget.

`max_diagnostic_bytes` optionally lowers the 256 KiB serialized hint cap.
Diagnostics report UTF-8 stream counts, serialized `bytes`, `retained`, and the
artifact name (empty if over cap). Only diagnostic data may be omitted over cap;
business data remains complete. Tool error previews are bounded to 2 KiB.
Compact metrics count serialized input/output and the final complete APP result
including hints and errors (`input_bytes`, `output_bytes`, `result_bytes`, no
framing newline). Serialization failures return explicit failures.

Safe structured-output example (bind the repository and PR as appropriate):

```yaml
plugin: github.cli@0.1.0
action: pr_view
with:
  repo: example/repo
  pull_request: "7"
  output_mode: compact
  output_file: repo://review/pr.json
```

Offline regression command: `python3 -B -m unittest discover -s plugins/github.cli/tests`.

### `pr_create`

Creates a new pull request.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `title` | string | No* | PR title. One of `title`, `title_file`, or `jira_issue_file` required. |
| `body` | string | No | PR description body. |
| `body_file` | string | No | Path to a file containing the PR body. |
| `body_prefix` | string | No | Static text to prepend to the body. |
| `labels` | string[] | No | Labels to apply after PR creation; missing labels are created automatically. |
| `base` | string | No | Target branch (default: remote default). |
| `head` | string | No | Source branch (default: current branch). |
| `draft` | boolean | No | Create as a draft (default: `false`). |

### `pr_list`

Lists pull requests in a repository.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `state` | enum | No | `open`, `closed`, `merged`, `all` (default: `open`). |
| `limit` | integer | No | Maximum number of PRs to return (default: `30`). |

### `pr_unresolved_comments`

Fetches unresolved review-thread comments.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `output_file` | string | **Yes** | Path to save the comments JSON. |
| `include_pr_conversation_comments` | boolean | No | Include top-level PR comments (default: `false`). |

## Feature Coverage

`github.cli` covers the slice of the GitHub CLI and GitHub GraphQL API that a
PR-centric delivery workflow needs:

- listing and viewing pull requests (`gh pr list`, `gh pr view --json ...`)
- checking out a PR branch locally (`gh pr checkout`)
- creating pull requests with title/body, body-prefix, and Jira-issue-derived
  titles (`gh pr create`)
- fetching unresolved review-thread comments, and optionally fresh PR
  conversation comments, via `gh api graphql`; each review-thread comment
  record carries `thread_id` (GraphQL node ID) and `review_comment_id`
  (numeric `databaseId`) for downstream in-thread replies and thread resolution
- counting approvals from unique non-author reviewers via `latestReviews`
- squash-merging PRs and optionally deleting the remote head branch via `gh api`
- publishing review replies from a structured response plan; when
  `resolve_addressed_threads` is truthy, resolves review threads listed in
  `addressed_thread_ids` via the GraphQL `resolveReviewThread` mutation; output
  includes `resolved_count` and `resolution_errors`
- upserting a single marker-based PR conversation comment for awareness/status updates
- execution inside the runtime-owned canonical workspace while handoff files stay in
  that workspace

Intentionally not covered: release management, workflow dispatch, issues,
discussions, project boards, assignees, repository administration, or
arbitrary `gh api` passthrough. Authentication is delegated to the operator's
existing `gh auth login` state.

`pr_view`, `pr_create`, and `pr_unresolved_comments` accept optional
workspace-relative file handoff paths so workflows can persist PR metadata and
comment snapshots for later steps. All actions run `gh` inside the
runtime-owned canonical workspace.

## Path Schemes And Lifecycle

Those file handoff paths also support:

- absolute paths
- `run://...` for invocation-local scratch under `InvokePaths.run_root`, private
  staging in Transist v0.1.0 that is removed after the invocation
- `repo://...` for files under the runtime-owned canonical workspace's
  `.transist/...`

Use root managed documents for workflow-owned PR metadata, review snapshots,
bodies and publication state needed across steps, retries or resume. Bind each
consumer by document name; never reuse a previous invocation's staged path.
Read/modify/write state uses `access: update`, including the previous snapshot
used to identify fresh conversation comments. See
[managed document authoring](../../docs/authoring.md#path-lifecycle-and-cross-step-storage).
Workspace and `repo://...` files remain supported for deliberately
repository-owned data, with repository-defined retention. Action `run://...`
paths are invocation scratch and cannot provide these handoffs.

Filesystem capability resources `run://artifacts` and `run://evidence` are
distinct from action path URIs. Runtime retention of accepted APP hints does
not preserve arbitrary action `run://...` files or grant access to other paths.

`pr_create` can also derive its title from a Jira issue artifact via
`jira_issue_file`, producing `[<jira-key>] <jira-summary>` by default or a
custom `jira_title_template` format string using `{key}` and `{summary}`.
When `body` or `body_file` is provided, `body_prefix` can prepend a stable line
such as `Addresses: PROJ-123` before the markdown body that gets published.

`pr_unresolved_comments` uses `gh api graphql` to fetch unresolved review
threads and flatten them into workflow-friendly comment records. When
`include_pr_conversation_comments` is `true`, it also includes pull-request
conversation comments created after the previous successful fetch recorded in
the existing `output_file` snapshot. Those conversation comments are emitted
once, while unresolved review-thread comments continue to appear until the
thread is resolved.

Review-thread comment records now include `thread_id` (GraphQL node ID such as
`RT_kwDOA...`) and `review_comment_id` (numeric `databaseId` of the first
comment in the thread). `thread_id` is required by the GraphQL
`resolveReviewThread` mutation; `review_comment_id` is required by the REST
`POST /pulls/:pr/comments` `in_reply_to` field.

`pr_publish_review_replies` reads the response plan produced by
`scripts/pr_review_replies.py plan`. When each response entry carries
`review_comment_id`, replies are posted in-thread via the REST pulls API
(`in_reply_to`); when it is absent, the action falls back to PR conversation
comments. When `resolve_addressed_threads` is truthy, threads listed in the
plan's top-level `addressed_thread_ids` are resolved via the GraphQL
`resolveReviewThread` mutation. Output includes `resolved_count` (number of
threads successfully resolved) and `resolution_errors` (list of error strings
for any failures).

### Reply publication reconciliation

Every pending `pr_publish_review_replies` dispatch reads **both** complete REST
collections before any reply POST: `issues/{pr}/comments` (PR conversation) and
`pulls/{pr}/comments` (all review comments, including replies in resolved/outdated
threads). Explicit GET pages contain at most 100 comments; fixed limits are 100
pages per collection and 10,000 comments combined. Existing per-response and total
UTF-8 read budgets also apply. A full final page requires another page to prove
completion. Exhaustion, invalid JSON/fields, partial retrieval and API errors fail
closed even if an earlier page contained a matching reply. There is no provider
retry/polling loop; existing invocation timeout/cancellation and process cleanup
remain authoritative. No new action inputs or schema fields are needed.

Plans retain the workflow helper's exact `comment_id`, `source`, numeric
`review_comment_id`/URL fallback, compacted reply text and visible formatting.
The first line remains `<!-- transist-review-reply -->`. A final hidden line
`<!-- transist-review-publication:<sha256> -->` binds the exact request to its
repository, PR, source and delivery target. The digest is SHA-256 of a compact
UTF-8 JSON array `[lowercase_owner, lowercase_repo, pr_number, source, comment_id,
surface, target]`, where `surface` is `issues` or `pulls` and `target` is the
decimal review parent ID (empty for conversation delivery). It excludes paths,
invocation IDs, clocks and reply text, so changed content cannot masquerade as a
new publication of the same identity.

An **exact marker line, complete body and matching destination** reconcile to
`status: skipped`, `reason: remote_duplicate_reply`; `skipped_count` increases,
`posted_count` stays zero. Unrelated IDs/markers never suppress a new reply.
The same identity with different text/target, or multiple matching remote replies,
fails clearly. An identical legacy generic-marker-only reply without committed
state requires manual reconciliation: it cannot prove request identity and is
neither silently accepted nor reposted. Existing completed local records still
skip without a remote fetch and retain their metadata. New records additionally
retain `publication_id` and `body_hash`; output responses preserve the original
request IDs and the existing typed legacy/compact schema.

The complete plan/state and all pending remote conflicts are checked before the
first POST. Successful POST acknowledgments must prove the returned body/target;
ambiguous or malformed acknowledgments leave local state untouched. Files are
fully serialized/staged, then individually atomically replaced (state last).
Failed managed invocations publish no document revision. Ordinary filesystem
state/output files are not a cross-file transaction; managed documents provide
that publication boundary. Remote acceptance cannot be rolled back by a local
file failure, and the next dispatch reconciles it from GitHub.

This is bounded serial-dispatch reconciliation, not GitHub-side exactly-once
concurrency control. Concurrent independent publishers can race between GET and
POST; removing/editing identity markers can defeat remote detection and requires
operator reconciliation. Remote comments
remain untrusted data: they establish only this exact publication, never workflow
authority. Changing an already-published reply under the same request identity
is a conflict, not an implicit edit operation.

The finite installed fixture uses the real selected payload, native sandbox,
managed files and CLI retry/resume; only `gh` is fake on private PATH. It is in the
default installed inventory and the `github.cli` selection. Run only this fixture:

```sh
env -u PYTHONPATH python3 -B scripts/check_installed.py \
  --transist-bin "$TRANSIST_BIN" --admin-bin "$TRANSIST_ADMIN_BIN" \
  --fixture-base "$FIXTURE_BASE" --package reply-publication
```

Historical repair scope: [Step 10 record](../../docs/history/revised-workflow-authoring-step-10.md).
Historical development acceptance: [Step 12 record](../../docs/history/revised-workflow-authoring-step-12.md).
The current public-runtime both-platform gate is [recorded separately](../../docs/validation.md).

`pr_check_approvals` refreshes PR metadata with `latestReviews`, counts unique
non-author reviewers whose latest review is `APPROVED`, and can persist that
refreshed PR handoff payload to `output_file`. It succeeds when the PR is
already merged or when `approval_count >= required_approvals`; otherwise it
returns an APP `failed` result with a resume-oriented message while still
writing the refreshed approval payload when `output_file` is set.

### Typed review decisions and context (`0.1.0`)

`pr_check_approvals` additionally returns required Boolean `approval_ready`, equal
to `ok`, in both modes and any optional output file. Existing `latestReviews`,
case-insensitive identity, submitted-time ordering, author exclusion, merged-PR
bypass, counts and default failed-threshold behavior are preserved. Optional
Boolean `soft_fail: true` returns a successful **negative decision** so a workflow
can branch; transport failures still fail. This action does not adopt the older
workflow approval helper's different `reviews` counting policy.

`pr_check_mergeability` fetches the helper's exact `gh pr view --json` fields.
Optional inputs: `pull_request`, `repo`, `allowed_merge_state_statuses`,
`output_file`, output mode and retrieval/diagnostic budgets. It returns Boolean
`merge_ready`, `ok`, normalized state/status, draft flag, raw `mergeable` and
`status_check_rollup` metadata, PR data, the normalized allowlist, full `reasons`
and `exit_code` (plus legacy streams or compact diagnostics).

- Allowlist strings are trimmed/uppercased; an empty effective list means `CLEAN`.
- State `MERGED` or truthy `mergedAt` bypasses other checks.
- Otherwise require `OPEN`, not draft, and an allowed `mergeStateStatus`.
- Missing/null/empty `mergeable` is permitted; supplied values must be literal
  `true` or normalize to `MERGEABLE`/`TRUE`. Numeric `1` is not true here.
- `reviewDecision` and status-check rollup are metadata, not extra policy.
- Completed negative decisions succeed with `merge_ready: false`; callers branch
  on this Boolean. Operational failures fail with `merge_ready: false` when a
  fetch response is available. Missing binaries/budget violations fail normally.
- Invalid/nonobject JSON and transient helper-defined CLI failures retry at most
  three attempts (0.5/1.0-second delays); auth/unknown-flag errors do not retry.
  Retrieval budgets also cover retries; incomplete captures never imply readiness.

`pr_prepare_review_context` is a **local projection** with required `input_file`
(raw normalized fetch), `pr_file`, `output_file`, and optional `pending_file`.
It makes no GitHub call; the package's existing static `gh` prerequisite still
applies at readiness. Inputs are regular JSON objects, each at most 4 MiB;
duplicate object keys, failed/incomplete checkpoints/counts, invalid identities,
sources, timestamps and malformed requests fail before replacing output.

The output file is the exact helper projection: `comments`, integer `count`, and
`pull_request` (`number`, `state`, `url`, `title` only). APP output adds Boolean
`ok` and optional compact diagnostic metadata. Each comment preserves its exact
body, author, location and IDs; only the diff header is retained, not its duplicate
body. There is no raw transport log or duplicated context artifact hint.

Pending conversation requests survive a newer fetch cutoff and absence from that
fetch. Current requests replace older copies by `(source, id)`; duplicate IDs do
not duplicate work. Pending threads are replaced by the complete current unresolved
thread set. Bot reply markers and leading slash commands are excluded; mandatory
`!!!` requests remain. The encoded UTF-8 file is capped at 64 KiB: overflow fails,
never truncates requests, and leaves an existing pending file intact. Publication
is atomic. `pending_file` may equal `output_file` for an update binding.

The complete final projection is checked against explicit output-shape constraints
before publication, including every optional location: `line`, `original_line`
and `start_line` accept JSON integers or null (Booleans and fractional numbers
are invalid); `side` accepts string/null; `diff_hunk_header` accepts string only.
There are no new positivity bounds or side enums. Integral floats retain JSON
Schema integer semantics. All other declared field types, required/closed fields
and standard JSON encoding are checked before replacing a file. Nonfinite numbers
cannot be published. These malformed-input rejections are the typed action's
stricter schema boundary, not parity with the old helper's permissive output.
Filtering/retirement/supersession still occurs before checking the final shape.

All four file fields use the managed-file resolver above: staged/allowed absolute,
workspace-relative, `repo://` and invocation-private `run://`. Existing nonsymlink
parents are required for context output. Ancestor/leaf links and special files are
rejected. These paths never select another workspace or grant authority. Use
managed documents for pre/post-gate handoffs; the gate decision remains separate.

```yaml
- id: mergeability
  uses: process:github.cli/pr_check_mergeability
  with: {output_mode: compact}
- id: approvals
  uses: process:github.cli/pr_check_approvals
  with: {required_approvals: 2, soft_fail: true, output_mode: compact}
```

Consume `steps.approvals.out.approval_ready` and
`steps.mergeability.out.merge_ready` directly as typed values. The current runtime
also supports copying either to `vars` with `set` for conditions.

Ports: `check_pr_mergeability.py` and `prepare_review_context.py` in
[`transist-workflows` at `88e6871`](https://github.com/transist-ai/transist-workflows/tree/88e6871cd8155b2d021e8fc1420685b273b7e5ea/examples/hotfix_ticket_delivery/modules/scripts).
The original mergeability helper copies were byte-identical at that port
checkpoint. Behavioral tests live in `tests/test_review_actions.py`; both
maintained delivery workflows now consume these plugin actions instead of keeping
mergeability/projection helper copies. Reviewer ledgers, gates,
reply publication, PR formatting and delivery loops remain separate actions or
workflow-local behavior. No model is invoked and no token usage is claimed.

Targeted installed validation (fake gh only; requires explicit matching runtime
and admin binaries and existing disposable storage):

```sh
env -u PYTHONPATH "$PY" -B scripts/check_installed.py \
  --transist-bin "$TRANSIST_BIN" --admin-bin "$TRANSIST_ADMIN_BIN" \
  --fixture-base "$FIXTURE_BASE" --package review-actions
```

This finite selector tests all Step 7 actions, including Git scanning. See the
[Step 7 evidence](../../docs/history/revised-workflow-authoring-step-7.md) for pinned
helper parity, unit/smoke commands and actual source/binary identities.

`pr_merge` keeps merge behavior explicit on the plugin surface. It refreshes the
current PR, no-ops successfully when the PR is already merged, otherwise runs
`gh pr merge --squash`, refreshes the merged PR payload, and can optionally
delete the remote head branch through `gh api`. Remote branches that are
already gone are treated as successful cleanup.

## Why This Shape

This plugin uses the `process` adapter because `gh` is already a local CLI
with authenticated repo context and stable subcommands for pull-request work.
Using `process` keeps the plugin small and lets Transist treat GitHub CLI as
the execution boundary instead of inventing another service layer.

The implementation mixes two GitHub interfaces on purpose:

- `gh pr list` and `gh pr view` are used where the normal PR commands already
  return structured JSON
- `gh api graphql` is used for unresolved review comments and optional fresh PR
  conversation comments because the PR JSON commands do not directly expose the
  filtered view the workflows need

This plugin assumes the operator already manages GitHub authentication through
`gh auth login`. It does not resolve tokens itself and it does not try to cover
the full GitHub API surface.

## Prerequisites

- `gh` available in `PATH`
- authenticated GitHub CLI session for live operations
- repository context for commands that omit `repo`
- declare the target repository as the Transist workspace
- installed `transist` binary from:
  <https://github.com/transist-ai/transist/releases/tag/v0.1.0>

## Install

```bash
transist plugins install --path ./plugins/github.cli
transist plugins inspect github.cli
```

## Testing

Offline smoke test:

- does not require the `gh` binary
- does not require GitHub auth or network access
- validates APP framing, schema-shaped output, missing-binary handling,
  unresolved-comment snapshot behavior, approval counting, and merge cleanup
  behavior

```bash
python3 ./plugins/github.cli/tests/smoke.py
python3 ./plugins/github.cli/tests/smoke.py --compact
```

Live integration test:

- requires `gh` in `PATH`
- requires `gh auth login` or equivalent existing auth state
- requires repo context that `gh repo view` can resolve

```bash
python3 ./plugins/github.cli/tests/live.py
```

## Demo Workflow

```bash
transist workflows check --path ./plugins/github.cli/workflow.yaml
transist workflows install --path ./plugins/github.cli/workflow.yaml
transist workflows check --workflow github_cli_demo@0.1.0 --output readiness.json
transist workflows run github-cli-demo --workflow github_cli_demo@0.1.0 --readiness-file readiness.json
```
