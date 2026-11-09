# Compatibility and validation evidence

> Historical development evidence. Versions, source identities and results below
> describe their original checkpoints, not public runtime v0.1.0 acceptance.
> Legacy runtime URLs are archival references and may be inaccessible; they are
> not public reproduction targets. Use [current compatibility](../compatibility.md)
> and [current validation](../validation.md) for the public release boundary.

## Baseline and sources of truth

The companion baseline is **Transist and transist-admin 0.12.0**, not a promise
that every public package is accepted on every platform. On 2026-10-05 GitHub
release lookups for both `v0.12.0` and `0.12.0` in canonical
`transist-ai/transist.oss` returned `release not found`.

### Historical fallback (2026-10-05)

The historical fallback source revision was
[`984898afc8acfb34ed3457037d1185955fca03c6`](https://github.com/transist-ai/transist.oss/tree/984898afc8acfb34ed3457037d1185955fca03c6).
Its existence was confirmed through the GitHub commit API; its `Cargo.toml`
declared workspace version 0.12.0 and Rust 1.88.0. This pin identifies historical
evidence, **not the current reproduction target**. Use the exact source tuple
and binary provenance in the [Step 12 record](revised-workflow-authoring-step-12.md)
for the latest coordinated validation. Those commits are local and unpushed;
their remote availability has not been established. [CI](../ci.md) is static-only
and has no runtime pin, runtime checkout/build, sandbox installation, or installed
execution job.

Consult the runtime documentation at the tested revision when investigating
behavior; `main` links below describe current upstream guidance:

1. [Compatibility and stability](https://github.com/transist-ai/transist.oss/blob/main/docs/public/reference/compatibility-and-stability.md)
2. [Plugin manifest](https://github.com/transist-ai/transist.oss/blob/main/docs/public/reference/plugin-manifest.md)
3. [Workflow schema](https://github.com/transist-ai/transist.oss/blob/main/docs/public/reference/workflow-schema.md)
   and [storage layout](https://github.com/transist-ai/transist.oss/blob/main/docs/public/reference/storage-layout.md)
   for declared paths and runtime resources
4. [Plugin support](https://github.com/transist-ai/transist.oss/blob/main/docs/public/reference/plugin-support.md)
5. [APP](https://github.com/transist-ai/transist.oss/blob/main/docs/public/reference/app.md),
   [TEP](https://github.com/transist-ai/transist.oss/blob/main/docs/public/reference/tep.md),
   and [CLI](https://github.com/transist-ai/transist.oss/blob/main/docs/public/reference/cli.md)
6. Runtime schemas, `crates/transist-plugins`, and `crates/transist-adapters`
   at the tested revision when implementation details remain ambiguous.

The companion packages implement `transist.plugin.v1`. Schemas, binding behavior,
APP/MCP framing, installed dispatch, policy, and platform sandboxing all matter;
manifest inspection alone is insufficient.

The stdlib process-package file helpers retain **Python 3.10+** syntax/API
compatibility; the maintained validation environment is **Python 3.11**. Python
3.14 is not required for macOS search-only directory traversal. On Darwin, older
interpreters use the public `O_SEARCH` ABI value (`0x40100000`, also exported by
CPython 3.14); Linux uses `O_PATH` when available. Descriptor-relative opens,
`O_NOFOLLOW`, regular-file checks and byte bounds still apply. Read permission
for ancestor directory listings is not required or granted.

## Release policy reset (2026-10-14)

Maintained public plugin/demo releases, exact dependency pins and discovery
identities now declare `0.1.0`. The source baseline for this reset is
`b2ae3e615dbfbfd7b1fa0e68114809cc7ad65c66`. APP/TEP and manifest/workflow schema
identifiers are unchanged; the vendored contracts retain their recorded
[upstream provenance](../../tests/contracts/README.md).

Every selected package digest changes because `plugin.yaml` is part of the
payload. Existing `1.0.0` signatures approve neither the new release identity
nor its selected bytes. No publisher signature or trust root is shipped in the
public source inventory, and no new publisher signing is claimed here. Obtain
matching signatures before governed adoption. The finite `package.files` lists
remain authoritative; there is no checked-in generated digest inventory to
refresh. Ollama's ignored selected binary must be rebuilt in a fresh package
from the updated Go source before installation.

Focused policy/catalog tests and Python APP/MCP, HTTP service and Go discovery
checks pass locally, as do the affected version assertions and scoped
Black/flake8/gofmt/Go vet checks. Logs are under
`/tmp/revised-authoring-step-1-plugins*.log`.

The installed Anthropic `test_trusted_lifecycle` **passes** on macOS using fresh
local default-feature debug Transist/admin 0.12.0 binaries with the coordinated
runtime trust fix. It verifies unsigned rejection, signed `0.1.0` plugin
verification/install/discovery, signed workflow dispatch to a local fake
provider, and tamper rejection, using disposable homes and fixture trust.
Log: `/tmp/revised-authoring-step-1-plugins-trust-default.log`. The fixture uses
current positional workflow references, explicit live readiness checks and
subcommand-local JSON output. This is fixture trust evidence, not publisher
signing or real-provider acceptance. The earlier reserved-version rejection in
`/tmp/revised-authoring-step-1-plugins-trust.log` is superseded for these default
binaries. The same installed fixture also passes with matching
`dev-version-bypass` runtime/admin debug binaries, including unsigned and
tampered `0.1.0` rejection. Log:
`/tmp/revised-authoring-step-1-plugins-trust-bypass.log`.

## Latest coordinated local evidence — Step 12

The [Step 12 record](revised-workflow-authoring-step-12.md) identifies runtime
`e3548d48a83ca01f34b12b7e69b9c06a8ea0c33b`, plugins
`64f79644436350329f2e893c6ca85f84eb6c91c0` and workflows
`ad8bbd49f407eebab6da71b567830f7d0cf973ca`, with exact tree identities.

- All 15 installed plugin runners and static/root/snapshot/offline checks passed
  on macOS and native Linux at plugin M4 tree
  `c164823b2ec37a9b397877c33005b4c88360e171` with the final runtime tree.
- The final M5 plugin tree changes only `tests/namespace_peers.py` and
  `tests/test_namespace_peers.py`. Focused peer (17) and checker (19) tests,
  Black/flake8 and the complete 35-root workflow matrix passed on both OSes.
  No selected production package bytes changed between M4 and M5.
- All 35 roots per OS had zero skips, including all four reply-publication-loss
  combinations. This is local deterministic fixture evidence, not live-provider
  or publisher-signing acceptance.
- Runtime/admin binaries were built before commit from the recorded trees,
  which now match the local commits; they were not rebuilt from clean commits.
  These source identities do not assert a published release or remote commit.

## Historical evidence matrix

This matrix retains earlier checkpoints and their then-pending checks. Use the
Step 12 record above for the latest coordinated platform and inventory results.

Step 6 document-compatible package evidence targets the clean local Step 5 runtime
`8c5721f0aa256a15bfc4d77b43203a9f5ad157fd` (default-feature debug binaries, 0.12.0).
See [the scoped Step 6 record](revised-workflow-authoring-step-6.md) for the exact
source tuple, executable digests, installed fake-provider matrix and log index.
This is separate from the historical fallback pin and platform evidence below.

Static CI success does not establish any behavioral acceptance in this matrix.
Package-local records may describe different source bytes, Python versions, or
runtimes; earlier configured execution jobs have been removed.

| Surface | Verified / observed | Pending or limited |
| --- | --- | --- |
| Runtime source identity | Canonical SHA exists; workspace version is 0.12.0 | No 0.12.0 GitHub release confirmed; clean-pin build/execution evidence remains pending outside CI |
| Public inventory | 16 current source directories under `plugins/`, including Tika HTTP | Publication acceptance depends on current-file gate and independent scanning |
| Root and package offline checks | Historical Step-4: 44 root tests and 101 package unit tests pass on macOS and Debian Linux; all 15 then-public packages checked, 13 package smoke commands plus 11 smoke reruns inside shell compatibility | Current root/static evidence is recorded below; offline checks do not prove live-provider acceptance |
| `git.mcp` standalone | Installed candidate E2E passes on macOS and Linux: both outputs, unchanged repository and uninstall checked; 13 offline unit tests | Clean canonical pin and governed signing/trust acceptance pending |
| `text.http` | Installed candidate E2E passes on macOS and Linux: real output, zero-call network denial, byte-bound failure and invalid output rejection; 6 offline unit tests | Clean canonical pin acceptance pending |
| `http`, OpenAI-compatible, Anthropic, OpenSpec | All four installed candidate runners pass on macOS and Linux; OpenAI-compatible 2 tests with 10 provider cases; Anthropic 5 tests with 33 HTTP cases, policy denial and fixture-generated trust/tamper checks | Local candidate source evidence, not clean canonical release evidence; no live-provider acceptance inferred |
| CLI optional inputs | 3 installed fixture tests pass on macOS and Linux via `plugins/codex.cli/tests/e2e_optional_inputs.py`, covering 6 required/explicit dispatches | Real authenticated CLI acceptance remains separate |
| Cursor lifecycle (normalized plugin version 1.0.0) | Baseline 10/10 and candidate 10/10 on both Linux/bubblewrap and macOS/sandbox-exec; publisher signature and trusted governed plugin installation verified | Historical local source evidence; governed invocation and live Cursor interoperability remain pending |
| Remaining process packages | Public offline unit/smoke runner coverage; package-local guides describe prerequisites | Real authenticated CLIs, live providers, and installed demos are not proven by mocks |
| Ubuntu 24.04 / Python 3.11 | Static PR/push checks configured; no installed E2E job | Linux behavioral execution still requires explicit validation with a working sandbox |
| macOS | Seven existing installed runners pass against candidate 0.12.0 with native `sandbox-exec` | No aggregate all-package installed, clean-pin or blanket governed acceptance claim |
| Other platforms | No acceptance evidence recorded here | Verify runtime platform support and each package's binaries/sandbox before use |
| Live providers | Opt-in package-local live scripts only; no live CI workflow | Offline fixtures do not prove provider/account interoperability |

Record the companion commit, selected package version/digest, runtime SHA and
version, OS/sandbox, Python/tool versions, exact commands, actual outputs, and
skips/blockers for acceptance. A locally patched runtime may differ from the
clean canonical SHA even if both print 0.12.0; record such patches explicitly.

### Local evidence recorded on 2026-10-05

The local runtime checkout has HEAD `984898afc8acfb34ed3457037d1185955fca03c6`
**and uncommitted changes**. Installed checks used its current source build
advertising 0.12.0; these passes are not clean-pin reproducibility evidence.
Linux was not run. The local logs are evidence references, not shipped artifacts:

- `/tmp/publication-reorg-root-tests.log`: 15 root tests pass.
- `/tmp/publication-reorg-offline.log`: 83 package unit tests pass
  (36 + 13 + 11 + 2 + 8 + 4 + 3 + 6), plus smoke checks.
- `/tmp/publication-reorg-{http,openspec,git-mcp,text-http}-e2e.log`:
  installed fixture workflows pass.
- `/tmp/publication-reorg-anthropic-e2e.log`: 5 tests pass, including 33/33
  installed HTTP cases and the tested trusted lifecycle.
- `/tmp/publication-reorg-openai-e2e.log`: 2 tests pass.
- `/tmp/publication-reorg-optional-e2e.log`: 3 optional-input tests pass.

Vendored APP/TEP schemas also come from this current local 0.12.0 working tree,
not an asserted byte-for-byte copy of the clean remote pin. Their
[provenance and MPL-2.0 attribution](../../tests/contracts/README.md) travel with the
export, so offline validation is self-contained.

## Final lifecycle validation (2026-10-07)

Tested CLI/admin version: **0.12.0**; the Cursor payload was normalized to **1.0.0** at that checkpoint. The recorded lifecycle evidence predates version normalization and does not independently verify the current **0.1.0** package bytes.
Linux baseline `6eed1688bd2ed22bed1b30b9cb020cbc27ec8140` was built from authentic `git archive` source. Candidate core:
`8ad853f0be7f8c7d3a088fe233e9beb889c03fde`, plus the dependency-allowlist
validation-script fix. Parent-recorded evidence is unpublished local source
validation, not acceptance of a canonical release.

| Surface | Final evidence | Limit |
| --- | --- | --- |
| Cursor installed lifecycle | Linux: baseline 10/10, candidate 10/10; macOS: baseline 10/10, candidate 10/10 | Native bubblewrap / sandbox-exec; no skips or bypass |
| Cursor offline coverage | 18 tests: 12 native lifecycle/helper + 6 driver; smoke PASS | Includes actual Python 3.9 import compatibility |
| Publisher-signed Cursor payload | Signature verification and trusted governed plugin install PASS | Governed invocation not established |
| Signed public canary v2 | Signatures verified; all 5 dependencies and governed workflow installed PASS | `run_created: false`; no invocation acceptance |
| Other installed E2E | All 7 existing runners PASS on macOS and Linux | Offline provider boundaries |
| Repository offline checks | Root 44/44; package 101/101; all smokes PASS on both OS | Does not establish live-provider interoperability |
| Static checks | Structure, Black, flake8, gofmt, actionlint PASS | Gitleaks was blocked then by 3 checksum matches; resolved in the 2026-10-14 tooling validation |
| Full runtime PR gate | PASS: 1,860 tests, 10 E2E suites, 12 shipped workflows | Parent-recorded candidate source evidence |
| Runtime security regression | PASS: 195 tests | Separate from publisher trust/install verification |

Publisher payload digest: `ac6f80e78cf7d710109b7e08209fdc23556c1e77d52d90579dd197d788254a4d`.
Signed public canary v2 digest: `f8bef30837afdad3213caee0c301b76be092e3e07ec130f59bf68399e1c4876c`.

### Reproducible contracts and commands

Keep recipe `source/`, runtime `home/` (including staging), and `workspace/`
disjoint; copy referenced assets alongside authored fixtures. This prevents a
read-only source bind from masking writable staging. Poll only `success`,
`failed`, `canceled` as terminal, then cancel/clean up before uninstall/removal.
Postponed module/helper annotations preserve Python 3.9 imports. Candidate
lingering-leader/orphan cases require `protocol_error` and lifecycle evidence;
baseline cases require `timeout_error` / exit 124. No runtime policy change.

Native `loopback` is namespace-local. The hosted Anthropic fake server instead
uses exact `127.0.0.1` + selected-port allowlist authority, `tls_required: false`,
`max_redirects: 0`; network-none, wrong-host/port and TLS-required negatives remain.
Resolve native Go binaries/GOROOT rather than isolated-HOME-dependent shims.

```sh
python3 -B scripts/check_static.py
python3 -B -m unittest discover -s tests -p 'test_*.py'
python3 -B scripts/check_plugins.py
python3 -B scripts/check_public_snapshot.py --list
python3 -B plugins/cursor.cli/tests/e2e_lifecycle.py \
  --transist "$CANDIDATE_TRANSIST" --candidate-lifecycle \
  --baseline-transist "$BASELINE_TRANSIST" --fixture-base "$FIXTURE_BASE"
```

Select explicit executable paths/native fixture storage; see [Cursor options](../../plugins/cursor.cli/README.md). Installed-run
commands: [HTTP](../../plugins/http/README.md), [Git MCP](../../plugins/git.mcp/README.md),
[text HTTP](../../plugins/text.http/README.md), [OpenAI-compatible](../../plugins/openai.compatible/README.md),
[Anthropic](../../plugins/anthropic/README.md), [OpenSpec](../../plugins/openspec/README.md),
[CLI optional inputs](../../plugins/codex.cli/README.md).

At this historical validation, Gitleaks 8.30.0 flagged public SHA-256 values in
`tests/services/tika_qdrant/model.lock.json:7–9`, blocking the full static gate.
The 2026-10-14 tooling validation resolved those false positives using structured
file/digest entries and lossless normalization; scanner rules and ignore behavior
were preserved.
Live Cursor was not executed: authenticated account paths are outside the current
allowed host scope. Live interoperability remains pending, not accepted.

## Shell ownership and separate versioning

The [runtime-bundled shell](https://github.com/transist-ai/transist.oss/tree/main/plugins/shell)
is the canonical owner for runtime quickstarts and bundled release workflows.
This repository's [companion shell](../../plugins/shell/README.md) is separately
versioned (fixed at 0.1.0 in its manifest). Companion APP fixes change selected
payload bytes and therefore fork its digest from the bundled package.

Do not copy a bundled signature onto companion bytes, overwrite an installed
version, or assume equal ids imply equal payloads. Pin the package/version that
the workflow was tested with. Under the companion's fixed `0.1.0` policy,
selected changes require a new digest and matching signature, validated in a
new home while older homes and runs are preserved. The runtime's own bundled
package version is authoritative in its release manifest, not in this companion
inventory.

## Local runtime baseline updates

To advance the baseline, verify the canonical full commit SHA (or independently
verify a real release and its artifact integrity), update the documented source
baseline and expected binary versions together, then run installed E2E locally
on supported platforms. Runtime baseline updates are not static CI prerequisites.
Do not substitute an unconfirmed URL, release version, or moving branch after a
download/build failure. Keep pending evidence visible until checks pass.

## Lean storage acceptance

All public plugin manifests and demo release/dependency pins remain exactly
`0.1.0`. Compatibility must identify the selected digest, not just that version.
Review consumers before compact adoption; omitted controls preserve direct-call
legacy defaults, while explicitly updated demos select new behavior. Older
installed homes retain their exact payloads, signatures and run locks. Adoption
uses a fresh home and new runs; no existing install overwrite or historical-run
deletion is part of this change. Runtime persistence and retention are unchanged.

| Gate | Evidence required | Boundary |
| --- | --- | --- |
| Public versions/templates | Root acceptance test checks every current public manifest, demo release and exact dependency pin at `0.1.0` | No workflow-repository or private-package publication inference |
| CLI model contracts | Codex-discovered matrix across Codex, Cursor, Copilot and OpenCode; input/output schemas, authoritative answers, overflow-before-launch, invocation replay and terminal failures | Mocked providers; independently selected identical `lean_policy.py` copies |
| Normalized command/REST output | Git/GitHub/Jira units plus legacy and `--compact` smokes | Complete business/control fields retained; diagnostics separate and capped |
| Shell semantic output | Units plus legacy and compact smokes, promoted guard/evidence/secret tests | Exact stdout remains business output; open business schema, closed diagnostics |
| OpenSpec/Ollama | Existing discovered units/smokes and package-local lean tests | Complete plan/report/response; explicit failures instead of business truncation |
| Existing bounded packages | Normal discovered units/smokes for HTTP, native model APIs, Git MCP, text HTTP and Tika HTTP | No speculative skips or unsupported compact flags |
| Read-only inventory | Temporary-directory tests: logical/allocated bytes, duplicate streaming limits, basename collisions, contained roots, overlap/hardlinks, JSON descriptor candidates, symlink/FIFO/race rejection, privacy | Best-effort filesystem metadata, no payload parsing, no pruning |
| Synthetic persistence | Real Codex handlers/output schema with fake provider; explicit depth/iteration projections, actual workspace history and modelled hint accounting; >=75% savings, linear replay at double iterations | Labelled serialized model, not installed runtime disk use; legacy hint acceptance caps not simulated |
| Installed fake-provider smoke | Explicit binary/base, fresh disposable unsigned package/home; actual successful CLI dispatch and run-tree inventory for both modes | Separate from synthetic nested benchmark, governed/trusted and live-provider acceptance |
| Publication | Root current-source snapshot gate and scoped formatting/lint | Public docs use generic private-package references; operational metadata stays local |

Commands and measurement assumptions are in
[authoring](../authoring.md#storage-inventory-and-benchmark). Record actual results,
log references, executable identity and limitations; a fixture/schema pass does
not imply an installed smoke passed.

### Local tooling evidence (2026-10-14)

On macOS with an isolated Python 3.14 environment, the root suite passed **84
tests** and the model fixture's separate suite passed **4 tests**. Scoped
Black/flake8 and the **full static gate** passed, including zero Gitleaks findings.
The three initial `generic-api-key` matches were public model checksums adjacent
to tokenizer filenames in `model.lock.json:7–9`. Structured file/digest entries
plus a stdlib loader preserve the original repository, revision, all six exact
SHA-256 pins and the existing verification-manifest shape. No scan rule, ignore
list, credential control, hash comparison or shared process helper was relaxed.

The collection runner captures both output streams and executes every discovered
command despite earlier failures. The final collection passed all 16 public
packages: **269 package tests**, package smokes, and four additional compact
smokes. Shell's materialization tests now preserve literal lean controls rather
than interpreting them as input templates. Root test dependencies explicitly
include Tika's HTTPX/Starlette/Uvicorn pins. All static checks, including the
unmodified secret scanner, pass.

Final regressions cover absent optional context, read-only scratch history,
diagnostic-only overflow preserving successful outcomes, operator-required
reconciliation after mutation budget failures, and supported Copilot JSON flags.
The Copilot action probes advertised transport options before task execution;
local help confirms the flags, but live provider event-shape validation remains
separate. Updated workflow source checks pass unbound and representative-bound
for all four public examples, without adapters, credentials or run creation.

| Measurement | Legacy | Compact | Interpretation |
| --- | --- | --- | --- |
| Synthetic storage, 8 iterations, contained depth 3, 98,304 transcript bytes | 13,130,261 bytes | 882,597 bytes | 93.28% less modelled serialized storage; excludes real adapter hint-cap acceptance |
| Compact replay history at 8 / 16 iterations | — | 5,272 / 10,544 bytes | Exactly 2x growth; 16 / 32 minimal replay files |
| Installed single-invocation run-tree logical bytes | 924,761 bytes | 140,152 bytes | Actual best-effort metadata, not nested/iteration benchmark savings |
| Installed single-invocation allocated bytes | 1,003,520 bytes | 212,992 bytes | OS-reported allocation; excludes workspace and other home directories |

Both installed fake-provider runs succeeded on an explicit local binary
advertising `transist 0.12.0`, with exact answer assertions and compact inline-log
omission checked. The disposable standalone home/package used denied network;
no operational home, account or signing keys were used. Full binary SHA-256 is
recorded in the local installed log; clean canonical source identity, governed
execution and live-provider interoperability are not established by this smoke.
Metadata counts can vary with asynchronous finalization and filesystem behavior.

Local evidence logs (not publication assets):

- `/tmp/lean-acceptance-plugin-root.log`, `/tmp/lean-validation-model-fixture.log`
- `/tmp/lean-acceptance-packages.log`, `/tmp/lean-validation-tika-tests.log`
- `/tmp/lean-validation-format.log`, `/tmp/lean-validation-lint.log`
- `/tmp/lean-acceptance-static.log`, `/tmp/lean-acceptance-plugin-public.log`
- `/tmp/lean-validation-lock-integrity.log`
- `/tmp/lean-secrets-report.json` (initial redacted findings)
- `/tmp/lean-benchmark-synthetic.log`, `/tmp/lean-acceptance-benchmark.log`
