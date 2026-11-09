# Revised workflow authoring — Step 6

> Historical development checkpoint, not public runtime v0.1.0 acceptance.
> Original source identities, versions and results are retained below.
> Use [current validation](../validation.md) for public reproduction.

## Design and scope

Prepared the maintained file contracts for runtime-managed documents. Final
independent review verified the file boundaries, clean-environment correction,
installed file cancellation and retained evidence. The original plan ledger
records the Step 6 commit after verification. File paths
remain action data; runtime-selected canonical workspaces remain authoritative.
Releases, dependency pins and APP handshakes remain `0.1.0`; schema IDs remain
`v1`, and runtime/admin remain `0.12.0`.

- Jira issue files accept staged absolute paths. Attachment manifests contain
  member paths relative to their own manifest parent, so complete trees relocate.
  Explicit manifests live directly in the output tree root. Sanitized collision
  names, per-download bounds, filtering and compact output are retained.
- CLI model context files accept staged absolute paths and preserve exact bounded
  UTF-8 text within prompt framing. Response/summary/PR-body destinations are data.
  External response paths use artifact-only invocation history; legacy sidecars
  stay in invocation artifacts. Workspace replay stays root-scoped. Actual used
  schemas have no `system_prompt_file` input.
- Git commit summaries/PR JSON and GitHub PR drafts/review plans/state/output
  accept staged data without changing command cwd. Reads are regular and bounded;
  PR body bytes are exact when no prefix transformation is requested.
- `shell/read_file` reads only a bounded regular workspace UTF-8 file and optionally
  writes exact bytes to a staged document. Source scope cannot be widened by the
  output path. Descriptor-relative no-follow opens reject ancestor/leaf symlinks,
  traversal, special files and overflow.
- OpenAI-compatible chat accepts exactly one of `messages` or `user_text_file`,
  optional file-mode `system_prompt`, and optional exact-byte `response_file`.
  Existing 64-KiB message, 256-KiB serialized request and 1-MiB response bounds
  remain; `max_prompt_bytes` can lower the aggregate prompt budget. Validation
  precedes transport. There is no ambient conversation history or response-path
  metadata. Usage and cancellation handling are retained.

The two new file-I/O modules are independently selected identical package copies.
Other maintained readers reject observed symlinks and final special files and
retain runtime-owned private-staging assumptions; this adds no host sandbox or
same-user isolation guarantee. Plugin process-group, stream and cancellation
helpers were not changed.

## Files

- `plugins/{codex.cli,cursor.cli,github.copilot.cli,opencode.cli}/`: path/history
  policy, handlers, input-schema documentation, package guides and affected tests.
- `plugins/{git.cli,github.cli,jira.rest,openspec}/`: file readers/resolvers,
  attachment manifest handling, affected schemas, guides and tests.
- `plugins/shell/{plugin.py,file_io.py,plugin.yaml,schemas/read_file.*.schema.json}`
  and `plugins/openai.compatible/{plugin.py,file_io.py,plugin.yaml,schemas/}`:
  new/extended action contracts and finite payload selections.
- `scripts/check_installed.py`, `tests/{installed_fixture.py,e2e_document_actions.py,
  test_document_contracts.py,test_installed_checker.py,support.py}` and affected
  existing installed runners: explicit selection, current CLI syntax, isolated
  fixture storage, finite installed-payload checks and fail-closed prerequisites.
- Package READMEs, `CONTRIBUTING.md`, `docs/{authoring,compatibility}.md`, and
  `tests/contracts/{README.md,provenance-step-6.json}` document the boundary.

## Source and executable identity

| Source | Checkpoint |
| --- | --- |
| Plugin clean baseline / current HEAD | `b5068e4c7fc64c53601809698fae228931572236`; Step 6 is an uncommitted working-tree diff on `main` |
| Runtime Steps 1–5 | clean `8c5721f0aa256a15bfc4d77b43203a9f5ad157fd` |
| Maintained workflows | `88e6871cd8155b2d021e8fc1420685b273b7e5ea` |

Used the supplied matching default-feature debug binaries at
`$PROJECTS/transist-oss/target/debug/{transist,transist-admin}`; no Rust rebuild.

| Binary | Reported version | SHA-256 |
| --- | --- | --- |
| `transist` | `0.12.0` | `dfbbf23585cc1dfdf8ed6e8417131221a5fec02773c0cb80392fd06a5ea4b874` |
| `transist-admin` | `0.12.0` | `c442375f583ac71643e2cf8dea44aae572140d8a886f99954a1e70caabb85bcd` |

All five vendored APP/TEP schemas compare byte-for-byte equal with that clean
runtime checkout. No schema bytes needed synchronization. The new provenance
record records their actual digests and comparison source, retaining MPL-2.0
attribution without asserting identity with the older fallback remote pin.

## Validation commands

Executed on macOS ARM64 / Darwin 24.6.0 with native `sandbox-exec` and Python
3.14.6. The original parent-process environment was **not isolated**:
`tools.log` records PyYAML **6.0.1** from inherited `PYTHONPATH`, despite
`PYTHONNOUSERSITE=1`. The earlier blanket claim of isolated PyYAML 6.0.2 was
incorrect. Historical logs are retained unchanged. The installed checker already
strips `PYTHONPATH` from its child environments.

Independent clean-environment rechecks unset `PYTHONPATH` and verify PyYAML
**6.0.2** from this venv: all 24 offline commands / 177 package tests, nine document
units and six checker units pass. Their retained logs are
`$PROJECTS/tmp/step6-review-clean-{offline,document-units,checker-units}.log`.
The current follow-up also completes dependencies from `tests/requirements.txt`
with `PYTHONPATH` unset; the original installation had treated some external
dependencies as already satisfied. `review-fix-clean-env-versions-final.log`
proves the pinned versions and actual imported module paths inside the venv
(Black 25.1.0, flake8 7.2.0, jsonschema 4.23.0, PyYAML 6.0.2 and the HTTP test pins).

All Python validation recipes below explicitly unset `PYTHONPATH`.
`PYTHONNOUSERSITE` alone does not do so. `$PROJECTS` denotes the local projects
directory; no machine-specific home path is required by sources.

```sh
LOG="$PROJECTS/tmp/revised-authoring-step-6-validation"
PY="$LOG/venv/bin/python"
TRANSIST="$PROJECTS/transist-oss/target/debug/transist"
ADMIN="$PROJECTS/transist-oss/target/debug/transist-admin"
env -u PYTHONPATH python3 -m venv "$LOG/venv"
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -m pip install \
  --cache-dir "$LOG/pip-cache" -r tests/requirements.txt
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -m pip check

env -u PYTHONPATH TMPDIR="$LOG" PYTHONNOUSERSITE=1 "$PY" -B -m unittest discover \
  -s tests -p 'test_document_contracts.py'
env -u PYTHONPATH TMPDIR="$LOG" PYTHONNOUSERSITE=1 "$PY" -B -m unittest discover \
  -s tests -p 'test_installed_checker.py'

env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B scripts/check_installed.py \
  --transist-bin "$TRANSIST" --admin-bin "$ADMIN" --fixture-base "$LOG" \
  --package shell --package jira.rest --package git.cli --package github.cli \
  --package codex.cli --package cursor.cli --package github.copilot.cli \
  --package opencode.cli --package openspec --package openai.compatible

env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B scripts/check_plugin_structure.py
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B scripts/check_public_snapshot.py --list
git diff --check
```

The clean version proof records both distribution versions and actual import
locations, and fails on inherited `PYTHONPATH` or an import outside the venv:

```sh
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B -c '
import importlib, importlib.metadata as metadata, json, os, sys
from pathlib import Path
expected = {
    "black": ("black", "25.1.0"), "flake8": ("flake8", "7.2.0"),
    "jsonschema": ("jsonschema", "4.23.0"), "PyYAML": ("yaml", "6.0.2"),
    "httpx": ("httpx", "0.28.1"), "starlette": ("starlette", "1.7.0"),
    "uvicorn": ("uvicorn", "0.54.0"),
}
assert "PYTHONPATH" not in os.environ and sys.flags.no_user_site
proof = {"python": sys.version, "executable": sys.executable,
         "prefix": sys.prefix, "PYTHONPATH_present": False,
         "no_user_site": sys.flags.no_user_site, "packages": {}}
for package, (module_name, version) in expected.items():
    module = importlib.import_module(module_name)
    path = Path(module.__file__).resolve()
    assert path.is_relative_to(Path(sys.prefix).resolve()), (package, path)
    assert metadata.version(package) == version
    proof["packages"][package] = {"version": version, "module_path": str(path)}
assert importlib.import_module("yaml").__version__ == "6.0.2"
print(json.dumps(proof, indent=2))'
```

The final Jira bound-preservation recheck additionally ran the same offline
command builder with only `jira.rest`, the focused document unit suite above,
and the installed checker with only `--package jira.rest`. Those commands invoke:

```sh
env -u PYTHONPATH TMPDIR="$LOG" PYTHONNOUSERSITE=1 "$PY" -B -m unittest discover \
  -s plugins/jira.rest/tests -p 'test_*.py'
env -u PYTHONPATH TMPDIR="$LOG" PYTHONNOUSERSITE=1 "$PY" -B plugins/jira.rest/tests/smoke.py
env -u PYTHONPATH TMPDIR="$LOG" PYTHONNOUSERSITE=1 "$PY" -B plugins/jira.rest/tests/smoke.py --compact
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B scripts/check_installed.py \
  --transist-bin "$TRANSIST" --admin-bin "$ADMIN" --fixture-base "$LOG" \
  --package jira.rest
```

The scoped offline command uses the existing command builder, including each
changed package's units/smokes and the four supported compact reruns:

```sh
env -u PYTHONPATH TMPDIR="$LOG" PYTHONNOUSERSITE=1 "$PY" -B -c '
import os, sys
sys.path.insert(0, "scripts")
import check_plugins as c
packages = ("shell", "jira.rest", "git.cli", "github.cli", "codex.cli",
            "cursor.cli", "github.copilot.cli", "opencode.cli", "openspec",
            "openai.compatible")
failures = []
commands = [(p, cmd) for p in packages
            for cmd in c.offline_commands(c.ROOT / "plugins" / p)]
for p, cmd in commands:
    print("RUN", p, *cmd, flush=True)
    if c.run_offline_command(cmd, dict(os.environ)):
        failures.append((p, cmd))
print("RESULT", len(commands)-len(failures), "passed", len(failures), "failed")
sys.exit(bool(failures))'
```

Formatting/linting selects only changed tracked and new nonignored Python files:

```sh
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B -c '
import subprocess, sys
paths = subprocess.check_output([
    "git", "ls-files", "--modified", "--others", "--exclude-standard", "-z"
]).decode().split("\0")
paths = sorted({p for p in paths if p.endswith(".py")})
subprocess.run([sys.executable, "-m", "black", "--check", *paths], check=True)
subprocess.run([sys.executable, "-m", "flake8", "--max-line-length=88",
                "--extend-ignore=E203,W503", *paths], check=True)'
```

These are corrected clean-environment recipes, not a claim that historical
commands unset `PYTHONPATH`. Output is redirected to the indexed retained logs;
the installed/offline checkers print expanded child commands. Initial attempts
remain historical evidence and are superseded only by passing targeted rechecks.

## Acceptance and evidence

Implementation matrix: **PASS**, 2026-10-15, on the source/binary tuple above.
Parent-process environment claims for the original matrix are corrected above;
the independent clean rechecks confirm the offline/unit outcomes.

| Gate | Actual result |
| --- | --- |
| Ten changed packages' unit suites | **177 tests pass** |
| New file/provenance/security contracts | **9 tests pass** |
| Installed-checker prerequisites/inventory/failure behavior | **6 tests pass**; child-failure/skip messages in this log are intentional negative fixtures, not skipped required tests |
| Offline command matrix | **24/24 commands pass**: ten unit commands, ten normal smokes and four compact reruns; the existing shell compatibility test also runs its eleven reference smoke paths |
| Selected installed inventory | **7/7 named runners pass**, no required skips |
| Original installed document actions | **10/10 package fixtures pass**, 16 real workflow dispatches including two expected validation failures; the follow-up adds a separately selected OpenAI cancellation case |
| Cursor native lifecycle | **10/10 scenarios pass**, including cancellation, timeout and post-result leader/orphan enforcement |
| Shared installed subprocess lifecycle | **14 real dispatches pass** across seven command wrappers, covering inherited pipes and TERM-resistant descendants |
| Existing optional CLI inputs | **3 tests / 6 dispatches pass** |
| Existing OpenAI-compatible installed runner | **2 tests pass**, ten provider cases plus the supplied demo |
| Existing OpenSpec installed runner | **1 test / 2 dispatches pass** |
| Storage smoke | Both real installed single invocations pass; synthetic savings/linear-history assertions also pass, separately labelled |
| Finite package/schema source validator | **PASS**, including new selected files and fixed releases/pins |
| Public snapshot inventory | **PASS**, including new source/test/documentation assets |
| Changed-file Black/flake8 | **PASS**, 38 Python files; pinned tools, no blanket line-length suppression |
| Whitespace | `git diff --check` **PASS** |

Retained evidence root: `$PROJECTS/tmp/revised-authoring-step-6-validation/`.

| Log | Contents |
| --- | --- |
| `dependencies.log`, `tools.log`, `environment.log` | Historical installation/parent environment and platform; `tools.log` records externally inherited PyYAML 6.0.1, not an isolated 6.0.2 environment |
| `source-runtime.log`, `source-workflows.log`, `source-plugins.log` | Actual checkpoint identities, sibling cleanliness and local plugin diff inventory |
| `contracts-comparison.log` | Actual paired vendored/runtime schema digests |
| `offline-final2.log` | Final 24-command scoped offline matrix and 177 package unit cases |
| `document-units-final3.log` | Nine final file/security/provenance cases, including unchanged Jira zero-as-default download bounds |
| `checker-units-final2.log` | Six fail-closed checker tests, including intentionally injected failed/skipped children |
| `installed-final2.log` | Actual seven-runner matrix, binary digests, expanded commands, provider fixtures and storage observations |
| `jira-offline-final3.log`, `jira-installed-final3.log` | Final Jira-only bound-preservation recheck: 13 units, normal/compact smokes and installed attachment relocation pass |
| `installed-inventory.log` | All twelve named inventory entries, listed without dispatching unrelated suites |
| `format-lint-final.log` | Changed-source formatting, Black check and flake8 results |
| `structure-final2.log`, `snapshot-final2.log`, `diff-check-final.log` | Final static payload/schema, publication and whitespace checks |

`*-attempt*.log` and earlier `*-final.log` records retain the initial failures and
intermediate passing subsets. Corrections covered absolute-path direct callers,
current CLI JSON/live-readiness syntax, fake answer transports, isolated HOME,
search-only directory descriptors under the native sandbox, schema-only contract
registry loading and bounded fixture cleanup. The `final2` matrix supersedes
those attempts. The final Jira-only recheck preserves the preexisting public
action's `max_bytes: 0` behavior (the default 10-MiB bound) rather than exposing
the lower-level downloader's unlimited mode. No failure was converted into a skip.

The new installed fixture verifies exact provider ingestion, attachment-tree
relocation, Git/GitHub cwd selection, revision/state handoffs, model usage, isolated
histories and byte-identical finite installed payloads. It also verifies that a
staged shell source cannot widen source authority and oversized OpenAI prompt
files cause no provider call or output publication.

### Independent-review follow-up

The dedicated `test_openai_file_cancellation` case uses the unchanged installed
OpenAI-compatible payload and a loopback provider that withholds its response.
It waits for the exact file-mode UTF-8 prompt bytes at the provider and confirms a
live invocation/runner before cancellation. The response document uses `update`
over a committed initial revision. After cancellation, all document heads and the
prior response bytes must be unchanged; there is no successful result, response
text or usage artifact. Native birth-identity observation verifies runner exit,
and provider-side EOF verifies the socket closes without releasing the answer.
A following run in the same installed home must receive only its own file prompt,
response revision and five-token provider usage, leaving the canceled run intact.

The new case is selected by both `--package openai.compatible` and
`--package document-actions`, with no required skips. Current targeted commands:

```sh
env -u PYTHONPATH TMPDIR="$LOG" PYTHONNOUSERSITE=1 "$PY" -B -m unittest discover \
  -s plugins/openai.compatible/tests -p 'test_*.py'
env -u PYTHONPATH TMPDIR="$LOG" PYTHONNOUSERSITE=1 "$PY" -B \
  plugins/openai.compatible/tests/smoke.py
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B scripts/check_installed.py \
  --transist-bin "$TRANSIST" --admin-bin "$ADMIN" --fixture-base "$LOG" \
  --package openai.compatible
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B -m black --check \
  tests/e2e_document_actions.py
env -u PYTHONPATH PYTHONNOUSERSITE=1 "$PY" -B -m flake8 \
  --max-line-length=88 --extend-ignore=E203,W503 tests/e2e_document_actions.py
```

Current clean-environment follow-up: **PASS**, 2026-10-15. The full 24-command
matrix is reused from the independent clean review; production package bytes
were not changed by this follow-up.

| Current check | Result / retained log under the evidence root |
| --- | --- |
| Pinned imports / venv locations | **PASS** — `review-fix-clean-env-versions-final.log`; PyYAML 6.0.2, no inherited `PYTHONPATH` |
| Clean dependency completion / consistency | **PASS** — `review-fix-clean-dependencies.log`, `review-fix-clean-tools.log`, `review-fix-clean-pip-check.log` |
| OpenAI-compatible units and smoke | **8 tests + smoke pass** — `review-fix-openai-offline-final.log` |
| Selected OpenAI installed checker | **2/2 runners pass**, four unittest cases: two existing provider/demo tests and two managed-document tests including cancellation — `review-fix-openai-installed-final.log` |
| Document and checker units | **9 + 6 pass** — `review-fix-document-units-final.log`, `review-fix-checker-units-final.log`; injected child failures/skips are expected negative fixtures |
| Modified Python test formatting/lint | **PASS** — `review-fix-format-lint-final.log`; Black 25.1.0 / flake8 7.2.0 |
| Publication / whitespace | **PASS** — `review-fix-snapshot-final.log`, `review-fix-diff-check-final.log` |

The independently retained clean logs are outside this evidence subdirectory at
`$PROJECTS/tmp/step6-review-clean-offline.log` (**24/24 commands, 177 tests**),
`$PROJECTS/tmp/step6-review-clean-document-units.log` (**9 tests**) and
`$PROJECTS/tmp/step6-review-clean-checker-units.log` (**6 tests**). They remain
unmodified review evidence. Historical `tools.log` and original parent-env logs
are not presented as clean 6.0.2 verification. The initial version-proof attempt
(`review-fix-clean-env-versions.log`) exposed missing HTTPX dependencies masked
by the inherited path; the completed clean dependency install and passing final
proof supersede that attempt.

The installed checker has an explicit inventory of twelve named runners,
including all existing deterministic E2Es, subprocess lifecycle, document actions
and the optional installed storage smoke. Package selection is checked and
deduplicated; missing binaries/environment/storage, zero-test suites and skips
fail. Only the seven runners selected by the ten changed package contracts are
Step 6 acceptance. The complete unfiltered inventory and aggregate catalog gates
belong to Step 12.

## Remaining platform/release work

Native Linux execution and the complete coordinated Step 12 gate remain pending.
Fake transports establish installed adapter/file behavior, not authenticated
provider interoperability or publisher signing. Same-version adoption requires
fresh homes and matching signatures for governed deployment; no existing install
was overwritten. Rollback selects the baseline package/home tuple and preserved
bytes. Parent review and any coordinated commits follow this local handoff.
