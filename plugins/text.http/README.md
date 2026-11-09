# text.http

Minimal local `http` adapter example: `text.http@0.1.0/analyze` computes
deterministic statistics for inline text. No account, model, external provider,
secret, path resolver, file access action, or outbound service traffic is used.
The service uses only Python's standard library (Python 3.10+).

## Contract

Input is an object with required string `text` and optional integer `maxBytes`
(default 4096, range 0–4096). Unknown fields, including path and secret fields,
are rejected. The input schema limits text to 4096 Unicode code points; the
service additionally enforces the UTF-8 byte bound. Unpaired surrogates are
rejected. URI-shaped text is literal text, never a file reference.

Output contains:

- `bytes`: UTF-8 encoded length, at most 4096.
- `chars`: Unicode code point count, not grapheme count.
- `lines`: zero for empty text; otherwise one plus the number of LF (`\n`)
  characters. A trailing LF adds an empty final line; CR is ordinary text.
- `sha256`: lowercase hexadecimal SHA-256 of the exact UTF-8 bytes, without
  normalization or newline conversion.

`workflow.yaml` explicitly analyzes `"Hello, 世界!\n"`. Expected output is
15 bytes, 11 characters, 2 lines, and the SHA-256 of those exact bytes.
Successful results have empty hints and metrics; `usage` and `error` are omitted,
never emitted as null. Invalid action input returns HTTP 200 with an
`app.invoke.result.v1` failure, the request's `invoke_id`, empty output, and
canonical `validation_error` (`class: validation`, `retryability: none`).

## Start the service separately

Run from the repository root in one terminal:

```sh
env -i "$(command -v python3)" -I plugins/text.http/service.py
```

The fixed published URL is **http://127.0.0.1:18765**. The listener always binds
IPv4 loopback; there is no public-host switch. `--port` is available for local
tests; changing it requires matching manifest and workflow endpoint/port edits.
Stop the foreground service with Ctrl-C.

**Installing the plugin does not start the server.** `service.py` is selected
in `package.files` for source convenience; the HTTP adapter only connects to
the configured URL. The installed service is managed separately from Transist.
No binary prerequisite is declared because Python runs on the service operator's
side, not as an adapter-launched process.

## Install and run the deterministic demo

Use a disposable standalone home outside the current workspace. From the
repository root, while the service is running:

```sh
DEMO_HOME="$(mktemp -d /tmp/text-http-demo.XXXXXX)"
transist home init --home "$DEMO_HOME" --mode standalone
transist plugins install --home "$DEMO_HOME" --path plugins/text.http/plugin.yaml
transist plugins list --home "$DEMO_HOME"
transist plugins inspect --home "$DEMO_HOME" text.http
transist workflows check --home "$DEMO_HOME" --path plugins/text.http/workflow.yaml
transist workflows install --home "$DEMO_HOME" --path plugins/text.http/workflow.yaml --approve
transist workflows check --home "$DEMO_HOME" --workflow text_http_demo@0.1.0
transist workflows run text-http-demo --home "$DEMO_HOME" --workflow text_http_demo@0.1.0 --local-only
transist runs show --home "$DEMO_HOME" text-http-demo
transist plugins uninstall --home "$DEMO_HOME" text.http --version 0.1.0
```

This unsigned example uses standalone mode. Governed installation additionally
requires operator-managed signatures and trust; no trust keys are bundled.
Only the manifest, service source and two action schemas are selected for
installation. README, tests and demo workflow are separate publication assets.

## HTTP and policy boundary

The current adapter uses bodyless `GET /app/handshake` and JSON
`POST /app/invoke`. For direct APP conformance testing the service also accepts
the exact `app.handshake.request.v1` JSON via `POST /app/handshake`.
`GET /app/health` returns HTTP 200 JSON `{"status":"ok"}` and health is
advertised. Cancellation is not advertised or implemented.

The service processes one connection at a time, with a hard three-second
connection deadline covering headers/body/response, a 65536-byte request-body
limit, and the standard library's bounded request line and header parsing.
It requires a single decimal Content-Length and JSON media type, rejects
chunked/compressed bodies, duplicate JSON keys, non-JSON numeric constants,
malformed envelopes and unknown routes. Malformed JSON/APP envelopes return a
generic HTTP 400 JSON response without echoing text; action validation failures
use HTTP 200 APP failed results. Oversized bodies return 413 and
unsupported content types/framing return 415. Slow peers are disconnected.
No request text or identifiers are logged. This is a small serial local example,
not a production multi-tenant server; loopback clients can invoke it without
authentication.

The workflow declares `network.mode: loopback`, the exact HTTP origin and port
18765. `mode: none` denies even handshake connections. HTTP network enforcement
is the **runtime-to-service boundary**, not transitively the service's own
traffic. This service makes no outgoing connections; it does not use policy
metadata to grant itself authority. APP `paths`, policy and requested capability
metadata are observational and never used to access paths or widen permissions.

## Environment

The service reads no application environment variables, forwards none, starts
no child processes and needs no credentials. The `env -i ... -I` startup above
avoids inherited credentials/proxies and Python environment injection.

| Variable | Classification / purpose | Safe value and handling | Runtime policy |
| --- | --- | --- | --- |
| `PATH` | Optional operator/test command discovery | Resolve Python/Transist before isolation; E2E creates `os.defpath` for CLI children | No service env allow/inject/redact needed |
| `HOME` | Test-managed CLI user state | E2E creates a disposable directory and supplies it only to CLI children | No service env allow/inject/redact needed |
| `TRANSIST_HOME` | Optional CLI runtime selection; test-managed in E2E | Disposable standalone home; demo uses explicit `--home` instead | CLI-only, not forwarded to service |
| `TMPDIR`, `TEMP`, `TMP` | Optional Python test temporary-directory selection | OS default; Python tempfile may read these in the test parent, not the service | Test-only, not forwarded to CLI children |

Python itself can interpret `PYTHON*` variables when started without `-I`;
the documented service command ignores them. No provider variables should be
inherited by the service. The E2E CLI child environment is exactly `PATH`,
`HOME`, and `TRANSIST_HOME`, with no provider credentials or proxy variables.

## Offline validation

Tests require `jsonschema` (verified with 4.26.0); the service does not. APP
schema tests use the repository's pinned `tests/contracts/app/` contracts,
without remote schema resolution. E2E targets verified public Transist/admin
v0.1.0, with explicit `TRANSIST_BIN`, `TRANSIST_ADMIN_BIN` and
`TRANSIST_FIXTURE_BASE`. Historical development-build evidence does not prove
public-release acceptance; the final both-platform gate is
[recorded separately](../../docs/validation.md). Tests are self-contained under this package.

```sh
python3 -m unittest discover -s plugins/text.http/tests -p 'test_*.py' -v > /tmp/text-http-tests.log 2>&1
python3 plugins/text.http/tests/e2e.py > /tmp/text-http-e2e.log 2>&1
black --check plugins/text.http > /tmp/text-http-format.log 2>&1
flake8 --max-line-length=88 plugins/text.http > /tmp/text-http-lint.log 2>&1
```

Wire tests cover actual HTTP, authoritative APP/action schemas, deterministic
Unicode results, byte bounds, path-field rejection, malformed JSON/envelopes,
HTTP framing/content types, oversized requests and deadline recovery. E2E starts
the supplied service on an ephemeral loopback port, adjusts disposable package
copies, installs the plugin and workflow, checks readiness, runs with
`--local-only`, rejects path input before transport, verifies a schema-valid
`text: "é", maxBytes: 1` invocation persists canonical `validation_error`, verifies no service calls
under `network: none`, and injects a
schema-invalid success output to prove adapter rejection without retaining it.
The temporary home/listener are cleaned up automatically; no live external
commands are involved.
