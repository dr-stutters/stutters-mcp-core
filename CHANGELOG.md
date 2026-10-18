# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (0.x: minor bumps may break consumers).

## [Unreleased]

### Fixed
- `<prefix>_api_call` (raw writes): the preview's `params` and `body` and the applied `result` mask secret
  fields at any depth (`manualPreSharedKey`, `preSharedKey`, passwords, secrets, communities, auth/MD5 keys)
  and scrub credential shapes in strings. Before, a raw FMC VPN body showed its pre-shared key in clear text.
  The request sent upstream is unchanged. New `audit.redact_value`; `SECRET_KEY_RE` also matches `md5Key` and
  `keyString`.

## [0.14.1] - 2026-10-16

### Fixed
- `check`: hints for a named instance name its own variables (`ISE_DR_URL`, not `ISE_URL`).
- `publish-image.yml`: the per-architecture smoke test removes the previous platform's local image first
  (the second `docker run` of the same index digest failed with "cannot overwrite digest").

## [0.14.0] - 2026-10-16

### Added
- `stutters_mcp_core.preflight` and `run_cli(..., preflight=fn)` (C21, #66): `<server> check` reports
  reachability per surface, TLS trust, credentials, the account's role vs the enabled read/write/dangerous tiers
  and the product version vs the tested matrix, for every instance; plain text or `--json`, `--strict`
  (warnings fail), `--instance NAME`; exit 1 on a failure, 2 on an unknown instance. Servers that pass no
  `preflight` keep the old behaviour (print `<prefix>_status`).
- `serve --http` / `serve --stdio` choose the transport on the command line.
- Reusable `publish-image.yml` (C22, #67): multi-arch (amd64 + arm64) GHCR image from the bundled tree, version /
  `latest` tags on releases and `-rcN` / `rc` tags on pre-releases, cosign keyless signature (recursive) and a
  signed SPDX SBOM attestation (also attached to the GitHub release), per-architecture smoke test, Trivy report.

### Changed
- `python-release.yml` marks the GitHub release as a pre-release for tags with a `-` (`v1.2.0-rc1`).
- Templates: `docker-compose.yml` pulls `ghcr.io/dr-stutters/__SERVER__:${MCP_IMAGE_TAG:-latest}`
  (`--build` still builds locally); BUILDING-A-SERVER §6a (preflight) and §9 (release + image jobs).

### Adopting
- Bump `[tool.bundle-core]` to `v0.14.0` (`python scripts/bundle_core.py update --tag v0.14.0`); no dependency
  changes.
- Write `async def preflight(settings, pf)` and pass it to `run_cli`; test each failure mode with respx.
- In `release.yml`: `python-release.yml` with `{bundle-core: true}` (no `image:`), plus a job
  `image: {needs: release, uses: .../publish-image.yml@main}`.

## [0.13.0] - 2026-10-16

### Added
- `scripts/bundle_core.py` and the reusable `bundle-core.yml` workflow (C20, #63, ADR-005): a server pins a core
  tag + commit in `[tool.bundle-core]`; `vendor` copies core into the server package as `<pkg>._core` with imports
  rewritten and merges core's runtime dependencies (plus extras) into the server's; `check` fails on drift (pin vs
  bundle, hand edits, moved tag, stale dependency block, stale script copy, built wheel/sdist contents);
  `update --tag` moves the pin. The workflow vendors, checks, builds the wheel and sdist, installs the wheel with
  `pip install --no-index` in a clean venv and runs the suite against it.
- `bundle_core.py` refuses a tag whose `pyproject.toml` version differs (`v0.13.0` must hold `0.13.0`), so a
  server never bundles a core that reports the wrong `core_version`.
- `python-ci.yml` and `python-release.yml`: `bundle-core` input (vendor before building; the release also checks
  drift and builds the image from the bundled tree).
- Templates: the Dockerfile builds and installs the bundled wheel; `.dockerignore` keeps `scripts/bundle_core.py`.

### Removed
- `CORE_READ_TOKEN`: the optional secret on `python-ci.yml` / `python-release.yml`, the private-repo git config
  step and the Docker build secret (core is public and bundled).

### Adopting
- Copy `scripts/bundle_core.py`; replace the git dependency with `[tool.bundle-core]` and the marker lines in
  `[project].dependencies`; `echo "src/<pkg>/_core/" >> .gitignore`; rewrite `stutters_mcp_core` imports to
  `<pkg>._core` (code, tests, `mock.patch` targets); `python scripts/bundle_core.py vendor && uv lock`; mypy:
  `ignore_errors` for `<pkg>._core.*`; CI: `with: {bundle-core: true}` plus a `bundle-core.yml` job; release:
  `bundle-core: true`; Dockerfile and compose from the templates (no token). A test asserting
  `importlib.metadata.version("stutters-mcp-core")` should read `<pkg>._core.__version__` instead. Optionally
  wrap the package's first `<pkg>._core` import so a missing bundle explains itself. Installs from a git URL stop
  working (no `_core` in a checkout): point users at the release wheel.

## [0.12.4] - 2026-10-15

### Added
- `field_changes(current, desired, replace=True)` also reports keys the update removes (`"removed": true`), for
  tools that send whole objects (PUT). Default behaviour is unchanged. FMC request: a rule-update preview hid a
  network literal that the replacement dropped.

### Adopting
- Write tools that PUT a full object should pass `replace=True` when building the preview diff.

## [0.12.3] - 2026-10-15

### Fixed
- The spec index collapses duplicate slashes in paths (Catalyst Center's spec has 12 `//dna/...` paths, which
  `<prefix>_api_call` would then reject). Index format 4, so cached indexes rebuild.

## [0.12.2] - 2026-10-15

### Fixed
- The spec tools' descriptions used ISE examples ('network device group', '/api/v1/endpoint/{id}') on every server,
  pointing models at paths that don't exist on CML/FMC/CatC (#57). Text is now neutral by default.

### Added
- `ServerSpec(spec_search_example=..., spec_path_example=...)` for product-specific examples in those descriptions.

## [0.12.1] - 2026-10-15

### Fixed
- `list_result(extra={"note": ...})` no longer replaces the "output truncated" warning; both notes are kept
  (catc request: the model could miss that a list was cut).

### Added
- `<prefix>_status` reports `server.server_version` (the server package's version; `ServerSpec(version=...)` overrides).
  Servers that added this themselves can drop their copy. (cml request)

## [0.12.0] - 2026-10-15

### Added
- **`<server> config [--json]` (C17)**: the effective configuration without contacting the appliance: every
  setting with its source (env, .env, `<VAR>_FILE`, `<VAR>_REF`, default; secrets only as set/unset), URLs without
  userinfo, the parsed safety rails, roles summary, instances with their URL/credentials state/rules, HTTP auth
  (token names, OAuth) and feature switches. Invalid configuration exits with the (scrubbed) validation error.

### Adopting (server builders)
- Bump to `tag = "v0.12.0"`; nothing else. Mention `<server> config` in your README's troubleshooting section.

## [0.11.0] - 2026-10-15

### Added
- **Per-instance write rules (C15)**: `<P>_READ_ONLY`, `<P>_TWO_PERSON`, `<P>_CHANGE_WINDOW`, `<P>_MAX_BLAST_RADIUS`
  (and `<P>_<NAME>_...` per named instance, never inherited) add to the server-wide rails in `preview_or_apply`;
  shown under `policy.instance_rules` and `server.instance_rules`. E.g. read-only or two-person production next to
  an unrestricted lab in one server.
- **Audit log rotation (C13)**: `MCP_AUDIT_LOG_MAX_MB` + `MCP_AUDIT_LOG_BACKUPS` rotate a file audit log
  (`configure_audit_log(path, max_mb=, backups=)`); unset = unchanged.
- **Session tokens masked automatically (C14)**: `LoginTokenAuth` registers every token it obtains (header values,
  the credential after `Bearer `, secret-named `Token.data` such as refresh tokens) with the log scrubber and
  tool-error scrubbing (`secrets.register_header_values`; credential headers only: Authorization, Cookie, `*Token*`
  and secret-named headers); the newest 64 runtime values are kept. `SecretScrubber.values` is now a read-only
  snapshot (`frozenset`); register values with `add` / `add_runtime`.
- **Per-caller rate limit (C16)**: `MCP_CALLER_RATE_LIMIT` (calls/min per caller) and `MCP_CALLER_RATE_LIMIT_BY_ROLE`;
  refusals are audited (`rate_limited`) and counted (`mcp_rate_limited_total`); status is exempt. Off by default.

### Adopting (server builders)
- Bump to `tag = "v0.11.0"`; no code changes. New product-prefixed settings `READ_ONLY`, `TWO_PERSON`,
  `CHANGE_WINDOW`, `MAX_BLAST_RADIUS` exist on every Settings class: don't define fields with those names.
- Hosted containers: consider `MCP_AUDIT_LOG_MAX_MB` if the audit log goes to a file, and `MCP_CALLER_RATE_LIMIT`.
- If your server obtains session tokens outside `LoginTokenAuth`, call `secrets.register_secret(token)`.

## [0.10.0] - 2026-10-15

### Added
- **MCP background tasks (C12)**, opt-in: `reg.read/write/dangerous(..., task=True)` marks long-running tools; with
  `MCP_TASKS=true` (and the `stutters-mcp-core[tasks]` extra, i.e. fastmcp-tasks) they run as SEP-2663 tasks on a
  Docket backend (`MCP_TASKS_URL`, default `memory://`). Clients without task support call them synchronously.
  Caller identity, previews/confirm tokens and `instance` work inside tasks; status lists `server.tasks`.
- **Public-release groundwork (G9)**: `SECURITY.md`, `CONTRIBUTING.md`, `docs/PUBLIC-RELEASE.md` (history-scrub
  decisions, repo hygiene, PyPI trusted-publishing steps, visibility last) and `python -m stutters_mcp_core.scrub_check`
  (report-only scan of git history or the current tree for lab addresses/host names, secrets, keys, tokens and
  forbidden files; `--strict` for CI, `--allow` for known-safe values, `--json`).

### Adopting (server builders)
- Bump to `tag = "v0.10.0"`. To offer tasks: depend on `stutters-mcp-core[tasks]` (git source unchanged), add
  `task=True` to deploy/discovery/provision/SWIM-style tools (async functions), and keep using `poll_until` inside
  them (progress reaches the task). Nothing changes until an operator sets `MCP_TASKS=true`.
- Before going public, run `uv run python -m stutters_mcp_core.scrub_check` in your repo (report only) and see
  docs/PUBLIC-RELEASE.md; copy SECURITY.md / CONTRIBUTING.md and adjust the product scope.

## [0.9.0] - 2026-10-15

### Added
- **Several appliances per server (G10, ADR-004)** (`stutters_mcp_core.targets`, docs/PRODUCTION.md):
  `<P>_INSTANCES=prod,dr` adds instances read from `<P>_PROD_URL`, `<P>_PROD_USERNAME`, ... (other fields inherited
  from the default instance, named by `<P>_DEFAULT_INSTANCE`). Every tool gets an optional `instance` argument and
  `get_client(ctx)` / `get_settings(ctx)` / the spec store follow it; confirm tokens are bound to their instance;
  status and `<server> check` cover each instance. Single-instance servers are unchanged (no new argument).

### Adopting (server builders)
- Bump to `tag = "v0.9.0"`. Check that tools read appliance state only through `get_client(ctx)` /
  `get_settings(ctx)` (module-level caches keyed by nothing would mix instances), and that no tool has a
  parameter named `instance`. Test: `make_settings(instances="prod")` with `monkeypatch.setenv("<P>_PROD_URL", ...)`,
  then call a tool with `{"instance": "prod"}`.
- `status` code reading `settings.url` from the closure should use `get_settings(ctx)`.

## [0.8.1] - 2026-10-15

### Fixed
- **Reusable release workflow broke consumer releases** (FMC/ISE v0.4.0): image attestations failed on the default
  docker driver. Provenance/SBOM attestations are now opt-in (`attest: true`, which sets up the docker-container
  builder); Trivy reports by default (`scan-fail: true` to enforce); the GitHub-release step is idempotent (re-runs
  and hand-made releases get their assets replaced). Callers need only `contents/packages/id-token: write`.
- `ToolRegistrar.prompt()` dropped `example=` (only `PromptRegistrar.prompt` honoured it).

### Added
- **Least-privilege kit (G3)**: `docs/least-privilege-template.md` (the per-server guide skeleton: role tables,
  creation steps, seed script, verification) and `stutters_mcp_core.testing.least_privilege`:
  `readonly_live_settings(Settings, "<P>_")` (logs in as `<P>RO_USERNAME`/`<P>RO_PASSWORD`, MCP role `ro`) and
  `read_tier_report` / `assert_read_tier_works(server, samples, skip=...)`, which call every read tool as a
  read-only identity and fail on any appliance denial or on a write tool visible to `ro`.

## [0.8.0] - 2026-10-15

### Added
- **OAuth 2.1 for HTTP (G1)** (`stutters_mcp_core.oauth`, docs/PRODUCTION.md): `MCP_OAUTH_PROVIDER=github|oidc` puts
  FastMCP's OAuth proxy in front of the server (metadata, dynamic client registration, consent, PKCE) next to the
  named bearer tokens (MultiAuth). `MCP_OAUTH_ALLOWED_USERS` is required. OAuth callers are `<provider>:<login>`
  for roles, previews, two-person and the audit log; token callers keep their name. HTTP may run OAuth-only.
  GitHub logins come from `login` (or `id:<n>` allow-list entries); OIDC uses a verified email or
  `MCP_OAUTH_LOGIN_CLAIM`. Client redirect URIs default to loopback. Steps to register the OAuth app are in
  docs/PRODUCTION.md (needs the owner's GitHub account).

### Adopting (server builders)
- Bump to `tag = "v0.8.0"`; no code changes. OAuth is an operator setting on the hosted container. Don't enable
  it on cortex until the user has registered an OAuth app (docs/PRODUCTION.md "Steps for the repo owner").
- If a test asserts the HTTP startup error text, it now mentions `MCP_OAUTH_PROVIDER` too.

## [0.7.0] - 2026-10-15

### Added
- **Prometheus metrics (G5)** (`stutters_mcp_core.metrics`, docs/PRODUCTION.md): `MCP_METRICS=true` serves `/metrics`
  (bearer-protected on the MCP port, or unauthenticated on `MCP_METRICS_HOST:MCP_METRICS_PORT`, loopback by default,
  which also works with stdio). Tool calls and durations by outcome, write events (preview/applied/blocked/refused),
  upstream requests by status and duration, retries, rate-limiter waits, and server/core versions. No new dependency.
- **Secrets (G4, finished).** `<VAR>_REF=<scheme>:<ref>` for every secret setting, resolved by a secret loader
  (`stutters_mcp_core.secrets.SecretLoader`: `load(ref) -> str`). Built-in `file:<path>#<key>` (JSON or KEY=VALUE);
  plug in others (Vault, 1Password; examples in docs/PRODUCTION.md) with `MCP_SECRET_LOADERS="scheme=module:Class"`.
- `docs/templates/docker-compose.secrets.yml`: compose override that mounts the password and token list as Docker
  secrets (`<P>_PASSWORD_FILE`, `MCP_AUTH_TOKENS_FILE`); the dockerignore template excludes `secrets/`.
- Secret hygiene: `scrub_text` / `scrub_url` / `SecretValueFilter` / `install_secret_filter`. The CLI masks every
  configured secret value and credential shape in all log output (including tracebacks); upstream error details,
  redirect locations and connection errors are scrubbed; status hides URL userinfo, masks secret-looking keys in
  the product status and the configured secret values in error text; settings validation errors no longer echo
  input values.
- `SecretErrorMiddleware` (installed by `build_server`): tool errors returned to the client are scrubbed the same way.
- `secrets.register_secret(value)`: mask a value obtained at runtime (session token, cookie) in later logs.
- **Security scanning in the reusable workflows (G8)** (docs/SUPPLY-CHAIN.md): `python-ci.yml` runs pip-audit on the
  locked runtime dependencies (`audit`, `audit-ignore` inputs); `python-release.yml` attaches a CycloneDX Python SBOM
  to every release and, with `image: true`, pushes the image with provenance + SBOM attestations, attaches an SPDX
  image SBOM, scans it with Trivy (`scan-severity`, `scan-fail`), and can sign it with cosign (`sign`, off until
  the key decision; both options documented).

### Adopting (server builders)
- Bump to `tag = "v0.7.0"`. ise: `test_status_all_surfaces` asserts the core version; update it.
- Nothing else is required. Optional: switch the cortex compose file to Docker secrets
  (`docs/templates/docker-compose.secrets.yml`), add `secrets/` to `.gitignore`, and copy the new
  dockerignore line; call `stutters_mcp_core.secrets.register_secret(token)` after a login that returns a
  session token/cookie you might log; set `MCP_METRICS=true` (and `MCP_METRICS_PORT` if you want a
  loopback-only scrape port) in the hosted container.
- CI: the reusable workflow now runs pip-audit. If it flags something you can't fix yet, pass
  `with: { audit-ignore: "<id>" }` with a comment explaining why.
- Snapshot/text tests: upstream error text and audit `error` now pass through the credential-shape scrubber,
  status masks secret-named keys in your product status dict, and settings errors no longer echo input values.

### Changed
- Upstream error messages (`detail`, redirect `Location`, connection errors), audit `error` fields and string
  arguments in audit events pass through `scrub_text` (credential shapes only: whole-word secret keys,
  Authorization/Cookie/X-*-Token headers, URL userinfo, `Bearer <credential>`); normal text is unchanged.
- `<prefix>_status` masks secret-looking keys in the product status (`mask_secrets`) and hides URL userinfo.
- Settings validation errors no longer include input values (`hide_input_in_errors`); tests matching on the
  echoed input need updating.
- Setting a secret both as `<VAR>` and `<VAR>_FILE`/`<VAR>_REF` reports "set A or B, not both" (unchanged for
  the existing pair). `MCP_AUTH_TOKENS` keeps `MCP_AUTH_TOKENS_FILE` (no `_REF`).

## [0.6.0] - 2026-10-15

### Added
- **Per-user roles (G2)** (`stutters_mcp_core.roles`): `MCP_ROLES` / `MCP_ROLES_FILE` map caller identities
  (token names or `<provider>:<login>`, exact or glob) to `ro` / `rw` / `admin`; `MCP_DEFAULT_ROLE` (default
  `ro`) covers the rest. Tools above the caller's role are hidden and refused (audit event `denied`); prompts
  follow their tier. Previews show `requested_by: {caller, role}`, status shows `server.access`, and every
  audit event carries `role`. Off by default; the stdio caller `local` is `admin` unless named.
- `<prefix>_review_change` needs `rw` or `admin` when roles are on. The HTTP startup log lists each token's role.

### Changed
- `local` is a reserved token name (it is the stdio caller's identity): `MCP_AUTH_TOKENS` and `<server> token`
  refuse it.

### Adopting (server builders)
- Bump the pin to `tag = "v0.6.0"`. No code changes needed: roles are an operator setting and gate tiers
  through `ToolRegistrar`'s tier registry. Tools you register with `mcp.tool(...)` directly (bypassing
  `reg.read/write/dangerous`) have no tier and become admin-only when roles are on: register everything
  through the registrar.
- Prompts: declare `tier=Tier.WRITE` (or DANGEROUS) on prompts that need write tools, so `ro` callers don't see them.
- Tests: `with as_caller("bob"): ...` with `make_settings(mcp_roles="bob:rw")`; check your read tier with a `ro` caller.
- README: add `MCP_ROLES` to the env table (link core `docs/PRODUCTION.md`).

## [0.5.0] - 2026-10-15

### Added
- **Safety rails (G7)**, all opt-in (`stutters_mcp_core.policy`, operator docs in `docs/PRODUCTION.md`):
  `MCP_DRY_RUN` (previews only, confirms refused), `MCP_CHANGE_WINDOW` (`"Mon-Fri 22:00-02:00 Australia/Sydney"`
  or cron style `"0 22 * * 1-5 4h Australia/Sydney"`; confirms outside it are refused with the next opening),
  `MCP_DENY_TOOLS` / `MCP_ALLOW_TOOLS` (globs and `@read`/`@write`/`@dangerous`; matching tools are not
  registered), `MCP_MAX_BLAST_RADIUS` + `MCP_BLAST_RADIUS_OVERRIDES` (per-tool limits), and
  `MCP_TWO_PERSON_TOOLS` (the confirm token must be redeemed by a different caller).
- `Plan(blast_radius=N)`: how many things a change touches; shown in the preview (`blast_radius`) and checked
  against the limits. `None` (the default) is never limited.
- Previews carry a `policy` section when any rail is on; `<prefix>_status` reports `server.safety`
  (including `withheld_tools`); the instructions describe the active rails.
- `<prefix>_review_change(token)` (read tier, only when `MCP_TWO_PERSON_TOOLS` is set): the approver sees the
  pending change, its arguments (secrets masked) and who requested it, without using up the token.
  Applied two-person results and audit events carry `requested_by` / `approved_by`.
- Audit events `blocked` (any preview with blockers, including the tool's own `Plan.blockers`; previously
  not logged) and `refused`.
- `tzdata` dependency, so IANA zones work on slim images.
- `testing.as_caller(name)`: run in-memory calls as a named caller (roles / two-person tests).
- `PreviewStore.peek()`; `PreviewStore.redeem()` now returns the pending entry (was `None`).

### Adopting (server builders)
- Bump the pin to `tag = "v0.5.0"`. Nothing else is required: with no rail configured, previews, results and
  status are unchanged (status gains `server.safety` only when a rail is on).
- Set `Plan(..., blast_radius=N)` in write tools that can count what they touch: deploys (devices),
  provisioning (devices), bulk endpoint/ANC changes (endpoints), template deploys (targets), lab start/stop/wipe
  (nodes). List which tools report it in your README so operators know what `MCP_MAX_BLAST_RADIUS` covers.
- Product rails (ISE I18, FMC F18, CatC D19/D20, CML L20) build on this: put product-specific checks in
  `Plan.blockers` as before; use the global rails for operator policy.
- If you snapshot previews, write tools that now set `blast_radius` gain that key.
- Tests: `with as_caller("alice"): ...` around in-memory calls; settings kwargs `mcp_dry_run=True`,
  `mcp_change_window=...`, `mcp_two_person_tools="@dangerous"`, etc.

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
