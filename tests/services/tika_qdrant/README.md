# Tika / Qdrant Step 1 fixtures

This directory provisions real Apache Tika, Qdrant and the official Qdrant MCP
server for targeted development tests. It contains no production plugin package.
The HTTPS wrapper is fixture-only: `/fixture/tika` and `/fixture/meta` forward raw
document bytes to Tika; it does **not** implement the production APP action
contract. `/app/handshake` and `/app/health` return a public fixture identity.
`/mcp/` delegates protocol handling and `qdrant-find` to the official server.

## Requirements and validation

- Docker daemon on **Linux aarch64**, with sufficient memory for Tika and ONNX.
- `docker-compose` executable (the `docker compose` subcommand is not used).
- Python 3.11 on the host and verified public `transist` / `transist-admin` **0.1.0**
  executables on `PATH`.
- Network access during image/dependency builds and model provisioning only.
  Provisioning fetches six model assets from the pinned Hugging Face revision,
  checking every SHA-256 against `model.lock.json`. The source lock stores explicit
  `file`/`digest` entries; `model_lock.py` normalizes them to the existing
  filename-to-digest verification manifest. This separates tokenizer filenames
  from checksum assignments for secret scanning without changing any pinned hash.

From the repository root:

```sh
PYTHONPYCACHEPREFIX=/tmp/tq-step1-pycache python3.11 -m unittest discover \
  -s tests/services/tika_qdrant -p test_fixture.py \
  > /tmp/tq-step1-unit.log 2>&1
python3.11 tests/services/tika_qdrant/validate.py \
  > /tmp/tq-step1-validation.log 2>&1
```

The validator fails on missing prerequisites, unsupported lock platform, download
or hash failure, model failure, parsing/search/authentication failure, or teardown
failure. There are no passing skips. Python assertions must remain enabled.
It does not run root tests or CI. Transist binaries are prerequisite-checked and
hashed; workflow/plugin E2E execution belongs to later steps.

The validator prints a unique `/tmp/tq-step1-*` evidence directory. It contains
build/provision/service/test/teardown logs, `model-manifest.json`,
`native-output.json` and `summary.json` (exact commands, source status, binary
hashes and actual container image identities). Logs are checked against raw,
URL-encoded and Base64 fixture credentials before teardown. Credentials are
never passed through argv or printed. Host evidence directories are mode 0700.

All generated documents, model files, TLS keys, secrets and DB storage live in
unique Docker volumes outside the repository. Secrets and private TLS/config
files are mode 0600. The default validator always removes its own containers,
volumes and networks in `finally`, and checks existing unrelated resources were
preserved. Downloaded/base and built images remain cached. There is no Docker
prune, global cleanup or production collection access.

## Targeted acceptance

- Real Tika extracts the known text and JSON metadata from genuine generated
  PDF, OOXML DOCX and TXT documents through authenticated, verified HTTPS.
- Only a loopback-bound HTTPS gateway port is published; Tika/Qdrant backend
  ports stay private. Runtime services use an internal Docker network with no
  download egress; an additional model test uses `network_mode: none`.
- Seeds `calibration`, `printer`, `inventory` using actual model inference.
  Payloads use `document` and nested `metadata.source_id`; collection
  `fixture_documents` has named `fast-all-minilm-l6-v2`, 384-dimensional cosine
  vectors. The database read-only credential can read but upsert, collection
  creation and collection deletion all return 403.
- Supported provider injection subclasses the official `FastEmbedProvider`,
  retaining its embedding/vector methods and using `specific_model_path` plus
  `local_files_only=True`. Startup checks immutable snapshot hashes, then eagerly
  loads ONNX/tokenizer assets. Real inference succeeds with sockets denied;
  each missing asset, missing snapshot and a corrupted asset fail closed.
- Anonymous MCP sessions initialize/list/ping/close, exposing only `qdrant-find`
  with its native query schema. Anonymous calls, wrong keys, cross-identity
  session traffic and anonymous-session upgrades are denied. Gateway and DB
  credentials are independently generated. This minimal fixture wrapper is not
  a production authentication/query-validation implementation.
- The official MCP output is captured intact: nonempty results are **one text
  block** containing a pretty-printed JSON array, with `calibration` first for
  the calibration query. An actual empty collection returns **zero blocks**.
  The validator briefly empties its own collection for this check, restores the
  original points/vectors/payloads in `finally`, and verifies exact equality.
  No replacement search tools, scores or structured matches are introduced.
- Client and hosted dependencies install separately with `--require-hashes`
  and pass `pip check`. No invocation-time dependency or model downloads occur.

## Immutable identities

| Component | Pin |
| --- | --- |
| Tika | `apache/tika:3.3.0.0@sha256:2a565f1ea1290bdcb74a7d35957d16a989ed44ef98790dcdcc28121d728fa583` |
| Qdrant | `qdrant/qdrant:v1.16.1@sha256:db1c735496dfa982ef27576a17b624e48e6b46a140bcdc2ac34e39d186204ef5` |
| Python | `python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d` |
| Model | `Qdrant/all-MiniLM-L6-v2-onnx@d13954661f83248295ba75c1ed411eef3b7b936e` |

`server.lock` freezes all 68 distributions in the compatible hosted resolution;
`client.lock` freezes all 31 distributions in the separate client resolution.
These are artifact-specific CPython 3.11 Linux aarch64 locks, not unverified
multi-platform locks. The top-level pins are unchanged from the development
plan. Provenance: Linux pip JSON reports and candidate hash locks from
`/tmp/tq-dependency-research-20261006/`; Docker builds demonstrate clean
`--require-hashes` installation and dependency checking. The model file hashes
are frozen separately in `model.lock.json`, with the ONNX file hash
`bbd7b466f6d58e646fdc2bd5fd67b2f5e93c0b687011bd4548c420f7bd46f0c5`.

## Formatting and lint (touched Python only)

Use Black and flake8 in a Python 3.11 environment; an isolated `/tmp` virtualenv
is suitable if they are not already installed:

```sh
python3.11 -m venv /tmp/tq-step1-lint-env
/tmp/tq-step1-lint-env/bin/python -m pip install black flake8 \
  > /tmp/tq-step1-lint-install.log 2>&1
/tmp/tq-step1-lint-env/bin/python -m black --check tests/services/tika_qdrant/*.py \
  > /tmp/tq-step1-format-check.log 2>&1
/tmp/tq-step1-lint-env/bin/python -m flake8 --max-line-length=88 \
  --extend-ignore=E203 tests/services/tika_qdrant/*.py \
  > /tmp/tq-step1-lint.log 2>&1
```

For manual debugging, select your own unique `docker-compose -p <project>`
and use this directory's `compose.yaml`: build/pull, run `provision`, run
`offline`, start `tika qdrant gateway`, run `seed`, then run `probe`.
Provisioning requires an empty asset volume. Use only
`docker-compose -p <same-project> -f tests/services/tika_qdrant/compose.yaml down
--volumes --remove-orphans` for cleanup. Never dump the credential file, config,
container environment or request headers into logs.

## Tika-only installed-workflow harness (Step 3)

`plugins/tika.http/tests/e2e.py` reuses this fixture's locked provisioning image,
TLS generation and generated documents, but starts only Tika and the production
APP service from `plugins/tika.http`. It invokes `provision.py --tika-only` with
network disabled, bypassing database/model provisioning completely. Default
Step 1 provisioning remains unchanged. `tika_app.py` is a fixture-only TLS
launcher around the actual production service, with memory-only route/status
counters for proving pre-dispatch denials. No secret or body logging is added.

The backend network is internal, with no published Tika port; only verified
HTTPS on the frontend is published to host loopback. The frontend additionally
uses a bridge network for local daemon port publication (Colima/Desktop). See
[the package guide](../../../plugins/tika.http/README.md) for invocation and cleanup, and
[E2E validation](../../../plugins/tika.http/E2E_VALIDATION.md) for historical development results and
runtime blockers. The current target is public runtime v0.1.0, commit
`44a762cdd8ec2d967497dbd5be5534618da61ec6`, supporting the opt-in
`action_data_v1` projection. Version strings alone do not verify artifact identity;
the current both-platform gate is [recorded separately](../../../docs/validation.md).
