# Static CI contract

Public release validation targets Transist v0.1.0, commit
`44a762cdd8ec2d967497dbd5be5534618da61ec6`. Its complete native macOS/Linux
gate has [separate native results](validation.md). Static CI does not establish that gate
or produce publisher-signed payloads; publication is unsigned source-only.

[Plugin checks](../.github/workflows/plugin-ci.yml) runs on pull requests and
pushes to `main`. One Ubuntu 24.04 job has a five-minute timeout and cancels
superseded runs for the same PR or branch. Actions are SHA-pinned, checkout does
not persist credentials, and the token grants only `contents: read`.

## Checks

| Check | Scope |
| --- | --- |
| Canonical source structure | `plugins/<id>/`, required source files/tests, manifest identity and adapter/action/entrypoint consistency |
| Payload and schemas | Explicit contained `package.files`, Draft 2020-12 action schemas and selected local references; see [authoring](authoring.md) |
| YAML/JSON syntax | Git-listed public YAML and JSON documents, including duplicate-key rejection |
| Python formatting/lint | Black 25.1.0 and flake8 7.2.0 on every Git-listed Python file, including tooling and tests |
| Go formatting | `gofmt -l` using Go 1.22.12; no compilation, vet or tests |
| Workflow lint | actionlint 1.7.12; optional host-discovered ShellCheck/Pyflakes checks are disabled for consistent results |
| Secrets | Gitleaks 8.30.0 with redacted findings, scanning current source bytes only |

Python dependencies are declared in `tests/requirements.txt`. CI downloads
prebuilt actionlint and Gitleaks releases and verifies their SHA-256 digests
before extraction. It does not compile either tool or use an organization-only
licensed action.

`scripts/check_static.py` enumerates tracked and nonignored new files through
Git. The scanner receives a short-lived copy of that inventory, with no Git
internals, ignored private material, tool installs or caches. This is not the
publication exporter and performs no staging, history copying or signing.
Scanner input is removed even if the scan fails. Findings and tool failures
fail the job. The explicit `scripts/gitleaks.toml` uses the pinned scanner's
default rules; inherited Gitleaks configuration, fingerprint ignores and inline
allow comments cannot suppress findings. There are no fixture exceptions.

## Run locally

Use Python 3.11, Go 1.22.12, actionlint 1.7.12 and Gitleaks 8.30.0 on `PATH`.
Install Python development dependencies in an isolated environment:

```sh
python3 -m pip install -r tests/requirements.txt
python3 -B scripts/check_static.py
```

The structure-only check is `python3 -B scripts/check_plugin_structure.py`.
Obtain native tool binaries from the pinned
[actionlint release](https://github.com/rhysd/actionlint/releases/tag/v1.7.12) and
[Gitleaks release](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.0),
verifying the published checksums. CI's Linux archive digests are embedded in
the workflow; other platforms have their own release checksums.

## Boundary

CI does not execute root or plugin tests, APP smokes, installed workflows or
live-provider calls. It does not build/download Transist, install sandbox
support, require provider credentials, or export publication snapshots.
Static success does not prove plugin behavior, installation, governed execution
or provider interoperability. Run relevant behavioral checks explicitly using
[Contributing](../CONTRIBUTING.md) and each package's guide.

The secrets scan does not inspect Git history. Continue to scan intended history
and approved exports separately for [publication](publication.md). The bespoke
privacy/link publication gate remains a local publication tool.
