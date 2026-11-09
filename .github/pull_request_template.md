# Pull Request

## Summary

Describe the change and why it is needed.

## Change Area

- [ ] Plugin package
- [ ] Package README
- [ ] Docs or guide
- [ ] Skill or contributor tooling
- [ ] CI or repository metadata

## Validation

- [ ] Static gate passed (`python3 -B scripts/check_static.py`): layout, schemas, formatting, lint and current-source secrets
- [ ] Relevant local root/offline package tests passed when tooling or behavior changed
- [ ] Plugin install and inspection passed locally when package behavior changed
- [ ] Installed workflow validation/E2E passed locally when applicable
- [ ] Relevant local live test ran when applicable and safe
- [ ] Publication inventory/export and independent export/history secrets scan passed when publishing
- [ ] Not run, with explanation below

List exact commands, results, package version/digest, runtime version/source SHA,
platform/sandbox, dirty local runtime changes, and pending or skipped checks.
CI runs static checks only. A static or offline pass does not prove installed,
governed, or live-provider acceptance. Redact credentials, customer data, private
paths, prompts, and run artifacts from logs.

## Tests

- [ ] Added tests
- [ ] Updated existing tests
- [ ] No test changes needed

## Docs

- [ ] Updated docs
- [ ] No doc changes needed

List the docs changed or explain why docs are unchanged.

## Compatibility

- [ ] No user-visible compatibility impact
- [ ] Backward-compatible additive change
- [ ] Breaking or behavior-changing update

Describe any plugin id, action, schema, workflow, or contributor-facing impact.

## DCO

- [ ] Every commit includes a `Signed-off-by:` line (`git commit -s`)
- [ ] I have the right to submit this contribution under Apache-2.0
- [ ] Vendored MPL-2.0 contract licenses and upstream attribution are retained when affected
