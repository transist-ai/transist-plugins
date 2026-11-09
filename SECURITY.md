# Security Policy

We take the security of Transist Plugins seriously.

## How to Report a Vulnerability

Do not disclose vulnerabilities in a normal issue, pull request, Discussion,
or other non-private channel.

### Supported Versions

Security support applies to the latest released version of an affected plugin
package. Development, unreleased, and locally modified package versions are not
supported; reproduce and report against the latest released package version
where possible.

### Reporting Channels

- **GitHub private vulnerability reporting** at
  <https://github.com/transist-ai/transist-plugins/security/advisories/new> may
  be used **only if the private reporting option is available** on the repository.
- The listed email contact is **`security@transist.ai`**. Delivery has not been
  verified for this publication; it is not an always-available fallback. If no
  private route is available or confirmed, keep vulnerability details private.

### Response Targets

These are non-binding targets, not an SLA:

- Acknowledge reports within **3 business days**.
- Complete initial triage within **7 business days**.

Fix timing, release timing, and disclosure timing are determined by
maintainers.

### What to Include

Include as much detail as possible:

- affected plugin package, version, or commit;
- impact assessment;
- reproduction steps and required inputs or environment; and
- suggested mitigations, if available.

## Handling

Reports are handled privately and triaged by maintainers with security
authority. Fix timing, release timing, and disclosure timing are determined by
maintainers.

## Scope Notes

- This repository contains plugin packages and plugin-authoring guidance. For a
  vulnerability in the Transist runtime, CLI, release artifacts, trust store,
  or workflow engine, report it through the
  [Transist source repository security policy](https://github.com/transist-ai/transist/blob/v0.1.0/SECURITY.md).
- Plugin packages must not persist secrets, private keys, or credentials in
  logs, package payloads, test fixtures, or generated artifacts.
- Report suspected secrets, signing-key exposure, untrusted package execution,
  policy bypasses, and external data-transfer defects through the same private
  reporting path.
