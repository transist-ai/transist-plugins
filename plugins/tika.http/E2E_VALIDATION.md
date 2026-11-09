# Step 3 installed-workflow validation

> Historical development evidence, not public runtime v0.1.0 acceptance.
> Original results, versions and binary identities are retained below. The final
> native macOS/Linux release gate is [recorded separately](../../docs/validation.md).

The maintained source release is now `0.1.0`. The dated evidence below describes
earlier payloads, not validation or signing of the new manifest/service bytes.

## Validated source version: 1.0.0 — 2026-10-07

**All named Step 3 E2E acceptance passed, with zero skips.** Scoped package,
fixture, formatting and lint checks passed. No remaining blocker was observed.
The parent records Step 3 completion in the original development plan after its commit.

The manifest opts both actions into `bindings.input_projection: action_data_v1`.
This requires the rebuilt **0.12.0 runtime supporting that binding**, not every
binary labelled 0.12.0. Top-level logical secret refs are projected away only
from adapter action data; original TEP input/metadata and secret capability
authority are explicitly asserted intact. Document schemas remain closed to
the original two fields. Selected manifest/service bytes changed, so package,
service and demo plugin identity are normalized together to **1.0.0**. Historical
1.0.0 evidence below and Step 2 `VALIDATION.md` are retained.

Actual plugin worktree baseline: `main` at
`d524c3fd4b4b30f7885803be240726c83f9d4d03`; the evidence records dirty Step 3
source paths. The `.git` worktree file is supported via native `git -C` commands.
Runtime fixes were already present and uncommitted in the separate runtime
checkout when this validation started; no further runtime changes were made.
Only its `target/release/transist` and `target/release/transist-admin` were used;
installed tools were not overwritten.

### Current acceptance matrix

| Named acceptance | Standalone | Signed/governed |
| --- | --- | --- |
| Plugin install/list/inspect and uninstall/list/inspect absence | Passed | Passed |
| Authored check, workflow install/check/authorize/readiness | Passed | Passed; signed packages and exact grants |
| Run/poll terminal state; TXT/PDF/DOCX text, metadata and document SHA-256 | Passed, all three | Passed, all three |
| Bad credentials | Passed; 401 observed, no successful invoke | Passed; 401 observed, no successful invoke |
| Untrusted CA | Passed; chain verification rejected, zero APP requests | Passed; handshake-only failure, zero APP requests |
| Wrong hostname | Passed; hostname verification rejected, zero APP requests | Passed; handshake-only failure, zero APP requests |
| Denied network | Passed; zero APP calls | Passed; explicit network denial and zero APP calls |
| Denied HTTP secret delivery | Passed, before birth | Passed, before birth |
| `--local-only` external-provider denial | Passed | Passed |
| Raw/encoded credential and prefix-free Bearer token persistence scan | Passed, all files/logs | Passed, all files/logs |
| No plugin-created client document files or leaked invoke staging | Passed | Passed |
| Stale signature after selected-payload tampering | Admin verification rejected | Governed install rejected; plugin absent |

The host sandbox denied an attempted write, and live runtime readiness
constructed `sandbox-exec`. Governed successful runs also assert
`trusted_execution_mode: true`, `policy_profile: allowlist` and
`capability_enforcement: enforced`. Real private Tika `/version` returned
`Apache Tika 3.3.0`; only the HTTPS APP frontend had a loopback-published port.
Provisioning ran with network disabled, created no model and started no Qdrant.
The production APP implementation was used through the fixture TLS launcher.

Governed TLS cases use separate valid-TLS authority homes to produce exact
CLI-generated policy fragments. The fragments are transferred unchanged into
the negative homes, with identical workflow IDs, pins, inputs and workspace
resources. Each home installs only one immutable package payload. TLS failures
must have exactly two failed `adapter_handshake` checks and no unrelated
readiness failure; independent TLS checks require verification code **20/21**
for untrusted roots and **62** for hostname mismatch. No trust, schema or
network enforcement is bypassed. Unsigned authored validation happens in a
standalone authoring home; governed installation uses signed workflow packages.

### Current identities and evidence

Platform: macOS 15.7.7 arm64; local Docker context **colima**, Linux aarch64.

| Component | Identity |
| --- | --- |
| Rebuilt Transist | 0.12.0; SHA-256 `1a6871634ff9eb4d52b301b81c64751608d63fedfa1f0b84869b47590f5eeddc` |
| Rebuilt admin | 0.12.0; SHA-256 `816adbd03bbda64d315385309ee264fee27f3b107db1717880ace59f34f3f97d` |
| Tika digest | `sha256:2a565f1ea1290bdcb74a7d35957d16a989ed44ef98790dcdcc28121d728fa583` |
| Tika resolved image | `sha256:1546f74efd8e8ea1f4506c5f9729a6ca3aa23a570abceba11e5dcaf30bbb722f` |
| Production APP image | `sha256:c95c7520062322e8c1741ca8a66da0ae17f9b0cf82c4484496ef71c0daea25ea` |

Final private evidence: **`/private/tmp/tika-step3-6oungpqy/`**.
`summary.json` records `accepted: true`, exact credential-free argv/exits,
runtime binary hashes, plugin version, source status, image identities, TLS
verification causes and document/result hashes. Its `logs/` and retained
runtime homes contain readiness, authorization, signed execution, failures,
cleanup and service logs. All owned containers, volumes and networks were
removed and pre-existing unrelated resource IDs were preserved. Private
credential/TLS/signing assets were removed, as were installed plugin payloads.
Images remain cached. Colima was started using its existing configuration;
the parent stops it after completing this validation handoff.

### Current exact scoped commands and results

Run from the plugin repository root; set `TRANSIST_RUNTIME_REPO` to the separate
runtime checkout, `EVIDENCE_PARENT` to an external log directory and
`LINT_PYTHON` to the external Black/flake8 virtualenv interpreter. These path
variables normalize private machine-specific locations for publication; the
E2E summary contains the exact resolved argv.

```sh
TRANSIST_BIN="$TRANSIST_RUNTIME_REPO/target/release/transist" \
TRANSIST_ADMIN_BIN="$TRANSIST_RUNTIME_REPO/target/release/transist-admin" \
python3.11 -B plugins/tika.http/tests/e2e.py \
  > "$EVIDENCE_PARENT/tika-step3-projection-final-ready.log" 2>&1

docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/repo:ro" -w /repo tika-step3-6a8e154e037b-app \
  python -B -m unittest discover -s plugins/tika.http/tests -p test_tika.py -v \
  > "$EVIDENCE_PARENT/tika-step3-projection-unit-wire-final.log" 2>&1
docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/repo:ro" -w /repo tika-step3-6a8e154e037b-provision \
  python -B -m unittest discover -s plugins/tika.http/tests -p test_schema.py -v \
  > "$EVIDENCE_PARENT/tika-step3-projection-schema.log" 2>&1
docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/repo:ro" -w /repo tika-step3-6a8e154e037b-provision \
  python -B -m unittest discover -s plugins/tika.http/tests -p test_e2e_harness.py -v \
  > "$EVIDENCE_PARENT/tika-step3-projection-harness.log" 2>&1
docker run --rm --network none \
  -v "$PWD/tests/services/tika_qdrant:/fixture:ro" \
  tika-step3-6a8e154e037b-provision \
  python -B -m unittest test_fixture test_tika_fixture -v \
  > "$EVIDENCE_PARENT/tika-step3-projection-fixture.log" 2>&1
```

Results: **21 unit/wire tests**, **3 schema/package tests**, **3 harness
regressions**, **6 fixture regressions** passed. The real production 30-second
trickling deadline test passed without changing the production deadline.
Black 26.3.1 `--check`, flake8 7.3.0 and `git diff --check` passed:

```sh
"$LINT_PYTHON" -m black --check \
  plugins/tika.http/service.py plugins/tika.http/tests/e2e.py \
  plugins/tika.http/tests/test_tika.py plugins/tika.http/tests/test_schema.py \
  plugins/tika.http/tests/test_e2e_harness.py \
  tests/services/tika_qdrant/provision.py tests/services/tika_qdrant/tika_app.py \
  tests/services/tika_qdrant/test_tika_fixture.py
"$LINT_PYTHON" -m flake8 --max-line-length=88 --extend-ignore=E203,W503 \
  plugins/tika.http/service.py plugins/tika.http/tests/e2e.py \
  plugins/tika.http/tests/test_tika.py plugins/tika.http/tests/test_schema.py \
  plugins/tika.http/tests/test_e2e_harness.py \
  tests/services/tika_qdrant/provision.py tests/services/tika_qdrant/tika_app.py \
  tests/services/tika_qdrant/test_tika_fixture.py
git diff --check
```

Earlier new-build attempts exposed harness-only assumptions: exact TXT media
type versus Tika's charset parameter; pre-birth text errors versus structured
readiness; failed-run cleanup before uninstall; unsigned checks in governed
homes; literal YAML version and `--approve` required for admin delivery;
sanitized TLS errors and exact authority isolation; immutable version reuse;
and cold Tika startup despite healthy APP liveness. These were corrected in
the harness, without runtime changes or invocation retries. Final acceptance
also explicitly verifies original secret authority and governed enforcement.

Two short-duration test fixtures failed under VM timing: the shared-deadline
test expired before its backend phase, and the TCP partial-body test raced
connection closure. Initial logs `tika-step3-projection-unit-wire.log` and
`tika-step3-projection-unit-wire-isolated.log` retain those failures. Test-only
durations were scaled to one second, preserving backend-call, timeout-code,
elapsed-time, response and credential assertions. The production 30-second
deadline and all input/output bounds are unchanged. The final 21-test run
passed in **32.925 seconds**. No root/full gate, CI, Step 4+ work, branch switch,
staging, commit, push or plan-status edit was performed.

## Historical 1.0.0 attempt — original runtime

**Historical status: blocked; Step 3 was not accepted at that time.**

Baseline: clean isolated Git worktree on `main`, commit
`b079896c32ca1b614c3cbbeb6968b683355e9c6d`. The worktree's `.git` is a file;
the harness uses `git -C <repo-root>` rather than assuming a `.git` directory.
Only Step 3 harness/fixture/docs changes were made. No runtime changes or
production-service corrections were made.

## Observed blocker

The matching Transist 0.12.0 binary rejects the first **bound authored workflow
check**, before workflow installation or dispatch:

```text
transist --home <disposable-home> --format json workflows check \
  --path <scratch-flow.yaml> --inputs-file <generated-inputs.json>
exit 1
text:     input_schema failed, instance path '', schema path '/additionalProperties'
metadata: input_schema failed, instance path '', schema path '/additionalProperties'
```

The action inputs contain `document_base64`, `content_type`, and the runtime's
required logical `with.secret_refs: [TIKA_AUTHORIZATION]`. Both Tika input
schemas correctly close the document contract to the two document fields.
The runtime manifest contract requires the secret reference because both
actions declare `secret_inputs.required: true`. This combination fails the
bound check in the supplied runtime. Removing the required secret reference or
allowing a third action-input field would weaken the fixed contract, so neither
was done. Execution validation stopped at this blocker.

## Results actually obtained

| Named acceptance | Result |
| --- | --- |
| Matching Transist/admin 0.12.0 | Passed |
| Host sandbox enforcement | Passed: `sandbox-exec` denied an attempted write; no file created |
| Runtime sandbox/dispatch acceptance | Not reached; host support alone does not prove runtime dispatch |
| Real pinned Docker Tika, private backend/no published backend ports | Passed |
| HTTPS production APP, trusted chain, discovery identity 1.0.0 | Passed |
| No unnecessary model provisioning | Passed; provision had network disabled and created no model assets |
| Admin plugin signing and signature verification | Passed |
| Standalone plugin install/list/inspect | Passed |
| Authored bound workflow check | Failed for both action schemas, as above |
| Workflow install/check/authorize/run/poll/text/metadata/document hashes | Not reached |
| Signed/governed installed workflow execution | Not reached |
| Bad credentials, wrong CA, wrong hostname | Not reached |
| Denied network, denied secret delivery, local-only denial | Not reached |
| Signed-package tampering | Not reached |
| Complete-run credential non-persistence/no client document acceptance | Not reached; partial runtime files/logs scanned and client workspace remained empty |
| Standalone uninstall/list/inspect absence | Passed in explicit post-blocker cleanup; governed uninstall not reached |
| Docker teardown and unrelated-resource preservation | Passed |

Partial E2E evidence: `/private/tmp/tika-step3-66rwzcei/`.
Its `summary.json` records exact credential-free command argv, exits, binary
hashes, dirty-source status, image IDs and teardown results. Key evidence:

- `logs/027-transist-admin.log`: successful plugin signature verification.
- `logs/029-transist.log`–`logs/031-transist.log`: install/list/inspect.
- `logs/032-transist.log`: both bound input-schema failures.
- `logs/034-docker-compose.log`: owned containers, volume and networks removed.
- `logs/035-docker.log`–`logs/040-docker.log`: unrelated resources preserved and
  project-labelled resources absent.

Post-blocker failure cleanup used the public CLI against the same disposable
standalone home: `plugins uninstall tika.http --version 1.0.0` succeeded;
`plugins list` returned `[]`; `plugins inspect tika.http` failed because the
plugin was not installed. The separate cleanup logs are
`tika-step3-failure-cleanup.log` and `tika-step3-failure-cleanup-inspect.log`
under the external project temporary directory. No further workflow execution
was attempted after the contract blocker.

Earlier attempts found and corrected harness-only setup issues: isolated Docker
HOME lost the Desktop context endpoint; an internal-only network did not publish
the frontend port; admin `plugin verify` requires explicit `--signature`.
They are recorded in `/private/tmp/tika-step3-sd65cubf/`,
`/private/tmp/tika-step3-97yjdf8u/`, and `/private/tmp/tika-step3-rd5jb69e/`.
The successful fixture/signing/lifecycle setup in the final attempt supersedes
those setup failures. No Tika action was dispatched.

## Runtime and image identities

Platform: macOS 15.7.7 arm64; Docker daemon: Linux aarch64.

| Component | Identity |
| --- | --- |
| Transist | 0.12.0; SHA-256 `f8af178003d7f7b03058209d0d820aee2ecc0bf9d4386df4963ae73083464b0b` |
| Transist admin | 0.12.0; SHA-256 `2a33db94fb1d640d5a95d7802c16faa875f2ee95ef465c56dd01e5daeef5497e` |
| Tika manifest digest | `sha256:2a565f1ea1290bdcb74a7d35957d16a989ed44ef98790dcdcc28121d728fa583` |
| Tika resolved image ID | `sha256:1546f74efd8e8ea1f4506c5f9729a6ca3aa23a570abceba11e5dcaf30bbb722f` |
| Production APP build ID | `sha256:f0dc5c8ea99a9646c73083d91d2e50e79e4341dff9d6d2b5564c871bac0d0ec1` |

The service Dockerfile and dependency pins are unchanged from Step 2. The E2E
builds that image and mounts only the fixture TLS/counter launcher separately.
The final harness includes clearer blocker reporting and failure-path uninstall
cleanup; these post-blocker changes were formatted/linted and scanner-tested,
not rerun through the blocked runtime. Downstream E2E cases remain unverified.

## Scoped regression checks

From the repository root:

```sh
python3.11 -B -m unittest discover -s plugins/tika.http/tests \
  -p test_e2e_harness.py -v
docker run --rm --network none \
  -v "$PWD/tests/services/tika_qdrant:/fixture:ro" \
  tika-step3-d722985d0adc-provision \
  python -B -m unittest test_fixture test_tika_fixture -v
```

- Harness regressions: **3 tests passed**, including raw/full/prefix-free token,
  padded/unpadded standard and URL-safe Base64, and safe redacted records.
  Negative-case cause matching also excludes recipe policy descriptions, so
  `tls_required` or `retryability: none` cannot prove a TLS/network failure.
- Scoped fixture regressions: **6 tests passed**, including the 4 existing
  fixture tests, real Tika-only TLS/document provisioning with HTTP client
  creation forbidden, and refusal to overwrite occupied private assets.
- Formatting/lint: Black 26.3.1 and flake8 7.3.0, only the five touched Python
  files; flake8 uses `--max-line-length=88 --extend-ignore=E203,W503`.
  Black `--check`, flake8 and `git diff --check` passed.
- No root tests, complete release gate or CI changes.

Host lint tooling was provisioned in an external private virtualenv. Exact
scoped lint commands (set `LINT_PYTHON` to that virtualenv's Python):

```sh
"$LINT_PYTHON" -m black --check \
  plugins/tika.http/tests/e2e.py plugins/tika.http/tests/test_e2e_harness.py \
  tests/services/tika_qdrant/provision.py tests/services/tika_qdrant/tika_app.py \
  tests/services/tika_qdrant/test_tika_fixture.py
"$LINT_PYTHON" -m flake8 --max-line-length=88 --extend-ignore=E203,W503 \
  plugins/tika.http/tests/e2e.py plugins/tika.http/tests/test_e2e_harness.py \
  tests/services/tika_qdrant/provision.py tests/services/tika_qdrant/tika_app.py \
  tests/services/tika_qdrant/test_tika_fixture.py
git diff --check
```

External logs: `tika-step3-scanner-regression.log`,
`tika-step3-fixture-regression.log`, `tika-step3-black-check.log`, and
`tika-step3-flake8.log`, under the external project temporary directory.

At that time, resumption required a matching runtime satisfying the unchanged
closed-input and required-secret-reference contract. The complete new E2E has
now been rerun with that support, as recorded in the current section above.
