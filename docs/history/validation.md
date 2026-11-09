# Publication reorganization validation — 2026-10-05

> Historical development evidence, not public runtime v0.1.0 acceptance.
> Commands below document the original environment; use
> [current validation](../validation.md) for public reproduction.

This is a historical behavioral/publication record. The current [CI contract](../ci.md)
is static-only; these behavioral suites and export checks now run explicitly
outside CI. Its earlier configured installed/live jobs have been removed.
For the latest coordinated local source tuple and macOS/native Linux results,
see [Step 12](revised-workflow-authoring-step-12.md).

This record applies to the uncommitted `publication-reorg` implementation.
No provider account, billed inference, remote mutation, commit, or push was used.
Bedrock remains local and ignored; it is not part of the public inventory.

## Environment and limits

- macOS, native `sandbox-exec`; Python 3.11.
- Transist and transist-admin report **0.12.0** from a locally built runtime.
- Runtime HEAD: `984898afc8acfb34ed3457037d1185955fca03c6`, with local changes.
  This is not proof of a clean pinned-runtime build or Ubuntu CI acceptance.
- Vendored contracts match that current runtime working tree, with MPL-2.0
  license and [explicit provenance](../../tests/contracts/README.md).
- Authenticated vendor CLIs and real-provider interoperability remain untested.

## Observed checks

| Check | Observed outcome |
| --- | --- |
| Root publication/collection/cleanup regressions | 15 tests passed |
| Public package units | 83 tests passed across the collection |
| APP smoke runners | All 13 process packages passed; shared compatibility tests also rerun 11 smoke checks |
| Python formatting and lint | All public Python files and root tooling passed Black and flake8 without E501 suppression |
| Go entrypoint | gofmt, go vet and go test passed; package contains no Go test files; APP smoke builds/tests it |
| HTTP installed E2E | Exact 512 KiB binary workspace download, checksum, metadata retention and staging cleanup passed |
| OpenSpec installed E2E | Required-only demo inputs and canonical export/hint mappings passed with offline backend fixtures |
| Git MCP installed E2E | Both tools, structured results, unchanged repository, lifecycle and uninstall passed |
| Text HTTP installed E2E | Successful local-only call, denied network with zero transport calls, invalid-input/output rejection and canonical action validation failure passed |
| Anthropic installed E2E | Five tests including 33 HTTP cases, bounded I/O, policy denial and governed signing/trust/tamper checks passed |
| OpenAI-compatible installed E2E | Two tests, including supplied demo, passed with loopback fixtures |
| Optional CLI demo inputs | Three tests covering six installed Codex/Copilot/OpenCode fixture runs passed |
| Clean-history export | Approved bytes, executable modes and staged Git blobs verified; no old history copied |
| Exported collection | Root and offline checks passed without a sibling runtime source checkout |
| Independent current-content scan | Gitleaks 8.30.1 found no leaks in the approved export |
| Workflow configuration | YAML/actionlint and whitespace checks passed |

The OpenSpec E2E initially exposed a terminal-supervisor flush versus temporary
directory deletion race. Its cleanup now retries only `ENOTEMPTY`, with a strict
deadline; permission/I/O errors and unfinished runs are not treated as success.

## Reproduce

Install the development dependencies in an isolated Python environment, put
matching Transist/admin 0.12.0 binaries on `PATH`, and run from the repository root:

```sh
python3 -B -m unittest discover -s tests -p 'test_*.py'
python3 -B scripts/check_plugins.py
python3 -m black --check scripts tests/*.py plugins
python3 -m flake8 --max-line-length=88 --extend-ignore=E203,W503 scripts tests/*.py plugins
python3 -B plugins/http/tests/e2e.py
python3 -B plugins/openspec/tests/e2e.py
python3 -B plugins/git.mcp/tests/e2e.py
python3 -B plugins/text.http/tests/e2e.py
python3 -B plugins/anthropic/tests/e2e.py
python3 -B plugins/openai.compatible/tests/e2e.py
python3 -B plugins/codex.cli/tests/e2e_optional_inputs.py
python3 -B scripts/check_public_snapshot.py
python3 -B scripts/export_public_snapshot.py --destination /tmp/transist-public-snapshot --init-git
gitleaks dir --redact --no-banner /tmp/transist-public-snapshot
```

Retain redacted logs outside the publication snapshot. Check the exact final
export again before the first commit; editing documentation changes its file
inventory even though unselected documentation does not change plugin digests.
Contributor rights, public contact channels, protected environments and the
first commit's identity/sign-off still require maintainer review.

## Subsequent static CI cleanup verification

The redesigned [static gate](../ci.md) was verified locally on macOS ARM64 with
Python 3.11.7, Go/gofmt 1.22.12, Black 25.1.0, flake8 7.2.0,
actionlint 1.7.12 and Gitleaks 8.30.0. Native tool archives were checksum-verified;
the workflow's Linux actionlint/Gitleaks download URLs and SHA-256 digests were
also independently checked.

- `python3 -B scripts/check_static.py` passed: all 15 current source packages,
  YAML/JSON and offline schema references, 68 Python files, Go formatting,
  workflow lint and current-source secrets scanning.
- The same gate passed in a disposable Git-listed source checkout without
  ignored private material, generated plugin binaries or a sibling runtime.
- `python3 -B -m unittest discover -s tests -p 'test_*.py'` passed 41 local
  root/checker/runner regressions. This suite is not invoked by CI.
- A synthetic-token negative check made the real pinned scanner fail without
  exposing the token, despite an inline allow comment and conflicting ambient
  configuration. Scanner inputs were temporary, not committed fixtures.
- The local publication privacy/link gate and Git whitespace checks passed.

No plugin unit/smoke runner, installed E2E, runtime build, sandbox installation
or live provider was invoked for this cleanup. Hosted Ubuntu execution remains
unverified: these local changes were not pushed or dispatched to Actions.
