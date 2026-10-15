"""Server factory: one call builds a FastMCP server with the shared plumbing wired in.

    SPEC = ServerSpec(name="ise-mcp-server", prefix="ise", product="Cisco ISE", env_prefix="ISE_",
                      instructions=INSTRUCTIONS)

    def create_server(settings, *, auth=None):
        return build_server(
            SPEC, settings,
            client_factory=IseClient.from_settings,   # anything with `async aclose()`
            register=[system.register, sessions.register, ...],   # each takes a ToolRegistrar
            status=IseClient.status,                 # async (app) -> dict, shown by ise_status
            spec_sources=ise_spec_sources,           # (app) -> list[SpecSource]; adds spec tools
            auth=auth,
        )

Lifespan context keys available to tools: "client" (the app object), "previews", "specs", "settings".
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.lifespan import lifespan
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from stutters_mcp_core import __version__
from stutters_mcp_core.audit import AuditMiddleware
from stutters_mcp_core.errors import InvalidRequest, UpstreamError
from stutters_mcp_core.formatting import set_max_chars
from stutters_mcp_core.inbound_auth import current_caller
from stutters_mcp_core.policy import SafetyPolicy
from stutters_mcp_core.prompts import registered_prompts
from stutters_mcp_core.roles import RoleMiddleware
from stutters_mcp_core.settings import BaseServerSettings
from stutters_mcp_core.spec.store import SpecSource, SpecStore
from stutters_mcp_core.spec.tools import register_spec_tools
from stutters_mcp_core.writes import PreviewStore, Tier, ToolRegistrar, get_previews

log = logging.getLogger(__name__)

DEFAULT_WRITE_INSTRUCTIONS = """
Writes are ENABLED on this server. Every write tool is PREVIEW-FIRST: a call without `confirm`
changes nothing and returns a preview plus a single-use confirm token. Show the preview to the user,
and only after they approve repeat the identical call with confirm=<token>. Tokens expire and are
refused if the arguments or the target objects changed; then preview again.
"""

DANGEROUS_INSTRUCTIONS = """
Disruptive operations (deploys, provisioning, session disconnects, deletions of live policy) are
ENABLED. Always state the blast radius from the preview before asking the user to confirm.
"""


@dataclass(frozen=True)
class ServerSpec:
    name: str  # distribution / FastMCP server name, e.g. "ise-mcp-server"
    prefix: str  # tool prefix, e.g. "ise"
    product: str  # human name, e.g. "Cisco ISE"
    env_prefix: str  # e.g. "ISE_"
    instructions: str
    write_instructions: str = DEFAULT_WRITE_INSTRUCTIONS


ClientFactory = Callable[[Any], Any]
Registrar = Callable[[ToolRegistrar], None]
StatusFn = Callable[[Any], Awaitable[dict[str, Any]]]
SpecSources = Callable[[Any], list[SpecSource]]


def get_client(ctx: Context) -> Any:
    """The product client object created by the server's client_factory."""
    return ctx.lifespan_context["client"]


def get_settings(ctx: Context) -> Any:
    return ctx.lifespan_context["settings"]


async def _aclose(app: Any) -> None:
    close = getattr(app, "aclose", None)
    if close is not None:
        await close()


def build_server(
    spec: ServerSpec,
    settings: BaseServerSettings,
    *,
    client_factory: ClientFactory,
    register: Iterable[Registrar],
    status: StatusFn | None = None,
    spec_sources: SpecSources | None = None,
    auth: AuthProvider | None = None,
) -> FastMCP[Any]:
    set_max_chars(settings.mcp_max_response_chars)
    cache_dir = settings.cache_dir_for(spec.name)

    @lifespan
    async def server_lifespan(server: FastMCP[Any]) -> AsyncIterator[dict[str, Any]]:
        app = client_factory(settings)
        if settings.url.startswith("http://"):
            log.warning("%sURL uses plain HTTP; credentials are sent unencrypted", spec.env_prefix)
        if not settings.verify_ssl and settings.ca_bundle is None:
            log.warning("%sVERIFY_SSL=false: the appliance's TLS certificate is not checked", spec.env_prefix)
        if status is not None:
            try:
                info = await status(app)
                log.info("Connected to %s at %s: %s", spec.product, settings.url, _brief(info))
            except Exception as e:  # noqa: BLE001 - never refuse to start over the status probe
                # The model can call <prefix>_status to see the problem.
                log.error("%s startup check failed: %s", spec.product, e)
        state: dict[str, Any] = {
            "client": app,
            "settings": settings,
            "previews": PreviewStore(ttl=settings.mcp_confirm_ttl),
            "policy": policy,
            "roles": roles,
        }
        if spec_sources is not None:
            state["specs"] = SpecStore(app, spec_sources(app), cache_dir, settings.url)
        try:
            yield state
        finally:
            await _aclose(app)

    instructions = spec.instructions.rstrip() + "\n"
    if settings.mcp_enable_writes:
        instructions += spec.write_instructions
    if settings.mcp_enable_dangerous:
        instructions += DANGEROUS_INSTRUCTIONS
    policy = settings.safety_policy()
    roles = settings.role_map()
    if settings.mcp_enable_writes and policy.active:
        instructions += _policy_instructions(policy)

    mcp: FastMCP[Any] = FastMCP(
        name=spec.name,
        instructions=instructions,
        version=__version__,
        lifespan=server_lifespan,
        mask_error_details=False,
        auth=auth,
        middleware=[AuditMiddleware(spec.name, roles)],
    )
    if roles is not None:
        mcp.add_middleware(RoleMiddleware(roles, mcp))

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        """Liveness for Docker/load balancers. Unauthenticated, so it reveals nothing upstream."""
        return JSONResponse({"status": "ok"})

    mcp._stutters_policy = policy  # type: ignore[attr-defined]
    reg = ToolRegistrar(mcp, settings, spec.prefix)
    _register_status(reg, spec, settings, status)
    if policy.two_person and settings.mcp_enable_writes:
        _register_review(reg, spec)
    for fn in register:
        fn(reg)
    if spec_sources is not None:
        register_spec_tools(reg, product=spec.product)
    return mcp


def _policy_instructions(policy: SafetyPolicy) -> str:
    lines = ["", "Safety rails are on (the preview's `policy` section shows how they apply):"]
    if policy.dry_run:
        lines.append("- DRY-RUN mode: previews only; no change can be confirmed.")
    if policy.window is not None:
        lines.append(
            f"- Changes can only be confirmed inside the change window ({policy.window.text}); "
            "previews work any time."
        )
    if policy.max_blast_radius is not None or policy.blast_overrides:
        lines.append("- Changes touching more objects than the blast-radius limit are blocked; split them.")
    if policy.two_person:
        lines.append(
            "- Some changes need two-person approval: a different user confirms the token "
            "(they can inspect it with the review_change tool)."
        )
    return "\n".join(lines) + "\n"


def _brief(info: dict[str, Any]) -> str:
    keys = [k for k in info if "version" in k.lower()] or list(info)[:3]
    return ", ".join(f"{k}={info[k]}" for k in keys[:4])


def _register_status(
    reg: ToolRegistrar, spec: ServerSpec, settings: BaseServerSettings, status: StatusFn | None
) -> None:
    description = (
        f"Check the connection to {spec.product}: product version and API user, which API surfaces "
        "answer, and this MCP server's configuration (writes enabled?, TLS verification). Call this "
        "first when other tools fail."
    )

    @reg.read(title=f"{spec.product} status", name=f"{spec.prefix}_status", description=description)
    async def status_tool(ctx: Context) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if status is not None:
            try:
                out[spec.prefix] = await status(get_client(ctx))
            except UpstreamError as e:
                out[spec.prefix] = {"error": str(e)}
            except ValueError as e:  # e.g. missing credentials
                out[spec.prefix] = {"error": f"Configuration problem: {e}"}
        tiers = reg.tiers
        out["server"] = {
            "name": spec.name,
            "core_version": __version__,
            "url": settings.url,
            "tls_verify": settings.verify_ssl or settings.ca_bundle is not None,
            "writes_enabled": settings.mcp_enable_writes,
            "dangerous_enabled": settings.mcp_enable_dangerous,
            "tools": {t.value: sum(1 for v in tiers.values() if v is t) for t in Tier},
        }
        roles = ctx.lifespan_context.get("roles")
        if roles is not None:
            out["server"]["access"] = roles.describe(current_caller())
        if reg.policy.active:
            out["server"]["safety"] = reg.policy.describe()
            if reg.withheld:
                out["server"]["safety"]["withheld_tools"] = sorted(reg.withheld)
        prompts = registered_prompts(reg.mcp)
        if prompts:
            out["server"]["prompts"] = sorted(prompts)
        specs = ctx.lifespan_context.get("specs")
        if specs is not None:
            out["server"]["spec_sources"] = list(specs.sources)
            if specs.errors:
                out["server"]["spec_errors"] = specs.errors
        return out


def _register_review(reg: ToolRegistrar, spec: ServerSpec) -> None:
    description = (
        "Show the pending change behind a two-person confirm token (MCP_TWO_PERSON_TOOLS): what it "
        "does, its preview, the arguments (secrets masked) and who requested it. The approver calls "
        "this, checks the change with the user, then repeats the requester's call with confirm=<token>. "
        "Changes nothing and does not use up the token."
    )

    @reg.read(title="Review a pending change", name=f"{spec.prefix}_review_change", description=description)
    async def review_change(
        ctx: Context,
        token: Annotated[str, Field(description="The confirm token from the requester's preview (pv_...)")],
    ) -> dict[str, Any]:
        store = get_previews(ctx)
        pending = store.peek(token)
        if pending is None or not pending.two_person or pending.preview is None:
            raise InvalidRequest(
                "Unknown or expired two-person token. Ask the requester to preview the change again."
            )
        return {
            "tool": pending.tool,
            "requested_by": pending.caller,
            "expires_in_seconds": store.remaining(pending),
            **pending.preview,
            "how_to_approve": (
                f"If the user approves, call {pending.tool} with exactly these arguments plus "
                f'confirm="{token}". You must be a different user than {pending.caller!r}. Masked '
                "(secret) or truncated arguments must be supplied in full by the requester: any "
                "difference refuses the token."
            ),
        }
