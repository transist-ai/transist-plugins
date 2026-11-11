# Compatibility

## Public runtime baseline

Target the public [Transist v0.1.0 release](https://github.com/transist-ai/transist/releases/tag/v0.1.0),
commit **`44a762cdd8ec2d967497dbd5be5534618da61ec6`**.
Use [verified platform-specific release artifacts](https://github.com/transist-ai/transist/blob/v0.1.0/docs/public/reference/release-artifact-verification.md); both executables must report
exactly `transist 0.1.0` and `transist-admin 0.1.0`. A version string alone does
not verify source identity or artifact integrity. Do not substitute a sibling
development build when a release check fails.

The **local release-candidate gate passed on native macOS and Linux ARM64**.
This baseline is a reproduction target, not an all-package acceptance claim.
See [validation](validation.md) for commands and required evidence.

## Runtime sources of truth

Read the release-pinned runtime documentation:

- [Plugin authoring](https://github.com/transist-ai/transist/blob/v0.1.0/docs/public/handbook/06-architecture-and-extension/03-plugin-authoring.md)
- [Plugin manifest](https://github.com/transist-ai/transist/blob/v0.1.0/docs/public/reference/plugin-manifest.md)
- [Workflow schema](https://github.com/transist-ai/transist/blob/v0.1.0/docs/public/reference/workflow-schema.md)

All 16 companion packages implement `transist.plugin.v1`; manifests, discovery,
demo releases and exact dependency pins start at `0.1.0`. After initial publication,
plugins and workflows may [evolve independently](authoring.md#plugin-version-policy);
demo pins must match the respective local manifests. Runtime compatibility is a
separate tested baseline, not implied by matching version numbers. Seven vendored test
schemas match the public commit byte-for-byte; [provenance](../tests/contracts/README.md)
records their paths and hashes. Schema equality is not installed acceptance.

## Execution and publication boundary

Use Python 3.11 for maintained checks, native Go for Ollama, Docker for required
Tika/service fixtures, and the runtime's native sandbox (`sandbox-exec` on macOS,
bubblewrap on Linux). Package guides list additional prerequisites. Missing
required prerequisites or skipped required suites block release acceptance.
Required tests use deterministic providers; live credentials are unnecessary.

Packages are unsigned source examples in a source-only release. No publisher
signature, trust root or live-provider acceptance is supplied. Fixture trust
checks do not establish publisher signing. Governed adoption requires signatures
and approving trust for the exact selected bytes.

The [runtime-bundled shell](https://github.com/transist-ai/transist/tree/v0.1.0/plugins/shell)
and [companion shell](../plugins/shell/README.md) have separate payload identities.
Do not interchange bytes or signatures. Selected-byte changes require a new
digest and, for governed use, a matching signature. After initial publication,
also bump the affected plugin version and validate in a fresh home without
overwriting existing installs.

## Historical evidence

The [archived compatibility record](history/compatibility.md),
[publication validation](history/validation.md) and
[Step 12 checkpoint](history/revised-workflow-authoring-step-12.md) retain older
development versions, source tuples and results. Their legacy links may be
inaccessible. They are not public reproduction targets and do not establish
acceptance of this public runtime or the final publication bytes.
