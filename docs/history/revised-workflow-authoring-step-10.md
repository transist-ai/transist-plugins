# Revised workflow authoring — Step 10 plugin blocker repair

> Historical development checkpoint, not public runtime v0.1.0 acceptance.
> Original source identities, versions and results are retained below.
> Use [current validation](../validation.md) for public reproduction.

**Status: both plugin repairs and final coordinated integration pass.** Independent
reviews verified reply reconciliation, typed gate feedback and both complete
delivery migrations. The original plan ledger records the coordinated Step 10
commits after verification. Public package/demo pins remain `0.1.0`.
The initial reply repair retains its manifest/action schema and bindings; the
gate-contract sections below record the additional schema/payload changes.

## Finding and design decisions

The retained workflow diagnosis in
`projects/tmp/revised-authoring-step-10-validation/` reproduced remote acceptance
followed by a lost response. The installed publisher failed before managed state
committed; retry/resume re-entered that failed action and posted an identical
reply. Earlier workflow reconciliation cannot protect that re-entry. The original
Cursor `pr_review_replies.py` helper also uses local hash deduplication only.

The repair belongs in `github.cli/pr_publish_review_replies`, a bounded GitHub
publication operation at the existing adapter/plugin/action boundary:

1. Validate the complete plan, state and output paths before provider mutation.
   Preserve exact request IDs, existing compacted reply text, delivery selection,
   generic first-line marker and visible reply formatting.
2. Derive an exact hidden publication marker from existing repo/PR/source/request
   and delivery-target identity, never a staged path, invocation ID or clock.
   Excluding reply text makes changed content under the same identity a conflict.
   The [package guide](../../plugins/github.cli/README.md#reply-publication-reconciliation)
   specifies the encoding and compatibility behavior.
3. Before posting pending replies, completely paginate **both** REST comment
   collections. The pulls collection includes replies in resolved/outdated review
   threads, avoiding the unresolved-context GraphQL query's nested truncation.
   Explicit GETs use `per_page=100` and positive page numbers. Fixed bounds are
   100 pages per collection and 10,000 comments combined; existing response-byte
   and aggregate-byte budgets remain enforced. Duplicate IDs, malformed JSON,
   fields/markers and incomplete retrieval fail closed. No polling/provider retry
   loop or alternate cwd is introduced; existing cancellation/timeout ownership
   remains in the process lifecycle.
4. Check every pending remote identity before the first POST. Reconcile only an
   exact hidden marker line plus the entire expected body and destination. Keep
   the original `comment_id` in output/state. Report reconciled replies through
   existing `skipped`/`skipped_count` fields and `remote_duplicate_reply` reason.
   Conflicting bodies/targets or multiple matching replies fail clearly.
5. Validate successful POST acknowledgments before treating them as publication
   proof. Serialize/stage all local destinations before individually atomic file
   replacement, state last. Failure preserves prior managed revisions; externally
   accepted replies are reconciled on the next dispatch. Existing completed local
   records preserve their metadata and avoid remote comment reads.

Only `plugins/github.cli/plugin.py` changes selected production bytes. Tests and
documentation are unselected. The finite installed fixture is registered as
`reply-publication`, selected by default, by `--package github.cli`, or alone by
`--package reply-publication`. Inventory tests prevent omission. Its code promotes
the task reproducer without depending on a sibling checkout or modifying APP.

## Verified source and binary identity

| Component | Source |
| --- | --- |
| Plugins | Clean `main` initially at `be895a6ca0cf406fba5d33b96485a67e2269f9c8`, plus this uncommitted repair |
| Runtime/admin | Clean Step 8 source `48b1d9aaf7b0f56d20eae73cd9068312dcae8771` |
| Workflow diagnosis | `fdc5ec1fc7dfbd140c5c39fc479fb1777bdad6e3`, with its retained diagnostic report/allowlist changes |

| Identity | SHA-256 |
| --- | --- |
| `transist` default-feature debug 0.12.0 | `8f67f25e31a54dd8a45342ea56f5cd00e7d14faa5f894dac576680ecdf699861` |
| `transist-admin` default-feature debug 0.12.0 | `dd99dc9ca20f5bec126e3b8fb15514be009c92172ac7075368493da36f2dee5e` |
| Selected `plugin.py` | `b9bd7a207d276a1fdf034af4ebbeb1e924c91cc8bca3b46e000d0acadab13d9d` |
| Runtime-locked GitHub plugin digest | `22d0959e1c983e6d4867e06f04bd0cf0f3152662bb1c2cd7ccca4ce3ff4381e5` |
| Fixture file-hash inventory, compact sorted JSON | `b140c0a92dd8de62c1c98ded5ac730bc564a8c174f2e6ffefd48afce3f6080c0` |

Each fresh standalone fixture installs the actual unchanged manifest/entrypoint
and all 29 selected files plus the manifest, verifies the exact installed file set
and byte-compares every file to source. Fake `gh` is outside the package on private
PATH; the actual plugin APP, schema validation, native macOS `sandbox-exec`, managed
materialization/publication and public CLI run/retry/resume/cancel are exercised.
No real network/provider login or signing material is used.

Host: macOS 15.7.7 arm64. Validation uses the retained Step 6 venv, Python 3.14.6,
with `PYTHONPATH` unset. `environment.json` records interpreter/module paths and
all repository-pinned direct dependency versions: Black 25.1.0, flake8 7.2.0,
jsonschema 4.23.0, PyYAML 6.0.2, HTTPX 0.28.1, Starlette 1.7.0 and Uvicorn 0.54.0.
Postponed annotations preserve the runtime-selected system Python import path.

## Results and retained evidence

Evidence base: `projects/tmp/revised-authoring-step-10-plugin-validation/`.

| Check | Actual result | Log |
| --- | --- | --- |
| All affected GitHub package units | 44 pass, including 17 new reply regressions | `units-final.log` |
| Affected installed inventory units | 6 pass; synthetic failure/skip messages are asserted negative cases | `checker-units-final.log` |
| Offline APP smoke | Legacy and compact both pass | `smoke-legacy-final.log`, `smoke-compact-final.log` |
| Installed reply inventory | 1 runner, 5 tests pass; zero skips | `installed-final.log` |
| Black / flake8 | 8 changed Python files pass | `format-final.log`, `lint-final.log` |
| Selected publication-content validation | 10 changed paths pass | `selected-files.log` |
| Whitespace diff check | Pass | `diff-check.log` |

The final installed suite uses **5 fresh homes, 15 fresh runs and 21 publication
attempts** (15 initial dispatches plus 6 retry/resume dispatches):

| Installed API case | Runs / attempts | Result |
| --- | --- | --- |
| Conversation, accepted then lost response, retry and resume | 2 / 4 | Each remote count stays 1; both GET collections queried on re-entry, no second POST |
| Review-thread reply, accepted then lost response, retry and resume | 2 / 4 | Same 1/1 count and exact numeric parent/request identity |
| Partial mixed batch, second accepted response lost | 1 / 2 | Both accepted replies reconciled; each marker beyond 100 unrelated comments on page 2 |
| Successful transport with malformed POST JSON | 1 / 2 | Failure commits no state; resume reconciles the accepted reply |
| Managed state exceeds its declared size after remote acceptance | 2 / 2 | Failed state/output uncommitted; fresh valid caller reconciles without another POST |
| Transient read failure, malformed page, 100-page exhaustion | 3 / 3 | No POST and no managed revision advancement |
| Initial post, same-ID changed-body conflict, unrelated exact ID | 3 / 3 | Conflict fails; unrelated identity posts despite the same visible reply |
| Cancellation during quiet remote GET | 1 / 1 | No POST; fake provider and detached runner exit |

Both maintained publication variants (`with_resolution` / `without_resolution`)
are exercised with their existing inputs and empty addressed-thread lists.
An already-successful authored predecessor executes exactly once across re-entry.
All failed managed state heads retain initialization sequence/execution zero, and
failed output documents stay absent. Native atomic output failure, missing/old
state, local completed-record retention without remote fetch, malformed plan/state
fields, exact body/target conflicts, legacy identity ambiguity, duplicate pages,
combined count bounds and exact UTF-8 byte boundaries are additionally unit-tested.
Existing smoke retains explicit thread-resolution checks.

Final retained fixture directories:

- `reply-publication-l3zomlxk`: four accepted/lost retry/resume combinations.
- `reply-publication-jxehtln4`: cancellation.
- `reply-publication-spre31m_`: read failures/bounds/conflict/unrelated ID.
- `reply-publication-zz7mzbtr`: rejected managed publication and fresh reconciliation.
- `reply-publication-1zmebet1`: partial batch, later pages and malformed POST reply.

Each retains numbered CLI transcripts, `payload.json`, per-attempt remote snapshots,
installed workflows, immutable document revisions, runner logs and
`settled-runners.json`. Every owned detached runner exited. Earlier `attempt*` and
`probe*` logs are diagnostic history, not the final acceptance receipt: initial
fixture JSON parsing and an eager annotation incompatible with system Python were
corrected; the final exact-byte installation reruns the complete selected suite.

## Commands

From the plugin repository, with `$PROJECTS` naming the projects directory:

```sh
PY="$PROJECTS/tmp/revised-authoring-step-6-validation/venv/bin/python3"
E="$PROJECTS/tmp/revised-authoring-step-10-plugin-validation"
RUNTIME="$PROJECTS/transist-oss/target/debug"

env -u PYTHONPATH TMPDIR="$E" "$PY" -B -m unittest discover \
  -s plugins/github.cli/tests -v > "$E/units-final.log" 2>&1
env -u PYTHONPATH TMPDIR="$E" "$PY" -B -m unittest discover \
  -s tests -p test_installed_checker.py -v > "$E/checker-units-final.log" 2>&1
env -u PYTHONPATH TMPDIR="$E" "$PY" -B plugins/github.cli/tests/smoke.py \
  > "$E/smoke-legacy-final.log" 2>&1
env -u PYTHONPATH TMPDIR="$E" "$PY" -B plugins/github.cli/tests/smoke.py \
  --compact > "$E/smoke-compact-final.log" 2>&1
env -u PYTHONPATH PATH="$(dirname "$PY"):$PATH" "$PY" -B \
  scripts/check_installed.py --transist-bin "$RUNTIME/transist" \
  --admin-bin "$RUNTIME/transist-admin" --fixture-base "$E" \
  --package reply-publication > "$E/installed-final.log" 2>&1
```

Black `--check` and flake8 `--max-line-length=88 --extend-ignore=E203,W503` were run
on `plugin.py`, the five changed GitHub test/smoke files, `scripts/check_installed.py`
and `tests/test_installed_checker.py`. The existing `check_public_snapshot.check_file`
validator checks exactly those eight Python files and the two changed Markdown
files; source and binary receipts are retained in `plugins-source.log`,
`runtime-source.log`, `workflows-source.log` and `binary-digests.log`.

## Limits and coordinated handoff

- This is serial publication reconciliation, not provider-side atomic idempotency.
  Concurrent independent publishers or concurrent remote edits/deletions can race
  paginated reads and POST. Edited/deleted identity markers can defeat detection;
  operators must reconcile such history. A legacy generic marker alone cannot
  prove the request and is never guessed complete.
- Raw read-byte limits remain post-capture checks; pagination/count bounds and
  runtime timeout/cancellation bound execution. There is no new streaming memory
  contract or new public budget knob.
- Atomic ordinary file replacement is per destination. Managed document publication
  supplies the cross-file commit boundary. External acceptance cannot be rolled
  back by local failure.
- This acceptance uses deterministic fake `gh`, native macOS and standalone homes.
  Live GitHub, Linux and governed signing/execution are not established here.
  Changed selected bytes need a matching signature before governed adoption; use
  fresh homes and preserve existing locked installs/runs for rollback.
- Parent can resume the delivery workflow migrations using these working-tree
  plugin bytes. Full workflow scenario matrices, both complete migrations and
  full Step 10 acceptance remain pending. Step 12 aggregate gates remain pending.
  Parent owns the coordinated single Step 10 commit per affected repository after
  complete workflow acceptance.

## Additional prerequisite — gate-decision contract repair

**Scoped plugin acceptance passes; complete Step 10 acceptance is still pending.**
The preceding sections retain the review/reply repair and its earlier receipt.
Their unchanged-schema/manifest/file-list statements describe that repair only.
This additional prerequisite changes the six gate-consuming packages below;
`github.cli/plugin.py` retains SHA-256
`b9bd7a207d276a1fdf034af4ebbeb1e924c91cc8bca3b46e000d0acadab13d9d`.

### Finding and rationale

Migrated document gates inject optional `gate_decision.review_request`. Six
closed input schemas rejected it before transport, and five feedback readers
also rejected the new field, silently losing valid request-changes feedback.
The repair belongs at the existing action/package contract boundary.

- Each package independently selects identical `schemas/gate-decision.schema.json`.
  It bundles the actual canonical decision/request and transitive
  revision/blob/digest definitions. The request is closed, has positive u32 gate
  execution and a nonempty typed revision list. Initialization document revisions
  allow execution zero with the canonical initialization producer/sequence rules.
  Tree revisions reference a blob; inline `manifest` or document content is not a
  revision property in the actual source schema and remains rejected.
- The five readers select identical stdlib-only `gate_review.py` helpers to check
  nested metadata types, required/unknown fields, digests, producers and integer
  bounds. Existing request-changes reason/actor/source formatting stays intact.
  Metadata is never appended to prompts or dereferenced as content. Shell already
  forwards the entire validated record as exact canonical JSON.
- Canonical runtime `decided_at_ms` remains optional in the bundle. Each of the
  six existing action schemas separately retains its **required** timestamp.
  Legacy decisions without review metadata and invocations without a decision
  remain valid. Cursor's two actions continue sharing one input schema.
- Exact request/revision approval and authority remain kernel checks; plugins
  only accept structural metadata. Public ids, versions (`0.1.0`), actions and
  bindings retain their existing contracts.
- Offline test validators now resolve package-local references through a local
  registry, including extracted decision subschemas and the lean CLI matrix.
  Cursor's installed payload inventory includes the selected new files and its
  existing `lean_policy.py` dependency.

Exact selected payload changes (28 files):

| Packages | Modified selected files | Added selected files |
| --- | --- | --- |
| `codex.cli`, `cursor.cli`, `github.copilot.cli`, `opencode.cli` | `plugin.py`, `plugin.yaml`, `schemas/exec_task.input.schema.json` | `gate_review.py`, `schemas/gate-decision.schema.json` |
| `openspec` | `plugin.py`, `plugin.yaml`, `schemas/generate_plan.input.schema.json` | `gate_review.py`, `schemas/gate-decision.schema.json` |
| `shell` | `plugin.yaml`, `schemas/exec.input.schema.json` | `schemas/gate-decision.schema.json` |

The six standalone package guides document this contract. Canonical schemas are
vendored byte-for-byte under `tests/contracts/runtime/`; actual source hashes,
MPL-2.0 provenance and the explicit local bundle transformation are recorded in
`tests/contracts/provenance-step-10.json`, the contract README and `NOTICE`.
Bundles deliberately omit upstream `$id` rather than relabeling altered schemas.

### Source and validation receipt

Plugins remain dirty `main` at `be895a6ca0cf406fba5d33b96485a67e2269f9c8`.
Runtime source is `48b1d9aaf7b0f56d20eae73cd9068312dcae8771` plus the current
Step 10 run-ID/staging repair, not a clean-source claim. Matching runtime binary:
`7f77e255101e16ba589a9aeaa3667c99ab6b1682d4d8b2aa408cd772f3cffea7`.
Admin remains `dd99dc9ca20f5bec126e3b8fb15514be009c92172ac7075368493da36f2dee5e`.
No runtime/workflow source was edited for this prerequisite.

Evidence: `projects/tmp/revised-authoring-step-10-gate-contract-validation/`.
A fresh isolated venv uses Python 3.14.6 and every direct dependency pin from
`tests/requirements.txt`; `environment.json` records versions/import paths and
unset `PYTHONPATH`. No real provider or aggregate Step 12 gate is used.

| Check | Result | Receipt |
| --- | --- | --- |
| Six affected package unit suites | 128 tests pass: Codex 53, Cursor 18, Copilot 16, OpenCode 5, OpenSpec 24, shell 12 | `units-*.log`; final Codex/shell results in `units-*-rerun.log` |
| New gate contract regressions | 6 tests pass, including nested malformed cases across all six schemas/five independent readers | `root-gate_decision_contract.log` |
| Installed inventory regressions | 7 pass; fixture inventory equals all seven declared gate bindings | `checker-final.log` |
| Existing document/lean acceptance units | 9 + 5 pass | `root-document_contracts.log`, `root-lean_acceptance.log` |
| Six package smokes + shell compact | All 7 commands pass; shell compatibility additionally exercises its existing 11 paths | `smoke-*.log`, `units-shell-rerun.log` |
| New installed gate fixture | 7 fresh homes, 14 successful real-gate runs, 7 rejected malformed callers; zero skips | `installed-final-verified.log`, retained `gate-contract-*` fixtures |
| Preserved GitHub reply regressions | 44 package units + all 5 installed publication tests pass | `reply-preservation-units.log`, `installed-final-verified.log` |
| Step 7 golden extraction parity | Pass against pinned helper objects | `step7-golden.log` |
| Changed Python format/lint, six-package structure, selected publication contents, whitespace | Pass | `format-final.log`, `lint-final.log`, `selected-files.log`, `scope-check-commands.json`, `diff-check.log` |

The new installed runner reuses the existing document fixture's transport-only
wrapper: production handlers, selected helpers/schemas, APP, native sandbox,
staging, output validation and document publication execute. Only fixture
entrypoint/prerequisites and its extra wrapper are selected differently. Both
Cursor actions are exercised, with a dummy declared API secret for `exec_task`.
Real initial-document review requests survive request-changes and approval
unchanged. Fakes assert nonempty reason/actor/source in request-changes prompts,
no revision digest leakage, successful committed response documents and exact
shell JSON forwarding. Malformed nested blobs fail live input-schema readiness
before run creation/provider transport. Numbered CLI transcripts, histories,
installed file sets and observations are retained. Owned runners are settled
after blocked/success checkpoints before the next gate command.

Earlier attempts are retained: the fixture initially needed explicit
feedback-only mode and runner-lease settlement; malformed input correctly failed
earlier at readiness rather than becoming a failed run. The first parallel unit
pass also had a native-timeout assertion and an Anthropic subprocess failure in
the existing shell compatibility suite. Both complete affected suites passed on
serial rerun with the venv on `PATH`; no unrelated implementation was changed.
Checker-unit synthetic failure/skip messages are expected negative assertions,
not acceptance skips.

### Reproduction and coordinated handoff

From this repository, using the same `$PROJECTS` convention as above:

```sh
E="$PROJECTS/tmp/revised-authoring-step-10-gate-contract-validation"
PY="$E/venv/bin/python3"
RUNTIME="$PROJECTS/transist-oss/target/debug"

env -u PYTHONPATH "$PY" -B "$E/validate.py"
env -u PYTHONPATH PATH="$(dirname "$PY"):$PATH" TMPDIR="$E" "$PY" -B \
  -m unittest discover -s plugins/codex.cli/tests -v > "$E/units-codex.cli-rerun.log" 2>&1
env -u PYTHONPATH PATH="$(dirname "$PY"):$PATH" TMPDIR="$E" "$PY" -B \
  -m unittest discover -s plugins/shell/tests -v > "$E/units-shell-rerun.log" 2>&1
env -u PYTHONPATH TMPDIR="$E" "$PY" -B -m unittest discover \
  -s tests -p test_installed_checker.py -v > "$E/checker-final.log" 2>&1
env -u PYTHONPATH PATH="$(dirname "$PY"):$PATH" "$PY" -B scripts/check_installed.py \
  --transist-bin "$RUNTIME/transist" --admin-bin "$RUNTIME/transist-admin" \
  --fixture-base "$E" --package gate-contract --package reply-publication \
  > "$E/installed-final-verified.log" 2>&1
env -u PYTHONPATH "$PY" -B "$E/check_scope.py"
```

`commands-results.json` retains the initial explicit command/exit inventory;
the serial reruns and `checker-final.log` supersede their corresponding initial
entries. `final-results.json` consolidates the final 199 passing unit tests and
installed results; `payload-inventory.json` records the exact 28 selected-file
changes and hashes plus each package's complete selected file set.
The selected `gate-contract` runner is registered by default and under
each affected package, so the later Step 12 inventory includes it without a
required skip. These are scoped plugin results: full workflow migrations,
complete Step 10 acceptance and Step 12 aggregate gates remain pending. Parent
owns the coordinated single Step 10 commit after full acceptance. Changes remain
uncommitted for review. Changed package bytes/file lists require fresh installs
and matching signatures for governed adoption; retain older locked homes/runs
for rollback.

## Independent review P2 — nested numeric parity and explicit semantic errors

The initial gate repair above needed this follow-up before integration. Review
reproducer `projects/tmp/step10-gate-review-parity.log` showed that canonical and
action schemas accepted integral floats such as gate execution `1.0` and blob
size `7.0`, while all five readers rejected them and suppressed request-changes
feedback. Canonical `$` regex anchors also accept a final newline that the
readers' whole-string identifier/digest checks reject. The earlier receipt is
retained as history; the following P2 receipt applies to the current payload.

### Repair and deliberate contract boundaries

- All five identical `gate_review.py` copies accept finite integral floats in
  nested integer fields, reject booleans/fractions/nonfinite values, and compare
  the represented numeric value directly with integer bounds. No float-to-int
  coercion is used: `float(2**64 - 1)` rounds to `2**64` and remains out of range;
  exact Python integers and the preceding representable integral float retain
  their actual values. Initial revision execution/sequence `0.0` obey the same
  initialization-tag and kind constraints as integer zero.
- Whole-string identifier/digest checks intentionally remain stricter than the
  published regex anchors. A provided decision containing `review_request`
  which fails validation now produces APP `validation_error` with the safe
  message `invalid gate_decision with review_request`, before provider work.
  Public action entry points check this before provider/environment setup, and
  feedback readers also raise instead of returning absent feedback. Approved
  and rejected malformed review decisions fail explicitly too.
- Legacy decisions without `review_request` retain their existing feedback
  format and malformed-decision behavior. In particular, the pre-existing
  timestamp reader still requires a nonnegative Python integer and rejects
  booleans/integral floats; only the new nested integer checks were widened.
  Schemas, versions, selected file lists and bindings are unchanged by P2.
  Metadata remains closed, is not dereferenced or appended to prompts, and
  creates no approval authority. Five package guides document the semantic check.

Relative to the initial gate repair, **only ten selected files change**:
`plugin.py` and `gate_review.py` in each of `codex.cli`, `cursor.cli`,
`github.copilot.cli`, `opencode.cli` and `openspec`. The selected schema copies
still hash to `a7241a9a064113b2a7d97690e85d54590766acc2139f9073d77368ab4ce2a0dc`.
All five helpers now hash to
`98e6c1ef6266f6b345acfc854daad51811b9b78e64260b59ec8bf2843bb05eae`.
The existing GitHub reply repair remains byte-identical at
`b9bd7a207d276a1fdf034af4ebbeb1e924c91cc8bca3b46e000d0acadab13d9d`.

### P2 acceptance and retained evidence

Evidence remains in the same gate-validation directory, using separate `p2-*`
receipts. The original review repro, the new pre-fix regression's **18 failing
action subcases**, earlier Step 10 attempts, and the P2 lint-fix attempts remain
available. No failure receipt was replaced by a success claim.

| P2 check | Actual result | Receipt |
| --- | --- | --- |
| Original six gate contract tests | 6 pass, now requiring explicit invalid-review feedback errors | `p2-contracts-final.log` |
| New differential/action regressions | 5 pass: 2,376 numeric schema cases across six packages/five readers, regex-newline errors, legacy timestamp distinction, 18 successful real-handler APP calls and pre-transport rejection across all six feedback bindings | `p2-parity-final.log` |
| Affected package unit suites, including shell compatibility | 128 pass: 53 + 18 + 16 + 5 + 24 + 12 | `p2-units-*.log` |
| Preserved GitHub reply package units | 44 pass | `p2-reply-preservation-units.log` |
| Package smokes, including shell compact | All 7 commands pass | `p2-smoke-*.log` |
| Installed gate and reply preservation | Both runners pass, 6 tests, zero skips; all 7 gate bindings, 14 successful gate runs and 7 malformed readiness rejections | `p2-installed-final.log` |
| Changed-source formatting/lint, six-package structure, selected publication and whitespace checks | Pass | `p2-scope-final.log`, `p2-scope-commands.json` |

The P2 rerun totals **183 passing unit tests**, plus the seven smoke commands and
six installed tests. Earlier unrelated focused root results remain their own
receipt, rather than being recounted as new P2 execution. Validation uses the same
clean requirements-pinned venv with unset `PYTHONPATH`, now serial package tests
with the venv on `PATH`. Runtime/admin identities remain `7f77e255…` / `dd99dc9c…`.

`p2-commands-results.json` contains exact unit/smoke command arguments and exit
codes. With `E`, `PY` and `RUNTIME` defined as above, the P2 commands are:

```sh
env -u PYTHONPATH "$PY" -B "$E/p2_validate.py"
env -u PYTHONPATH PATH="$(dirname "$PY"):$PATH" "$PY" -B scripts/check_installed.py \
  --transist-bin "$RUNTIME/transist" --admin-bin "$RUNTIME/transist-admin" \
  --fixture-base "$E" --package gate-contract --package reply-publication \
  > "$E/p2-installed-final.log" 2>&1
env -u PYTHONPATH "$PY" -B "$E/p2_scope.py"
env -u PYTHONPATH "$PY" -B "$E/p2_receipt.py"
```

### Current workflow-agent payload handoff

`p2-final-results.json` supersedes the prior consolidated result for this repair.
`p2-payload-inventory.json` records every selected file's SHA-256, mode and size,
the ten P2 byte changes, unchanged manifests/file lists, and these canonical
source package fingerprints. Its digest implementation follows the current
runtime's sorted path/NUL/u32-LE mode/raw bytes/NUL contract and is cross-checked
against all seven final installed gate fixture recipe locks. Fixture wrappers
have distinct digests; every selected production implementation/schema byte is
also checked against its installed copy.

| Package (`0.1.0`) | Current source `plugin_dir_sha256` |
| --- | --- |
| `codex.cli` | `836e6b0304de7f5dc4f560a5e0c0e44e02ba3504f2ea0f6e08a104434d5ea24b` |
| `cursor.cli` | `ef218932dd7e4397948aec8de010e54622a6a964d41af9ddfd1da434275b3b6b` |
| `github.copilot.cli` | `461621a8fd129123890320e81c685a6ad4b7c1b123f8d93ae95f2287793d148d` |
| `opencode.cli` | `a9de48a0c936a3917743dfced61e053da14f00f5a8ddb9da5806abaf44d88799` |
| `openspec` | `2a4da3d515f6e1004c49e52c25b54ded0ead77b627ae37d95e8f5e9899dce57d` |
| `shell` (unchanged by P2) | `02d55de908957ae2b98a14538974ec837a34229c6e23aa374257bf3620469269` |

P2 is ready for workflow-agent integration review. Full Step 10 acceptance and
the parent-owned coordinated commit remain pending; no runtime, sibling or plan
source was edited, and no commit, push or aggregate gate was performed.
