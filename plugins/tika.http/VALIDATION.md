# Step 2 validation evidence

> Historical development evidence, not public runtime v0.1.0 acceptance.
> Original results and versions are retained below. The final native macOS/Linux
> release gate is [recorded separately](../../docs/validation.md).

Date: 2026-10-06. Baseline: `main`, Step 1 commit `c824272`.
Scope: new `plugins/tika.http` package only. No commits or plan status changes.
No runtime source changes, installed workflow execution, real Tika E2E,
repository-wide tests, CI or Step 3 validation were performed.

## Contract inspection

Read the fixed development plan, `plugins/text.http` service/manifest/workflow,
`tests/contracts/app/{invoke.request,invoke.result,handshake.response}.v1.schema.json`,
`tests/contracts/tep/error.schema.json`, and the runtime's APP documentation,
manifest secret-binding definition, HTTP preflight code and actual invoke
request/result schemas under `transist-oss/schemas/app` (read-only).
The runtime schemas agree with the repository contract copies. Requests use the
complete APP envelope; results require `output`, hints and metrics even on
failure. Secret bindings use `type: header`, not a guessed binding name.

## Environment and immutable identities

- Host: Darwin arm64; Docker context `colima`, Linux arm64 daemon.
- Service/test interpreter: pinned Python 3.11.14 Linux aarch64 image.
- Python base: `python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d`.
- Final minimal service image: `tq-step2-tika-app-reviewed2`,
  `sha256:d6faf1614d543784bd2201a562e6eb81173f5390388ad74e94087fb7ce44d31d`.
- Existing Step 1 schema-test image: `tqstep1-480b6321225e-gateway`,
  `sha256:bcff8851680a2effd2b772e72ea2715321593ffa12f60172d97e7bdf0ece8d69`.
  This supplies pinned `jsonschema`/PyYAML test dependencies, not a running fixture.
- Runtime dependencies: all ten artifacts in `requirements.lock`; complete
  minimal closure derived from the accepted Step 1 lock, with unchanged hashes.
- Service image build verified `--require-hashes`; subsequent `pip check`
  reported **No broken requirements found**.
- Tests used `--network none`, loopback TCP only, read-only repository mounts and
  `PYTHONDONTWRITEBYTECODE=1`. No document/model files or caches were created.
  Disposable test servers and containers were closed/removed. Built images remain
  available for review. Logs live outside the repository under `/tmp`.

## Commands and results

Working directory for all commands: the repository root.

Complete named Step 2 package suite:

```sh
docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/repo:ro" \
  -w /repo tqstep1-480b6321225e-gateway \
  python -m unittest discover -s plugins/tika.http/tests -p 'test_*.py' -v \
  > /tmp/tq-step2-tests-final.log 2>&1
```

**24 tests passed in 31.143 seconds; zero skips.** Covers all supported types,
1-byte/5-MiB boundaries, empty/oversized documents, padding/alphabet/padding-bit
errors, additional fields, duplicate JSON keys, malformed JSON, nonfinite
numbers, text preservation/UTF-8 limits, metadata normalization and exact
128-KiB serialized-output boundary, unsafe backend types/responses, fixed error
classification, no retries/redirect following, proxy poisoning, anonymous
discovery, authentication before receive, incoming trickling and a shared
body/backend operation deadline. The actual TCP backend emits a chunk every
100 ms: the default 30-second deadline terminates it (asserted elapsed time
29.5–32 seconds), rather than resetting a read timeout on every chunk.

Formatting, lint and final service build:

```sh
/tmp/tq-step1-lint-env/bin/python -m black \
  plugins/tika.http/service.py plugins/tika.http/tests \
  > /tmp/tq-step2-format-reviewed.log 2>&1
/tmp/tq-step1-lint-env/bin/python -m black --check \
  plugins/tika.http/service.py plugins/tika.http/tests \
  > /tmp/tq-step2-black-reviewed2.log 2>&1
/tmp/tq-step1-lint-env/bin/python -m flake8 --max-line-length=88 \
  --extend-ignore=E203 plugins/tika.http/service.py plugins/tika.http/tests \
  > /tmp/tq-step2-flake8-reviewed2.log 2>&1
docker build --platform linux/arm64 -t tq-step2-tika-app-reviewed2 \
  plugins/tika.http > /tmp/tq-step2-build-reviewed2.log 2>&1
```

**Black: three files unchanged. flake8: no diagnostics. Docker build: success.**
Early lint attempts identified long docstrings/wire literals; these were fixed
before the successful final checks. Docker emitted a missing-buildx notice and
successfully used its installed legacy builder; this did not skip the build.

Rechecked all unit/wire tests in the **minimal deployed service environment**:

```sh
docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/repo:ro" \
  -w /repo tq-step2-tika-app-reviewed2 \
  python -m unittest discover -s plugins/tika.http/tests -p test_tika.py -v \
  > /tmp/tq-step2-minimal-reviewed.log 2>&1
docker run --rm --network none tq-step2-tika-app-reviewed2 python -m pip check \
  > /tmp/tq-step2-dependencies-reviewed.log 2>&1
docker run --rm --network none -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/repo:ro" \
  -w /repo tqstep1-480b6321225e-gateway \
  python -m unittest discover -s plugins/tika.http/tests -p test_schema.py -v \
  > /tmp/tq-step2-schemas-reviewed.log 2>&1
```

**21 unit/wire tests passed in 31.036 seconds; three schema/package tests passed
in 0.068 seconds;
zero skips.** Schema checks compile all four Draft 2020-12 schemas, validate
actual APP discovery/request/success/failure shapes using offline schema
resources, verify selected payload containment and both required Authorization
secret bindings, and require both demo byte-input fields.

## Reviewer boundaries

No Step 2 prerequisite blocker remains. The HTTPS endpoint is deliberately a
deployment placeholder. Deployment must supply the strong Bearer secret, private
Tika origin, HTTPS ingress configuration and the client network/secret authority
documented in README. Header parsing and response transmission deadlines/log
suppression are ingress/ASGI-server settings; the service enforces the shared
30-second operation deadline from dispatch through body/backend processing.
Real hosted Tika parsing and installed standalone/governed workflows remain
Step 3 acceptance, not claims made by these tests.

Scoped whitespace checks used `git diff --no-index --check /dev/null <file>`
for each of the 13 newly authored files; `/tmp/tq-step2-whitespace.log` contains
no diagnostics.

All authored changes are beneath `plugins/tika.http`. Concurrent unrelated
changes were observed and left untouched. Step 2 is committed separately on
`main`; no remote push is part of this validation.
