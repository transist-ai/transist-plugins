# `git.mcp` — bounded local Git inspection

Version `0.1.0`, adapter `mcp`, default provider kind `local`. This example
exposes two read-only tools over MCP `2025-06-18` stdio. Requires Python 3.9+
and Git on an operator-controlled `PATH`; no Python runtime dependencies,
accounts, credentials, remote endpoints, or network access are required.
Native cleanup supports Linux (procfs/pidfds, kernel 5.3+) and Darwin (`libproc`);
denied or unsupported native observation fails closed.

## Action contracts

| Action / MCP tool | Input | Structured output |
| --- | --- | --- |
| `current_branch` | `{}` only | `{"branch":"main","detached":false}`; detached HEAD returns `{"branch":null,"detached":true}` |
| `list_branches` | Optional `limit`: integer 1–100, default 20 | `{"branches":["alpha","main"],"truncated":true}` |

Both schemas use JSON Schema Draft 2020-12 and reject additional input/output
properties. Successful MCP results contain `structuredContent` and an equivalent
JSON text content block. Branch names are UTF-8, at most 1024 characters.
`list_branches` lists only `refs/heads/`, ordered by Git refname order; it requests
one extra ref to determine `truncated`. Remote-tracking refs and tags are not
listed. An unborn HEAD returns its symbolic branch name and an empty branch list.
There are no checkout, commit, fetch, push, or arbitrary-command actions.

Unknown tools and invalid arguments return JSON-RPC `-32602`. Missing or
unsupported repositories and Git execution failures return a `CallToolResult`
with `isError: true` and generic text, without leaking host paths or Git stderr.
Transist maps that to `tool_execution_error`; successful output is schema-checked
by the adapter. Transport/JSON-RPC failures map to `protocol_error`.

## Workspace and path boundary

Use the included `workflow.yaml`: its required absolute `repo_path` input binds
**only** `workspace.path`, with `access: read_only` and `network.mode: none`.
Transist canonicalizes that workspace and starts both preflight and invocation
MCP processes there. Git uses that actual CWD, explicit `--git-dir=.git`, and a
fixed worktree override. The installed executable and schemas resolve from the
plugin package, independently of CWD.

Neither action accepts a host path, repository selector, environment map,
executable, revision, pattern, or command. There is no action resolver for
`repo://`, `run://`, `source://`, `file://`, `workspace://`, or `runtime://temp`.
`runtime://temp` in the manifest is a runtime temporary-directory sentinel,
not repository authority. Running the server directly uses the caller's CWD;
MCP itself does not communicate a Transist workspace declaration. Always declare
a workspace in workflows; do not rely on legacy CWD fallback.

The example deliberately supports only regular `.git` directories at the
workspace root. It rejects parent discovery, bare repositories, `.git` pointer
files (including linked worktrees/submodules), metadata symlinks/special files,
`commondir`, `config.worktree`, object alternates, config includes, `core.worktree`,
and extensions other than `objectformat`. Config is checked before Git starts
using a conservative UTF-8, single-line section/key syntax subset; escaped section
names and continuation lines are rejected. No system/global Git config is loaded.
Local config cannot enable hooks/fsmonitor, replace the builtin commands with
aliases, select another worktree, or add config/objects from host paths.

The repository metadata scan permits at most 10,000 entries and 5 seconds.
Config, packed refs, and aggregate loose ref bytes are each limited to 256 KiB.
Each Git execution has a 5-second timeout and 256 KiB combined stdout/stderr cap;
output is drained incrementally rather than buffered without limit. MCP frames
are capped at 1 MiB. Git is invoked with an argument list, no shell, no pager,
no optional locks, no replacement objects, disabled hooks/fsmonitor/untracked
cache, and no mutation commands. Inspection never writes repository files or
creates plugin artifacts. Bounds are intentionally conservative for an example,
not a general-purpose Git service for large/advanced repositories.

Filesystem authority remains the runtime's responsibility. The checks are not an
atomic snapshot against a concurrently hostile repository writer. macOS runtime
sandboxing also does not promise complete isolation of unrelated host reads.
Use an operator-controlled workspace and executable search path.

## Capabilities and protocol limits

The demo requires MCP adapter invoke, `plugin:git.mcp@0.1.0` load, and
`action:git.mcp@0.1.0/current_branch` / `action:git.mcp@0.1.0/list_branches` invoke
authority. Filesystem authority is exactly `list` and `read` on the canonical
`workspace:///absolute/root` capability resource; no external filesystem grants
or workspace writes are requested. `workflows check` reports missing authority;
`workflows authorize` approves that exact authority for the bound inputs.

Wire messages are compact UTF-8 JSON-RPC 2.0, one object per LF or CRLF line.
The server implements `initialize`, receives `notifications/initialized`, returns
both tools in one complete `tools/list` page, handles `tools/call`, and answers
`ping`. It advertises only `tools`. Malformed UTF-8/JSON, blank or unterminated
frames, duplicates, non-JSON numbers, and oversized frames are rejected.
`Content-Length` framing is unsupported. Oversized/unterminated frames end the
session; other parse errors return `-32700`. Tool requests require initialization.

No resources, prompts, sampling, roots, elicitation, HTTP transport, APP health,
or cooperative cancellation are advertised. Calls run synchronously and bounded;
on cancellation/timeout the Transist adapter terminates the server process group.
No usage, token metrics, artifacts, or plugin evidence are reported or inferred
from MCP text/structured outputs. Transist may separately persist its own runtime
invocation evidence; that is not MCP evidence produced by this server.

## Environment contract

| Variables | Classification / safe value | Use and runtime policy |
| --- | --- | --- |
| `PATH` | Required inherited operator-controlled binary search path, e.g. `/opt/homebrew/bin:/usr/bin:/bin` | `/usr/bin/env` locates `python3`; server resolves `git` once per call. Permit a trusted path at startup. Git's child `PATH` is replaced with Python `os.defpath`, not forwarded. |
| `TMPDIR` | Runtime-managed: manifest `runtime://temp` | Transist injects its invocation-private directory last. The server forwards that value to Git, does not override it, create durable state there, or require a host `/tmp` grant. No user injection/redaction is needed; direct protocol tests use their own disposable `/tmp` resources. |
| `LC_ALL` | Plugin-managed `C` | Created for Git for deterministic locale; not inherited. No policy injection/redaction needed. |
| `GIT_CONFIG_NOSYSTEM` | Plugin-managed `1` | Created for Git to disable system config. No inherited override or policy injection. |
| `GIT_CONFIG_SYSTEM`, `GIT_CONFIG_GLOBAL` | Plugin-managed `/dev/null` | Created for Git to suppress host configuration. No inherited override or policy injection. |
| `GIT_OPTIONAL_LOCKS`, `GIT_TERMINAL_PROMPT` | Plugin-managed `0` | Created for Git: no optional locks or authentication prompts. No injection/redaction needed. |
| `GIT_PAGER` | Plugin-managed `cat` | Created for Git; `--no-pager` disables paging entirely, so no pager executable is launched. No injection/redaction needed. |
| All other inherited variables, including `HOME`, `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_EXEC_PATH`, `GIT_CONFIG*`, `GIT_SSH*`, `GIT_ASKPASS`, `GIT_EXTERNAL_DIFF`, `LD_*`, `DYLD_*` | Not used/forwarded to Git | Child environment is constructed from the above allowlist. Do not inject credentials; no secret bindings exist. Python runs in isolated `-I` mode, ignoring `PYTHONPATH`/`PYTHONHOME` and user site packages. |

`TRANSIST_HOME` selects the CLI runtime home in the commands/tests below; the
server does not read or forward it. Tests may use `PYTHONDONTWRITEBYTECODE=1` to
avoid project caches. Provider temporary storage is always runtime-owned during
installed execution; test fixtures and logs are explicitly confined to `/tmp`.

## Standalone demo commands

From the companion repository root, select an existing ordinary repository
outside the new Transist home. Replace `/absolute/repository` with its absolute
path. These commands inspect only; no Git commit/push or network command runs.

```sh
export TRANSIST_HOME="$(mktemp -d /tmp/git-mcp-home.XXXXXX)"
REPO=/absolute/repository
transist home init --mode standalone
transist plugins install --path plugins/git.mcp/plugin.yaml
transist plugins list
transist plugins inspect git.mcp
transist workflows check --path plugins/git.mcp/workflow.yaml
transist workflows install --path plugins/git.mcp/workflow.yaml --approve
transist workflows check git_mcp_demo@0.1.0 --input "repo_path=$REPO" --local-only --live
# Only if the check reports exact missing authority:
transist workflows authorize git_mcp_demo@0.1.0 --input "repo_path=$REPO" --approve
transist workflows check git_mcp_demo@0.1.0 --input "repo_path=$REPO" --local-only --live
transist workflows run git_mcp_demo@0.1.0 --run-ref git-mcp-demo --input "repo_path=$REPO" --local-only
transist runs show git-mcp-demo
# Run is asynchronous: repeat show until status is success and both steps succeeded.
transist plugins uninstall git.mcp --version 0.1.0
```

Unsigned standalone install is intentional for this example. Governed delivery
requires a signed selected plugin payload, imported publisher trust, and a signed
workflow package; standalone results do not prove governed signing/trust behavior.
`plugin.yaml` implicitly plus the six `package.files` entries are the entire
installed payload. README, demo, dev requirements and tests are unselected.
These commands target public Transist `v0.1.0`: workflow refs are
positional and explicit run names use `--run-ref`. `--live` selects fresh local MCP
readiness probes; validation-only checks do not report plugin readiness. It does
not enable any external Git provider, credential or network call here.

## Self-contained verification

Install `requirements-dev.txt` in a disposable environment if the tools are not
already available (runtime itself needs only the standard library). No shared
test helper is required. Run from the companion repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s plugins/git.mcp/tests -p 'test_*.py' -v > /tmp/git-mcp-unit.log 2>&1
PYTHONDONTWRITEBYTECODE=1 python3 plugins/git.mcp/tests/e2e.py --keep > /tmp/git-mcp-e2e.log 2>&1
# Use verified public v0.1.0 executables via TRANSIST_BIN and TRANSIST_ADMIN_BIN,
# and existing disposable storage via TRANSIST_FIXTURE_BASE.
BLACK_CACHE_DIR=/tmp/git-mcp-black-cache black --check plugins/git.mcp/server.py plugins/git.mcp/tests > /tmp/git-mcp-format.log 2>&1
flake8 --max-line-length=100 --extend-ignore=E203,W503 plugins/git.mcp/server.py plugins/git.mcp/tests > /tmp/git-mcp-lint.log 2>&1
```

The E2E test seeds a deterministic temporary repository, initializes a standalone
home, installs/lists/inspects the exact payload, checks and installs the demo,
checks/authorizes/checks readiness as needed, runs both installed tools, polls
public inspection, verifies real outputs and unchanged repository hashes, and
uninstalls the plugin. `--keep` retains only its `/tmp` fixtures for inspection;
without it fixtures are removed after completion.

## Native per-tool lifecycle and partial verification

Each `_git` call snapshots its inherited runtime group before launch. The server,
ancestors and preexisting members are protected, including across PID reuse.
`process_lifecycle.py` copies Cursor's native ownership core into the finite package
payload. Python `-I` remains enabled; the server adds only its trusted `__file__`
package root for this selected helper, never the workspace or `PYTHONPATH`.
No raw process argv/environment is inspected for ownership. Git's command allowlist,
environment construction, selector output cap and MCP protocol are unchanged.

Git does not create a new session/group, so runtime cancellation can reach it.
Every return/error and `BaseException` cancellation path finalizes in `finally`:
individual identity-verified TERM then KILL within one total four-second deadline,
pipe closure and at most one second to reap the direct child. There is no `killpg`
or unbounded `Popen.__exit__`/final wait. Unverified cleanup returns a generic tool
failure, never successful Git data. Protocol cancellation notifications remain
unadvertised; this does not add asynchronous MCP cancellation handling.

Group isolation is runtime-owned. Newly joining members of that isolated group
are treated as invocation-owned even after reparenting. Provider-created group
escape remains a runtime containment concern. Darwin's immediate birth/group check
before signaling has its OS-native check-to-signal race; Linux pins identity using
pidfds. Neither process metadata access inside an installed sandbox nor live/runtime
interoperability is proven by local fixtures alone.

Historical development checks and blockers are recorded in [VALIDATION.md](VALIDATION.md).
The current public-runtime both-platform gate is [recorded separately](../../docs/validation.md).
The maintained release is `0.1.0`; changed selected bytes/helper selection are
not covered by prior signatures. No signing or live-provider execution was done.
