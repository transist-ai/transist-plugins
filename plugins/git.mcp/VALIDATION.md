# Local validation

> Historical development evidence. All results and commands below retain their
> original runtime/source scope, not public v0.1.0 acceptance. The final native
> macOS/Linux release gate is [recorded separately](../../docs/validation.md).

The maintained release is now `0.1.0`. The dated records below describe earlier
payloads and do not verify the new manifest/handshake bytes or their signatures.

## Native lifecycle follow-up — 2026-10-09

Current checks (older evidence below describes the earlier session-owning payload):

```sh
python -B -m unittest discover -s plugins/git.mcp/tests -p 'test_*.py' -v > /tmp/git-mcp-lifecycle-tests.log 2>&1
python -m black --check plugins/git.mcp/server.py plugins/git.mcp/process_lifecycle.py plugins/git.mcp/tests
python -m flake8 --max-line-length=88 --extend-ignore=E203,W503 plugins/git.mcp/server.py plugins/git.mcp/process_lifecycle.py plugins/git.mcp/tests
```

`python` above is an isolated Python 3.11 venv with `tests/requirements.txt`.
The expanded suite covers per-tool inherited group/session behavior, protected
siblings, normal/nonzero/orphan/held-pipe cleanup, cancellation, timeout/output
limits, TERM resistance, PID/birth/group safety, and fail-closed cleanup proof.

| Check | Exact current result |
| --- | --- |
| macOS arm64 / Python 3.11 offline suite | 18 tests passed in 13.796s; includes 5 isolated native invocation modes |
| Linux arm64 / Python 3.11 native stdlib suite | 4 tests passed in 2.603s; includes normal, nonzero, orphan, held-pipe and cancellation modes |
| Black 25.1.0 (both packages' changed Python/check assets) | Passed; 8 files unchanged |
| flake8 7.2.0, 88-column limit | Passed; no diagnostics |
| Current installed Transist 0.12.0 / macOS sandbox-exec E2E | Passed: selected payload, readiness, both outputs, unchanged repository and uninstall |

Linux native helper checks use the existing
`transist-publication-linux-unit:local` image with a read-only package mount and
network disabled; see `/tmp/git-mcp-lifecycle-linux-tests.log`. The full Linux suite
is blocked in that image by missing `jsonschema`; the native helper suite uses
stdlib only. Local unit checks do not establish sandbox process-observation grants.

The earlier installed CLI-syntax blocker is resolved. The runner and README use
the actual installed `0.12.0` help: `workflows check <id@version>`,
`workflows authorize <id@version> --approve`, and
`workflows run <id@version> --run-ref <runid>`. Installed readiness checks explicitly
select `--live` (fresh local MCP probes); validation-only checks otherwise report
`not_ready` without probing plugins. Authorization is called only for exact missing
capabilities; the successful fresh standalone fixture needed no authority changes.

```sh
TMPDIR=/tmp python3 -B plugins/git.mcp/tests/e2e.py --transist "$TRANSIST_BIN" > /tmp/git-mcp-lifecycle-e2e.log 2>&1
```

The current E2E used a disposable `/tmp` home, source and seeded repository, native
`sandbox-exec`, read-only workspace and network-none policy. Install/list/inspect,
exact selected payload, authored check/install, both fresh readiness checks,
actual installed invocation, terminal success, branch `main`, branches
`[alpha, main, zeta]`, unchanged repository hashes, no plugin artifacts/usage and
uninstall all passed. See `/tmp/git-mcp-lifecycle-e2e.log`. Runtime version strings
do not establish a clean canonical source pin; this validates the current local
installed binary, not a canonical release or broader Linux installed acceptance.
Governed trust/signing and live providers were not exercised; no commits or pushes.
Version at that checkpoint was `1.0.0`; previous signatures did not cover it.

## Historical validation — 2026-10-05

## Commands and results

Executed from the companion repository root on `publication-reorg`:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s plugins/git.mcp/tests -p 'test_*.py' -v > /tmp/git-mcp-unit.log 2>&1
PYTHONDONTWRITEBYTECODE=1 python3 plugins/git.mcp/tests/e2e.py > /tmp/git-mcp-e2e.log 2>&1
python3 -m black --check plugins/git.mcp/server.py plugins/git.mcp/tests
python3 -m flake8 --max-line-length=100 --extend-ignore=E203,W503 plugins/git.mcp/server.py plugins/git.mcp/tests
```

- **13 tests passed** after the cleanup regression update: NDJSON/CRLF,
  initialize/initialized, discovery, ping,
  matching IDs, schemas/structured outputs, unborn/detached HEAD, local-only
  sorted lists, default/exact/maximum limits, packed refs and Unicode names,
  malformed calls/framing, authority-shaped inputs, metadata/config escapes,
  inherited Git redirection, executable side-effect settings, timeout and output
  limits, and oversized response IDs.
- **Black passed**, four files unchanged; **flake8 passed**, no diagnostics.
- **Installed-workflow E2E passed** using local source-built Transist `0.12.0`,
  Python `3.14.6`, and macOS `sandbox-exec`, with read-only workspace and no network.

## Public lifecycle exercised

The self-contained E2E script ran these commands against a new standalone home
and deterministic temporary repository (see its log for full absolute bindings):

| Command | Observed result |
| --- | --- |
| `home init --mode standalone` | Disposable standalone home initialized |
| `plugins install --path <package>/plugin.yaml` | `git.mcp@1.0.0` installed; prerequisites passed |
| `plugins list` / `plugins inspect git.mcp` | Correct version, MCP tools bindings, locality and temporary sentinel |
| Exact installed payload assertion | Only implicit manifest plus entrypoint and four schemas installed |
| `workflows check --path <package>/workflow.yaml` | Authored definition valid; required input appropriately not evaluated |
| `workflows install --path <package>/workflow.yaml --approve` | Unsigned standalone demo installed |
| `workflows check --workflow git_mcp_demo@1.0.0 --input repo_path=<repo> --local-only` | Ready; both MCP handshakes passed |
| `workflows authorize ... --approve` | Not needed: the fresh standalone home's check was already ready; script calls it only if authority is missing |
| Second installed readiness check | Ready |
| `workflows run git-mcp-e2e --workflow git_mcp_demo@1.0.0 --input repo_path=<repo> --local-only` | Run created and started asynchronously |
| `runs show git-mcp-e2e` (polled) | Final run `success`; both steps `succeeded` |
| Actual persisted action outputs | Branch `main`, detached `false`; branches `[alpha, main, zeta]`, truncated `false` |
| Repository snapshot assertion | Every repository file hash unchanged |
| `plugins uninstall git.mcp --version 1.0.0` | Installed package removed |

Fixtures use disposable system-temporary directories; `--keep` is an optional
local diagnostic setting. The plugin is uninstalled before cleanup.
The E2E test omits plugin artifacts/usage and
does not treat runtime-generated invoke evidence as evidence from MCP.

## Limits and blockers

Independent-review follow-up: 13 unit tests passed, including wrapper/grandchild
cleanup for timeout, output limit, unexpected I/O failure and cancellation.
The wrapper reaps its grandchild; the caller reaps the wrapper, within the test's
two-second bound. A SIGTERM-resistant executable is killed with SIGKILL and reaped.
Installed-workflow E2E passed again on source-built `0.12.0`.
Logs: `/tmp/review-git-mcp-unit.log` and `/tmp/review-git-mcp-e2e.log`.

No blocker to the tested standalone lifecycle. Governed package signing/trust
and Linux sandbox execution were not exercised. The supported repository shape
and conservative bounds are deliberate and documented in README; linked
worktrees, alternate-object repositories, and concurrent adversarial mutation
are not supported guarantees. Validation made no external provider requests and
did not commit or push companion-repository changes.
