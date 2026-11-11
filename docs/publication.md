# Public snapshot publication

Export only approved current files into a new directory. Do not publish this
working repository's history as a substitute for a reviewed clean snapshot.
The public inventory is 16 packages under `plugins/<id>/`; ignored/private root
material, credentials, generated artifacts, and local binaries are excluded.

For initial publication, publish an **unsigned source-only v0.1.0 release** with all 16 package/demo
identities and exact pins fixed at `0.1.0`; no publisher-signed bundles or trust
root are supplied. Target public runtime v0.1.0, commit
`44a762cdd8ec2d967497dbd5be5534618da61ec6`. The final complete native macOS/Linux
gate has **passed locally**; [validation](validation.md) defines its reproduction and
evidence requirements. Required checks use deterministic fixtures without live
credentials; live-provider acceptance is excluded.

## Small-step plan

| Step | Deliverable | Acceptance criteria |
| --- | --- | --- |
| 1. Review sources | Approved inventory, licenses/NOTICE, package versions, docs and CI | 16 packages; no private/local assets or broken public links; contributor rights reviewed |
| 2. Validate current bytes | Complete final-snapshot evidence on native macOS and Linux | All required commands pass on both platforms with zero required-suite skips; live providers excluded |
| 3. Export and inspect | New approved-file directory without source Git history | Export succeeds; gate rerun in destination; independent secrets scan and manual rights review completed |
| 4. Initialize and publish | Fresh repository metadata and reviewed first commit | Correct author identity and DCO; approved policies; maintainer-approved remote publication |

Each step is reviewable before the next. Roll back before publication by discarding
the disposable export and regenerating it after fixes. If already published,
coordinate credential revocation or withdrawal with maintainers rather than
assuming a later deletion removes historical exposure.

## Checklist

- [ ] Confirm public inventory and explicit relative-file inclusion list:

  ```sh
  python3 -B -m unittest discover -s tests -p 'test_*.py'
  python3 -B scripts/check_public_snapshot.py --list
  python3 -B scripts/check_plugins.py
  ```

- [ ] Pass the complete [release gate](validation.md) with verified public
  v0.1.0 runtime/admin artifacts on both platforms. Record exact source, artifact,
  binary and inventory/mode identities, commands, exit codes and tool versions.
  Include all 16 offline packages, all 15 installed runners and additional
  deterministic service suites. Required failures/skips block publication.
  Re-export after any documentation change and repeat against the final bytes.
- [ ] Review Apache-2.0 LICENSE/NOTICE and third-party provenance. DCO sign-off
  certifies the contributor's right to submit; automated checks do not prove it.
  Vendored `tests/contracts/` schemas remain MPL-2.0 with their local license and
  [provenance](../tests/contracts/README.md). Preserve the upstream copyright
  credit in `NOTICE` and `tests/contracts/README.md`: the publication gate allows
  only that exact legal-credit text in those two files for its personal-name
  check. This does not exempt general private identifiers, emails, paths, or keys.
- [ ] Verify README, authoring/compatibility docs, support/security policies,
  issue/PR forms, and CI match actual delivery. Confirm existing contact channels
  and private vulnerability reporting availability before making promises.
  Email delivery is unverified here; private GitHub vulnerability reporting may
  be advertised as usable only when that option is available on the repository.
- [ ] Export to a new, absent destination:

  ```sh
   python3 -B scripts/export_public_snapshot.py --destination /tmp/transist-public-snapshot --init-git
   # Fresh Git is needed to rerun the inventory gate; no commit is created.
  ```

- [ ] In the exported directory rerun the source gate and offline checks. Inspect
  the exact file inventory; never copy `.git/`, private keys, caches, ignored local
  material, or a compiled Ollama binary from the original workspace.
- [ ] Run an **independent secrets scanner** over the export with a reviewed
  configuration and inspect findings. If retaining or importing any history,
  separately scan all intended history and review its rights/provenance.

  [Static CI](ci.md) installs the checksum-verified prebuilt
  [Gitleaks v8.30.0 release](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.0)
  and scans current Git-listed source bytes. For publication, separately scan
  the actual approved export using that version on `PATH`:

  ```sh
   gitleaks dir --redact --no-banner /tmp/transist-public-snapshot
  ```

  CI neither creates nor certifies a publication export, scans Git history, nor
  runs the behavioral checks in this checklist. Export acceptance and rights
  review remain explicit local publication work. Gitleaks does not need a Go
  compilation toolchain or a licensed organization-only action.
- [ ] Verify fresh author/committer identity and `Signed-off-by:` on each commit;
  initialize new history only after review. Commit/push/publication require the
  maintainer's explicit approval.
- [ ] Preserve private development history in the approved private archive;
  publish one reviewed root commit in the fresh repository. Repeat the complete
  both-platform gate on that committed snapshot before public visibility, the
  annotated `v0.1.0` tag and source-only release. Confirm static CI on the exact
  commit; static success alone does not authorize publication.

## Subsequent repository releases

After the initial publication, plugins and demos follow the independent
[version policy](authoring.md#plugin-version-policy). A repository release is a
validated catalog snapshot, not a requirement that all included packages share
its version or the runtime version. Leave unchanged plugins at their old versions.

Create a new source-only GitHub Release when an adoption-worthy fix, feature,
new plugin or runtime-compatibility baseline passes the documented release gate
on the exact candidate commit. Routine documentation-only changes usually do not
need a release. Keep published tags and payloads immutable; corrections ship in
a new release, never by moving the old tag.

Release notes must list changed plugin/workflow versions, supported runtime
identity, validation results and limitations, breaking changes and migration
steps. GitHub's automatic source archives suffice; custom binaries, signed
bundles and a release automation pipeline are not required. Source-only releases
remain unsigned unless a separately reviewed signing process is introduced.
The initial clean-history export steps above are not an instruction to discard
public history for every subsequent release. Preserve prior snapshots and homes
for rollback; later source validation does not retroactively accept new payloads.

## Design decisions and rationale

- An allowlisted current-file export makes the publication boundary reviewable
  and avoids inheriting private development history.
- The source gate catches known forbidden bytes, payload/inventory problems, and
  relative-link defects. It is **not** an independent secrets scanner and cannot
  certify all secrets, history sanitization, or copyright ownership.
- Root export regressions check path containment, source-change races, indexed
  executable modes, and fresh Git initialization. Passing these checks supports
  export integrity, not ownership or history acceptance.
- Publication, offline protocol tests, installed adapter tests, and live-provider
  acceptance are separate evidence surfaces. Keep pending gates visible.
- Explicit local installed checks use canonical runtime source identity and exact
  binary versions; static CI does not install or build a runtime.
- Immutable selected payloads preserve digest/signature meaning. Root/package
  documentation outside `package.files` can improve without rewriting releases.
  Companion shell bytes/signatures are not interchangeable with runtime-bundled
  shell, even when ids or version strings match.
