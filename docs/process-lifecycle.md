# External-tool subprocess lifecycle

CLI-backed actions finish external work before returning their terminal result.
The reference is `plugins/cursor.cli/process_lifecycle.py`; the runtime remains
the owner of process-group isolation, sandboxing and cancellation.

## Package implementations

- `openspec`, `github.copilot.cli`, `opencode.cli`, `codex.cli`, `shell`,
  `git.cli` and `github.cli` select package-local `process_lifecycle.py` and
  `process_runner.py`. No sibling package or repository is a runtime dependency.
- The Python native helper copies match Cursor byte-for-byte. Root tests enforce
  copy parity, manifest selection and version `0.1.0`.
- `ollama.cli` implements equivalent native identity checks and finalization in Go.
- `git.mcp` applies the ownership core to each bounded Git call, preserving the
  MCP server and runtime ancestors. It does not create a separate session.
- HTTP-library integrations do not need subprocess machinery unless they add
  external command execution.

## Invariants

- Snapshot protected ancestors and existing group members before launch.
- Inherit the runtime group; never create a new session or signal it wholesale.
- Verify PID birth and group before signaling newly observed invocation members.
- Finalize on success, nonzero exit, timeout and exceptions.
- Bound TERM/KILL cleanup to four seconds, then reader/reap completion to one
  additional second. Keep execution timeout headroom for this finalization.
- Deliver finite stdin alongside output reads without blocking on backpressure.
- Emit APP frames and mutate output/parser state only on the invoking thread.
- Stop readers and callbacks before emitting the terminal result.
- Fail closed on uncertain cleanup, PID reuse, denied observation or reader errors.
  Keep native exception details, process arguments and environments out of errors.
- Preserve normal action output, artifacts, backend arguments and usage extraction.

Linux uses pidfds; Darwin rechecks identity immediately before individual signals.
Deliberate provider-created session/group escape remains a runtime containment
concern, not permission for a plugin to widen its cleanup authority.

## Offline checks

```sh
python3 -B -m unittest discover -s tests -p 'test_*.py'
python3 -B scripts/check_plugins.py
python3 -B tests/e2e_process_lifecycle.py
python3 -B plugins/git.mcp/tests/e2e.py
```

The installed runner requires Transist on `PATH` or `TRANSIST_BIN`. It uses
disposable standalone homes, denied network, fake CLIs, and a fixture-only
entrypoint that sets environment in the same process. Production plugin/helper
bytes remain unchanged in those fixture packages. It checks all seven Python
wrappers with inherited pipes and TERM-resistant children, including OpenSpec's
validator and OpenCode's session export. Root native tests cover timeouts,
callback/setup failures, orphans, PID safety, stdin backpressure and finalization
deadlines. Ollama native/APP tests are documented in its package guide.

Offline and standalone installed passes do not establish live-provider or
governed-signature acceptance. Record the runtime revision, OS and package digest.

### Historical local verification, 2026-10-09

This development checkpoint does not establish public runtime v0.1.0 acceptance;
the current both-platform release gate is [recorded separately](validation.md).

- macOS: 65 root tests, 163 package unit tests, package smokes and all 11
  compatibility smoke reruns pass with Python 3.11.
- Installed Transist `0.12.0` / `sandbox-exec`: all 14 Python-wrapper lifecycle
  cases and the Git MCP installed workflow pass. These are local binary results,
  not evidence for a clean canonical runtime revision.
- Ollama Go race tests and vet pass on macOS; its package guide records the
  separate Linux native checks. Bundled shell's seven unit tests pass.
- Package/schema structure, Black, flake8, gofmt, actionlint, public snapshot
  checks and diff whitespace checks pass.
- At this historical validation, full static acceptance was blocked by three
  checksum false positives in `tests/services/tika_qdrant/model.lock.json:7–9`.
  The [2026-10-14 tooling validation](history/compatibility.md#local-tooling-evidence-2026-10-14)
  resolves them with lossless model-lock normalization; the full static gate passes
  without changing scanner rules or ignores.
- Linux installed Python-wrapper workflows, live providers and publisher-signed
  governed invocation remain unverified.

## Delivery at version 0.1.0

Maintained packages now declare `0.1.0` under the repository's fixed-version
rule. Changed payloads have new digests: existing signatures and
run asset locks do not cover them. Do not overwrite a live signed installation
or reinterpret a prepared/failed run against different bytes. Prepare fresh
publisher signatures, install through the public lifecycle in a separate home,
and create fresh runs. The separately bundled runtime shell needs its own
publisher signature refresh when carrying the same source fix.
