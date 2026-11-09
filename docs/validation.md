# Public release validation

## Status: local release-candidate validation passed

The complete gate passed on native **macOS ARM64 and Linux ARM64** on
2026-11-08 against identical exported source bytes, with zero required-suite
skips. Publication and validation of the eventual committed snapshot still
require a separate approval checkpoint. The target is public runtime **v0.1.0**, commit
**`44a762cdd8ec2d967497dbd5be5534618da61ec6`**; see
[compatibility](compatibility.md) and the
[public release](https://github.com/transist-ai/transist/releases/tag/v0.1.0).

| Required surface | Release status |
| --- | --- |
| Native macOS complete release-candidate gate | PASS: all 11 commands |
| Native Linux complete release-candidate gate | PASS: all 11 commands |
| Exact inventory/mode parity and independent secrets scan | PASS: 400 files; no unresolved findings |

### Observed coverage and environment

Both platforms passed 148 root tests, all 16 offline packages, all 15 registered
installed runners, native Go tests/race/vet, five host service-fixture tests, two
network-none container fixture tests, and the complete real Tika/Qdrant validator.
The latter includes hash-locked model provisioning, offline operation and runtime
network-isolation checks. Negative-test output is not a skipped suite.

| Tool/platform | macOS | Native Linux VM |
| --- | --- | --- |
| OS / kernel | macOS 15.7.7 (24G720), ARM64 | Linux 6.1.0-54-cloud-arm64, ARM64 |
| Python | 3.11.7 | 3.11.2 |
| Go | 1.22.5 | 1.22.12 |
| Docker | 26.1.3 | 20.10.24 |
| Compose | 2.27.0 | 1.29.2 |
| Gitleaks / actionlint | 8.30.0 / 1.7.12 | 8.30.0 / 1.7.12 |
| Sandbox | sandbox-exec | bubblewrap 0.8.0 |

Release executable SHA-256 identities:

| Platform | `transist` | `transist-admin` |
| --- | --- | --- |
| macOS ARM64 | `8dd6761fefb521b4f8778c8b6990c4f985c1073728ca61d7a8d15b735402670d` | `82230e43493e47ad46153f13ed64085976944931e099b3073277addd8d898b07` |
| Linux ARM64 | `593a9b5b470f57b785461a5ca73971c262ed11718e7465470ec1f4ae55d4c43b` | `4e0a23ef1cb525e1f3859967a57cc14157fe7e6e46efcdd8f3c8208a1d6ce181` |

The release archives and signed release manifest were verified against the public
release key; the Git tag itself is unsigned. Exact candidate inventories,
before/after hashes and per-command logs are retained in the external preparation
record. Documentation changes require a fresh export and full rerun before that
snapshot is approved. No earlier candidate's results substitute for final bytes.

Fixture repairs resolve virtualenv interpreter aliases before sandbox execution
and separate scanner/readiness setup budgets from the deadline behavior under
test. Direct APP 350ms deadline checks remain in place. A dedicated VM resolver
repair enabled Docker provisioning DNS without changing internal/none runtime
networks. Timing-sensitive lifecycle checks also failed during preparation; their
failures were retained, and complete serial validation passed with the original
process-exit and cleanup assertions. Run the gates without competing validation
jobs on the same machine.

## Reproduce the final gate

Use identical exported source bytes on both platforms and independently verified
platform-specific runtime release artifacts. Install `tests/requirements.txt` in
an isolated Python 3.11 environment. Provide native Go, Docker, platform sandbox
support and the pinned [static tools](ci.md). Unset inherited `PYTHONPATH`.
Linux namespace-peer fixtures require controller `setns` privileges. Use a
dedicated native test VM with a root-owned source/fixture tree and a privileged
test controller; do not grant capabilities to an operator's Python installation
or weaken the runtime sandbox. This is a fixture prerequisite, not a plugin
deployment requirement.
Set `EXPORT` to the reviewed export, `TRANSIST_BIN` and `TRANSIST_ADMIN_BIN` to
verified absolute executable paths, and `FIXTURE_BASE` to a fresh existing native
filesystem directory outside the export and runtime installation. Both binaries
must report exactly `0.1.0`; no development-runtime fallback is permitted.

From the exported repository root, run each command separately, retaining its
exit code and redacted stdout/stderr outside the snapshot:

```sh
python3 -B scripts/check_static.py
python3 -B -m unittest discover -s tests -p 'test_*.py'
python3 -B scripts/check_plugins.py
python3 -B scripts/check_public_snapshot.py --list
python3 -B scripts/check_installed.py \
  --transist-bin "$TRANSIST_BIN" \
  --admin-bin "$TRANSIST_ADMIN_BIN" \
  --fixture-base "$FIXTURE_BASE"
gitleaks dir --redact --no-banner "$EXPORT"
```

Require all 16 packages in offline coverage and all 15 registered installed
runners, with zero required-suite skips or zero-test substitutes. Run additional
deterministic suites outside the runners, including the
[Tika/Qdrant fixture](../tests/services/tika_qdrant/README.md), using their
documented entrypoints. Missing prerequisites and command failures block the gate.
Run the host fixture units with `-p test_fixture.py`. The service validator also
runs `test_tika_fixture.py` inside its locked network-none container: those tests
require root-only certificate ownership changes and model-provider dependencies,
not an operator's host Python environment.
From `plugins/ollama.cli/`, also run `go test ./...`, `go test -race ./...` and
`go vet ./...`: the Python offline runner's smoke does not replace native Go tests.
Required checks use deterministic provider fixtures; authenticated live-provider
checks are excluded and need no credentials. [CI](ci.md) remains static-only.

## Evidence required before changing status

Record exact source/export inventory and executable-mode identities, release
artifact and binary hashes, OS/tool versions, commands, exit codes, coverage,
skips, limitations and independent scanner findings. Require zero unresolved
secret findings and unchanged tested source bytes. After any source or docs
change, regenerate the export and repeat the complete gate on both platforms.
Validate the final committed snapshot again before public release.

Fixture signing/trust checks are separate from publisher signing. Publication is
unsigned source-only; no signed payloads, operational trust or live-provider
acceptance are claimed. See [publication](publication.md).

## Development archive

The [original publication record](history/validation.md),
[compatibility record](history/compatibility.md) and
[Step 12 record](history/revised-workflow-authoring-step-12.md) preserve their
original results and provenance. Package-local validation records labelled
historical likewise remain development evidence, not public v0.1.0 acceptance.
