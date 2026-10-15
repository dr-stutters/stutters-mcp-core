# Security policy

This policy covers `stutters-mcp-core` and the servers built on it (cml-, fmc-, catc- and ise-mcp-server).
The repositories are private today; this file is part of the groundwork for a public release.

## Reporting a vulnerability

Please **don't open a public issue**. Report privately through GitHub: *Security → Report a vulnerability*
on the affected repository (private vulnerability reporting), or email the maintainer listed in
`pyproject.toml`. Include the version (`<server> --version` or the image tag), what you did, what happened,
and whether a lab appliance or a production one was involved.

You'll get an acknowledgement within 5 working days. Fixes ship as a patch release of the affected
package(s) with a CHANGELOG entry; reporters are credited unless they ask not to be.

## Supported versions

Only the latest minor release of each package receives fixes (0.x: minor releases may change APIs; see each
CHANGELOG's "Adopting" notes).

## What's in scope

- Anything that lets an MCP caller do more than its configuration allows: bypassing write tiers, preview/confirm
  tokens, safety rails (`MCP_DRY_RUN`, change windows, deny lists, blast radius, two-person confirm), per-user roles,
  or HTTP authentication (bearer tokens, OAuth).
- Secret leakage: appliance credentials, tokens or keys reaching logs, the audit log, `<prefix>_status`, tool
  output or error messages.
- Injection into the managed appliance's API through tool arguments (paths, filters, payloads) beyond what the
  tool documents.

Out of scope: vulnerabilities in the Cisco products themselves (report those to Cisco PSIRT), and anything that
requires an operator to configure the server insecurely on purpose (e.g. `VERIFY_SSL=false`, `MCP_OAUTH_ALLOWED_USERS=*`).

## How the project handles security

- Least privilege by default: read-only unless `MCP_ENABLE_WRITES`; disruptive tools need `MCP_ENABLE_DANGEROUS`;
  every write is preview-first with a single-use token (core ADR-002).
- HTTP requires authentication (named bearer tokens stored as SHA-256 hashes, and/or OAuth 2.1).
- Secrets are `SecretStr`, can come from files or secret managers, and are scrubbed from logs and errors
  (docs/PRODUCTION.md).
- CI runs pip-audit on every change; releases carry SBOMs and a Trivy image scan (docs/SUPPLY-CHAIN.md).
- `python -m stutters_mcp_core.scrub_check` reports lab data and secrets in git history before anything goes public.
