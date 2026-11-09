# Revised workflow authoring — Step 12 plugin evidence

> Historical development checkpoint, not public runtime v0.1.0 acceptance.
> Original source identities, versions and results are retained below. Legacy
> runtime URLs may be inaccessible; use [current validation](../validation.md).

## Scope and source identity

This is a concise index of the coordinated local validation supplied for Step
12. The central
[runtime Step 12 report](https://github.com/transist-ai/transist.oss/blob/main/docs/developer/revised-workflow-authoring-step-12.md)
owns the complete command/log and executable-identity record. That canonical
`main` link is the intended publication location, not proof that the report or
the local commits are remotely available now.

| Repository | Local commit | Source tree |
| --- | --- | --- |
| Runtime/admin | `e3548d48a83ca01f34b12b7e69b9c06a8ea0c33b` | `f6b76f6d04ea832e3d2384dad04651e732b635b1` |
| Plugins | `64f79644436350329f2e893c6ca85f84eb6c91c0` | `06910cde41e4fdd27e2233636a75a3d5c26437ea` |
| Workflows | `ad8bbd49f407eebab6da71b567830f7d0cf973ca` | `d920b386913801aff2b0afe21abb50ff6333dccc` |

These commits are local and unpushed. The macOS and Linux CLI/admin binaries
were built before commit from trees now matching these commits; they were **not
rebuilt from clean commits**. The documentation revision containing this report
is later than the tested plugin tree and does not retroactively become its test
target. Exact package payloads remain the manifest plus finite `package.files`;
this report is not a generated package-digest inventory.

## Coordinated results and provenance

| Checkpoint | Reported result on both macOS and native Linux | Source boundary |
| --- | --- | --- |
| Runtime final tree | All five runtime commands exited zero; macOS default/all-feature counts 2,118 / 2,132, Linux 2,123 / 2,137 | Runtime tree `f6b76f6d04ea832e3d2384dad04651e732b635b1` |
| Plugin M4 | All 15 installed runners plus static, root, snapshot and offline checks passed | Plugin tree `c164823b2ec37a9b397877c33005b4c88360e171`, with the final runtime tree |
| Plugin M5 | Focused peer 17 and checker 19 tests, Black and flake8 passed | Final plugin tree `06910cde41e4fdd27e2233636a75a3d5c26437ea` |
| Final workflow matrix | All 35 roots per OS passed with zero skips; all four public examples and three author fixtures included | Final coordinated source tuple above |

M4 → M5 changes only `tests/namespace_peers.py` and
`tests/test_namespace_peers.py`; no selected production package files changed.
The M4 full plugin commands were **not rerun as one full suite on M5**. The final
workflow matrix and focused M5 regressions are separately recorded evidence.

All four reply-publication-loss combinations passed on each OS. The macOS
matrix's 24 controller receipts all recorded cleanup zeros; the separate post-run
source guards recorded `source_after_guard=0`. Elapsed times were 3,729.22 seconds on macOS and
2,208.90 seconds on native Linux, with authorized outer/idle budgets of
7,200/300 and 2,400/300 seconds respectively. These are finite fixture runs,
not live-provider acceptance.

## Documentation and adoption boundary

- Follow [authoring](../authoring.md) for managed document handoffs, exact-revision
  review, prompt/skill bindings and package-local modules. Workspace files and
  invocation-private `run://` paths are not substitutes for managed revisions.
- Public package/demo releases and exact dependency pins remain `0.1.0`.
  Selected-byte changes still require the exact new digest and matching signature
  before governed adoption in a fresh home. Documentation work does not authorize
  private-key use, publisher signing, or reuse of an earlier signature.
- The OpenSpec skill at `plugins/openspec/skills/transist-plugin-authoring/SKILL.md`
  is absent from the plugin's `package.files`, but explicitly referenced by its
  demo workflow's `skills`. Its wording now distinguishes supplied output paths,
  workspace deliverables and managed-document handoffs. This changes a captured
  workflow asset, not selected plugin payload; existing workflow signatures do not
  approve the new asset bytes. New governed packages require reviewed new captures
  and matching signatures; no signing or graph migration is performed here.
  The demo still binds plan/metadata/report fields through `artifacts`, not root
  managed documents; do not treat it as a migrated document-handoff fixture.
- Local fixture success does not establish published availability, publisher
  trust, live provider/account interoperability, or a clean-commit rebuild.
  Earlier dated records retain their original source and platform limits; the
  historical `984898...` fallback is not the current validation target.

The documentation follow-up runs current-source privacy/link publication checks,
the static gate and scoped Markdown lint; it does not rerun native installed
workflows or retroactively extend their acceptance to changed workflow assets.
Retain its check results outside the publication snapshot. See
[Contributing](../../CONTRIBUTING.md) and [publication](../publication.md).
