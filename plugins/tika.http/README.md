# tika.http 0.1.0

Remote Apache Tika parsing through the Transist HTTP adapter and **APP v1**.
Both actions are external, including Docker deployments. The client supplies
bytes; no document paths, URLs, workspace access, local documents or caches are
supported. Runtime input/output persistence still applies.

## Required runtime release

Use verified public **Transist/admin v0.1.0**, commit
`44a762cdd8ec2d967497dbd5be5534618da61ec6`, with per-action
`bindings.input_projection: action_data_v1`. A version string alone does not
verify artifact identity. Earlier development-build blockers are historical;
the current public-runtime both-platform gate is [recorded separately](../../docs/validation.md).

Both actions opt in. The runtime removes only top-level `secret_refs` from the
cloned action input before static/live schema validation and HTTP APP transport.
Original TEP input, metadata, secret-reference authority, capabilities and
delivery policy remain intact. Workflows must still declare
`with.secret_refs: [TIKA_AUTHORIZATION]`. The service and schemas continue to
reject extra document fields; the service does not strip or accept secret refs.
The manifest, service identity and demo plugin pin report **0.1.0**. The
historical Step 2 and Step 3 validation records retain the versions and results
that were observed when those checks ran.

## Actions and bounds

`extract_text` and `extract_metadata` require exactly `document_base64` and
`content_type`. Use canonical standard Base64 (including required padding;
no whitespace, URL-safe alphabet, redundant padding or nonzero padding bits).
Decoded documents must contain **1–5 MiB**. Supported types:

- `application/pdf`
- `application/vnd.openxmlformats-officedocument.wordprocessingml.document`
- `text/plain`

Text output is `{document_sha256, text}`. The checksum covers the supplied bytes;
Tika text is preserved, including leading/trailing whitespace and newlines.
The text is capped at **128 KiB UTF-8**, not 128 KiB JSON wire size.
Metadata output is `{document_sha256, metadata}`. Only `Content-Type`,
`Content-Length`, `Content-Encoding`, `dc:title`, `dc:creator`, `dcterms:created`,
`dcterms:modified`, and `xmpTPg:NPages` survive. Scalar strings become one-element
arrays; string arrays retain their order. Invalid whitelisted values, more than
16 values/key, more than 4,096 characters/value, or more than 128 KiB compact
UTF-8 serialized **output** fail rather than silently truncating. Unknown keys
are discarded. The raw backend metadata response is additionally capped at
1 MiB. Schemas compile as Draft 2020-12; byte-size and canonical padding-bit
checks are enforced by the service in addition to schema checks.

## APP deployment

The manifest's `https://tika.example.invalid` is an explicit deployment
placeholder, with no base path. Configure a real HTTPS origin before use;
the adapter's appended paths match the service:

- public `GET /app/handshake`: plugin identity and two actions; health supported;
  cancellation/artifact/evidence hints unsupported;
- public `GET /app/health`: `{"status":"ok"}` (APP liveness, not a Tika parse probe);
- authenticated `POST /app/invoke`: the **full** runtime
  `app.invoke.request.v1` envelope, not bare action input.

`paths`, `policy`, `metadata`, and capabilities are observational. No path is
opened, and these fields cannot change the backend origin. Results are full
`app.invoke.result.v1` envelopes with output, empty hints and empty metrics.
There is no fabricated usage. Unsupported routes/methods return 404. Missing,
wrong, or duplicate Authorization headers return 401 **before any body read**.
Anonymous discovery never returns document results or contacts Tika.

Authenticated validation errors, including malformed/duplicate-key JSON,
unsupported actions and oversized bodies, return HTTP 200 APP failures:

| Code | Class | Fixed message |
| --- | --- | --- |
| `validation_error` | validation | Invalid invocation or document |
| `timeout_error` | timeout | Operation deadline exceeded |
| `transport_error` | transport | Backend unavailable |
| `protocol_error` | protocol | Invalid backend response |

All use `retryability: none` and empty output. Malformed envelopes whose identity
cannot be recovered use the nonempty sentinel invoke ID `invalid`; valid envelopes
retain their invoke ID. No backend body, exception, redirect location, or header
is exposed in errors. There are no automatic retries.

One absolute **30-second** operation deadline covers body receipt, validation,
backend connection/upload and streamed download, including trickling peers.
Individual HTTPX timeouts also apply. The incoming body limit is canonical
5 MiB Base64 plus 64 KiB for the APP envelope (7,056,044 bytes). Backend calls
are `PUT /tika` with `Accept: text/plain`, or `PUT /meta` with
`Accept: application/json`; both set `X-Tika-Skip-Embedded: true`.
Backend redirects and environment proxies are disabled. TLS verification stays
enabled. Compressed backend bodies, wrong media types, invalid UTF-8 and malformed
JSON fail closed.

Deploy `service.py` separately; installing the package does not start it. Build
from this directory:

```sh
docker build --platform linux/arm64 -t tika-app .
```

Inject the server secret through deployment secret management, never image bytes,
argv or committed files. The image runs non-root with bytecode writes disabled.
Run with a read-only filesystem and no writable document volume. Terminate
verified HTTPS at an ingress that forwards these exact APP paths, streams request
bodies without disk spooling, disables access/header/body/error-detail logging,
sets a finite header timeout and response transmission deadline, and does not
buffer anonymous extraction bodies before forwarding headers. The ASGI operation
deadline begins when HTTP headers have reached the application; header parsing
and final response delivery belong to the ingress/ASGI server boundary.
Keep Tika private on the deployment network, with no published backend port.
Use `apache/tika:3.3.0.0@sha256:2a565f1ea1290bdcb74a7d35957d16a989ed44ef98790dcdcc28121d728fa583`.
Tika is a separate parser process and may use its own deployment-side temporary
storage; the APP service only holds input and output bytes in memory.

`requirements.lock` contains the complete minimal HTTPX/Starlette/Uvicorn closure
derived from Step 1's validated client/server locks. It is pinned for **CPython
3.11 Linux aarch64**, not an unverified cross-platform lock. Docker pins
`python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d`
and installs with `--require-hashes`. Provision dependencies before execution.
The selected payload is exactly the manifest and `package.files`: four schemas,
service, lock and Dockerfile. README, workflow, validation evidence and tests
are authoring assets. No credentials, CA private keys or generated data are selected.

## Configuration and policy

| Name | Classification/default | Consumption and policy |
| --- | --- | --- |
| `TIKA_AUTHORIZATION` | Required secret; no default | Runtime secret bound to HTTP `Authorization` on both actions; server independently reads the matching deployment environment secret. Value includes `Bearer ` plus an independently generated token with at least 32 token characters (recommend 256 bits of entropy). Exact constant-time comparison. Runtime policy must allow and inject HTTP raw-secret delivery and redact it. Never forwarded to Tika. |
| `TIKA_BACKEND_URL` | Optional server-only; `http://tika:9998` | Read at factory startup; fixed HTTP(S) origin, without userinfo/path/query/fragment. Operator-controlled private Tika target; server egress policy must permit it. Not a workflow input or client variable. |
| `PYTHONDONTWRITEBYTECODE` | Image-managed; `1` | Python prevents local bytecode files. No runtime client injection required. |
| `TMPDIR`, `TMP`, `TEMP` | Optional inherited host controls; unused | Service does not read, forward, create or use temporary files. No plugin temporary-directory grants are required. Tika's own temporary settings are separate server administration. |
| `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` (and lowercase variants) | Optional inherited; ignored | HTTPX `trust_env=False` prevents environment proxy routing. No policy injection needed. |
| `SSL_CERT_FILE`, `SSL_CERT_DIR` | Optional inherited; ignored | HTTPX ignores environment trust overrides. Backend HTTPS uses normal bundled trust. Client additional public CA configuration belongs to deployment packaging. |

No child process is launched by the APP service. Its Docker command fixes
Uvicorn host/port, factory, lifespan-off and logging-off configuration; no extra
service environment is required. Do not enable diagnostic client/server logging.
Grant client network access to the configured HTTPS host/port and HTTP secret
delivery explicitly; secret delivery is denied by default for HTTP in governed
homes. The required secret is action-bound, not a discovery prerequisite.

## Demonstration and targeted checks

`workflow.yaml` requires **both** byte-input fields; it embeds no sample path or
URL and binds the same input to both actions. An accepted input object is:

```json
{"document_base64":"SGVsbG8sIFRpa2EhCg==","content_type":"text/plain"}
```

Supply `TIKA_AUTHORIZATION` through Transist's secret store, including the Bearer
prefix. Configure a deployment endpoint and matching workflow network authority
before installing/launching. Historical installed standalone and fixture-signed
governed E2E acceptance passed with the development projection-supporting build.
See [historical E2E validation](E2E_VALIDATION.md) for its exact build hashes,
commands and results; these do not establish public runtime v0.1.0 acceptance
or publisher signing.

Run only this package's tests (no root suite or E2E):

```sh
python -m unittest discover -s plugins/tika.http/tests -p 'test_*.py' -v
```

Tests require the pinned service environment plus `jsonschema` and `PyYAML` for
schema checks (available in Step 1's fixture image). They use in-memory APP
transport and disposable loopback TCP servers, not real Tika. One wire test
deliberately lasts 30 seconds to prove the production absolute deadline.
See [historical validation](VALIDATION.md) for its commands, environment and results.

### Installed real-service E2E (Step 3)

From the repository root, with a local Docker daemon (native Linux, Colima or Docker Desktop),
`docker-compose`, Python 3.11, and verified public Transist/admin v0.1.0:

```sh
TRANSIST_BIN="$TRANSIST_TOOLS_DIR/transist" \
TRANSIST_ADMIN_BIN="$TRANSIST_TOOLS_DIR/transist-admin" \
TRANSIST_FIXTURE_BASE="$FIXTURE_BASE" \
env -u PYTHONPATH python3.11 -B plugins/tika.http/tests/e2e.py > /tmp/tika-step3-e2e.log 2>&1
```

Set `TRANSIST_TOOLS_DIR` to the matching explicit binary directory and
`FIXTURE_BASE` to an existing absolute disposable directory. The runner has no
binary PATH fallback. This does not replace installed tools.
Missing prerequisites, sandbox
support, failed checks and unmet acceptance cases fail; nothing is skipped.
This harness requires the native host sandbox: `sandbox-exec` on macOS or
`bwrap` on Linux. Linux must permit real user, mount and network namespaces and
the runtime's namespace-local capability setup (`CAP_NET_ADMIN`, `CAP_SETPCAP`,
`CAP_SETGID`, `CAP_SETUID`). The actual `transist-sandbox-launcher` must be
installed beside the resolved runtime binary or in its standard
`/usr[/local]/libexec/transist/` location. Its probe requires a read-only file
mount to reject writes with `EROFS`, distinct namespace identities, loopback down
for `network:none`, zero remaining capabilities and `NoNewPrivs`. Only the probe's
own output mount is writable; the Docker socket is never mounted into the
sandbox or plugin. Missing binaries, denied namespace creation, launcher failure
and unsupported operating systems fail the required test.

Live runtime readiness must construct the exact native backend (`sandbox-exec`
or `bwrap`) for **both** Tika actions. HTTP requests still use the runtime's
host-side HTTP adapter and its actual TLS/network policy enforcement; the local
probe verifies the host's subprocess isolation prerequisites. Governed runs must
report trusted execution and enforced capabilities. Docker dependency locks
remain Linux aarch64 on either host platform.

The inventory checker resolves the parent Docker context's public UNIX socket
before isolating `HOME`, and passes only `DOCKER_HOST` into the fixture. Direct
invocation resolves the context before creating its own isolated environment.
An explicit `DOCKER_HOST` must select an existing local UNIX socket; missing or
remote endpoints fail prerequisites. Host Docker configuration and credentials
are never copied. Both the daemon OS/architecture and the pinned pulled Tika
image must be native Linux arm64, and Compose pins all sidecars to that platform.

Only the pinned Tika backend and production APP image are started. A fixture-only
TLS launcher delegates APP requests to `service.create_app()` and retains route
and response-status counters in memory; it never records headers or document
bytes. Tika has no published ports and lives on an internal backend network.
The HTTPS frontend also has a bridge network so the local daemon can publish its
loopback-only TLS port. Anonymous fixture counters are test instrumentation,
not a production service endpoint. Provisioning runs with network disabled and
does not create/download a Qdrant model or start Qdrant.
The harness separately waits for actual Tika `/version` readiness on the private
network before dispatch. APP health is only liveness; document invocations are
never retried to conceal a backend startup failure.

The harness creates a scratch deployment manifest with identity `tika.http@0.1.0`
and plugin-local public `entrypoint.ca_cert`, selected in `package.files`.
Certificate-chain and hostname verification remain enabled. This is a Step 3
test deployment, not the immutable deployment builder planned for Step 6.

Unsigned authored checks use a disposable standalone authoring home. Governed
homes install signed workflow packages, acquire exact authority, then check and
run installed code. TLS-negative homes load unchanged CLI-generated authority
fragments from separate valid-TLS homes with the same workflow/input resources;
each home installs only one immutable deployment payload. The wrong-hostname
case scopes the two loopback names to the one fixture port. TLS verification
errors are independently classified and runtime readiness must fail only at
both handshake checks, with no APP request reaching the service. No TLS check,
policy ceiling or document schema is relaxed.

All homes, inputs, deployment packages, signing keys and logs are outside the
checkout in a private temporary directory printed at startup. Disposable
credentials are passed to runtime commands only through the environment, and
loaded server-side from a private fixture volume; never through argv. Raw and
encoded full credentials **and prefix-free Bearer tokens** are scanned in all
runtime files and command/service logs. Generated documents are fixture-owned;
the client workspace must remain empty. Docker resources are project-scoped,
removed on failure as well as success, and unrelated resource IDs are checked.
Private credential/TLS/signing assets are removed; runtime records, command
logs and `summary.json` remain for inspection. Treat the evidence directory as
private. `accepted: true` is written only after every named case succeeds and
cleanup completes.
