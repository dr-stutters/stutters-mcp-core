# stutters-mcp-core

Shared [FastMCP](https://gofastmcp.com) foundation for a family of network-platform MCP servers:

| Server | Product |
|---|---|
| [cml-mcp-server](https://github.com/dr-stutters/cml-mcp-server) | Cisco Modeling Labs |
| [ise-mcp-server](https://github.com/dr-stutters/ise-mcp-server) | Cisco Identity Services Engine |
| [catc-mcp-server](https://github.com/dr-stutters/catc-mcp-server) | Cisco Catalyst Center |
| [fmc-mcp-server](https://github.com/dr-stutters/fmc-mcp-server) | Cisco Secure Firewall Management Center |

> **Disclaimer.** These are not official Cisco products or repositories, and they aren't supported or endorsed
> by Cisco. They're a personal project by [@dr-stutters](https://github.com/dr-stutters), for lab and learning
> use only: don't point them at production systems. They're provided as is, with no warranty (see the
> [license](LICENSE)). Cisco and the Cisco product names here are trademarks of Cisco Systems, Inc.

> Status: 0.12.0 (see [CHANGELOG.md](CHANGELOG.md)). See [docs/BUILDING-A-SERVER.md](docs/BUILDING-A-SERVER.md) to
> build a server, [docs/PRODUCTION.md](docs/PRODUCTION.md) for the operator controls, and [ROADMAP.md](ROADMAP.md).

## What lives here

Settings, transports (stdio / Streamable HTTP), inbound bearer-token auth, an upstream HTTP client
(login/refresh auth strategies, retries, rate limiting, pagination), error mapping, output formatting,
preview-first write guard, async task poller, OpenAPI spec search tools, audit logging, MCP prompts, and
test helpers: tool and prompt contract linters plus the prompt test kit (manifests exported with
`<server> tools --json`, cross-server reference checks, prompt snapshots, scenario manifests checked against
those manifests ([docs/scenarios/README.md](docs/scenarios/README.md))) and the suite prompt pack
(`suite_*` cross-product prompts every server registers, [docs/SUITE-PROMPTS.md](docs/SUITE-PROMPTS.md)), with
the snapshot contract for pre/post change checks, health and per-object configuration digests ([docs/SNAPSHOTS.md](docs/SNAPSHOTS.md)). Product-specific code lives in each server repo.

Production controls, all opt-in ([docs/PRODUCTION.md](docs/PRODUCTION.md)): safety rails (dry-run, change
windows, deny/allow lists, blast-radius limits, two-person confirm, a backup-age requirement before dangerous changes with `MCP_REQUIRE_BACKUP_HOURS`), per-user roles, OAuth 2.1 next to named
tokens, secret files/loaders and log scrubbing, Prometheus `/metrics`, several appliances per server with
per-instance write rules and `instance='*'` fan-out on reads, per-caller rate limits, audit-log rotation, MCP background tasks, JSON-text list/dict arguments (`MCP_PARSE_JSON_ARGS`) and toolsets (`<P>_TOOLSETS`, with a
tool-surface budget lint for CI). Supply chain:
[docs/SUPPLY-CHAIN.md](docs/SUPPLY-CHAIN.md); security policy: [SECURITY.md](SECURITY.md).

## Using it from a server

Servers bundle core into their own package as `<pkg>._core` ([ADR-005](docs/adr/005-bundle-core-into-servers.md)),
so a server wheel or image needs nothing else. Copy [`scripts/bundle_core.py`](scripts/bundle_core.py) into the
server, pin a tag, mark where core's dependencies go, and import `<pkg>._core`:

```toml
[project]
dependencies = [
    # >>> bundled stutters-mcp-core
    # <<< bundled stutters-mcp-core
]

[tool.bundle-core]
tag = "v0.12.4"
commit = "70a6bafab04a94df152944acc9f024562fbd7020"   # the tag's commit (git rev-parse v0.12.4^{commit})
extras = []                                           # e.g. ["tasks"] for MCP background tasks
```

```bash
echo "src/<pkg>/_core/" >> .gitignore
python scripts/bundle_core.py vendor     # after a clone or a pin change; fills the dependency block
uv lock && uv sync
python scripts/bundle_core.py update --tag v0.14.0   # later: move the pin
```

CI for a server:

```yaml
jobs:
  ci:
    uses: dr-stutters/stutters-mcp-core/.github/workflows/python-ci.yml@main
    with: {bundle-core: true}
  bundle:   # drift check, wheel + sdist, suite against the installed wheel (pip --no-index)
    uses: dr-stutters/stutters-mcp-core/.github/workflows/bundle-core.yml@main
```

## Decisions

- [ADR-001: Shared core package](docs/adr/001-shared-core-package.md)
- [ADR-002: Write tiers and preview tokens](docs/adr/002-write-tiers-and-preview-tokens.md)
- [ADR-005: Bundle core into each server package](docs/adr/005-bundle-core-into-servers.md)

## Development

```bash
uv sync
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
```

Not affiliated with or endorsed by Cisco Systems, Inc.

MIT licensed.
