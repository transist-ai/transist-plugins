# Contributing to Transist Plugins

Focused plugin, example, documentation, and tooling contributions are welcome
under [Apache-2.0](LICENSE), subject to maintainer review. Runtime/CLI changes
belong in [Transist](https://github.com/transist-ai/transist) and follow
its contribution rules. Discuss substantial contract changes before implementing
them through the [issue tracker](https://github.com/transist-ai/transist-plugins/issues).
See [Support](SUPPORT.md) for the support boundary.

## First contribution

1. Read [README](README.md), [authoring](docs/authoring.md),
   [compatibility](docs/compatibility.md), and the affected package's guide,
   manifest, schemas, and tests under `plugins/<id>/`.
2. Reproduce with the smallest workflow, exact inputs, runtime and package
   versions, platform, and redacted output.
3. Keep the change scoped and reviewable. Update package docs and tests when
   behavior changes; explain action/schema/binding and compatibility impact.
4. Run applicable offline and installed checks, recording commands, results,
   runtime revision, platform, and every skip/blocker in the PR template.

Redact credentials, authorization headers, customer data, private paths, prompts,
and run artifacts from reports and logs. Do not disclose vulnerabilities in
public issues or pull requests; follow [Security](SECURITY.md).

## Package conventions

- Public packages live only under `plugins/<id>/`; keep their READMEs standalone.
- Treat installed/published selected payloads as immutable. `plugin.yaml` plus
  `package.files` determines the digest. After initial publication, selected-byte
  or file-list changes require a new plugin version, digest and matching signature
  for governed use, validated in a fresh home. Preserve older homes, installs,
  and runs. Unselected docs/tests are not runtime dependencies. Read the
  [version policy](docs/authoring.md#plugin-version-policy). Keep reported plugin
  versions and exact demo pins consistent with the affected manifests; leave
  unchanged plugins alone. Update affected release-specific fixtures when bumping.
- Preserve ids, actions, schemas, and bindings deliberately: workflows depend
  on them. Declare provider locality and unconditional startup prerequisites.
- Document all read/inherited/forwarded/plugin-managed environment variables,
  their defaults, and policy allow/inject/redact requirements. Credentials must
  be supplied externally through declared secret bindings.
- Keep account credentials, signing private keys, generated runs, local binaries,
  ignored/private packages, and machine-specific paths out of public sources.
- Use canonical runtime links rather than requiring a sibling developer checkout
  in public instructions. See [authoring](docs/authoring.md) for full conventions.

## Static CI and local validation

Unset inherited `PYTHONPATH` for every Python validation command, including
package-local examples: `env -u PYTHONPATH python3 ...`. Use the interpreter in
the requirements-installed venv. `PYTHONNOUSERSITE=1` alone does not disable
`PYTHONPATH`; verify imported module versions/locations when recording evidence.

[CI](docs/ci.md) runs static checks only. With Python 3.11, Go 1.22.12,
actionlint 1.7.12 and Gitleaks 8.30.0 available, run its exact gate locally:

```sh
env -u PYTHONPATH python3 -m pip install -r tests/requirements.txt
env -u PYTHONPATH python3 -B scripts/check_static.py
```

This covers canonical plugin layout, selected payload/schema consistency,
YAML/JSON syntax, Python formatting/lint, Go formatting, workflow lint and a
redacted current-source secrets scan. It does not run any test suite or plugin,
build Transist, install sandbox support, export publication files, or use live
credentials. New packages must satisfy the [source layout](docs/authoring.md).

### Explicit local behavioral checks

Use Python 3.11 and Go 1.22+ with explicit development dependencies in an isolated
environment. From the repository root:

```sh
env -u PYTHONPATH python3 -m pip install -r tests/requirements.txt
env -u PYTHONPATH python3 -B -m unittest discover -s tests -p 'test_*.py'
env -u PYTHONPATH python3 -B scripts/check_public_snapshot.py --list
env -u PYTHONPATH python3 -B scripts/check_plugins.py
```

Offline contracts are vendored in `tests/contracts/`; no sibling runtime checkout
or network schema retrieval is required. They retain MPL-2.0 and upstream credit;
see [provenance](tests/contracts/README.md) and [NOTICE](NOTICE). Original companion
contributions remain Apache-2.0. Required behavioral tests use deterministic
fixtures without live credentials. These local suites are not part of CI;
historical test counts are not final publication evidence.

The runner discovers every `test_*.py`, including Codex's four-model lean contract
matrix. It runs legacy smoke plus `--compact` for only Git CLI, GitHub CLI, Jira
REST and shell; other smokes exercise their supported contracts internally.
Every command's stdout/stderr is captured and retained in the parent log. A
failed unit suite does not omit later smokes or packages; any failure keeps the
overall exit status nonzero. Root test dependencies include the public Tika
package's pinned HTTPX/Starlette/Uvicorn versions for its offline ASGI/wire tests.
Root acceptance tests cover every public manifest and exact demo dependency
pin, independently selected lean-helper parity, inventory boundaries, and labelled
synthetic storage savings/linear replay growth. Process-helper behavior remains
covered by the existing lifecycle suites.

For explicit storage checks, use the [inventory/benchmark commands](docs/authoring.md#storage-inventory-and-benchmark).
Capture benchmark output to `/tmp/lean-benchmark-*.log`. Installed fake-provider
smoke needs an explicit binary and existing disposable fixture base; it fails
rather than skipping missing prerequisites. Report synthetic measurements and
installed execution separately, including failures. Do not use operational homes,
keys or real providers. These tooling checks do not change runtime persistence.

Format and lint **new or changed Python code** before review, using the package's
documented configuration. For example:

```sh
env -u PYTHONPATH python3 -m black --check scripts tests plugins
env -u PYTHONPATH python3 -m flake8 --max-line-length=88 --extend-ignore=E203,W503 scripts tests plugins
```

The publication reorganization normalized all public Python files. CI checks
Black and flake8 across all Git-listed Python source without
blanket line-length suppression. Keep future changes focused and preserve that
baseline rather than reformatting unrelated files in feature pull requests.

For lifecycle validation with the [public v0.1.0 baseline](docs/compatibility.md)
in a fresh disposable standalone home (`DEMO_HOME`):

```sh
transist plugins install --home "$DEMO_HOME" --path plugins/<id>/plugin.yaml
transist plugins inspect --home "$DEMO_HOME" <id>
transist workflows check --home "$DEMO_HOME" --path plugins/<id>/workflow.yaml
```

Then use the package guide to install its workflow, bind inputs, check installed
readiness, authorize only exact missing authority, run, and inspect actual
outputs. Use disposable homes and fixtures for deterministic checks.

Installed E2E runners are separate from the offline runner. Run them locally
when applicable to the change:

The inventory entrypoint is now `scripts/check_installed.py`. It requires explicit
matching executables and existing allowed disposable storage; it has no PATH
fallback and treats missing prerequisites, child failures and skips as failures:

```sh
env -u PYTHONPATH python3 -B scripts/check_installed.py \
  --transist-bin "$TRANSIST_BIN" --admin-bin "$TRANSIST_ADMIN_BIN" \
  --fixture-base "$FIXTURE_BASE" --package shell --package jira.rest
```

Repeat `--package` for a checked, deduplicated selection; omit it for the complete
deterministic installed inventory (including subprocess lifecycle, Tika's explicit
Docker prerequisite, and document/action fixtures). `--package document-actions`
runs all ten managed-file package contracts. `--list` prints the selected named
inventory after validating binary/storage prerequisites. `--package review-actions`
selects only the finite Step 7 Git/GitHub action fixture (typed approvals,
mergeability, pre/post review projection and instruction scanning). This runner
is also included by either `--package git.cli` or `--package github.cli`; those
package selectors additionally retain their existing lifecycle/document runners.
New installed suites require an explicit `Runner` entry. Each runner receives
`TRANSIST_BIN`, `TRANSIST_ADMIN_BIN`,
`TRANSIST_FIXTURE_BASE` and isolated fixture storage; direct invocation must
provide these variables. Use current positional workflow references and
subcommand-local `--format json`. Fresh homes preserve immutable 0.1.0 installs.

```sh
env -u PYTHONPATH python3 -B plugins/http/tests/e2e.py
env -u PYTHONPATH python3 -B plugins/git.mcp/tests/e2e.py
env -u PYTHONPATH python3 -B plugins/text.http/tests/e2e.py
env -u PYTHONPATH python3 -B plugins/openai.compatible/tests/e2e.py
env -u PYTHONPATH python3 -B plugins/anthropic/tests/e2e.py
env -u PYTHONPATH python3 -B plugins/openspec/tests/e2e.py
env -u PYTHONPATH python3 -B plugins/codex.cli/tests/e2e_optional_inputs.py
env -u PYTHONPATH python3 -B plugins/cursor.cli/tests/e2e_lifecycle.py --transist /absolute/path/to/transist --fixture-base /absolute/path/to/allowed/projects/tmp
```

These require a working platform sandbox and verified Transist/admin 0.1.0.
Use the inventory checker above, or explicitly provide `TRANSIST_BIN`,
`TRANSIST_ADMIN_BIN` and `TRANSIST_FIXTURE_BASE` for direct runners. No sibling
checkout is required. Missing prerequisites fail rather than proving acceptance.
Cursor's stdlib runner requires an explicit executable (or `TRANSIST_BIN`),
accepts a separate `--baseline-transist`, and selects candidate lifecycle evidence
only with `--candidate-lifecycle`. See its [package guide](plugins/cursor.cli/README.md)
for the isolated fixtures, partial-case option and system-Python import regression.
Read [compatibility](docs/compatibility.md) and [validation](docs/validation.md)
for the pinned public commit and full reproduction gate. Final acceptance on
both native macOS and native Linux has **passed for the local release candidate**. The
[Step 12 record](docs/history/revised-workflow-authoring-step-12.md) is historical
development evidence. Matching version strings do not prove matching source bytes.

Run package-local `tests/live.py` only for explicitly intended real-provider
validation, with credentials supplied externally. Never embed credentials in
commands or logs. A live skip is not acceptance; absent credentials or sandbox
support leave that gate pending. CI runs static analysis and uses no live
provider secrets.

## Pull requests and DCO

Use the PR template and list exact validation evidence and pending checks. Every
commit needs `Signed-off-by:` (for example, `git commit -s`). Contributions are
accepted under the [Developer Certificate of Origin](https://developercertificate.org/):
you certify that you wrote the contribution or have the right to submit it and
agree it may be distributed under Apache-2.0. Maintainer status does not replace
sign-off or ownership review.

For publishing to a fresh public repository, follow the small-step
[publication checklist](docs/publication.md). Current-file checks neither
sanitize existing Git history nor certify rights to third-party material.
Publication is an unsigned, source-only release; no publisher-signed bundles or
trust root are supplied. Runtime-bundled and companion shell bytes/signatures
are separate identities; do not interchange them.
