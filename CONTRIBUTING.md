# Contributing

Thanks for helping. This repository is the shared core of a family of MCP servers for network platforms
(Cisco Modeling Labs, ISE, Catalyst Center, Secure Firewall Management Center). Product servers live in
their own repositories and follow [docs/BUILDING-A-SERVER.md](docs/BUILDING-A-SERVER.md).

## Development setup

```bash
uv sync                    # Python 3.12+, uv 0.10+
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
```

CI runs exactly those, plus `uv build` and pip-audit. Live tests (`-m live`) need a lab appliance and are
skipped otherwise; never point them at production.

## Ground rules

- **Backwards compatibility.** Four servers pin core by tag. New behaviour is opt-in and defaults stay the
  same; when something must change, say so in the CHANGELOG under *Changed* and in an *Adopting* note.
- **Safety first.** Read-only by default. Anything that changes an appliance is a write or dangerous tool and
  goes through `preview_or_apply` (preview, then confirm with a single-use token). Don't add bypasses.
- **No secrets, no lab data.** Never commit `.env`, keys, captures, real hostnames or addresses from a lab.
  Use documentation ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24) and `example.com` in tests and
  docs. Run `uv run python -m stutters_mcp_core.scrub_check --head` before opening a PR.
- **Pull-only.** Servers query platforms on demand; they don't receive events (ADR-003).
- **Design decisions** that affect every server get an ADR in `docs/adr/`.

## Pull requests

1. One topic per PR; the description says what changed, why, and `Closes #<issue>`.
2. Tests for new behaviour (unit tests with respx mocks; the demo server in `tests/unit/demo.py` is the
   reference consumer). Docs updated (`docs/PRODUCTION.md` for operator settings, BUILDING-A-SERVER for server
   authors), and a CHANGELOG entry under *Unreleased*.
3. CI green and a review with no blocking findings, then squash-merge.

## Releases

Bump `version` in `pyproject.toml`, move *Unreleased* to the new version in the CHANGELOG (with *Adopting*
notes for server builders), merge, then tag `vX.Y.Z` on main; the release workflow builds and publishes.

## Code of conduct

Be respectful and constructive. Harassment or abuse isn't tolerated; report it to the maintainer.
