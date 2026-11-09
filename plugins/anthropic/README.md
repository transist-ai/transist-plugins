# Anthropic Messages provider

`anthropic@0.1.0` is an external APP/process plugin exposing `chat_complete`
through the native, non-streaming Anthropic Messages API. Requires Python 3.10+
on a Unix platform with `SIGALRM`/`setitimer`. Uses only the standard library,
with no SDK, sibling runtime dependency, child process or temporary storage.

## Inputs and native mapping

| Input | Contract / default |
| --- | --- |
| `model` | Required nonblank string, at most 256 characters, no C0/C1 controls |
| `system` | Optional nonblank text, at most 65,536 UTF-8 bytes; top-level API `system` |
| `messages` | 1–64 objects containing exactly `role` and `content` |
| Message `role` | `user` or `assistant`; conversation must start and end with `user` |
| Message `content` | Nonblank string, at most 65,536 UTF-8 bytes; mapped to one native text block |
| `max_tokens` | Integer 1–4096 (booleans rejected), default 256 |
| `timeout_ms` | Integer 1–120000, default 30000; transport budget only |
| `base_url` | Origin only, default `https://api.anthropic.com` |
| `secret_refs` | If supplied, exactly `[ANTHROPIC_API_KEY]`; required in workflows |

Unknown action/message fields are rejected. System roles, assistant prefill,
array content, tools, stop sequences, streaming, temperature, thinking and beta
controls are unsupported. History ordering, consecutive same-role turns and all
supplied text are preserved, without trimming, merging or synthetic messages.
Only `model`, optional `system`, native `messages`, `max_tokens` and `stream: false`
are sent. No workflow/user identifiers or secret references go upstream.

Draft 2020-12 schemas describe the structural contract. Additional runtime checks
enforce UTF-8 encodability/byte limits, the final user role, origin validity and
the **256 KiB serialized UTF-8 request** limit before DNS or connection dispatch.

## Console setup and runtime secrets

1. Sign in to the [Anthropic Console](https://console.anthropic.com/), select the
   single workspace that will own this integration, and configure API billing
   and any required account permissions.
2. Open that workspace's API Keys settings and create an API key. Use a model
   available to that workspace. **API credentials and billing are separate from
   Claude consumer subscriptions**; a Claude subscription is not an API key.
3. Have your external secret manager or secure launcher supply the key as
   `ANTHROPIC_API_KEY` in the environment of the Transist process. Do not put the
   value in shell command arguments, workflow inputs, YAML, prompt files or logs.
   Fresh homes use the environment secret provider: the runtime resolves the
   named reference from its own environment, rather than relying on unrestricted
   environment inheritance into the plugin.
4. In the explicitly selected home's `config.yaml`, merge this policy fragment
   into the existing `policy` mapping; retain the other policy settings:

   ```yaml
   policy:
     remote_secret_delivery:
       process: true
       http: false
       mcp: false
   ```

5. Declare `with.secret_refs: [ANTHROPIC_API_KEY]` on the invoke step (the demo
   already does). The manifest requires a secret named `ANTHROPIC_API_KEY`, with
   `binding: {type: env, name: ANTHROPIC_API_KEY}`. The process adapter injects the
   resolved value into that environment binding and applies runtime redaction.
   If secret allowlists are configured, they must allow this exact reference.

P0 requires a **single-workspace Console credential**; there is no workspace
selection input/header or cross-workspace routing feature. Missing, blank,
whitespace-bearing, control-bearing or non-ASCII credentials fail with
`secret_resolution_error`.

## Runtime Capabilities And Endpoints

- Adapter: `process`; plugin `anthropic@0.1.0`; action `chat_complete`, invoked as
  `process:anthropic/chat_complete`; provider kind: `external`. A local-only run
  cannot dispatch this action.
- Governed policy must grant this version-pinned plugin/action, resolution and
  delivery of `ANTHROPIC_API_KEY`, and the exact destination network capability.
  Use the installed workflow's input-bound readiness report and
  `workflows authorize` commands below to apply missing eligible authority.
- No workspace or user filesystem access is required by the action. The plugin
  reads its installed payload, uses no child process or temporary storage, and
  emits usage as an artifact hint for runtime persistence.
- Official endpoint: `https://api.anthropic.com:443/v1/messages`.
- An input-defined `base_url` override always targets `<origin>/v1/messages`;
  its host/port must have matching policy authority. This is for loopback tests
  or explicitly approved compatible deployments, not Bedrock/Vertex routing.

Require effective `policy.network` access to `api.anthropic.com:443`, normally
with `mode: allowlist`,
`allow_hosts: [api.anthropic.com]`, `allow_ports: [443]`, `tls_required: true`.
Only `allowlist` and `loopback` modes are accepted. Nonempty host/port restrictions
and `tls_required` apply to both. Allowlist mode requires a matching host.

## Transport, cancellation and retries

Every invocation makes at most one POST to exactly `/v1/messages`, sending
`Authorization: Bearer <resolved credential>`, `anthropic-version: 2023-06-01`,
`Content-Type: application/json`, `Accept: application/json`, and
`Accept-Encoding: identity`. No API-version override, beta header or `x-api-key`
fallback is supported.

Overrides must be an HTTPS origin with an empty path or `/`, no userinfo, query,
fragment, malformed port or controls. HTTP is allowed only for literal loopback
addresses or `localhost`, subject to policy. Hosts are normalized; all DNS
answers must be loopback for loopback destinations or globally routable,
non-multicast otherwise. Mixed safe/unsafe answers are rejected. One validated
address is pinned; TLS still verifies the hostname using the default trust store.
Proxies are ignored and redirects are never followed.

The smallest action/APP `timeout_ms`/APP `max_timeout_ms` is a single absolute
deadline starting before DNS. DNS waiting, connect, TLS, send, headers and body
are bounded; small repeated reads cannot extend it. DNS runs in a daemon thread:
uncancellable libc resolution may finish later but owns no credential/socket and
cannot dispatch HTTP. Deadline timers and handlers are removed/restored on exit.
SIGTERM interrupts waiting/I/O, closes the active connection and emits `cancelled`
if stdout is still available. Connections are closed on every exit path.
There are no provider retries; Transist workflow policy owns retries.

## Outputs and usage

Only HTTP 200 is success. A successful body must be a native `message` with
`role: assistant` and nonempty content. Every block needs a nonempty string
`type`; text blocks need string `text`. Their ordered texts are concatenated
without separators into nonblank `response_text`. Non-text blocks are ignored:
this is a **text projection**, not a complete native message lifecycle.
`tool_use` and `pause_turn` stop reasons fail with `protocol_error`.

`stop_reason` becomes optional `finish_reason`, preserving well-formed additive
values. Optional `id`, `model` and HTTP `request-id` become `response_id`,
`response_model` and `request_id`. Missing/null metadata is omitted. Malformed
stop reasons fail; unsafe IDs are omitted (only `[A-Za-z0-9_-]{1,256}` survives).
Unknown response fields are tolerated. Bodies are bounded to **1 MiB**; oversized,
truncated, malformed or structurally invalid successes fail safely.

Complete nonnegative integer `usage.input_tokens`/`output_tokens` produces APP
usage with their sum, `provider: anthropic`, and response model or input model.
Each count and the sum must fit unsigned 64-bit; booleans are invalid. Invalid or
partial usage is omitted entirely. Valid usage also emits the standard
`usage.json` artifact hint. Absent APP `usage`/`error` fields are omitted;
`metrics: {}` leaves timing to Transist. No evidence hints or health are exposed.

## Errors and confidentiality

| Failure | APP code |
| --- | --- |
| Invalid input; HTTP 400, 404, 413, 422 | `validation_error` |
| Invalid endpoint or network policy/DNS safety denial | `configuration_error` |
| Missing or invalid credential | `secret_resolution_error` |
| HTTP 401, 403 | `auth_error` |
| HTTP 429 | `rate_limit_error` |
| HTTP 408, 504 or local deadline | `timeout_error` |
| Other HTTP failures, including redirects, 500, 503, 529 | `tool_execution_error` |
| DNS/connect/TLS/socket failures | `transport_error` |
| Invalid success or unsupported continuation | `protocol_error` |
| SIGTERM cancellation | `cancelled` |

Errors use canonical APP class/retryability. Diagnostics contain fixed messages
and HTTP status only, optionally a validated request ID (header first, bounded
error-body fallback). Body parsing failures retain the known HTTP classification.
Raw provider messages, bodies and exception text are never forwarded. The raw
credential and padded/unpadded standard/URL-safe Base64 variants are redacted
from emitted strings, including reflected text, metadata and artifact contents.
Stdout contains APP frames only; normal invocations emit no stderr or prompt,
response or header logs.

## Environment

| Variable | Classification / behavior | Runtime policy |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Required; read from injected environment; no default | Allow process secret delivery, inject via binding, redact |
| `ANTHROPIC_TEST_MODEL` | Required for manual live smoke only; externally supplied model, no default; read by runner, not forwarded to plugin | No runtime injection needed |
| `REQUIRE_LIVE_PROVIDER` | Optional live-runner control; `1` makes missing live configuration fail, otherwise skip; not forwarded | No runtime injection needed |
| `ANTHROPIC_MODEL`, `ANTHROPIC_PROMPT` | Required only for the demo commands below; externally supplied nonblank model/prompt, no defaults; shell expands them into workflow inputs, not plugin environment bindings | No environment injection needed; prompt is ordinary run data, not a secret |
| `TRANSIST_HOME` | Optional CLI home selector; no value required here because commands use explicit `--home` | Runtime configuration, not an action binding |
| `TRANSIST_TRUSTED_EXECUTION` | Optional CLI trust-mode control; leave unset for the explicit standalone example; governed example uses trusted execution | Runtime configuration, not an action binding |
| `PATH` | Required launcher environment for `python3` discovery; use an approved Python 3.10+ installation and the installed Transist/admin binaries | Runtime must permit prerequisite discovery; not read as action input |
| `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` (and lowercase variants) | Optional inherited values; ignored by direct transport | No injection/allowance needed |
| `SSL_CERT_FILE`, `SSL_CERT_DIR` | Optional inherited OpenSSL trust-store overrides read while loading default CA trust; system trust defaults | Allow only operator-approved trust configuration |
| `SSLKEYLOGFILE` | Optional inherited value; ignored; TLS session-key logging is not enabled | No injection/allowance needed |
| `TMPDIR` | Optional inherited; not used by this plugin | No plugin-specific allowance needed |
| `PYTHONDONTWRITEBYTECODE` | Optional Python interpreter control; E2E sets `1` to avoid cache writes; no action-level default or direct plugin read | No secret injection/redaction needed |

No plugin-managed environment variables are created or forwarded to children.
The E2E harness sets `TMPDIR` for its disposable tools; the plugin itself does
not use it.

## Package and local validation

This package is distributed as a **source directory**, not a provider SDK or
pre-signed archive. The selected runtime payload is `plugin.yaml` plus exactly
`plugin.py` and the two action schemas in `package.files`. The
`plugin_dir_sha256` digest and detached signature cover only that selected
payload; README/tests/demo workflow are excluded and not installed as runtime
dependencies. The demo must be installed or packaged separately as a workflow.
This source package ships no signature.

Published and installed version payloads are immutable. This repository fixes
maintained releases at `0.1.0`; changes to selected bytes or `package.files`
require a new digest and matching signature, validated in a fresh home. Never
overwrite `anthropic@0.1.0` in place. Documentation-only changes do not change
its selected payload. Prepared runs pin installed plugin bytes.

The lifecycle commands below target public **Transist/admin v0.1.0**.
Current both-platform release validation is [recorded separately](../../docs/validation.md).
Check `transist --version` and
`transist-admin --version` before following them. Keep private signing keys
outside the source package and never commit generated runtime data.

From this package directory (`plugins/anthropic`), run the targeted local checks:

```sh
python3 -B -m unittest discover -s tests -p 'test_*.py'
python3 -B tests/smoke.py
python3 -B tests/e2e.py
git diff --check -- .
```

The smoke runner checks the exact selected payload, manifest/handshake identity
and capabilities, Draft 2020-12 schema shapes, and a canonical invalid-input APP
result in a real subprocess. It also tests the system-only `PATH` used by
runtime readiness probes. Postponed annotation evaluation lets those
probes handshake on macOS's system Python 3.9; use Python 3.10+ for invocation.

The deterministic E2E runner requires verified public **Transist and transist-admin 0.1.0**,
Python 3.10+, and working platform sandbox support. Set explicit `TRANSIST_BIN`,
`TRANSIST_ADMIN_BIN` and disposable `TRANSIST_FIXTURE_BASE`.
Missing prerequisites or sandbox failures are errors, never skips.
No real provider credential or internet access is needed. The silent server
binds only to `127.0.0.1` and verifies authentication as booleans without retaining
authorization header values.

E2E covers byte-exact native requests with system/history and Unicode, ordered
text projection, optional metadata/usage omission, canonical usage and
checksummed artifact persistence, unsupported continuations, HTTP status
mappings, redirect refusal, deadlines (including trickling headers and bodies),
malformed/invalid/truncated/oversized responses, policy denial before dispatch,
and SIGTERM cancellation with active connection closure. Allowed invocations
must send exactly one request. SIGTERM and both APP timeout minima are tested
directly against the real subprocess transport; provider cases run through
installed workflows and poll persisted run inspection for terminal outcomes.

Each test uses fresh standalone homes with only process secret delivery enabled.
It installs, lists and inspects the package, installs generated workflows and
checks readiness, and scans **all regular home files** and emitted CLI/APP data
for raw and padded/unpadded standard/URL-safe Base64 credential canaries,
including deliberately reflected provider output.

The trusted lifecycle generates an ephemeral key, signs a copied selected
payload, verifies/imports public trust, installs in a separate governed home,
packages/signs a workflow and executes a trusted loopback invocation. A modified
selected file must fail signature verification and installation in another
fresh home. Failed resumable runs are cleaned up before uninstall; removal is
checked through listing and inspection. Homes, fixtures and signing material
are removed automatically.

All test temporary directories are explicitly created under `/tmp`, with paths
canonicalized for Transist on macOS. This does not change the plugin's
no-temporary-storage behavior. Capture check output outside the package when
keeping test evidence.

## Demo workflow

`workflow.yaml` defines `anthropic_demo@0.1.0`, pinned to `anthropic: 0.1.0`, with
required `model` and `prompt` inputs and a fixed system instruction. Root network
access is denied; only the `ask` step permits TLS to `api.anthropic.com:443`.
It binds `ANTHROPIC_API_KEY` through `with.secret_refs`. Configure that runtime
secret externally and allow process secret delivery before checking readiness.

### Standalone install and run

From this package directory, initialize an explicitly selected standalone home
(choose a fresh local directory):

```sh
transist --home ./demo-home home init --mode standalone
```

Apply the process secret-delivery configuration above to
`./demo-home/config.yaml`. Supply the credential externally, plus nonempty
`ANTHROPIC_MODEL` and `ANTHROPIC_PROMPT` in your shell environment. Then:

```sh
: "${ANTHROPIC_MODEL:?Supply a workspace-accessible model externally}"
: "${ANTHROPIC_PROMPT:?Supply a non-secret prompt externally}"
transist --home ./demo-home plugins install --path ./plugin.yaml
transist --home ./demo-home plugins list
transist --home ./demo-home plugins inspect anthropic
transist --home ./demo-home workflows check --path ./workflow.yaml \
  --input model="$ANTHROPIC_MODEL" --input prompt="$ANTHROPIC_PROMPT"
transist --home ./demo-home workflows install --path ./workflow.yaml --approve
transist --home ./demo-home workflows check --workflow anthropic_demo@0.1.0 \
  --input model="$ANTHROPIC_MODEL" --input prompt="$ANTHROPIC_PROMPT"
transist --home ./demo-home workflows run anthropic-demo --workflow anthropic_demo@0.1.0 \
  --input model="$ANTHROPIC_MODEL" --input prompt="$ANTHROPIC_PROMPT"
transist --home ./demo-home runs show anthropic-demo
```

Run only after check reports `ready`. If check reports eligible missing
authority, authorize with the same inputs and check again:

```sh
transist --home ./demo-home workflows authorize --workflow anthropic_demo@0.1.0 \
  --input model="$ANTHROPIC_MODEL" --input prompt="$ANTHROPIC_PROMPT" --approve
transist --home ./demo-home workflows check --workflow anthropic_demo@0.1.0 \
  --input model="$ANTHROPIC_MODEL" --input prompt="$ANTHROPIC_PROMPT"
```

`ANTHROPIC_MODEL`/`ANTHROPIC_PROMPT` are example-command variables, not plugin
environment inputs. A run sends the prompt to Anthropic and may incur API costs.
Poll `runs show anthropic-demo` to a terminal state and require `success`; a CLI
exit code alone does not establish provider success. Use a new run reference for
another invocation. Inspect privately: workflow inputs and model output are run
data even though the plugin does not log them.

### Governed signing, trust and run

Unsigned standalone installation does not need signing. A governed home requires
both a signed plugin and a signed workflow package, plus an approved public trust
root. From this package directory, this publisher-side example creates local
signing material in a separate directory; for production use an approved
publisher key and distribute only the public trust root and signed packages:

```sh
mkdir -p ../anthropic-demo-release
transist-admin key generate --name anthropic-demo \
  --private-key-file ../anthropic-demo-release/signing.key
transist-admin key export-public --private-key-file ../anthropic-demo-release/signing.key \
  --trust-root-file ../anthropic-demo-release/trust.pub
transist-admin plugin sign . --private-key-file ../anthropic-demo-release/signing.key \
  --output ../anthropic.sig
transist-admin plugin verify . --signature ../anthropic.sig \
  --trust-root-file ../anthropic-demo-release/trust.pub
transist-admin workflow package ./workflow.yaml --version 0.1.0 \
  --private-key-file ../anthropic-demo-release/signing.key \
  --output ../anthropic-demo-release/workflow-package
transist --home ./governed-home home init --mode governed
transist --home ./governed-home trust import ../anthropic-demo-release/trust.pub
transist --home ./governed-home plugins install --path ./plugin.yaml --trusted-execution
transist --home ./governed-home plugins list
transist --home ./governed-home plugins inspect anthropic
transist --home ./governed-home workflows install \
  --package ../anthropic-demo-release/workflow-package --approve
```

The default plugin signature sidecar is `<plugin-directory>.sig` (here
`../anthropic.sig` when the directory is named `anthropic`). If the publisher
stores it elsewhere, pass `--signature <sidecar-path>` to plugin installation.
Verification checks selected bytes; tampering invalidates the signature. Trust
import approves the public signer, not unsigned packages.

Enable process secret delivery in `./governed-home/config.yaml` as above and
supply the same external credential/model/prompt. Check and apply only eligible
missing authority using the exact bound inputs:

```sh
transist --home ./governed-home workflows check --workflow anthropic_demo@0.1.0 \
  --input model="$ANTHROPIC_MODEL" --input prompt="$ANTHROPIC_PROMPT"
# If check reports eligible missing authority:
transist --home ./governed-home workflows authorize --workflow anthropic_demo@0.1.0 \
  --input model="$ANTHROPIC_MODEL" --input prompt="$ANTHROPIC_PROMPT" --approve
transist --home ./governed-home workflows check --workflow anthropic_demo@0.1.0 \
  --input model="$ANTHROPIC_MODEL" --input prompt="$ANTHROPIC_PROMPT"
# Only after status is ready:
transist --home ./governed-home workflows run anthropic-trusted-demo \
  --workflow anthropic_demo@0.1.0 --trusted-execution \
  --input model="$ANTHROPIC_MODEL" --input prompt="$ANTHROPIC_PROMPT"
transist --home ./governed-home runs show anthropic-trusted-demo
```

To uninstall a version, select the home explicitly:

```sh
transist --home ./demo-home plugins uninstall anthropic --version 0.1.0
transist --home ./governed-home plugins uninstall anthropic --version 0.1.0
```

Failed resumable runs can still pin the package. If removal reports a run pin,
finish or explicitly clean up that run with `transist --home <home> runs cleanup
<run-ref>`, wait for cleanup completion in `runs show`, and retry uninstall.

## Manual live smoke and CI

Supply `ANTHROPIC_API_KEY` and `ANTHROPIC_TEST_MODEL` externally in the environment,
then run from this package directory:

```sh
python3 -B tests/live.py
REQUIRE_LIVE_PROVIDER=1 python3 -B tests/live.py
```

The local runner skips with a fixed message if either value is absent or empty;
`REQUIRE_LIVE_PROVIDER=1` turns that into failure. It uses no credential files,
model defaults or endpoint overrides. It sends exactly one native request to
`https://api.anthropic.com/v1/messages`, with one short user prompt,
`max_tokens: 16`, and `timeout_ms: 30000`; the APP subprocess is bounded to 45
seconds. Success requires nonblank response text, without checking exact wording.
There are no retries. Only fixed pass/skip/failure messages are printed; provider
output, APP errors, credentials, subprocess stderr and exception text are never
displayed. Timeout and other subprocess failures produce a nonzero exit status.

GitHub Actions runs static structure/schema, formatting/lint and secrets checks
only. Anthropic unit tests, APP smoke, deterministic E2E and real live smoke are
explicit local checks. CI does not install Transist, start the plugin, or need
Anthropic credentials.

## Troubleshooting

| Symptom | Check / action |
| --- | --- |
| `secret_resolution_error` or readiness secret denial | Supply `ANTHROPIC_API_KEY` to the Transist parent process, keep `with.secret_refs`, enable process delivery and allow the exact secret reference. Do not print the credential to diagnose it. |
| HTTP 401/403 (`auth_error`) | Check Console key validity/revocation, selected workspace, API permissions and API billing. A consumer Claude subscription does not grant API access. |
| Invalid model / HTTP 400 or 404 (`validation_error`) | Use the exact model ID available to the key's Console workspace; P0 has no discovery or fallback. Check roles and supported fields too. |
| Oversized request / HTTP 413 (`validation_error`) | Reduce prompt/history: 64 messages, 65,536 UTF-8 bytes per text field and 256 KiB for the serialized body are independent limits. Raising `max_tokens` does not increase them. |
| HTTP 429 (`rate_limit_error`) | Check workspace rate/spend limits, reduce concurrency and configure bounded workflow backoff. The plugin does not retry. |
| HTTP 500/503/529 (`tool_execution_error`) | Provider failure/overload; check provider status and use bounded workflow retries if appropriate. No raw provider body is exposed. |
| `timeout_error` / HTTP 408 or 504 | Check connectivity and provider latency. Adjust action timeout up to 120000 ms and any tighter APP limits; all phases share one deadline. |
| `configuration_error` / capability denial | Check exact host/port/TLS policy and input-bound grants. Root network denial is intentional; only the demo's `ask` step allows the official endpoint. Proxies cannot bypass policy. |
| `transport_error` | Check DNS, verified TLS trust and direct connectivity; proxy-only environments require an approved direct route. |
| `protocol_error` | The response must contain nonblank projected text within 1 MiB. Tool-use/pause continuations are unsupported; use the P0 non-streaming text contract. |

Diagnostics retain only fixed text, HTTP status and safe request IDs. Use a safe
request ID for provider support; never attach credentials or unredacted run data.

## P1 and non-goals

P1 requires separate plans: SSE streaming via APP progress; native client tool
use with explicit schemas and tool-result round trips; richer content, thinking,
caching and structured output. None is implemented in P0. Managed agents,
server-side tools, model discovery/fallback, Files/Batches APIs, Bedrock/Vertex
and administrative APIs are initial-release non-goals.
