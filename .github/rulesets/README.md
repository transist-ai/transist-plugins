# GitHub protection snapshots

These JSON files record the intended post-cutover `transist-ai/transist-plugins`
rulesets and selected repository settings, not necessarily the current live state.
They **do not apply themselves**: checking them in, merging a PR, or running CI
does not configure GitHub. Live repository settings remain authoritative.
Each ruleset contains only `name`, `target`, `enforcement`, `conditions`, `rules`
and `bypass_actors`. API response IDs, timestamps and links are omitted; the
required check's `integration_id` is retained because it binds the check to its
actual GitHub App. The separate [repository settings](../repository-settings.json)
contain only the selected repository-level PATCH fields, not a ruleset wrapper.

## Effective protections

- `linear-pr-branches` requires linear history on branches other than the default
  branch and literal `main`. Keep PR branches current by **rebasing**, without
  merge commits. After authorization to rewrite your PR branch, publish the
  rebased commits with `git push --force-with-lease`; never force-push `main`.
- `main-branch-protection` separately protects the default branch: no deletion or
  non-fast-forward updates, linear history, a PR with resolved review threads,
  and the actual GitHub Actions **`Static checks`** status check, with strict
  up-to-date checking. There is no minimum approving-review count. A local check
  or a differently named job does not satisfy the required GitHub check.
- The intended post-cutover `main-dco-signoff` snapshot is disabled. Until the
  authorized cutover, the existing live ruleset remains active and checks a
  matching sign-off line on `main`. After cutover, `main-dco` checks every new
  commit pushed to literal `main` after it lands; it is not a required PR check
  or a release gate. It checks trailer format and presence, not identity or rights.
  Follow [CONTRIBUTING](../../CONTRIBUTING.md) for the human merger's DCO duties.
- Squash is the only final PR merge method. Rebase PR updates locally; squash
  only at final integration. Repository settings disable merge commits, rebase
  merges and auto-merge, allow branch updates, retain merged branches, and set
  `web_commit_signoff_required` to false after cutover. The final squash title
  uses `COMMIT_OR_PR_TITLE` and its body uses `COMMIT_MESSAGES`; review the final
  message and DCO trailers.
- `v-tag-protection` makes existing `v*` tags immutable: no updates, force updates,
  or deletion. Tag creation is not blocked by this ruleset.
- The three other rulesets remain active. All four snapshots have empty
  `bypass_actors`: no configured bypass, including for maintainers.

## Staged DCO rollout

Before any live change, an administrator must re-read repository visibility,
settings and effective `main` rules, including inherited rules and required
checks; stop on unexplained drift, save editable rollback settings, and obtain
approval. Merge the rollout PR while the live `main-dco-signoff` rule is still
active, preserving the required sign-off. Require a green `main-dco` run on the
rollout merge SHA before disabling that dedicated ruleset. Then set
`web_commit_signoff_required=false` and verify effective live settings. Preserve
`Static checks` and all unrelated protections; do not require `main-dco` on PRs.
Snapshots alone do not perform these changes. On the first approved post-cutover
PR, verify unsigned feature commits pass the normal PR gate, sign the squash
message, and verify `main-dco` on the landed SHA. Do not rewrite historical
commits or tags.

## Refresh from GitHub (read-only API calls)

From the repository root, with authenticated `gh`, export the live objects.
Resolve each name uniquely to its current ID rather than checking API IDs into
the snapshot. This writes local files only; review the resulting diff. Before
cutover, a refresh will overwrite the intended DCO and web sign-off values with
the live values; preserve or restore the reviewed intended values deliberately.

```sh
set -eu
repo=transist-ai/transist-plugins
for name in linear-pr-branches main-branch-protection main-dco-signoff v-tag-protection; do
  id="$(gh api "repos/$repo/rulesets" --paginate --jq \
    ".[] | select(.name == \"$name\") | .id")"
  case "$id" in ''|*[!0-9]*) exit 1 ;; esac
  gh api "repos/$repo/rulesets/$id" \
    --jq '{name,target,enforcement,conditions,rules,bypass_actors}' \
    > ".github/rulesets/$name.json"
done
gh api "repos/$repo" \
  --jq '{allow_merge_commit,allow_squash_merge,allow_rebase_merge,allow_auto_merge,allow_update_branch,delete_branch_on_merge,web_commit_signoff_required,squash_merge_commit_title,squash_merge_commit_message}' \
  > .github/repository-settings.json
```

Do not export the whole repository response: it includes unrelated metadata.
Validate JSON and run the static gate documented in `CONTRIBUTING.md` after refresh.

## Applying a separately authorized administrative change

A refresh does not authorize changing GitHub. If an administrator is explicitly
authorized to apply a reviewed snapshot, first resolve the **existing** ruleset
by its exact name and require one match, then update that ID with `PUT`:

```sh
repo=transist-ai/transist-plugins
name=main-branch-protection
id="$(gh api "repos/$repo/rulesets" --paginate --jq \
  ".[] | select(.name == \"$name\") | .id")" || exit 1
case "$id" in ''|*[!0-9]*) exit 1 ;; esac
gh api --method PUT "repos/$repo/rulesets/$id" \
  --input ".github/rulesets/$name.json"
```

Repeat the lookup for each intended ruleset. Stop on missing or ambiguous names;
never blindly `POST` duplicates. Repository settings are a separate operation:

```sh
gh api --method PATCH "repos/$repo" --input .github/repository-settings.json
```

After an authorized change, re-export both surfaces and compare them to the
reviewed snapshots. No workflow in this directory applies these files.
