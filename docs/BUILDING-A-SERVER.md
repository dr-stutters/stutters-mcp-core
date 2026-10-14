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
dependencies = ["stutters-mcp-core"]

[project.scripts]
ise-mcp-server = "ise_mcp:main"

[tool.uv.sources]
stutters-mcp-core = { git = "https://github.com/dr-stutters/stutters-mcp-core", tag = "v0.1.0" }
# while co-developing core locally instead:
# stutters-mcp-core = { path = "../stutters-mcp-core", editable = true }

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
`MCP_ENABLE_WRITES`, `MCP_ENABLE_DANGEROUS`, `MCP_CONFIRM_TTL`, `MCP_CACHE_DIR`,
`MCP_MAX_RESPONSE_CHARS`, `MCP_TASK_TIMEOUT`, `MCP_AUDIT_LOG`, `LOG_LEVEL`.

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
- Prefer a few task-oriented tools ("why did this auth fail") over many thin CRUD wrappers. `<prefix>_api_call` covers the rest.
- Long jobs: `poll_until(...)` with `ctx=ctx`. Take `wait: bool = True`; when not waiting, return the task id and the tool that checks it.

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

## 7. Tests

```python
from stutters_mcp_core.testing import assert_tool_contract, call_tool, live_settings

async def test_contract():                       # run with writes+dangerous on so every tool is checked
    await assert_tool_contract(create_server(settings(mcp_enable_writes=True, mcp_enable_dangerous=True)), "ise")
```

- Unit tests mock HTTP with `respx` and call tools through `call_tool` or `fastmcp.Client`.
- Live tests use `live_settings(Settings, "ISE_")`, which reads `.env` and skips when the lab is unreachable.
- Live write tests create and delete objects named `mcp-test-*`.

## 8. Docker / cortex

Copy `docs/templates/Dockerfile` and `docs/templates/docker-compose.yml`, then replace `__SERVER__`
(e.g. `ise-mcp-server`), `__PORT__` (8032 CML, 8033 ISE, 8034 CatC, 8035 FMC) and `__ENV__` (e.g. `ISE`).
The image build needs the private core while the repos are private. Pass it as a BuildKit secret:
`docker build --secret id=core_read_token,env=CORE_READ_TOKEN .`

## 9. CI

```yaml
# .github/workflows/ci.yml
name: ci
on: {push: {branches: [main]}, pull_request: {}}
jobs:
  ci:
    uses: dr-stutters/stutters-mcp-core/.github/workflows/python-ci.yml@main
    secrets: inherit
```
```yaml
# .github/workflows/release.yml
name: release
on: {push: {tags: ["v*"]}}
jobs:
  release:
    uses: dr-stutters/stutters-mcp-core/.github/workflows/python-release.yml@main
    with: {image: true}
    secrets: inherit
```
