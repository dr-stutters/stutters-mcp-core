# stutters-mcp-core

Shared [FastMCP](https://gofastmcp.com) foundation for a family of network-platform MCP servers:

| Server | Product |
|---|---|
| [cml-mcp-server](https://github.com/dr-stutters/cml-mcp-server) | Cisco Modeling Labs |
| [ise-mcp-server](https://github.com/dr-stutters/ise-mcp-server) | Cisco Identity Services Engine |
| [catc-mcp-server](https://github.com/dr-stutters/catc-mcp-server) | Cisco Catalyst Center |
| [fmc-mcp-server](https://github.com/dr-stutters/fmc-mcp-server) | Cisco Secure Firewall Management Center |

> Status: 0.1.0. See [docs/BUILDING-A-SERVER.md](docs/BUILDING-A-SERVER.md) to build a server, and
> [docs/PLAN.md](docs/PLAN.md) for the design and roadmap.

## What lives here

Settings, transports (stdio / Streamable HTTP), inbound bearer-token auth, an upstream HTTP client
(login/refresh auth strategies, retries, rate limiting, pagination), error mapping, output formatting,
preview-first write guard, async task poller, OpenAPI spec search tools, audit logging, and a pytest
plugin with a tool-contract linter. Product-specific code lives in each server repo.

## Using it from a server

```toml
[project]
dependencies = ["stutters-mcp-core"]

[tool.uv.sources]
stutters-mcp-core = { git = "https://github.com/dr-stutters/stutters-mcp-core", tag = "v0.1.0" }
```

CI for a server:

```yaml
jobs:
  ci:
    uses: dr-stutters/stutters-mcp-core/.github/workflows/python-ci.yml@main
    secrets: inherit   # provides CORE_READ_TOKEN while the repos are private
```

## Decisions

- [ADR-001: Shared core package](docs/adr/001-shared-core-package.md)
- [ADR-002: Write tiers and preview tokens](docs/adr/002-write-tiers-and-preview-tokens.md)

## Development

```bash
uv sync
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
```

Not affiliated with or endorsed by Cisco Systems, Inc.

MIT licensed.
