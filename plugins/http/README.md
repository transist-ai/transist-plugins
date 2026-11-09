# `http`

Status: experimental. Package version: `0.1.0`.

`http` is a generic outbound HTTP/S `process` plugin for one-off REST-style
calls from Transist workflows without authoring a service-specific client
plugin.

## Runtime Capabilities And Endpoints

- Adapter: `process`.
- Capability grants: adapter invoke, `plugin:http@0.1.0` load, and selected `action:http@0.1.0/{request|download}` invoke.
- Secrets: optional `HTTP_SECRET_1` through `HTTP_SECRET_8`, delivered only when referenced by the action.
- Filesystem: declare a writable workflow workspace for durable downloads; `run://` destinations are invocation-private scratch.
- External endpoints: defined by each action's `url` input. Grant the exact HTTPS host and port for every destination; HTTP is supported only for loopback hosts.

## Current Scope

This package ships `request` and `download` actions with a fail-closed network
policy model:

- external egress requires `policy.network.mode: allowlist`
- workflow-authored `allow_hosts` and optional `allow_ports` are enforced in-plugin
- policy absence or `mode: any` is denied unless `HTTP_ALLOW_ANY=1`
- `mode: none` and `mode: loopback` surface a clean `transport_error`
- DNS resolution is validated before connect and the plugin pins the socket to a validated IP
- redirects are revalidated hop-by-hop and bounded by `max_redirects`
- proxy environment variables are ignored because the plugin opens direct sockets
- secret-backed headers are supplied through the fixed `HTTP_SECRET_1..8` slot pool
- downloads stream to a temp file, enforce a hard byte cap, verify optional `sha256`, and atomically move into a workspace-relative destination or invocation-local `run://...`
- `SIGTERM` cancellation closes the active socket and removes partial downloads before exit

The package is intentionally not a proxy, not a per-API SDK, and not a generic
local filesystem or execution surface.

The plugin consumes the runtime-forwarded effective network policy. The demo
declares the narrow root allowlist required for `https://httpbin.org/json`:
`httpbin.org:443`, TLS required, and at most five redirects.

## Actions

- `request`
- `download`

## `request`

Send a one-off HTTP request with bounded headers, timeout, and response size.

Supported inputs include:

- `method`: `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `HEAD`
- `url`: `https` by default; `http` only for loopback hosts
- `headers`, `query`
- `json` or `body_text`
- `secret_headers`: maps a declared secret slot to an outbound header
- `timeout_ms`, `max_response_bytes`, `expected_status`, `secret_refs`

The action returns `status_code`, bounded response `headers`, parsed `json` or
decoded `text` when possible, `body_bytes`, `truncated`, and `url_final`.

## `download`

Download a response body into a workspace-relative file (durable) or `run://` scratch (invocation-local).

Supported inputs include:

- `method`: `GET` or `POST`
- `url`: `https` by default; `http` only for loopback hosts
- `headers`, `query`, `secret_headers`
- `dest`: ordinary workspace-relative path, e.g. `downloads/data.bin`, or `run://...` for scratch only. Absolute paths, other schemes, and paths escaping the selected root (including symlinks) are rejected.
- `max_bytes`: default and hard cap `25 MiB`
- `sha256`: optional expected checksum
- `timeout_ms`, `secret_refs`

The action returns the final `path`, written byte count, `content_type`, final
`sha256`, `truncated` (always `false` on success), and `url_final`. Successful
downloads emit a `download.json` metadata artifact. The actual binary bytes stay
in the workspace, accessible to later steps; they are never decoded into an
inline APP hint. The runtime promotes the metadata hint into execution-scoped
storage. It does not promote arbitrary files written into scratch or scan the
artifact directory for unreported files. `run://` bytes and their returned path
expire after the invocation. Use a declared `workspace: {path: ..., access: full}`
and a relative `dest` for durable data, and compare the saved bytes with `sha256`.

## Secret Header Slots

Workflows map any needed secret to one of these optional slots:

- `HTTP_SECRET_1`
- `HTTP_SECRET_2`
- `HTTP_SECRET_3`
- `HTTP_SECRET_4`
- `HTTP_SECRET_5`
- `HTTP_SECRET_6`
- `HTTP_SECRET_7`
- `HTTP_SECRET_8`

`secret_headers` then chooses which slot becomes which outbound header, for
example `Authorization: Bearer {value}`.

## Governance

- adapter: `process`
- provider kind: `external`
- `--local-only` should deny workflow dispatch before the plugin runs

## Known Limitations

- the plugin consumes runtime-forwarded `request.policy.network`; the root workflow declaration is the authority request
- this `process` plugin cooperatively enforces the forwarded allowlist for
  outbound requests and redirects; the process adapter does not provide an
  OS-level egress boundary. Host-enforced HTTP-adapter checks do not apply to
  this package.
- the secret-header pool is fixed at eight slots per call
- oversize downloads map to `validation_error` because the canonical APP set does not currently expose a dedicated resource-limit code

## Verification

Unit schema checks require Python's `jsonschema` package. The offline E2E uses
verified public Transist/admin v0.1.0 executables selected with `TRANSIST_BIN`
and `TRANSIST_ADMIN_BIN`, plus disposable storage in `TRANSIST_FIXTURE_BASE`,
and a working native sandbox. No sibling source-checkout binary is required;
its only HTTP endpoint is a local fake server.

```bash
python3 plugins/http/tests/smoke.py
python3 -m unittest discover -s plugins/http/tests -p 'test_*.py'
python3 plugins/http/tests/e2e.py
transist plugins install --path ./plugins/http/plugin.yaml
transist plugins inspect http
transist workflows check --path ./plugins/http/workflow.yaml
```
