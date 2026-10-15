# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (0.x: minor bumps may break consumers).

## [Unreleased]

## [0.4.0] - 2026-10-15

### Added
- **Prompt test kit (C9).** `<server> tools [--json] [--out FILE] [--tier read|write|dangerous]` lists a
  server's tools and prompts; `--json` writes a manifest (`stutters_mcp_core.manifest`, format 1: tools with
  tiers, prompts with `uses`, arguments and the tool names they reference). Built without the lifespan, so
  no appliance connection and no credentials are needed (placeholder URL when none is configured).
- `testing.load_tool_manifest(path)`, `load_tool_manifests(*files_or_dirs)` (→ `{prefix: names}`),
  `read_tool_manifests`, and `prompt_contract_problems` / `assert_prompt_contract(external_tools=...)`:
  references to another server's tools are checked against its manifest (or a hand-written list), so
  cross-product prompts such as `ise_quarantine_host` → `fmc_contain_host` are verified in CI without
  running servers.
- `testing.inbound_reference_problems` / `assert_inbound_references`: the reverse check, so a server
  learns when it renames or drops a tool another server's prompt uses.
- `testing.render_prompts`, `prompt_snapshot_problems` / `assert_prompt_snapshots`: every prompt rendered
  to `<dir>/<prompt>.md` and compared with a diff; `UPDATE_PROMPT_SNAPSHOTS=1` accepts changes.
- `reg.prompt(example={...})`: valid string arguments the kit and the manifest render a prompt with
  when a test passes no `samples`.
- **`<VAR>_FILE` for secret settings (G4, partial).** Every `SecretStr` setting (`<P>_PASSWORD`,
  `<P>_TOKEN`, and server-specific ones) can be read from a file, e.g. `ISE_PASSWORD_FILE=/run/secrets/ise`
  (Docker/Kubernetes secrets). Setting both the variable and its `_FILE` is an error.

### Changed
- Prompt placeholder arguments handle `Optional[...]`, defaults and numeric minimums (an `int | None`
  argument used to get `"example"` and fail to render).
- The CLI reports configuration problems from `_FILE` secrets the same way as validation errors.

All additions; the 0.3.1 APIs are unchanged (the four servers' unit suites pass unmodified against 0.4.0).

## [0.3.1] - 2026-10-15

### Added
- `<prefix>_get_definition(..., examples=True)` returns the spec's request/response examples and the full
  operation and parameter descriptions (normally cut to one short line). Requested by fmc-mcp-server: FMC's
  real minimum payloads and filter syntax only appear there. Spec index format 3 (caches rebuild automatically).

## [0.3.0] - 2026-10-15

### Removed
- The event framework and inbound receivers added in 0.2.0 (`stutters_mcp_core.events`: `EventSource`,
  `PollingEventSource`, `EventBuffer`/`EventHub`, `SyslogReceiver`, `WebhookReceiver`, `<prefix>_list_events`,
  `<prefix>_event_sources`, `events://` resources, `build_server(event_sources=, event_options=)` and the
  `MCP_EVENT_*` / `MCP_SYSLOG_*` / `MCP_WEBHOOK*` settings). The servers stay **pull-only**: they read logs
  and events from the platforms when a tool is called, and never receive pushed data. See ADR-003.
  No released server used these APIs.

### Kept from 0.2.0
- MCP prompts (`PromptRegistrar`, `tool_ref`, `assert_prompt_contract`) and `fetch_page(max_page=...)`.

## [0.2.0] - 2026-10-15 (superseded by 0.3.0; its event APIs were removed)

Roadmap "Now" (C2–C5). Backwards compatible with 0.1.x: servers that pass no `event_sources` and
register no prompts see no change, and every new setting is off by default.

### Added
- MCP prompts (#7): `ToolRegistrar.prompt(...)` / `PromptRegistrar` register workflow prompts `<prefix>_<workflow>` with
  `meta={"uses": [...], "tools": [...], "tier": ...}`; `tool_ref()` / `prompts.find_tool_refs()` for tool names in prompt bodies;
  `testing.prompt_contract_problems` / `assert_prompt_contract` (naming, description, argument docs, `uses`, local tool references).
  `<prefix>_status` lists the server's prompts.
- `fetch_page(..., max_page=N)` overrides a pager's page size for one call (per-endpoint caps); also `fetch_all(..., max_page=)` (#10).
  The `Pager` protocol gained the optional keyword; a custom pager without it still works with `fetch_all` at runtime but should add it to satisfy mypy.
- docs: ADR-003 (events, receivers, prompts), BUILDING-A-SERVER §5a prompts and §5b events.

### Changed
- `<prefix>_status` reports `server.prompts` and `server.events` when present.
- Test lint config ignores E501/ASYNC109 under `tests/`.

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
