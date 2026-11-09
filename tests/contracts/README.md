# Vendored test contracts

The public provenance record also pins the two runtime-bundled shell lifecycle
helper digests at the same release commit. Offline parity checks use those exact
digests rather than an optional sibling development checkout; they do not equate
the complete bundled and companion shell packages or their signatures.

All **seven vendored schemas** match the public Transist **v0.1.0** source
byte-for-byte at commit **`44a762cdd8ec2d967497dbd5be5534618da61ec6`**.
[provenance-public-v0.1.0.json](provenance-public-v0.1.0.json) records the verified
public repository, tag, exact source paths and per-file SHA-256 hashes. No schema
bytes changed in this comparison. Source:
[public runtime schemas](https://github.com/transist-ai/transist/tree/v0.1.0/schemas).

They cover APP messages, the transitive TEP error reference and runtime
gate/document contracts. Tests load them locally by `$id`; no sibling checkout
or network retrieval is needed. Schema equality does not prove installed
acceptance; current both-platform results are [recorded separately](../../docs/validation.md).
Refreshes must preserve exact upstream bytes and record verified provenance.

## Historical provenance and derived bundles

Step 6 rechecked all five existing schema files against the clean local runtime
Step 5 commit `8c5721f0aa256a15bfc4d77b43203a9f5ad157fd` (0.12.0). They already
match byte-for-byte, so no vendored schema bytes were changed. The comparison and
per-file SHA-256 values are recorded in [provenance-step-6.json](provenance-step-6.json).
This verification does not retroactively assert identity with the older remote pin.

Step 10 adds byte-for-byte `runtime/gate-review.v1.schema.json` and
`runtime/managed-documents.v1.schema.json` from actual current source at
`48b1d9aaf7b0f56d20eae73cd9068312dcae8771` plus dirty Step 10 changes. Source
hashes and the explicit package-local bundle transformation are recorded in
[provenance-step-10.json](provenance-step-10.json). Tests compare all six bundled
definition sets and their selected payloads with these offline sources. Bundle
schemas omit upstream `$id` rather than claiming an altered schema is canonical.
The derived `plugins/{codex.cli,cursor.cli,github.copilot.cli,opencode.cli,openspec,shell}/schemas/gate-decision.schema.json`
files retain MPL-2.0, including their upstream definition content. Other original
package files remain Apache-2.0. No runtime test requires a sibling checkout.

## License attribution

Transist — Copyright 2026 Andrey Chernykh.
These schema source files are licensed under the **Mozilla Public License,
version 2.0 (MPL-2.0)**, as specified by the upstream
[`LICENSE`](https://github.com/transist-ai/transist/blob/v0.1.0/LICENSE)
and [`NOTICE`](https://github.com/transist-ai/transist/blob/v0.1.0/NOTICE).
The complete license text is included locally in [`LICENSE`](LICENSE), copied
from the runtime's `LICENSE`.

The root Apache License, Version 2.0 applies to original Transist Plugins
companion source code. The vendored APP and TEP JSON schemas in this directory
are an MPL-2.0 exception; the root Apache license does not relicense them.

This Source Code Form is subject to the terms of the Mozilla Public License,
v. 2.0. If a copy of the MPL was not distributed with this file, You can obtain
one at <http://mozilla.org/MPL/2.0/>.
