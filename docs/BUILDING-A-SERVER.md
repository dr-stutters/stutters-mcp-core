# Building a server on stutters-mcp-core

This is the checklist and skeleton every server in the suite follows. `tests/unit/demo.py` in this
repo is a complete, tested miniature of everything below.

## 1. Layout

```
<server>/                          e.g. ise-mcp-server
  pyproject.toml  README.md  LICENSE  CHANGELOG.md  .env.example  .gitignore
  Dockerfile  docker-compose.yml   (copy from docs/templates/, replace the placeholders)
  src/<pkg>/                       e.g. ise_mcp
    __init__.py      main() -> run_cli(SPEC, Settings, create_server)
    config.py        Settings(BaseServerSettings) with model_config = server_config("ISE_")
    client.py        the product client ("app"): ApiClient(s), auth, pagers, status()
    server.py        SPEC, INSTRUCTIONS, create_server()
    tools/<area>.py  one register(reg: ToolRegistrar) per area
  tests/unit/        respx-mocked tests + contract test
  tests/live/        @pytest.mark.live tests against the lab (auto-skip when unreachable)
  scripts/seed_lab.py   idempotent: creates the objects the live tests expect
  .github/workflows/ci.yml, release.yml   (call core's reusable workflows)
```

## 2. pyproject.toml

```toml
[project]
name = "ise-mcp-server"
version = "0.1.0"
requires-python = ">=3.12,<3.15"
dependencies = [
    # >>> bundled stutters-mcp-core
    # <<< bundled stutters-mcp-core
]

[project.scripts]
ise-mcp-server = "ise_mcp:main"

# core is bundled as ise_mcp._core (ADR-005): python scripts/bundle_core.py vendor
[tool.bundle-core]
tag = "v0.12.4"
commit = "70a6bafab04a94df152944acc9f024562fbd7020"

[build-system]
requires = ["uv_build>=0.10.9,<0.11.0"]
build-backend = "uv_build"

[tool.uv.build-backend]
module-name = "ise_mcp"

[dependency-groups]
dev = ["mypy>=2.4.0", "pytest>=9.1.1", "pytest-asyncio>=1.4.0", "respx>=0.23.1", "ruff>=0.17.0"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests/unit"]
markers = ["live: needs the lab appliance (skipped when unreachable)"]

[tool.ruff]
line-length = 110
target-version = "py312"
extend-exclude = ["docs"]

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM", "ASYNC"]

[tool.mypy]
strict = true
files = ["src"]
```

Live tests run with `uv run pytest tests/live -m live`.

## 3. config.py

```python
from stutters_mcp_core import BaseServerSettings, server_config

class Settings(BaseServerSettings):
    model_config = server_config("ISE_")
    ers_port: int = 443            # ISE_ERS_PORT; product-specific extras go here
```

You get for free: `<P>_URL, _USERNAME, _PASSWORD, _TOKEN, _VERIFY_SSL, _CA_BUNDLE, _TIMEOUT, _RETRIES,
_RATE_LIMIT, _MAX_CONCURRENCY` plus `MCP_TRANSPORT/HOST/PORT/PATH/AUTH_TOKENS(_FILE)`,
`MCP_ENABLE_WRITES`, `MCP_ENABLE_DANGEROUS`, `MCP_CONFIRM_TTL`, `MCP_CONFIRM_MODE`, `MCP_CACHE_DIR`,
`MCP_MAX_RESPONSE_CHARS`, `MCP_TASK_TIMEOUT`, `MCP_AUDIT_LOG`, `LOG_LEVEL`, and the opt-in production
controls in [PRODUCTION.md](PRODUCTION.md) (`MCP_DRY_RUN`, `MCP_CHANGE_WINDOW`, `MCP_DENY_TOOLS`, ...).

Every secret setting (any `SecretStr` field, yours too) can also come from a file: `<VAR>_FILE`, e.g.
`ISE_PASSWORD_FILE=/run/secrets/ise_password` (Docker/Kubernetes secrets; one trailing newline is
stripped). Setting both `<VAR>` and `<VAR>_FILE` is a configuration error. `MCP_AUTH_TOKENS_FILE` keeps
its own semantics (appended to `MCP_AUTH_TOKENS`). Values passed to the constructor win and skip the file.
If your Settings overrides `settings_customise_sources`, call `super()` or `_FILE` support is lost.

## 4. client.py: the "app" object

```python
from stutters_mcp_core.http import ApiClient, LoginTokenAuth, OffsetLimit, RateLimiter, Token

class FmcApp:
    def __init__(self, settings: Settings, transport=None):   # transport: tests inject respx/MockTransport
        limiter = RateLimiter(settings.rate_limit or 110, settings.max_concurrency or 8)
        self.api = ApiClient(settings.url, product="FMC", env_prefix="FMC_", auth=LoginTokenAuth(self._login,
                             refresh=self._refresh), verify=settings.verify, timeout=settings.timeout,
                             retries=settings.retries, limiter=limiter, error_detail=fmc_error_detail,
                             transport=transport)
        self.pager = OffsetLimit(items="items", total="paging.count", max_page=1000)

    async def _login(self, client: ApiClient) -> Token:
        resp = await client.send_raw("POST", "/api/fmc_platform/v1/auth/generatetoken", headers=...)
        if resp.status_code != 204: raise client.error_from_response(resp)
        return Token({"X-auth-access-token": resp.headers["X-auth-access-token"]},
                     Token.expiring_in(30 * 60), {"refresh": ..., "domain": ...})

    async def aclose(self) -> None: await self.api.aclose()
    async def status(self) -> dict[str, Any]: ...   # version, API user, reachable surfaces
```

- Login flows use `client.send_raw` (no auth or retry recursion) and `client.error_from_response`.
- Several surfaces (ISE) mean several ApiClients, optionally sharing one RateLimiter.
- One pager per paging style is enough: endpoints with a smaller page-size cap pass it per call,
  `await app.pager.fetch_page(app.api, path, params, limit=limit, offset=offset, max_page=25)` (also `fetch_all(..., max_page=)`).
- `error_detail=` is a function that pulls a readable message out of the product's error body.

## 5. Tools

```python
# No `from __future__ import annotations` in tool modules: FastMCP inspects signatures at runtime.
from typing import Annotated, Any
from fastmcp import Context
from fastmcp.tools import ToolResult
from pydantic import Field
from stutters_mcp_core import Confirm, Plan, ResponseFormat, ToolRegistrar, get_client, list_result, \
    object_result, preview_or_apply

def register(reg: ToolRegistrar) -> None:
    @reg.read(title="List network devices")
    async def ise_list_nads(
        ctx: Context,
        name: Annotated[str | None, Field(description="Filter: name contains")] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
        response_format: ResponseFormat = "markdown",
    ) -> ToolResult:
        """List network access devices (NADs) with IPs and device groups. ..."""
        app = get_client(ctx)
        page = await app.ers_pager.fetch_page(app.ers, "/ers/config/networkdevice", params, limit=limit, offset=offset)
        return list_result(page, fmt=response_format)

    @reg.write(title="Create NAD")
    async def ise_create_nad(ctx: Context, name: Annotated[str, Field(description="...")], ...,
                             confirm: Confirm = None) -> dict[str, Any]:
        """Create a NAD. Preview-first ..."""
        plan = Plan(f"Create NAD {name}", details={...}, blockers=[...if name already exists...])
        return await preview_or_apply(ctx, tool="ise_create_nad", args={...every arg except confirm...},
                                      confirm=confirm, plan=plan, apply=lambda: app.ers.post(...))
```

Rules (enforced by `assert_tool_contract`):
- Names are `<prefix>_<verb>_<noun>`, snake_case.
- Descriptions are 30–1500 chars: what it returns, when to use it, which tool supplies the ids.
- Every parameter has a `Field(description=...)`, except `limit`, `offset`, `response_format`, `confirm` and `fields`.
- `*_list_*` tools take `limit`, `offset` and `response_format`.
- Write and dangerous tools take `confirm: Confirm = None` and go through `preview_or_apply`.
  - For update and delete, set `Plan.fingerprint` to something that changes when the target changes (e.g. its JSON or a last-modified stamp).
  - Use `field_changes(current, desired)` for the diff.
- Use the dangerous tier for anything disruptive: deploys, provisioning, CoA/disconnect, HA changes, deleting live policy, rebooting.
- Accept names **or** ids wherever possible, resolving them in the tool.
- Name arguments from the suite vocabulary ([VOCABULARY.md](VOCABULARY.md)): `since_hours`, `wait`,
  `timeout_seconds`, `response_format`, `limit`/`offset`, `detail`, `device`, `confirm`. `warn_vocabulary`
  (§7) reports drift as warnings.
- Prefer a few task-oriented tools ("why did this auth fail") over many thin CRUD wrappers. `<prefix>_api_call` covers the rest.
- When a write can count what it touches (devices deployed to, endpoints, rules), set
  `Plan(blast_radius=N)`; operators can cap it (`MCP_MAX_BLAST_RADIUS`, see [PRODUCTION.md](PRODUCTION.md)).
  Never bypass `preview_or_apply`: the safety rails (dry-run, change window, two-person) live there.
- Multi-instance (ADR-004): keep per-appliance state on the client object from `get_client(ctx)` (never in
  module globals), read settings with `get_settings(ctx)`, and don't name a tool parameter `instance`.
  Read tools also run with `instance='*'` (C74): once per instance, several at a time, so a read must not
  keep per-call state anywhere but locals and the client object.
- Long jobs: `poll_until(...)` with `ctx=ctx`, and mark the tool `@reg.write(..., task=True)` (or read/dangerous)
  so that with `MCP_TASKS=true` it runs as an MCP background task (async functions only; depend on
  `stutters-mcp-core[tasks]`). Without MCP_TASKS the flag does nothing. Take `wait: bool = True`; when not waiting, return the task id and the tool that checks it.

## 5a. Prompts (cross-product workflows)

Workflows ship as MCP prompts in the server that **starts** them (ROADMAP "Cross-product workflow
prompts"). Register them from the same `register(reg)` functions as tools:

```python
from stutters_mcp_core import Tier, tool_ref

def register(reg: ToolRegistrar) -> None:
    @reg.prompt(title="Troubleshoot endpoint access", uses=["ise", "catc"])
    def ise_troubleshoot_endpoint_access(
        mac: Annotated[str, Field(description="Endpoint MAC address, any notation")],
        since_minutes: Annotated[int, Field(description="How far back to look")] = 60,
    ) -> str:
        """Why can't this endpoint get on the network? Walks ISE sessions, the failure reason and the
        policy hit, then the switch port through Catalyst Center if that server is connected."""
        return f"""Troubleshoot network access for {mac} over the last {since_minutes} minutes.
1. Call {tool_ref("ise_why_auth_failed")} with mac="{mac}".
2. Call {tool_ref("ise_endpoint_360")} for the profile, groups and the NAD/port.
3. If the catc server is connected, run {tool_ref("catc_run_show_commands")} on that switch port.
Every write step stays preview-first: show the preview and wait for approval."""
```

- Name `<prefix>_<workflow>`; the docstring (30–1500 chars) is the description; every argument has a
  `Field(description=...)`. Clients send arguments as strings; FastMCP converts them to the annotated type.
- `uses=[...]` lists every server prefix the workflow expects (yours is added if missing). It is published
  in the prompt's `meta` (`{"uses": [...], "tools": [...], "tier": "read"}`) so clients can tell the user
  which servers to connect.
- Write tool names with `tool_ref("...")` (renders them in backticks). `tools=[...]` may also declare them.
- A workflow that needs write tools takes `tier=Tier.WRITE` (or `Tier.DANGEROUS`): it is hidden when that
  tier is off, like the tools it calls.
- `example={"mac": "AA:BB:CC:DD:EE:FF"}` registers valid string arguments. The test kit and
  `<server> tools --json` render the prompt with them when no `samples` are passed (placeholders like
  `"example"` fail argument validation).
- Test it with `assert_prompt_contract(server, "ise", samples={...}, known_prefixes={"cml","ise","fmc","catc"})`
  (all tiers on). It renders every prompt and fails on unknown local tools, foreign tools whose server is
  not in `uses`, missing argument docs, and bad names or descriptions.

### Prompt test kit (cross-server references and snapshots, core 0.4.0)

Each server exports a static manifest of its tools and prompts. No appliance or running server is needed:
the CLI uses a placeholder URL when none is configured and turns all tiers on.

```bash
uv run ise-mcp-server tools                 # human-readable list
uv run ise-mcp-server tools --json --out tests/manifests/ise.json
```

Commit the *other* servers' manifests under `tests/manifests/` (copy them from their repos or regenerate
them from a checkout), then:

```python
from stutters_mcp_core.testing import (assert_inbound_references, assert_prompt_contract,
                                       assert_prompt_snapshots, load_tool_manifests)

async def test_prompts_against_other_servers():     # cross-product tool names exist on those servers
    await assert_prompt_contract(server_rw, "ise", samples=SAMPLES,
                                 external_tools=load_tool_manifests("tests/manifests"))

async def test_other_servers_prompts_still_resolve():  # nobody references a tool we removed/renamed
    await assert_inbound_references(server_rw, "ise", "tests/manifests")

async def test_prompt_snapshots():                   # rendered text, title, description, arguments
    await assert_prompt_snapshots(server_rw, "tests/snapshots/prompts", samples=SAMPLES)
```

Snapshots live in `<dir>/<prompt>.md`. A missing one is written (even in CI) and the test fails until it is committed;
a changed one fails with a unified diff. `UPDATE_PROMPT_SNAPSHOTS=1 uv run pytest` accepts changes and
deletes snapshots for removed prompts. Use one directory per configuration (e.g. `prompts-ro`,
`prompts-rw`) when the text depends on the enabled tiers. `external_tools` also accepts a hand-written
`{"fmc": ["fmc_contain_host", ...]}` mapping, e.g. for a tool another builder has promised but not shipped.

### Suite prompt pack (C48)

Cross-product workflows with no natural owner (`suite_*`: change check, maintenance window, ...) ship in core.
Every server registers the whole pack by adding `suite.register` to `build_server(register=[...])`, and tests
it with `assert_suite_pack(server_rw, "<prefix>", "tests/manifests", pending=[...])`. See
[SUITE-PROMPTS.md](SUITE-PROMPTS.md).

### Snapshots for change checks (C53)

`<prefix>_snapshot` / `<prefix>_compare_snapshot` return and compare a compact, versioned health blob that the
client keeps (no server state). Build them with `snapshot.register_snapshot_tools(reg, take, ...)`; the
contract (blob schema, item and metric rules, severities) is [SNAPSHOTS.md](SNAPSHOTS.md).

### Topology diagrams (C65)

A tool that draws a topology takes `format: Literal["mermaid", "drawio"]` (alongside `response_format` for
the data) and returns `render.diagram_result(graph, format, host="<server name>")`. Build the
`render.Graph` from what the tool already summarised: `Node(id, lines=[...], group=<site>, shape=...,
status="ok"|"warn"|"down")` and `Edge(a, b, label=..., status=...)`, or pass the same structure as dicts.
Groups become Mermaid subgraphs and draw.io containers; nodes with `x`/`y` keep their positions. Don't write
a renderer in the server.

**Logs and events:** servers are pull-only (ADR-003). Expose a platform's logs and events as ordinary read
tools that query the platform when called; don't add listeners, background pollers or buffers.

## 6. server.py

```python
from stutters_mcp_core import ServerSpec, build_server
from stutters_mcp_core.spec import SpecSource

SPEC = ServerSpec(name="ise-mcp-server", prefix="ise", product="Cisco ISE", env_prefix="ISE_",
                  instructions=INSTRUCTIONS)

def create_server(settings, *, auth=None, transport=None):
    return build_server(SPEC, settings,
        client_factory=lambda s: IseApp(s, transport=transport),
        register=[system.register, sessions.register, ...],
        status=IseApp.status,
        spec_sources=lambda app: [SpecSource("openapi", app.load_openapi_specs, client=lambda a: a.openapi), ...],
        auth=auth)
```

`build_server` adds `<prefix>_status`, `<prefix>_search_spec`, `<prefix>_get_definition`,
`<prefix>_api_call`, `/health`, audit logging and the writes/dangerous instructions.
`<prefix>_api_call` only accepts non-GET methods when dangerous is enabled.

**Always forward `auth=` to `build_server`.** If it's missing, the HTTP transport runs unauthenticated.
`run_cli` only passes auth when it calls your `create_server`.

**Clock and timestamps (C69).** `<prefix>_status` reports `server.clock`: the appliance's clock offset,
measured from the HTTP `Date` header of the requests `ApiClient` sends (positive = the appliance runs ahead),
a `warning` past 5 s, and the timestamp convention. Return timestamps in ISO 8601 as the product reported
them (with Z or an offset when the product gives a zone; never "corrected" for drift), and describe the
product's own log and event stamps in `ServerSpec(log_timestamps="...")`, e.g. `"MnT: appliance local time,
no zone"`. Timeline prompts subtract each server's `offset_seconds` before merging. A status that does not
go through `ApiClient` can call `stutters_mcp_core.clock.observe(url, date_header, sent=, received=)`.

Aggregating tools that use `fetch_all` must check `page.complete` and say when results are
partial. List tools get this for free: `Page.more`/`next_offset` covers APIs that don't report a total.

## 6a. `check` preflight (core 0.14.0)

`<server> check` runs without an MCP client and tells an operator what is missing. The server supplies one
coroutine; the core runs it for every instance (`<P>_INSTANCES`), adds the configuration rows, classifies
errors (TLS trust → `tls`, connection/timeout → `reachability`, 401 → `auth`, 403 → `role`), prints text or
`--json`, and exits 1 on a failure (`--strict`: on a warning too).

```python
from ise_mcp._core.preflight import Preflight

async def preflight(settings: Settings, pf: Preflight) -> None:
    async with IseApp(settings) as app:
        for surface in ("openapi", "ers", "mnt"):            # one reachability row per API surface
            await pf.reach(surface, app.probe(surface), seconds=settings.probe_timeout)
        if pf.failed:
            return                                          # tls/auth/role/version are reported as skipped
        role = await app.admin_role()                       # whatever the product exposes
        pf.access(read=True, write=role.can_write, role=role.name)   # vs MCP_ENABLE_WRITES/_DANGEROUS
        pf.version(await app.version(), tested=["3.5"], minimum="3.1")

def main() -> None:
    run_cli(SPEC, Settings, create_server, preflight=preflight)
```

- `pf.reach(surface, awaitable, detail=..., seconds=...)` records `reachability[surface]` ok (with the time) or
  the classified failure, and returns the value or None. Pass authenticated calls: a successful reach also
  counts as "credentials accepted" unless you record `auth` yourself.
- `pf.access(read=, write=, dangerous=, role=)` takes True / False / None (unknown). It fails when an enabled
  tier isn't allowed, warns when unknown, and warns when the account can do more than the enabled tiers need
  (least privilege on a shared host). `dangerous` defaults to `write`.
- `pf.version(found, tested=[...], minimum=...)`: ok on a tested release prefix, warn when untested, fail
  when older than `minimum`.
- `pf.ok/warn/fail(check, detail, hint=...)` for anything product-specific (FMC's rate budget, CatC's ISE
  integration). Details and hints go through the secret scrubber.
- Test each failure mode against respx mocks (`tests/unit/test_preflight.py` in core shows the pattern).

`serve --http` / `serve --stdio` pick the transport on the command line (`MCP_TRANSPORT` otherwise; stdio
is the default).

## 7. Tests

```python
from stutters_mcp_core.testing import assert_prompt_contract, assert_tool_contract, call_tool, live_settings

async def test_contract():                       # run with writes+dangerous on so every tool is checked
    server = create_server(settings(mcp_enable_writes=True, mcp_enable_dangerous=True))
    await assert_tool_contract(server, "ise")
    await assert_prompt_contract(server, "ise", samples={"ise_troubleshoot_endpoint_access": {"mac": "aa:bb:cc:dd:ee:ff"}})
```

- Unit tests mock HTTP with `respx` and call tools through `call_tool` or `fastmcp.Client`.
- Live tests use `live_settings(Settings, "ISE_")`, which reads `.env` and skips when the lab is unreachable.
- Live write tests create and delete objects named `mcp-test-*`.
- Vocabulary (C68): `await warn_vocabulary(server, "ise", allow={...})` in the contract test reports argument
  names off [VOCABULARY.md](VOCABULARY.md) as pytest warnings; it never fails the build.
- Prompts: also the prompt test kit (§5a): `external_tools=load_tool_manifests(...)`,
  `assert_inbound_references` and `assert_prompt_snapshots`.

## 8. Docker / cortex

Copy `docs/templates/Dockerfile`, `docs/templates/docker-compose.yml` and `docs/templates/dockerignore`
(as `.dockerignore`, which keeps `.env` out of the build context), then replace `__SERVER__`
(e.g. `ise-mcp-server`), `__PORT__` (8032 CML, 8033 ISE, 8034 CatC, 8035 FMC) and `__ENV__` (e.g. `ISE`).
Also replace `__PKG__` (e.g. `ise_mcp`) in the Dockerfile. The image is built from the bundled wheel
(ADR-005) and needs no token: `docker build .` (it clones the pinned core tag itself when the context
isn't vendored yet).

The compose file pulls the published image (`ghcr.io/dr-stutters/__SERVER__`, `MCP_IMAGE_TAG` picks the tag,
default `latest`); `docker compose up -d --build` still builds from the checkout. Releases publish the image
with `publish-image.yml` (§9).

### Offline demo mode (C64)

`stutters_mcp_core.mock` records a storyline's reads from a real appliance and replays them, so a demo runs
when the lab is down. Recording is a reverse proxy (`python -m <pkg>._core.mock record --upstream https://...
--out routes.json --storyline NAME --replace LAB=DOC`): point the server's URL at it and run the storyline.
It forwards GETs and logins only, masks secrets and tokens, and `check` lists anything still looking like lab
data. Replay: `python -m <pkg>._core.mock serve routes.json` (or the file alone in `python:3.12-slim`, in place
of the examples' `appliance.py`, whose routes files it still reads), with the server's URL set to the mock.
`<prefix>_status` then shows `mode: mock`, the storyline and `captured_at`. The production client has no
replay branch: only the URL changes. Matching is query-aware (most specific query wins; time-window keys in
`ignore_query` are dropped). Servers' own unit tests can use `MockAppliance(Routes.load(...)).transport()`.

## 9. CI

```yaml
# .github/workflows/ci.yml
name: ci
on: {push: {branches: [main]}, pull_request: {}}
jobs:
  ci:
    uses: dr-stutters/stutters-mcp-core/.github/workflows/python-ci.yml@main
    with: {bundle-core: true}
  bundle:
    uses: dr-stutters/stutters-mcp-core/.github/workflows/bundle-core.yml@main
```
```yaml
# .github/workflows/release.yml
name: release
on: {push: {tags: ["v*"]}}
permissions: {contents: write, packages: write, id-token: write}   # required: callers must grant these
jobs:
  release:   # wheel + sdist + Python SBOM on the GitHub release (a -rc tag makes a pre-release)
    uses: dr-stutters/stutters-mcp-core/.github/workflows/python-release.yml@main
    with: {bundle-core: true}
    secrets: inherit
  image:     # multi-arch GHCR image, cosign keyless signature, SBOM attestation (docs/SUPPLY-CHAIN.md)
    needs: release
    uses: dr-stutters/stutters-mcp-core/.github/workflows/publish-image.yml@main
```
