# Cisco MCP Suite — Project Plan

_Drafted 2026-10-14. Status: planning, nothing built yet._

## 1. Goals

Build four FastMCP servers — **Cisco Modeling Labs (CML)**, **Cisco ISE**, **Cisco Secure Firewall
Management Center (FMC / Firepower)** and **Cisco Catalyst Center (CatC)** — each a standalone public
project, all built on one shared, versioned Python package that holds everything that isn't
product-specific.

### Decisions so far

| Topic | Decision |
|---|---|
| Sharing mechanism | Shared Python package **`stutters-mcp-core`** (import `stutters_mcp_core`) that each server depends on |
| Repos | 5 separate public GitHub repos under `dr-stutters` |
| Distribution | **No PyPI for now.** Servers depend on core via git tags (`stutters-mcp-core @ git+https://github.com/dr-stutters/stutters-mcp-core@vX.Y.Z`); local editable path dep while developing. Users install with `uvx --from git+https://github.com/dr-stutters/<server> <server>` or pull `ghcr.io/dr-stutters/<server>`. CI still builds wheels so a later PyPI switch is trivial |
| Relationship to old servers | Fresh builds. Old ISE/FMC/CML/CatC servers are mined **for tool ideas only** — no code copied |
| NetBox-MCP | Stays standalone. It is the *design reference* (patterns, quality bar), not a consumer of the core |
| Audience | Public, generic, multi-user; everything configured through env vars |
| Build order | CML → ISE → Catalyst Center → FMC (confirmed) |
| Writes | Off by default; preview-first with single-use confirm tokens; extra "dangerous" tier |
| Transport | stdio **and** Streamable HTTP container from day one (cortex, ports 8032–8035) |
| Test labs | lab (VPN): CML 198.18.128.10, ISE 198.18.134.35, Catalyst Center 198.18.128.11, FMC 198.18.134.76. Short-lived, so live tests auto-skip |
| Supported versions | **Whatever the lab runs** = the tested matrix: CML 2.10.0, ISE 3.5.0.527 (standalone), Catalyst Center 3.2.2, FMC 10.0.1 (verified 2026-10-14). Older versions best-effort |
| Disk location | `~/MCP-Suite/<repo>` |
| Later roadmap | cdFMC (Security Cloud Control), ISE pxGrid, cross-product workflows. NetBox-MCP does **not** adopt core |
| Naming | Packages `cml-mcp-server`, `ise-mcp-server`, `fmc-mcp-server`, `catc-mcp-server`; tools `cml_*`, `ise_*`, `fmc_*`, `catc_*` |
| Core extras | OpenAPI spec search/definition tools, async task poller, audit log + per-user identity, test kit |
| Git identity | Commits use `drstuttersau@gmail.com` (as for netbox-mcp) |

---

## 2. Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │                  stutters-mcp-core                    │
                    │  settings · server factory · inbound auth    │
                    │  upstream http client + auth strategies      │
                    │  retries · rate limit · pagination           │
                    │  errors · formatting · write guard/preview   │
                    │  task poller · spec index tools · audit log  │
                    │  testing (pytest plugin, contract linter)    │
                    └──────────────────────────────────────────────┘
                         ▲            ▲            ▲            ▲
          depends on     │            │            │            │   pinned  >=0.N,<0.N+1
                 ┌───────┴──┐  ┌──────┴───┐  ┌─────┴────┐  ┌────┴─────┐
                 │ cml-mcp- │  │ ise-mcp- │  │ catc-mcp-│  │ fmc-mcp- │
                 │  server  │  │  server  │  │  server  │  │  server  │
                 └──────────┘  └──────────┘  └──────────┘  └──────────┘
                 product client (auth strategy, surfaces, paging quirks)
                 product tools (curated task tools + generic spec tools)
                 product settings (CML_*, ISE_*, FMC_*, CATC_*)
```

**Rule of thumb for what goes in core:** if two or more products need it, or it's about MCP itself
(transport, auth, tool contracts, output shaping), it goes in core. Anything that knows a product's
URLs, fields or quirks stays in that product's repo.

Each server is thin: a `Settings` subclass, a client subclass (or a few, for ISE), tool modules,
and a `main()` that calls the core server factory.

---

## 3. The core package (`stutters-mcp-core`)

Package layout (src layout, `uv_build`, Python ≥3.12, `mypy --strict`, ruff — same toolchain as NetBox-MCP):

```
stutters_mcp_core/
  settings.py      BaseServerSettings
  server.py        build_server(), run(), CLI (serve / token / check)
  inbound_auth.py  named bearer tokens (sha256), identity in context, OAuth hook later
  http/
    client.py      ApiClient (httpx.AsyncClient wrapper)
    auth.py        Basic, StaticHeader, LoginToken (with refresh), strategy protocol
    retry.py       backoff on 429/5xx/connect errors, honours Retry-After
    ratelimit.py   async token bucket + concurrency semaphore
    paging.py      offset/limit, page/size, 1-based offset, link/next, none
  errors.py        UpstreamError hierarchy → ToolError with actionable text
  formatting.py    response_format markdown|json, truncation, tables, paging footer
  writes.py        WriteGuard: tiers, preview, HMAC confirm tokens
  tasks.py         poll_until() + progress reporting
  spec/
    loader.py      OpenAPI 3 / Swagger 2 from URL, file, or bundled package data
    index.py       compact index, disk cache keyed by product+version
    tools.py       register_spec_tools(mcp, prefix=...) → search_spec, get_definition, api_call
  audit.py         FastMCP middleware → structured JSON audit events
  redact.py        secret/PII redaction for logs and errors
  suite/           suite_* prompt pack every server registers (C48, docs/SUITE-PROMPTS.md)
  clock.py         appliance clock offset from the HTTP Date header, timestamp convention (C69)
  snapshot.py      snapshot contract: blob builder, parse, compare, <prefix>_snapshot tools (C53, docs/SNAPSHOTS.md)
  vocabulary.py    argument vocabulary lint, warnings only (C68, docs/VOCABULARY.md)
  testing/
    plugin.py      pytest plugin (fixtures, respx router, live-lab marker)
    contract.py    tool-contract linter
```

### 3.1 Settings — `BaseServerSettings`

`pydantic-settings` base class; each product subclasses it with its own `env_prefix`.

Shared (unprefixed, because each server runs in its own process or container):

| Var | Default | Notes |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` \| `http` |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | `0.0.0.0` / `8000` / `/mcp` | |
| `MCP_AUTH_TOKENS` / `MCP_AUTH_TOKENS_FILE` | — | `name:token` or `name:sha256:<hex>`; required when transport is http |
| `MCP_ENABLE_WRITES` | `false` | |
| `MCP_ENABLE_DANGEROUS` | `false` | Needs writes enabled too |
| `MCP_CONFIRM_TTL` | `300` | seconds |
| `MCP_CACHE_DIR` | `~/.cache/<server>` | empty string disables |
| `MCP_AUDIT_LOG` | `stderr` | `stderr` \| file path \| `off` |
| `LOG_LEVEL` | `INFO` | |
| `MCP_DRY_RUN`, `MCP_CHANGE_WINDOW`, `MCP_DENY_TOOLS`, `MCP_ALLOW_TOOLS`, `MCP_MAX_BLAST_RADIUS`, `MCP_BLAST_RADIUS_OVERRIDES`, `MCP_TWO_PERSON_TOOLS` | off | Safety rails (core 0.5.0); see [PRODUCTION.md](PRODUCTION.md) |

Per-product (prefixed, e.g. `ISE_`), defined once in core as a mixin so every product has the same knobs:
`<P>_URL`, `<P>_USERNAME`, `<P>_PASSWORD` (or `<P>_TOKEN`), `<P>_VERIFY_SSL` (**default true**),
`<P>_CA_BUNDLE`, `<P>_TIMEOUT`, `<P>_RETRIES`, `<P>_RATE_LIMIT` (req/min), `<P>_MAX_CONCURRENCY`.

URL normalisation (strip trailing `/`, strip `/api`, require scheme), auth-scheme stripping on tokens,
`SecretStr` everywhere. These are lessons carried over from NetBox-MCP.

### 3.2 Server factory

```python
mcp = build_server(
    name="ise-mcp-server",
    instructions=INSTRUCTIONS,          # product overview + "start here" tools
    settings=settings,
    client_factory=IseClient.from_settings,   # opened/closed in lifespan
    tool_modules=[system, sessions, nads, policy, trustsec, writes],
    spec_sources=[...],                 # optional → registers <prefix>_search_spec etc.
)
run(mcp, settings)                      # stdio or http (+ /health, + auth)
```

Responsibilities:
- Lifespan opens the upstream client once and exposes it through a typed dependency, the same `deps.py` pattern as NetBox.
- `/health` (liveness) and `/ready` (upstream reachable + authenticated) routes for HTTP.
- Registers a `<prefix>_status` tool automatically. It reports product version, API user, writes/dangerous state, core version, and reachable surfaces.
- Write tools are only registered when writes are enabled, so the model never sees tools it can't use. NetBox works the same way.
- CLI: `ise-mcp-server serve`, `ise-mcp-server token <name>` (prints a new token plus its sha256 line), `ise-mcp-server check` (validates env, logs in, prints status, exits non-zero on failure. Useful in Docker healthchecks and CI).

### 3.3 Inbound auth and identity

- Named bearer tokens hashed with sha256 and constant-time compare, as in NetBox.
- The token *name* becomes the caller identity, stored in request context, so the audit log and write previews show *who* made a change.
- Structure it so FastMCP `MultiAuth` / OAuth (GitHub IdP + allow-list) can be added later without touching servers.
- Later option: per-user roles (`ro` / `rw` / `admin`) bound to token names, feeding `WriteGuard`. Designed now, built later.

### 3.4 Upstream HTTP client

`ApiClient` wraps `httpx.AsyncClient`, with pluggable **auth strategies**. These cover all four products:

| Strategy | Used by | Behaviour |
|---|---|---|
| `BasicAuth` | ISE (OpenAPI, ERS, MnT) | Static header; optional ERS CSRF token fetch |
| `LoginToken` | CML, Catalyst Center, FMC | POST credentials → token; store expiry; **single-flight lock** so concurrent calls don't all log in; re-login once on 401; proactive refresh before expiry |
| `LoginToken` + refresh hook | FMC | Uses the refresh token (up to 3 refreshes) before a full re-login; also captures `DOMAIN_UUID` from the login response |

Also in the client:
- **Retries** with jittered exponential backoff on connect errors, 429 and 502/503/504. Honours `Retry-After`. Never retries non-idempotent methods unless the caller marks them safe.
- **Rate limiter**: async token bucket (`<P>_RATE_LIMIT`) plus a semaphore (`<P>_MAX_CONCURRENCY`). FMC needs both (about 120 req/min and 10 concurrent connections per source IP). CatC has per-API limits and returns 429.
- **Pagination strategies** with one `paginate()` interface:
  - `OffsetLimit` (FMC: `offset`/`limit`, `expanded=true`, max 1000)
  - `OneBasedOffset` (CatC: `offset` starts at 1, `limit` ≤ 500)
  - `PageSize` (ISE ERS & OpenAPI: `page`/`size`, max 100, `nextPage` link)
  - `NoPaging` (CML returns full collections; core slices client-side)
- **Multiple surfaces per product.** ISE has three base URLs and ports (OpenAPI 443, ERS 9060/443, MnT 443). The client takes a dict of named surfaces, each with its own base URL, auth, headers and paging.
- **XML support**: a small helper for ISE MnT, which returns XML. Uses `defusedxml` to parse safely, then converts to dicts.
- Request/response debug logging with redaction, plus a request-id header where the product supports one.

### 3.5 Errors

`UpstreamError` → subclasses `AuthError`, `NotFound`, `Conflict`, `RateLimited`, `Unreachable`,
`ValidationFailed`, `Timeout`. Every one becomes a FastMCP `ToolError` whose text tells the model **what
to do next** (e.g. "ERS on port 9060 is unreachable. Run ise_status to see which surfaces work, or set
ISE_ERS_PORT=443."). Product error bodies (ISE `ERSResponse`, FMC `error.messages`, CatC
`response.detail`) are parsed by small per-product extractors registered with the client.

### 3.6 Output formatting

- Every list/get tool takes `response_format: "markdown" | "json"` (markdown by default) plus `limit`/`offset`.
- A character budget (about 25k by default, configurable) truncates and says how to narrow the query.
- Consistent paging footer: `Showing 1–50 of 812. Next: offset=50`.
- Helpers for markdown tables, key/value blocks, and drop-noise field filters (`links`, `metadata`, …).

### 3.7 Writes: `WriteGuard`

Three tiers declared on each tool with a decorator:

| Tier | Example | Registered when | Annotations |
|---|---|---|---|
| `read` | list devices | always | `readOnlyHint=True` |
| `write` | create NAD, add object, start lab | `MCP_ENABLE_WRITES` | `destructiveHint=False/True` per tool |
| `dangerous` | FMC deploy, CatC provision/SWIM activate, ISE ANC/CoA, delete lab, break HA | writes **and** `MCP_ENABLE_DANGEROUS` | `destructiveHint=True` |

**Preview-first**, the same as NetBox:
1. A call without `confirm` changes nothing. It returns a preview: the HTTP method and path, a field-level diff against current state (fetched by a per-tool `preview()` hook), side effects ("will need a deploy to 3 devices"), and a confirm token.
2. The token is HMAC(secret, tool name + canonical args hash + caller identity + expiry), single-use, with a TTL. Repeating the identical call with `confirm=<token>` executes it.
3. Changed args, a different user, a reused token or an expired token are all rejected with a clear message.
4. The audit log records both the preview and the execution.

The secret is generated per process, or set with `MCP_CONFIRM_SECRET` so tokens survive restarts and work across replicas.

### 3.8 Task poller

```python
result = await poll_until(
    fetch=lambda: client.get(f"/dna/intent/api/v1/task/{task_id}"),
    done=lambda r: r["endTime"] is not None,
    failed=lambda r: r.get("isError"),
    timeout=settings.task_timeout, interval=2.0, backoff=1.5, max_interval=15,
    ctx=ctx,                       # sends MCP progress notifications
)
```
Used by: CatC tasks and executions, FMC deployments and device registration jobs, ISE tasks (certs,
backups), and CML convergence waits. Tools that start long jobs take `wait: bool = True`. If the wait
times out, they return the task id plus the name of the `*_get_task` tool to check it later.

### 3.9 OpenAPI spec tools

All four products publish specs, so core provides the generic "everything else" layer that kept
NetBox-MCP useful beyond its curated tools:

- `<prefix>_search_spec(query)` — keyword search over operations (path, method, summary, tags, params).
- `<prefix>_get_definition(name_or_path)` — resolved schema with `$ref` expansion to a depth limit; enums and required fields kept.
- `<prefix>_api_call(method, path, params, body)` — the escape hatch. Restricted to the configured base URL(s), with paths validated (no scheme or host, no `..`). Non-GET methods go through `WriteGuard` like any write tool.

Spec sources: fetched from the appliance (CML, ISE, FMC expose theirs), a file path, or **bundled
package data** (CatC: download the versioned spec from developer.cisco.com and ship it in the wheel).
The index is cached on disk, keyed by product and version. Handles OpenAPI 3.x and Swagger 2.0.

### 3.10 Audit log

FastMCP middleware emits one JSON line per tool call:
`ts, server, caller, tool, tier, args (redacted), duration_ms, outcome, upstream_status, confirm_token_id, changed_object`.
Destinations are stderr (default), a file, or off. In containers it goes to stdout so `docker logs`
or Splunk can collect it.

### 3.11 Test kit — `stutters_mcp_core.testing`

- pytest plugin auto-loaded through an entry point, with fixtures for settings, an in-memory FastMCP `Client` connected to the server, and a respx router pre-wired to the product base URL.
- **Contract linter** that every server runs as a unit test:
  - every tool name starts with the server prefix and is snake_case
  - descriptions are non-empty, under a length cap, and mention key args
  - annotations are present (`readOnlyHint` on reads, `destructiveHint` on destructive writes)
  - no write/dangerous tool is registered when writes are off
  - list tools accept `limit`/`offset`/`response_format`
  - no secrets are echoed in `<prefix>_status`
- `@pytest.mark.live` marker: skipped unless `<P>_URL` is set **and** a reachability probe passes within 3s. This matters because labs come and go.
- Recorded-fixture helper: capture real responses from a lab session into `tests/fixtures/*.json` (redacted) so unit tests use realistic payloads.

### 3.12 Shared CI

The core repo hosts **reusable GitHub Actions workflows** (`workflow_call`) that every server references:
- `ci.yml`: uv sync → ruff → mypy strict → pytest (unit) → build
- `release.yml`: on tag `v*` → build wheel/sdist (attached to the GitHub release) → GHCR image (`ghcr.io/dr-stutters/<server>`). PyPI publishing step present but disabled.
- Renovate in each server bumps the `stutters-mcp-core` git tag (Dependabot doesn't track git-tag deps well).

Versioning: core is SemVer at 0.x. Servers pin an exact core git tag (`@v0.N.M`); uv.lock records the commit. Breaking core changes bump
the minor version until 1.0. The core CHANGELOG is mandatory.

### 3.13 What *isn't* in core (deliberately)

- A cookiecutter/copier template. Not chosen. A short "new server checklist" in the core README covers it; the Dockerfile and compose file are small enough to copy.
- Product data models. Each server owns its pydantic models, if it uses any.

---

## 4. Shared conventions for all four servers

**Repo layout** (mirrors NetBox-MCP):
```
<server>/
  pyproject.toml   README.md   LICENSE (MIT)   CHANGELOG.md
  Dockerfile       docker-compose.yml   .env.example
  src/<pkg>/ __init__.py  config.py  client.py  server.py  instructions.md
             tools/ system.py  <domain>.py ...  writes/<domain>.py
  tests/unit/  tests/live/  tests/fixtures/
  scripts/     (lab seeding, fixture capture)
  .github/workflows/  ci.yml  release.yml   (call core's reusable workflows)
```

**Tool design rules**
1. **Task-oriented tools first, CRUD second, spec escape hatch last.** "Why did this MAC fail auth?" is better than three CRUD calls. Aim for about 30–50 tools per server, not hundreds.
2. Naming is `<prefix>_<verb>_<noun>`. Verbs: `list`, `get`, `find`, `create`, `update`, `delete`, plus domain verbs (`deploy`, `start`, `trace`).
3. IDs are resolved for the model: accept a name **or** an id wherever possible (helpers in core `resolve.py`).
4. Server `instructions` give the object model, "start here" tools and known gotchas, kept short like the existing ISE/FMC ones.
5. Every list tool has `limit`, `offset`, `response_format` and filters.
6. MCP **prompts** for common workflows (e.g. `troubleshoot_endpoint_auth`, `pre_change_firewall_check`). Optional, but cheap.

**Hosting (cortex)**

| Server | Port | Container |
|---|---|---|
| netbox-mcp (existing) | 8031 | netbox-mcp |
| cml-mcp-server | 8032 | cml-mcp |
| ise-mcp-server | 8033 | ise-mcp |
| catc-mcp-server | 8034 | catc-mcp |
| fmc-mcp-server | 8035 | fmc-mcp |

Same hardening as NetBox: non-root, read-only rootfs, tmpfs `/tmp`, `cap_drop: ALL`,
`no-new-privileges`, named volume for the spec cache, healthcheck runs `<server> check`.

---

## 5. Server plans

Tool lists are a starting point drawn from the old servers' coverage plus the product APIs.
✱ = task/"smart" tool (more than a thin API wrapper). Exact endpoints get confirmed against each spec while building.

### 5.1 CML — `cml-mcp-server` (build first)

**API:** REST `/api/v0`, JWT from `POST /api/v0/authenticate` → `LoginToken`. No server-side paging.
OpenAPI is served by the controller. Target CML 2.7+ (verify the lab version).

**Why first:** simplest auth, clean spec, writes are safe because it's a lab, and it can **build test
topologies for the other three servers** (e.g. switches as NADs for ISE).

| Area | Read tools | Write / dangerous |
|---|---|---|
| System | `cml_status`, `cml_system_health`, `cml_resource_usage`, `cml_licensing` | — |
| Labs | `cml_list_labs`, `cml_get_lab`, ✱`cml_lab_topology` (summary/full), `cml_export_lab` (YAML), `cml_lab_events` | `cml_create_lab`, `cml_import_lab` (YAML), `cml_start_lab`, `cml_stop_lab` · **D:** `cml_wipe_lab`, `cml_delete_lab` |
| Nodes | `cml_list_nodes`, `cml_get_node`, `cml_node_console_log`, `cml_node_l3_addresses`, `cml_list_node_definitions`, `cml_list_images` | `cml_add_node`, `cml_set_node_config`, `cml_set_node_state` · **D:** `cml_delete_node` |
| Links | `cml_list_links`, `cml_get_link`, `cml_link_condition` | `cml_connect`, `cml_set_link_condition`, `cml_start_capture`/`stop` · **D:** `cml_delete_link` |
| Captures | `cml_capture_status`, `cml_capture_packets` | — |
| CLI | ✱`cml_run_commands` (show/ping/traceroute via console or pyATS; **optional `[pyats]` extra**) | `cml_configure` (config-mode lines) is a write |
| Smart | ✱`cml_wait_for_converged`, ✱`cml_ping_matrix`, ✱`cml_lab_diff` (running vs exported), ✱`cml_build_lab` (from a compact YAML spec) | |

Core pieces exercised: `LoginToken`, `NoPaging`, spec tools, poller (convergence), WriteGuard.

### 5.2 ISE — `ise-mcp-server`

**API:** three surfaces, all HTTP Basic:
- **OpenAPI** `/api/v1/...` on 443: policy sets, endpoints, TrustSec, deployment, certs, licensing, patches, backup. Swagger per group.
- **ERS** `/ers/config/...` on 9060 (443 on newer releases; configurable): NADs, NDGs, internal users, identity and endpoint groups, ANC, guest. Paging `page`/`size` ≤100; optional CSRF.
- **MnT** `/admin/API/mnt/...` on 443: active sessions, auth status, failure reasons, CoA. **XML**.
- pxGrid is out of scope for v1 (separate cert-auth design).

Target ISE 3.2+ (lab is 3.x; you also have a 3.5 ISO).

| Area | Read | Write / dangerous |
|---|---|---|
| System | `ise_status` (✱ probes all 3 surfaces), `ise_deployment_nodes`, `ise_license_status`, `ise_patches`, `ise_backup_status`, `ise_list_certs` | **D:** cert delete/import |
| Sessions (MnT) | `ise_active_sessions`, `ise_session_count`, `ise_session_by` (mac/ip/user), `ise_auth_status`, `ise_failure_reasons`, `ise_recent_auths` | **D:** `ise_coa_reauth`, `ise_coa_disconnect` |
| Smart | ✱`ise_why_auth_failed(mac\|user)` (MnT status + failure reason + matched policy set/rule + authz profile), ✱`ise_endpoint_360(mac)`, ✱`ise_policy_lookup` (which rule would match given conditions — best-effort, read-only), ✱`ise_nad_audit` (NADs missing secrets/NDGs) | |
| NADs | `ise_list_nads`, `ise_get_nad` (name/ip), `ise_list_ndgs` | create/update NAD & NDG · **D:** delete |
| Endpoints | `ise_list_endpoints`, `ise_get_endpoint`, `ise_list_endpoint_groups` | create/update/bulk, group membership · **D:** delete/bulk delete |
| Identity | internal users, identity groups, identity source sequences, AD join points | create/update · **D:** delete |
| Policy | policy sets, authn/authz rules, authz profiles, dACLs, allowed protocols, conditions | create/update rules & profiles, dACLs · **D:** delete policy set/rule |
| TrustSec | SGTs, SGACLs, egress matrix, IP-SGT mappings, SXP | create/update · **D:** delete |
| ANC | `ise_list_anc_policies`, `ise_anc_endpoints` | **D:** `ise_anc_apply`, `ise_anc_clear` |
| Escape hatch | `ise_search_spec`, `ise_get_definition`, `ise_api_call(surface=…)` | via WriteGuard |

Core pieces exercised: multi-surface client, `PageSize`, XML helper, surface probing in `status`,
Basic auth, spec tools over **multiple spec groups**.

### 5.3 Catalyst Center — `catc-mcp-server`

**API:** `POST /dna/system/api/v1/auth/token` (Basic) → `X-Auth-Token` (about 1h) → `LoginToken`.
Intent API `/dna/intent/api/v1|v2/...`. Many writes return a **task id** (or execution id for business
APIs), which go through the poller. Paging is 1-based `offset`, `limit` ≤500. Per-API rate limits
return 429. The spec is **bundled** per supported version (2.3.7.x first). Check whether the lab has a
CatC lab, and fall back to the DevNet always-on sandbox for read-only testing.

| Area | Read | Write / dangerous |
|---|---|---|
| System | `catc_status`, `catc_audit_logs`, `catc_get_task` | — |
| Sites | `catc_site_hierarchy`, `catc_get_site`, `catc_site_settings` | create area/building/floor · **D:** delete site |
| Inventory | `catc_list_devices` (filters), `catc_get_device` (name/ip/serial), `catc_device_interfaces`, `catc_device_config`, `catc_device_modules`, `catc_device_eox`, `catc_device_license` | assign to site, tags, `catc_resync_device` · **D:** delete device |
| Assurance | `catc_network_health`, `catc_device_health`, `catc_client_health`, `catc_get_client` (mac), `catc_issues`, `catc_get_issue`, `catc_events` | resolve/ignore issue |
| Smart | ✱`catc_device_360`, ✱`catc_client_360(mac)`, ✱`catc_path_trace(src,dst)` (start + poll + summarise), ✱`catc_run_show_commands` (command runner, **show-only allow-list**, read tier) | |
| Topology | `catc_physical_topology`, `catc_l3_topology`, `catc_vlan_topology` | — |
| Compliance | `catc_compliance_summary`, `catc_compliance_detail` | run compliance check |
| Templates | `catc_list_templates`, `catc_get_template`, ✱`catc_preview_template` | create/commit template · **D:** `catc_deploy_template` |
| SWIM | `catc_list_images`, `catc_golden_images` | import/tag golden · **D:** distribute/activate |
| Discovery | `catc_list_discoveries`, `catc_discovery_devices` | create discovery · **D:** delete |
| Provisioning / SDA / wireless | read: fabric sites, devices, SSIDs, wireless profiles | **D:** provision device, fabric changes (phase 2) |
| Escape hatch | `catc_search_spec`, `catc_get_definition`, `catc_api_call` | via WriteGuard |

Core pieces exercised: `LoginToken` with expiry, `OneBasedOffset`, poller (task **and** execution
styles), rate limiter on 429, bundled spec source.

### 5.4 FMC — `fmc-mcp-server`

**API:** `POST /api/fmc_platform/v1/auth/generatetoken` (Basic) → `X-auth-access-token` +
`X-auth-refresh-token` (30 min, 3 refreshes) + `DOMAIN_UUID`. Config API
`/api/fmc_config/v1/domain/{uuid}/...`. Paging `offset`/`limit` ≤1000 with `expanded=true`.
**About 120 requests/min and 10 concurrent connections per source IP**, so the rate limiter and semaphore are mandatory.
Multi-domain support: `FMC_DOMAIN` sets the default and tools accept `domain=`. Target FMC 7.2+ (cdFMC
via SCC is a later option, with different auth). **Your current FMC config has a placeholder URL, so
we need a lab with FMC.**

| Area | Read | Write / dangerous |
|---|---|---|
| System | `fmc_status` (version, domains, user), `fmc_list_domains`, `fmc_license_status`, `fmc_audit_records`, `fmc_get_task` | — |
| Devices | `fmc_list_devices`, `fmc_get_device`, `fmc_device_interfaces`, `fmc_list_ha_pairs`, `fmc_list_clusters`, `fmc_device_health`/alerts | update interface, zones · **D:** register/delete device, form/break HA, HA switchover |
| Policies | `fmc_list_access_policies`, ✱`fmc_get_access_policy` (rules flattened, with resolved objects), `fmc_list_nat_policies`, `fmc_get_nat_rules`, `fmc_list_prefilter_policies`, intrusion/file/platform policy lists, policy assignments | create/update/move ACP rule (with insert position), NAT rules · **D:** delete rule/policy |
| Objects | `fmc_list_objects(type)`, `fmc_get_object`, ✱`fmc_where_used(object)`, ✱`fmc_find_object(ip/port)` | create/update object & group · **D:** delete (blocked if in use; preview shows references) |
| Smart | ✱`fmc_rule_lookup(src,dst,port,proto)` (offline: which ACP rule would match), ✱`fmc_pending_changes` (what a deploy would push, per device), ✱`fmc_unused_objects`, ✱`fmc_shadowed_rules` (best-effort) | |
| Deploy | `fmc_deployable_devices`, `fmc_deployment_history` | **D:** `fmc_deploy(devices, wait)` (poller) |
| VPN / routing | S2S & SD-WAN topologies, VTIs, BGP/OSPF/EIGRP, static routes | create/update · **D:** delete |
| Escape hatch | `fmc_search_spec`, `fmc_get_definition`, `fmc_api_call` | via WriteGuard |

Core pieces exercised: `LoginToken` + refresh hook, domain context, `OffsetLimit`, rate limiter and
concurrency semaphore, poller (deploys, registration), WriteGuard dangerous tier.

---

## 6. Security checklist (applies to core and every server)

- Upstream TLS verification **on by default**. `*_VERIFY_SSL=false` logs a warning on startup and shows in `*_status`. `*_CA_BUNDLE` is supported. The lab will need `false` or a CA bundle.
- Secrets are `SecretStr`, never logged, and redacted from errors, audit lines and `status`.
- `api_call` is locked to configured surfaces (SSRF guard), with path validation and no absolute URLs.
- XML parsed with `defusedxml` only.
- HTTP transport refuses to start without auth tokens unless `MCP_ALLOW_ANONYMOUS=true` is set explicitly. That's intended for localhost only.
- CLI-on-device tools (CML console, CatC command runner) use an allow-list for read tier; anything else is a write.
- One least-privilege service account per product, documented in each README (e.g. ISE ERS Operator for read-only; FMC custom role).
- `security-review` and `python-review` passes before each server's 0.1 release.

---

## 7. Testing strategy

| Layer | Where | How |
|---|---|---|
| Core unit | core repo | respx; auth strategies (expiry, refresh, single-flight), paging, retry/backoff, rate limiter timing, WriteGuard token rules, spec index, redaction |
| Contract | every server | `stutters_mcp_core.testing.contract`, run as one parametrised test |
| Server unit | every server | in-memory FastMCP client + respx with recorded (redacted) lab fixtures |
| Live | every server | `@pytest.mark.live`, auto-skipped when the lab is unreachable; read-only by default; write tests create and delete `mcp-test-*` objects |
| Lab seeding | `scripts/seed_lab.py` per server | idempotent: creates the objects live tests expect (as `seed_lab.py` does in NetBox) |
| Smoke | CI on tag | build the container, run `<server> check` against a respx stub |

Coverage target: 80%+ on core, 70%+ on servers (thin tool wrappers are covered by contract and fixtures).

---

## 8. Roadmap

Each milestone is a set of PRs: one feature branch per item, you merge, then redeploy on cortex
(the workflow that worked for NetBox).

**M0 — Foundations (½ day)**
- Create `~/MCP-Suite/` and 5 public MIT GitHub repos: `stutters-mcp-core`, `cml-mcp-server`, `ise-mcp-server`, `catc-mcp-server`, `fmc-mcp-server`. Git identity `drstuttersau@gmail.com`.
- Enable GHCR package publishing for the org/user.
- Confirm the lab credentials + API services for each product (see §11).
- Write ADR-001 (shared package, versioning policy) and ADR-002 (write tiers and preview tokens).

**M1 — Core 0.1: minimal, enough for CML read-only**
- settings, server factory + CLI, inbound bearer auth, ApiClient + `LoginToken` + retries, errors, formatting, `NoPaging`, testing plugin + contract linter, reusable CI workflows.
- Tag `v0.1.0` (git tag + GitHub release; no PyPI).

**M2 — CML 0.1 (read) → 0.2 (writes)**
- Read tools, spec tools (→ core 0.2 adds the `spec/` package), status, Docker on cortex :8032.
- Writes + WriteGuard (→ core 0.3 adds `writes.py`, `audit.py`, `tasks.py` for convergence waits).
- Retire `uvx cml-mcp` in your Claude config once it reaches parity.
- **Exit check:** contract linter green, live tests pass on the lab, can build an ISE test topology in CML.

**M3 — ISE 0.1 → 0.2**
- Core 0.4: multi-surface client, `PageSize`, XML helper, surface probing.
- Read tools + smart troubleshooting tools first (highest value), then writes, then dangerous (ANC/CoA).
- Retire `~/Cisco/MCP/ise-mcp`.

**M4 — Catalyst Center 0.1 → 0.2**
- Core 0.5: `OneBasedOffset`, bundled spec source, poller "execution" style, 429-driven limiter.
- Inventory/assurance/topology read tools, path trace and command runner, then templates/sites writes; SWIM and provisioning as dangerous in a later minor release.

**M5 — FMC 0.1 → 0.2**
- Core 0.6: `LoginToken` refresh hook, domain context, concurrency semaphore.
- Read tools + `rule_lookup`/`where_used`/`pending_changes`, then object/rule writes, then deploy and HA.
- Retire `~/Cisco/MCP/firepower-mcp`.

**M6 — Hardening and 1.0**
- cdFMC via Security Cloud Control (SCC API-token auth strategy in core; FMC server gains a `FMC_MODE=cdfmc`).
- ISE pxGrid (cert auth, WebSocket/STOMP subscriptions → session/ANC tools). Own design doc first.
- Core 1.0 once all four servers are on it without core changes for one release cycle.
- OAuth (GitHub IdP) and per-user roles in core. Optional public HTTPS instance (VPS + Caddy, as planned for NetBox).
- Cross-product workflows: MCP prompts that chain the servers (e.g. "onboard a switch": CML node → CatC discovery → ISE NAD → NetBox device; "quarantine host": ISE session lookup → ANC → FMC dynamic object).

---

## 9. Definition of done (per server release)

- [ ] Contract linter, ruff, mypy strict, unit tests green in CI
- [ ] Live tests pass against a lab (run log attached to the PR)
- [ ] README: what it does, env table, least-privilege account setup, Claude Code/Desktop config snippets (stdio + HTTP), tool list (auto-generated), "not affiliated with Cisco" note
- [ ] `.env.example`, Dockerfile, compose; container healthy on cortex
- [ ] CHANGELOG entry, tagged release, published to PyPI + GHCR
- [ ] Old server for that product removed from `~/.claude.json` (after you confirm)

---

## 10. Risks and mitigations

| Risk | Mitigation |
|---|---|
| labs expire, so live tests rot | Recorded fixtures for unit tests; live tests auto-skip; seed scripts make a fresh lab usable in minutes |
| Core API churn breaks servers | 0.x minor-pin policy; core CHANGELOG; each server CI also runs against core `main` nightly (allowed to fail) |
| Over-abstracting core before a second consumer exists | Core grows **per milestone**: a feature only enters core when the server being built needs it, and is generalised when the second server needs it |
| Tool sprawl (old ISE server had about 150 tools) | Target 30–50 curated tools per server; everything else via spec tools + `api_call` |
| FMC rate limits / CatC 429s under multi-user load | Shared limiter per process; document one container per FMC; cache read-mostly lookups (domains, object names) for about 60s |
| Product version drift | `*_status` reports versions; spec index cached per version; each README states the supported version matrix |
| Trademark/naming | Avoid "Cisco" in package names (decided); README disclaimer |

---

## 11. Open questions

Resolved 2026-10-14: core name (`stutters-mcp-core`), no PyPI (git tags + GHCR), location `~/MCP-Suite`,
order CML → ISE → CatC → FMC, versions = whatever the lab runs, later scope = cdFMC + pxGrid +
cross-product workflows, NetBox stays standalone.

Lab probe 2026-10-14 (read-only login + version, all OK):
| Lab | Host | Version | Notes |
|---|---|---|---|
| CML | 198.18.128.10 | 2.10.0 | 0 labs (empty) |
| ISE | 198.18.134.35 | 3.5.0.527, standalone | OpenAPI, MnT and ERS all answer on 443; 9060 blocked → default ERS to 443 |
| Catalyst Center | 198.18.128.11 | 3.2.2 | 0 devices (empty) |
| FMC | 198.18.134.76 | 10.0.1 | Domain `Global`, 0 managed devices |

All four present self-signed certs: lab `.env` files need `*_VERIFY_SSL=false` (or a CA bundle).
The labs are empty, so seed scripts (and CML-built topologies) are needed for meaningful live tests.

Nothing left open.
