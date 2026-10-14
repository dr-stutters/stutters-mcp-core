# ADR-001: Share code through a versioned core package

- Status: accepted
- Date: 2026-10-14

## Context

Four FastMCP servers (CML, ISE, Catalyst Center, FMC) need the same plumbing: settings, transports,
inbound bearer auth, an upstream HTTP client with login/refresh, retries, rate limiting and paging,
error mapping, output formatting, preview-first writes, a task poller, OpenAPI spec tools, an audit
log and a test kit. Each server must remain its own project (repo, README, releases).

Options considered: a project template (copier/cookiecutter), a monorepo, or a shared runtime package.

## Decision

- All shared runtime code lives in `stutters-mcp-core` (import `stutters_mcp_core`), one repo.
- Each server is a separate repo that depends on the core.
- **Distribution (for now): git tags, not PyPI.** Servers declare
  `stutters-mcp-core @ git+https://github.com/dr-stutters/stutters-mcp-core@vX.Y.Z` and `uv.lock`
  records the commit. CI still builds wheels and the release workflow has a disabled PyPI step, so
  switching to PyPI later only changes the dependency line.
- During development a server may use a uv `path` source (`../stutters-mcp-core`, editable); it is
  swapped for a git tag before a server release.
- **Versioning:** SemVer, 0.x until all four servers have used one core minor without changes. In
  0.x a minor bump may break consumers and must be listed under "Changed"/"Removed" in CHANGELOG.
- **Growth rule:** a feature enters core only when the server being built needs it; it is generalised
  when a second server needs it. Product knowledge (URLs, fields, quirks) never enters core.
- NetBox-MCP stays standalone; it is the design reference, not a consumer.

## Consequences

- One fix to auth/retry/paging reaches all servers through a version bump.
- Private repos for now: consumer CI needs a read token for the core repo (secret `CORE_READ_TOKEN`),
  and core's reusable workflows are shared at the "user" access level.
- Core changes need care: a breaking change means updating up to four servers.
