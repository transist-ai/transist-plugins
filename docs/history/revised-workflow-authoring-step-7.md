# Revised workflow authoring — Step 7

> Historical development checkpoint, not public runtime v0.1.0 acceptance.
> Original source identities, versions and results are retained below.
> Use [current validation](../validation.md) for public reproduction.

## Implementation

Complete local implementation on `transist-plugins/main`, based on clean
`86d375c6459a916fa1a15d55c14de285f477dbcb`. Final independent review verified
the pre-write projection correction, preservation regressions, golden boundaries
and retained evidence. The original plan ledger records the Step 7 commit after
verification. Public releases remain `0.1.0`; schema IDs remain v1.

| Action | Contract |
| --- | --- |
| `github.cli/pr_check_approvals` | Adds required Boolean `approval_ready = ok`; preserves prior fields, latest-review/time ordering, unique non-author reviewers, merged bypass and default failed-threshold behavior. Optional typed `soft_fail: true` exposes negative decisions for branching; operational failures still fail. |
| `github.cli/pr_check_mergeability` | Ports exact helper decisions and bounded transient retries. Completed checks return typed `merge_ready` and full reasons; transport/parse/budget failures fail. Default allowed status is `CLEAN`; merged PRs bypass checks. No new review/check-rollup policy. |
| `github.cli/pr_prepare_review_context` | Ports full projection/deduplication/retirement rules. Pending conversations survive post-gate cutoffs; current unresolved threads replace prior threads. Full request bodies/locations remain. Complete fetch evidence is mandatory. |
| `git.cli/scan_instruction_comments` | Ports tracked-source selection, operational exclusions, language/leading-marker rules, deletion handling and all scanner budgets. Runtime canonical workspace is the only source root. |

File inputs/outputs accept Step 6 staged absolute paths and existing supported
data-path conventions. They cannot select another workspace. Context reads remain
4 MiB/object, projected context 64 KiB, scanner output 32 KiB; exhausted limits
fail rather than silently dropping requests. Context replacement is atomic.
Descriptor-relative no-follow traversal uses search-only directory handles where
available, supporting native sandbox staging without parent listing grants.

New modules/schemas are explicitly selected by `package.files`. Manifests,
handshakes, action discovery, closed output contracts and compact metrics agree.
Local file actions emit no duplicated context/log hints. Existing Git/GitHub
command lifecycle and compact diagnostics remain; scanner enumeration retains its
original streaming byte/count/deadline bounds and single-process cleanup.
No model-backed action or usage behavior changes.

Behavioral tests accompany both packages. Workflow copies remain at their pinned
source until Step 10. Reviewer ledgers, gate decisions, PR formatting and delivery
orchestration remain workflow-owned. The new actions do not own a delivery loop.

## Sources and executables

| Repository | Actual source |
| --- | --- |
| Plugins | `86d375c6459a916fa1a15d55c14de285f477dbcb` plus this working-tree diff |
| Runtime Steps 1–5 | clean `8c5721f0aa256a15bfc4d77b43203a9f5ad157fd` |
| Workflow reference | clean `88e6871cd8155b2d021e8fc1420685b273b7e5ea` |

Used the supplied default-feature debug binaries in
`$PROJECTS/transist-oss/target/debug/`, both reporting **0.12.0**. No runtime build.

| Binary | SHA-256 |
| --- | --- |
| `transist` | `dfbbf23585cc1dfdf8ed6e8417131221a5fec02773c0cb80392fd06a5ea4b874` |
| `transist-admin` | `c442375f583ac71643e2cf8dea44aae572140d8a886f99954a1e70caabb85bcd` |

Golden checks import only fixed helper paths from the pinned local Git objects
into disposable fixtures, under non-main names. No workflow entrypoint or live
provider is executed. Both delivery copies of mergeability/scanning are verified
byte-identical. Approval parity uses the **plugin baseline**, whose rules differ
from the workflow's old `reviews` helper.

| Pinned reference | SHA-256 |
| --- | --- |
| `check_pr_mergeability.py` | `dcc1b63fe62df987e48d4017bc33b58bfbed29be6c151969e6f7a96f74c5cd52` |
| `prepare_review_context.py` | `8a5eee88503cb5cd287a4f48a2efa171a692ad1851e503ef366199f084322bbf` |
| `scan_tst_comments.py` | `e358d312ab7a05460c97903434ba0b0208cdf7d296b4e48ffed8a4016ad9aa2f` |
| Prior `github.cli/plugin.py` | `fc6fb03bc425672af2f1c4e1a2674c22dc7b52541cf2ca3c7a75c1bffdb6eb68` |

## Targeted validation

Executed on macOS 15.7.7 ARM64 with native sandboxing and Python 3.14.6.
Reused the requirements-installed Step 6 venv with `PYTHONPATH` explicitly unset
for every Python command. Import-location/version proof is retained: Black
25.1.0, flake8 7.2.0, jsonschema 4.23.0, PyYAML 6.0.2, and all HTTP dependency
pins are inside that venv. All temporary source, operational data and Git indices
belong to disposable fake workspaces beneath the evidence directory.

From the plugin repository (`$PROJECTS` denotes its parent directory):

```sh
LOG="$PROJECTS/tmp/revised-authoring-step-7-validation"
PY="$PROJECTS/tmp/revised-authoring-step-6-validation/venv/bin/python"
TRANSIST="$PROJECTS/transist-oss/target/debug/transist"
ADMIN="$PROJECTS/transist-oss/target/debug/transist-admin"

# Affected package units plus normal/compact offline smokes (six commands).
env -u PYTHONPATH PYTHONNOUSERSITE=1 TMPDIR="$LOG" "$PY" -B -c '
import os, sys
sys.path.insert(0, "scripts")
import check_plugins as c
commands = [(p, cmd) for p in ("git.cli", "github.cli")
            for cmd in c.offline_commands(c.ROOT / "plugins" / p)]
failed = []
for p, cmd in commands:
    print("RUN", p, *cmd, flush=True)
    if c.run_offline_command(cmd, dict(os.environ)):
        failed.append((p, cmd))
print("RESULT", len(commands)-len(failed), "passed", len(failed), "failed")
sys.exit(bool(failed))' > "$LOG/offline-final2.log" 2>&1

env -u PYTHONPATH PYTHONNOUSERSITE=1 TMPDIR="$LOG" "$PY" -B \
  -m unittest discover -s tests -p test_installed_checker.py \
  > "$LOG/checker-units-final.log" 2>&1
env -u PYTHONPATH PYTHONNOUSERSITE=1 TMPDIR="$LOG" "$PY" -B \
  tests/golden_review_actions.py --workflow-repo "$PROJECTS/transist-workflows" \
  --fixture-base "$LOG" > "$LOG/golden-final.log" 2>&1
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B scripts/check_installed.py \
  --transist-bin "$TRANSIST" --admin-bin "$ADMIN" --fixture-base "$LOG" \
  --package review-actions > "$LOG/installed-final3.log" 2>&1
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B scripts/check_plugin_structure.py \
  > "$LOG/structure-final.log" 2>&1
git diff --check > "$LOG/diff-check-final.log" 2>&1
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B "$LOG/record_identity.py" \
  > "$LOG/identity-final.json"
```

Changed-file Black and flake8 use the finite Git modified/new Python inventory,
not the full catalog. The final command selects:

```sh
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B -c '
import subprocess, sys
paths = sorted({p for p in subprocess.check_output([
    "git", "ls-files", "--modified", "--others", "--exclude-standard", "-z"
]).decode().split("\0") if p.endswith(".py")})
print("FILES", *paths, sep="\n", flush=True)
subprocess.run([sys.executable, "-m", "black", "--check", *paths], check=True)
subprocess.run([sys.executable, "-m", "flake8", "--max-line-length=88",
                "--extend-ignore=E203,W503", *paths], check=True)'
```

`--package review-actions` is the complete finite Step 7 installed fixture: all
three new actions and changed approvals. It is also included by either Git/GitHub
package selector. Explicit binary/admin/base prerequisites, unknown selectors,
child failures, zero-test suites and required skips fail. The general installed
inventory retains its existing entries and adds this runner.

## Initial implementation evidence

Historical Step 7 results follow. The independent-review P2 correction below
supersedes the GitHub/unit/golden/installed acceptance and source inventory.

Evidence root:
`$PROJECTS/tmp/revised-authoring-step-7-validation/`, where `$PROJECTS` is the
operator-selected parent directory for the checkouts and disposable evidence.

| Log | Actual result |
| --- | --- |
| `environment.log`, `identity-final.json` | Pinned import locations, platform, source cleanliness, executable identities and changed-source/selected-file SHA-256 inventories |
| `offline-final2.log` | **6/6 commands pass**: Git 28 units, GitHub 23 units, normal and compact smokes for each; no skips |
| `checker-units-final.log` | **6 tests pass**, including new inventory selection and failure/skip/zero-test rejection; printed child failures are deliberately mocked negatives |
| `golden-final.log` | **20 merge decisions/full outputs**, **12 prior-plugin approval cases**, **5 full review projections**, **13 failed/incomplete/malformed/overflow projections**, and **9 located scanner requests/identical Markdown** match the references |
| `installed-final3.log` | **4 tests / 10 real workflow runs / 17 action dispatches pass**. Six runs intentionally fail: incomplete/oversized/duplicate-key review input, scanner overflow, and two unknown-flag transport failures. Existing document heads remain intact. No required skip. |
| `selected-inventory.log` | Explicit `review-actions` selection, binary versions/digests and runner path |
| `format-lint-final4.log` | **15 changed Python files pass** Black and flake8 |
| `structure-final.log`, `diff-check-final.log` | Finite payload/schema/version structure and whitespace **pass** |

Installed fixtures retain all production selected bytes; the GitHub fixture adds
only a selected fake `gh` executable and launcher, replacing the host-login
prerequisite. Real subprocesses use the production runner and actual selected
workspace. Git scanning uses a real fixture Git index. Exact installed selected
files are compared byte-for-byte. Managed documents exercise absolute staged
paths, two advancing pre/post-gate fetches, no raw-checkpoint rewrite, and failed
publication preserving prior bytes. Typed true/false values cross APP output
validation and direct `steps.out` input binding; `set`/`vars` drives a real runtime
condition. No JSON-to-shell conversion occurs.

Earlier `*-attempt*.log`, `format-lint-final.log` and `installed-final.log` are
retained history. Initial failures exposed a missing subprocess import, the new
no-stream compact schema requirement, unused fixture plugin declarations and
formatting issues. The indexed passing rechecks supersede those failures.

## Independent-review P2 correction

The reviewer reproduced five malformed projected-location fields returning APP
success and replacing an existing workspace pending file before runtime output
schema validation failed. Original evidence remains at
`$PROJECTS/tmp/step7-review-schema-repro.log`; a verbatim copy is retained as
`review-p2-original-repro.log`. `review-p2-red.log` reproduces the `line: "12"`
schema failure against the original implementation using the new regression.

`review_context.validate_projection` now checks **all** projected schema fields
with dependency-free explicit constraints before returning a projection and again
before opening any output destination. The public schema is unchanged. Integer
locations exclude Booleans/fractional numbers but retain JSON Schema's integral
float semantics. Nullability, unconstrained line ranges, arbitrary string sides,
complete request bodies and existing bounds remain. Standard JSON serialization
also rejects nonfinite projected numbers before any file I/O.

This intentionally rejects surviving malformed fields that the old helper
accepted; it is the action's strict output-schema boundary, not a changed review
decision rule. Golden evidence labels **29 strict-schema boundary cases** and
separately retains the original goldens plus four valid nullable/integral/range
cases and five filtering/retirement/supersession cases. Only the final projected
shape is checked, so discarded or superseded fields do not introduce new policy.

Current commands, from `transist-plugins`, with the same variables and Step 5
binary pair defined above (all logs retained under `$LOG`):

```sh
# Set PROJECTS to your local checkout/evidence parent before these commands.
: "${PROJECTS:?Set PROJECTS to the local checkout parent}"
LOG="$PROJECTS/tmp/revised-authoring-step-7-validation"
PY="$PROJECTS/tmp/revised-authoring-step-6-validation/venv/bin/python"
TRANSIST="$PROJECTS/transist-oss/target/debug/transist"
ADMIN="$PROJECTS/transist-oss/target/debug/transist-admin"

# Red proof, executed before the production fix; expected nonzero exit.
env -u PYTHONPATH PYTHONNOUSERSITE=1 TMPDIR="$LOG" "$PY" -B -m unittest discover \
  -s plugins/github.cli/tests -p test_review_actions.py -f \
  -k test_invalid_projected_locations_never_overwrite_pending \
  > "$LOG/review-p2-red.log" 2>&1

env -u PYTHONPATH PYTHONNOUSERSITE=1 TMPDIR="$LOG" "$PY" -B -m unittest discover \
  -s plugins/github.cli/tests -p 'test_*.py' -v \
  > "$LOG/review-p2-github-units.log" 2>&1
env -u PYTHONPATH PYTHONNOUSERSITE=1 TMPDIR="$LOG" "$PY" -B \
  plugins/github.cli/tests/smoke.py > "$LOG/review-p2-smoke.log" 2>&1
env -u PYTHONPATH PYTHONNOUSERSITE=1 TMPDIR="$LOG" "$PY" -B \
  plugins/github.cli/tests/smoke.py --compact > "$LOG/review-p2-smoke-compact.log" 2>&1
env -u PYTHONPATH PYTHONNOUSERSITE=1 TMPDIR="$LOG" "$PY" -B \
  tests/golden_review_actions.py --workflow-repo "$PROJECTS/transist-workflows" \
  --fixture-base "$LOG" > "$LOG/review-p2-golden.log" 2>&1
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B scripts/check_installed.py \
  --transist-bin "$TRANSIST" --admin-bin "$ADMIN" --fixture-base "$LOG" \
  --package review-actions > "$LOG/review-p2-installed.log" 2>&1
env -u PYTHONPATH PYTHONNOUSERSITE=1 TMPDIR="$LOG" "$PY" -B -m unittest discover \
  -s tests -p test_installed_checker.py > "$LOG/review-p2-checker.log" 2>&1
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B scripts/check_plugin_structure.py \
  > "$LOG/review-p2-structure.log" 2>&1
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B -c '
import subprocess, sys
paths = sorted({p for p in subprocess.check_output([
    "git", "ls-files", "--modified", "--others", "--exclude-standard", "-z"
]).decode().split("\0") if p.endswith(".py")})
print("FILES", *paths, sep="\n", flush=True)
subprocess.run([sys.executable, "-m", "black", "--check", *paths], check=True)
subprocess.run([sys.executable, "-m", "flake8", "--max-line-length=88",
                "--extend-ignore=E203,W503", *paths], check=True)' \
  > "$LOG/review-p2-format-final.log" 2>&1
git diff --check > "$LOG/review-p2-diff-check.log" 2>&1
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B "$LOG/record_identity.py" \
  > "$LOG/review-p2-identity.json"
```

| Current proof | Result |
| --- | --- |
| `review-p2-github-units.log` | **27 tests pass**; 116 fresh/pending and legacy/compact invalid-location combinations preserve prior bytes without calling the writer; all projected schema fields are checked against the unchanged schema |
| `review-p2-smoke.log`, `review-p2-smoke-compact.log` | Normal and compact GitHub offline smokes **pass** |
| `review-p2-golden.log` | Original goldens **pass**; 29 explicitly labelled stricter-schema rejections preserve pending bytes; four valid-location and five final-projection filtering/supersession cases retain parity |
| `review-p2-installed.log` | **6 tests / 31 workflow runs / 38 action dispatches pass**. Ten invalid-location cases each exercise workspace `pending_file == output_file` and staged managed output: unchanged workspace/pending bytes, no published output document, and plugin-owned validation failure. A native runtime run accepts nullable/integral/unbounded valid locations. |
| `review-p2-checker.log` | **6 checker tests pass**; mocked failed/skipped/zero-test children remain expected negatives |
| `review-p2-format-final.log`, `review-p2-structure.log`, `review-p2-diff-check.log` | Changed-file Black/flake8, finite payload/schema structure and whitespace **pass** |
| `review-p2-identity.json` | Current source/selected-file SHA-256 inventory, clean sibling SHAs, pinned imports and unchanged runtime/admin digests |

All earlier logs are preserved. `review-p2-format-attempt1.log` records the initial
line-length findings, corrected before the final lint check. Source remains an
uncommitted Step 7 diff on `main`, ready for parent re-review. No sibling/plan edit,
operational-home replacement, commit, push, live provider call or aggregate gate.

## Boundaries and rollback

The pre/post-gate fixture simulates advancing fetches; it does not rerun runtime
gate persistence, already owned by Step 5. Raw GitHub PR/mergeability/check-rollup
metadata remains provider data; typed decision fields carry control semantics.
File data support widens to staged paths, not source authority. Scanner language
handling and subprocess enumeration remain the original bounded lexical helper,
not a parser or new sandbox guarantee.

This is native macOS, offline/fake-provider acceptance. Linux, aggregate gates,
live GitHub/account interoperability and publisher signing remain outside Step 7.
Fixtures use fresh standalone homes; no operational home or existing same-version
install is overwritten. Changed selected bytes require matching signatures for
governed adoption. Rollback reverts this package diff after any dependent Step 10
consumers, selecting the preserved package/home tuple. Parent review and commits
are separate from this uncommitted handoff.
