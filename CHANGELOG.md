# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (0.x: minor bumps may break consumers).

## [Unreleased]

## [0.1.0] - 2026-10-14

### Added
- `BaseServerSettings` / `server_config(prefix)`: per-product `<P>_*` upstream settings plus shared `MCP_*` settings.
- `build_server(ServerSpec, ...)`: lifespan-managed product client, automatic `<prefix>_status`, `/health`,
  audit middleware, writes/dangerous instructions.
- `run_cli`: `serve` (stdio/HTTP, fail-closed bearer auth), `token <name>`, `check`.
- Inbound named bearer tokens (sha256), `current_caller()`.
- `http.ApiClient`: retries with backoff (429 for any method, 5xx for idempotent), Retry-After, TLS hints,
  error mapping to `AuthError`/`NotFound`/`Conflict`/`ValidationFailed`/`RateLimited`/`Unreachable`/`UpstreamTimeout`.
- Auth strategies: `BasicAuth`, `StaticHeaderAuth`, `LoginTokenAuth` (single-flight login, expiry, refresh hook, re-login on 401).
- `RateLimiter` (requests/minute + concurrency cap, shareable across clients).
- Pagers: `OffsetLimit` (0/1-based), `PageNumber`, `NoPaging`; `Page` with paging metadata.
- Formatting: `compact` (noise removal, secret masking), `list_result`/`object_result` with size budget and Markdown.
- Writes: `ToolRegistrar` tiers (read/write/dangerous) with annotations, preview-first `preview_or_apply`, `field_changes`.
- `poll_until` task poller with MCP progress notifications.
- OpenAPI 3 / Swagger 2 spec index with disk cache, multi-source; `<prefix>_search_spec`, `<prefix>_get_definition`, `<prefix>_api_call`.
- `testing`: tool contract linter, `call_tool`, `live_settings` (auto-skip when the lab is unreachable).
- docs/BUILDING-A-SERVER.md and Docker/compose templates.

## [0.0.1] - 2026-10-14

### Added
- Project skeleton, plan (`docs/PLAN.md`), ADR-001 and ADR-002.
- Reusable GitHub Actions workflows for consumer servers: `python-ci.yml`, `python-release.yml`.
