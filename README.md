# Transist Plugins

Versioned integration examples for [Transist](https://github.com/transist-ai/transist),
the runtime for explicit, repository-local workflows. Plugins connect declared
steps to commands, services and MCP tools; workflows define their inputs,
authority and ordering. Model-backed CLIs are optional participants.

All **16 public packages** live under `plugins/<id>/`, initially at **0.1.0**.
After initial publication, plugins may be [versioned independently](docs/authoring.md#plugin-version-policy).
This is an **unsigned, source-only publication**, with no publisher-signed bundles
or trust root. Original companion source is [Apache-2.0](LICENSE); vendored and
derived runtime schemas retain [MPL-2.0](tests/contracts/LICENSE). See
[NOTICE](NOTICE) and [contract provenance](tests/contracts/README.md).

## Prerequisites and shell quickstart

Install and verify the public [Transist v0.1.0 release](https://github.com/transist-ai/transist/releases/tag/v0.1.0)
using the pinned [artifact verification instructions](https://github.com/transist-ai/transist/blob/v0.1.0/docs/public/reference/release-artifact-verification.md).
Both `transist --version` and
`transist-admin --version` must report `0.1.0`. The pinned runtime commit is
`44a762cdd8ec2d967497dbd5be5534618da61ec6`; see [compatibility](docs/compatibility.md).
Use Python 3.11 for the shell example and local validation. Other packages may
require Go, additional CLIs, local services or Docker; read their guides first.

From this repository root, inspect the companion shell in a fresh standalone home:

```sh
DEMO_HOME="$(mktemp -d /tmp/transist-plugin-demo.XXXXXX)"
DEMO_HOME="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$DEMO_HOME")"
transist home init --home "$DEMO_HOME" --mode standalone
transist plugins install --home "$DEMO_HOME" --path plugins/shell/plugin.yaml
transist plugins list --home "$DEMO_HOME"
transist plugins inspect --home "$DEMO_HOME" shell
transist workflows check --home "$DEMO_HOME" --path plugins/shell/workflow.yaml
```

This checks source and installation; it does not execute a workflow. Follow the
[shell guide](plugins/shell/README.md) for workflow installation, bound readiness,
authorization, execution, inspection and cleanup. No provider account is needed.
Companion shell bytes and signatures are **not interchangeable** with
[runtime-bundled shell](https://github.com/transist-ai/transist/tree/v0.1.0/plugins/shell).
Use a fresh home for changed same-version payloads; preserve older installs and runs.
The path normalization above is required on macOS, where `/tmp` is a symlink;
the runtime rejects symlink traversal in runtime-home paths.

## Package inventory

| Package | Adapter | Purpose |
| --- | --- | --- |
| [shell](plugins/shell/README.md) | `process` | Command execution and deterministic echo |
| [git.cli](plugins/git.cli/README.md) | `process` | Branch/worktree operations and instruction-comment scanning |
| [github.cli](plugins/github.cli/README.md) | `process` | Pull requests, approvals, mergeability and review projection |
| [github.copilot.cli](plugins/github.copilot.cli/README.md) | `process` | Copilot CLI wrapper |
| [codex.cli](plugins/codex.cli/README.md) | `process` | Codex CLI wrapper |
| [cursor.cli](plugins/cursor.cli/README.md) | `process` | Cursor CLI wrapper |
| [opencode.cli](plugins/opencode.cli/README.md) | `process` | OpenCode CLI wrapper |
| [jira.rest](plugins/jira.rest/README.md) | `process` | Jira REST operations |
| [openspec](plugins/openspec/README.md) | `process` | Plan/verify wrapper with backend CLIs |
| [ollama.cli](plugins/ollama.cli/README.md) | `process` | Local generation; build the Go entrypoint |
| [http](plugins/http/README.md) | `process` | Outbound HTTP/S requests and downloads |
| [anthropic](plugins/anthropic/README.md) | `process` | Bounded Anthropic Messages |
| [openai.compatible](plugins/openai.compatible/README.md) | `process` | Bounded Chat Completions |
| [git.mcp](plugins/git.mcp/README.md) | `mcp` | Local read-only branch inspection |
| [text.http](plugins/text.http/README.md) | `http` | Inline-text statistics service |
| [tika.http](plugins/tika.http/README.md) | `http` | Document text/metadata extraction via Tika |

Adapter names describe transport: `http` launches a process that makes outbound
requests; `text.http` and `tika.http` reach independently managed HTTP services.
Process/HTTP participants speak APP; the runtime maps MCP tools into action
results. Inventory inclusion does not imply live-provider or platform acceptance.

## Validation

[CI](docs/ci.md) is **static-only**: structure, schemas, syntax, formatting/lint
and current-source secrets. It executes no plugins or workflows. Install the
documented native tools and isolated Python dependencies before running it:

```sh
python3 -m pip install -r tests/requirements.txt
python3 -B scripts/check_static.py
```

The **local release-candidate gate passed on native macOS and Linux ARM64**.
[Validation](docs/validation.md) lists the complete reproduction commands,
prerequisites and evidence requirements. Required behavioral checks use
deterministic fixtures without live credentials; paid/authenticated providers
are excluded. Offline success does not prove installed execution or live
interoperability. Earlier [development records](docs/history/compatibility.md)
retain their original evidence and do not establish public-release acceptance.

## Authoring, publication and contribution

- [Authoring](docs/authoring.md): package contracts, adapters, secrets, managed
   documents, compact outputs and independent plugin versioning.
- [Publication](docs/publication.md): clean source export, rights, independent
  secret scanning and the final both-platform gate.
- [Contributing](CONTRIBUTING.md): scoped checks, focused changes and DCO sign-off.
- [Support](SUPPORT.md), [Security](SECURITY.md) and [Code of Conduct](CODE_OF_CONDUCT.md).

External plugin and example contributions are welcome under Apache-2.0 with
maintainer review. Repository availability does not imply consulting, bespoke
architecture work or guaranteed feature delivery.
