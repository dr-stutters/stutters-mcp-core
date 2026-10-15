# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (0.x: minor bumps may break consumers).

## [Unreleased]

### Added
- MCP prompts (#7): `ToolRegistrar.prompt(...)` / `PromptRegistrar` register workflow prompts `<prefix>_<workflow>` with
  `meta={"uses": [...], "tools": [...], "tier": ...}`; `tool_ref()` / `prompts.find_tool_refs()` for tool names in prompt bodies;
  `testing.prompt_contract_problems` / `assert_prompt_contract` (naming, description, argument docs, `uses`, local tool references).
  `<prefix>_status` lists the server's prompts.
- `fetch_page(..., max_page=N)` overrides a pager's page size for one call (per-endpoint caps); also `fetch_all(..., max_page=)` (#10).
  The `Pager` protocol gained the optional keyword; a custom pager without it still works with `fetch_all` at runtime but should add it to satisfy mypy.

## [0.1.2] - 2026-10-15

### Fixed
- `list_result` kept `Page.more` only when it truncated: lists from APIs without a total (Catalyst Center)
  lost `next_offset`. It now carries the source page's `more` flag.

## [0.1.1] - 2026-10-14

Requests from the first server builds (see ~/MCP-Suite/CORE-REQUESTS.md).

### Added
- `SpecSource.path_params`: defaults for `{placeholders}` in `<prefix>_api_call` paths (e.g. FMC `domainUUID`).
- `ApiClient.request(ok_status=...)`: return listed error statuses instead of raising, with auth and retries intact (ISE MnT).
- `formatting.mask_secrets()` (mask without dropping anything); `NOT_SECRET_KEYS` allow-list.
- `docs/templates/dockerignore`.

### Fixed
- Audit log masks secrets nested inside tool arguments (e.g. `api_call` bodies).
- Flag-like values ("true"/"false"/"enabled"...) under secret-looking keys are no longer masked;
  `passwordIDStore`, `enableMultiSecret` allow-listed.
- Server startup no longer fails when the status probe raises a non-upstream error; `require_credentials`
  raises `AuthError`; `<prefix>_status` reports configuration problems.
- Dockerfile template copies `LICENSE` (needed by `license-files`).

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

### Security (from pre-release review)
- Raw non-GET `<prefix>_api_call` requires the dangerous tier; its responses are secret-masked.
- `validate_path` rejects percent-encoded dot/slash segments.
- Secret masking covers nested values and more key names; BGP `community` values are no longer masked;
  `metadata` is no longer dropped.
- `Page.more`/`complete` signal possibly-truncated results when the API reports no total; `fetch_all` returns a Page.

## [0.0.1] - 2026-10-14

### Added
- Project skeleton, plan (`docs/PLAN.md`), ADR-001 and ADR-002.
- Reusable GitHub Actions workflows for consumer servers: `python-ci.yml`, `python-release.yml`.
