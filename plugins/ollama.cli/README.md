# ollama.cli

Package version: `0.1.1`.

`process` plugin for local LLM generation via the [Ollama](https://ollama.com) CLI.

## Overview

Wraps `ollama run <model>` as a Transist `process` plugin. The prompt is
delivered via stdin; the model response is captured from stdout. ANSI escape
sequences and spinner noise in stderr are stripped automatically.

## Runtime Capabilities And Endpoints

- Adapter: `process`.
- Capability grants: adapter invoke, `plugin:ollama.cli@0.1.1` load, and `action:ollama.cli@0.1.1/generate` invoke.

The manifest requires `ollama` on `PATH`; Go is a build/test prerequisite only.
- Secrets: none declared by the plugin.
- Filesystem: grant the run artifact/evidence paths and prompt paths used by the workflow.
- External endpoints: local Ollama normally listens at `http://127.0.0.1:11434`; no external network grant is needed unless the local Ollama installation delegates to a remote service.

## Prerequisites

- [Ollama](https://ollama.com) installed and available in `$PATH`
- At least one model pulled: `ollama pull qwen3:8b`
- Go 1.22+ to build the plugin binary
- Darwin lifecycle observation requires cgo, Apple's C SDK/compiler, and system
  `libproc`; no third-party Go dependency or explicit unsafe Go import is used.
  Linux amd64/arm64 uses procfs and pidfds (kernel 5.3+), including with cgo disabled.
  Unsupported builds/denied native observation fail closed before execution.

## Build

From the repository root, build inside the package's Go module:

```bash
sh plugins/ollama.cli/build.sh /absolute/unused/path/ollama.cli
```

The output path must not exist. On macOS the build verifies the executable's OS
signature, repairs an invalid signature with an ad-hoc signature, and verifies it
again before publishing the output. This uses no publisher private key. Complete
OS signing before computing the package digest or creating a Transist publisher
signature; never modify a signed or installed payload in place.

## Install

From the repository root:

```bash
mkdir -p plugins/ollama.cli/bin
sh plugins/ollama.cli/build.sh "$PWD/plugins/ollama.cli/bin/ollama.cli"
transist plugins install --path ./plugins/ollama.cli
```

## Demo

```bash
transist workflows check --path ./plugins/ollama.cli/workflow.yaml
transist workflows install --path ./plugins/ollama.cli/workflow.yaml
transist workflows check ollama_cli_demo@0.1.1 --inputs-file <inputs-file> --output readiness.json
transist workflows run ollama_cli_demo@0.1.1 --run-ref demo --inputs-file <inputs-file> --readiness-file readiness.json
```

## Actions

### `generate`

Run a prompt against a local Ollama model and return the response.

**Input** (`schemas/generate.input.schema.json`):

| Field | Type | Required | Description |
|---|---|---|---|
| `prompt` | string | one of `prompt` / `prompt_file` | Inline prompt text |
| `prompt_file` | string | one of `prompt` / `prompt_file` | Path to file containing the prompt |
| `model` | string | yes | Ollama model name (e.g. `qwen3:8b`) |
| `timeout_seconds` | integer | no | Execution timeout in seconds (default: 300) |
| `timeout_ms` | integer | no | Execution timeout in milliseconds |
| `output_mode` | enum | no | `legacy` (default) or `compact`; null/empty invalid |
| `max_prompt_bytes` | positive integer | no | Raw UTF-8 prompt byte budget; absent means no caller limit |
| `max_output_bytes` | positive integer | no | Aggregate stdout+stderr capture budget; default 1,048,576, supported maximum 4,194,304 |

**Output** (`schemas/generate.output.schema.json`):

| Field | Type | Description |
|---|---|---|
| `ok` | boolean | `true` if `ollama run` exited with code 0 |
| `response_text` | string | Trimmed stdout from `ollama run` |
| `response_file` | string | Always empty string (reserved for future use) |
| `stdout` | string | Full raw stdout |
| `stderr` | string | Full stderr with ANSI sequences stripped |
| `exit_code` | integer | `ollama` process exit code (`-1` on timeout) |

### Bounded input and compact contract

The example workflow selects `output_mode: compact` and
`max_prompt_bytes: 98304`. Inline and file prompts are counted **before trimming**;
file reads use at most the configured budget plus one byte. Invalid UTF-8,
noninteger/nonpositive limits and prompt overflow fail before lookup or launch of
Ollama. `prompt_file` uses an ordinary OS path (relative to process cwd or absolute),
not `run://`, `repo://`, or `source://` URI resolution.

Compact keeps complete `response_text`, `response_file` (still the existing empty
reserved value), `ok`, and `exit_code`. It sets `output_mode: compact`, omits
`stdout`/`stderr`, and reports `diagnostics.logs` with logical `name`, original
UTF-8 `bytes`, and `truncated`. Its closed schema rejects inline logs even if empty.
There is no duplicate response artifact in compact mode. Legacy preserves complete
inline response/stdout/stderr; `response.txt` is included only when it fits the
256 KiB per-hint limit.

The aggregate capture guard counts stdout+stderr together, including line framing
and ANSI bytes, across all lines. The existing 4 MiB scanner ceiling is a separate
per-line guard, **not** the aggregate budget. Overflow stops execution using the
existing verified cleanup, returns explicit `validation_error`, and does not
expose the captured prefix as an authoritative response. `produced-evidence.json`
labels this case `capture_complete: false`. Bytes beyond the capture guard are
not retained; already captured evidence is preserved.

Full captured logs are saved once to `stdout.txt` and `stderr.txt` in
`InvokePaths.artifacts_dir`. Diagnostic inline hints can use a UTF-8-safe prefix
under `*.truncated.txt`, with original byte counts in metadata. Progress previews
are limited to 4 KiB and explicitly labeled when shortened. Complete APP results
are capped at 1 MiB including JSON escaping; business response text is never
truncated. If it cannot fit, the result fails explicitly and complete captured logs
remain in artifacts. Artifact promotion/retention is runtime-owned; oversize raw
files are not automatically promised as persisted inline hints.
These full files are invocation-private staging, not evidence of accepted durable
storage. Failure inventories label `storage` (`invocation_private`, or `not_written`
when no artifact directory was supplied) and `retention: not_guaranteed`.
With `max_prompt_bytes` supplied, unavailable prompt-file errors use a sanitized
`prompt_file` label rather than disclosing the path or OS error details.

`cmd/plugin/lean_io.go` is compiled into the selected `bin/ollama.cli` payload;
there is no dynamically loaded helper or new selected source-file dependency.
Rebuild the binary for these changes. No provider history or usage inference is
introduced; the existing cleanup and best-effort/no-usage behavior remains.

## Agent Controls

| Control | Field | Notes |
|---|---|---|
| Model | `model` | Required. Any model available via `ollama list`. |

## Notes

- Prompt is piped to `ollama run <model>` via stdin; the process receives EOF
  when the full prompt has been delivered, causing it to generate a response
  and exit.
- `response_file` is always an empty string; use `response_text` to access the
  model output in downstream steps.
- The plugin binary must be built before install. The binary is `.gitignore`d
  and is never committed to the repository.

## Native subprocess lifecycle

The CLI inherits the runtime's process group/session; the plugin never creates a
new one or sends a wholesale group signal. Before launch it protects its own PID,
parents/ancestors, and every existing group member. Native PID/birth/group
verification scopes TERM then KILL to newly observed invocation members, including
orphans and pipe-holding children. A preexisting Ollama service is protected; the
plugin does not enumerate process argv or environments. Runtime group isolation
is required: new processes joining that isolated group are invocation-owned.

Finalization runs on normal exit, nonzero exit, timeout and errors. Cleanup has
one four-second budget (two seconds TERM, then KILL); pipe reader finalization and
direct-child reaping share one additional second. The invocation goroutine alone
collects output and emits progress; readers only queue events and are stopped and
joined before returning. Scanner errors, interrupted output, PID reuse or denied
cleanup proof return `adapter_internal_error` rather than successful provider data.
Stdin uses an owned interruptible pipe writer, not an exec-managed copier. The
direct child is polled using native exit metadata and reaped only after exit proof;
there is no uncancellable `Wait` goroutine that can outlive a failed cleanup.
EOF draining uses the remaining one-second budget, reserving one 25ms poll tick
to interrupt/join workers if a pipe stays open. It no longer stops after 50ms.
Queued reader failures and observed nonzero exits take precedence over timer
scheduling; output verification failures remain internal errors even on timeout.
Ordinary timeout remains `timeout_error`; ANSI stripping, output fields, artifacts
and usage behavior are unchanged. No usage is inferred from subprocess metadata.

Darwin has no pidfd: identity/group are checked immediately before individual
signals, with the native check-to-signal race inherent to that OS. Linux pins the
identity with pidfds. These checks do not track a deliberately escaping provider
that creates its own group/session; runtime containment remains authoritative.

The plugin reads `PATH` to resolve Ollama and forwards its inherited environment
unchanged. `PATH` is required and must be operator/runtime-controlled. Provider
settings such as `OLLAMA_HOST`, `OLLAMA_MODELS` and `HOME` are optional inherited
provider controls, with Ollama's defaults when absent; runtime policy must permit
only intended endpoint/state paths. `TMPDIR`, when inherited, is forwarded for
provider temporary work and should be runtime-owned. Other inherited variables
are also forwarded, not inspected for process ownership; inject/redact credentials
externally according to runtime policy, never through fixture logs.

## Offline verification — 2026-10-09

```sh
# From plugins/ollama.cli:
TMPDIR=/tmp GOCACHE=/tmp/ollama-review-go-cache go test -race -count=1 -v ./cmd/plugin > /tmp/ollama-lifecycle-tests.log 2>&1
TMPDIR=/tmp GOCACHE=/tmp/ollama-review-go-cache go vet ./cmd/plugin
gofmt -d cmd/plugin
# From the repository root, with tests/requirements.txt available:
TMPDIR=/tmp GOCACHE=/tmp/ollama-review-go-cache python3 -B plugins/ollama.cli/tests/smoke.py > /tmp/ollama-lifecycle-smoke.log 2>&1
TMPDIR=/tmp python3 -B -m unittest discover -s plugins/ollama.cli/tests -p 'test_*.py' -v > /tmp/ollama-smoke-cleanup-tests.log 2>&1
```

Native tests use an isolated **outer test session**, with fake Ollama executables
inheriting its group. They cover children/orphans/held pipes, TERM resistance,
timeout, blocked stdin, nonzero exit, ANSI output, reader failure, launch failure,
denied observation/finalization, PID reuse, protected service survival and native
signal identity rejection. macOS arm64 race tests and Linux arm64 native tests
are recorded separately; Linux uses a cgo-disabled cross-built test executable
in the existing `transist-publication-linux-unit:local` container, with no network.
Linux log: `/tmp/ollama-lifecycle-linux-tests.log`.

These are historical development checks, not final public-runtime v0.1.0
acceptance. The complete both-platform release gate is [recorded separately](../../docs/validation.md).

| Check | Historical result |
| --- | --- |
| macOS arm64, Go 1.22.5, `go test -race -count=1 -v ./cmd/plugin` | 6 top-level tests, 12 lifecycle + 4 APP subcases passed; no race reports |
| macOS arm64, normal `go test -c` native test binary | Same 6 top-level tests and 16 subcases passed after temporary OS signature repair |
| Linux arm64, cgo disabled, offline container | Same 6 top-level tests and 16 subcases passed |
| `go vet ./cmd/plugin` / `gofmt -d cmd/plugin` | Passed / empty diff |
| Linux amd64 cross-compile / Darwin cgo-disabled build | Passed; amd64 execution and cgo-disabled Darwin invocation not verified |
| Compiled APP smoke | All 6 paths passed using default `go build`, fake Ollama, and temporary OS signature repair |
| Smoke harness cleanup regressions | 3 passed: deadline with inherited pipes, invalid JSON, and BaseException cancellation |

APP fixture tests invoke the actual `main` path in the native test binary and
assert canonical success/nonzero/timeout/internal errors, ANSI output and exactly
one final result with no subsequent frames. Native regressions additionally retain
all 180 queued ANSI-stripped lines under a slow callback and preserve a nonzero
exit when its output callback crosses the timeout deadline. macOS race linking emitted an
`LC_DYSYMTAB` linker warning; the test executable ran successfully.

The earlier standalone SIGKILL blocker is resolved as a Go/cgo + Apple linker OS
signature issue, not a runtime execution bug. Unique default and external-linker
builds both failed `codesign --verify --strict` with an invalid embedded ad-hoc
signature; temporary `codesign --force --sign -` repaired them. The empty-stdin
probe then exited normally with code 1. See `/tmp/ollama-signature-review.log`.
External linking alone did not resolve this host's issue.
The normal native test binary was independently compiled under a unique `/tmp`
directory, signature-repaired and run with `-test.v`; all tests passed. See
`/tmp/ollama-lifecycle-native-normal-tests.log`.

The smoke builds uniquely under `/tmp` using `build.sh` before testing actual APP
execution. The runtime sources do not invoke signing or change trust. Ad-hoc OS
signing is distinct from Transist publisher signing and grants no publisher trust.
The harness always kills its isolated outer fixture group, closes pipes and reaps
the leader on success, JSON errors, cancellation and deadlines. Production
subprocess cleanup continues to use individual native-verified signals.

This is offline verification, not installed or live Ollama acceptance. No real
model/service, Transist publisher signing, commit or push was performed. Version
is `0.1.0`; existing publisher signatures do not cover changed package bytes.
