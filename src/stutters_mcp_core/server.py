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
from starlette.responses import JSONResponse, PlainTextResponse, Response

from stutters_mcp_core import __version__, metrics
from stutters_mcp_core.audit import AuditMiddleware
from stutters_mcp_core.clock import clock_status
from stutters_mcp_core.errors import InvalidRequest, UpstreamError
from stutters_mcp_core.formatting import mask_secrets, set_max_chars
from stutters_mcp_core.inbound_auth import current_caller
from stutters_mcp_core.overrides import OverrideStore
from stutters_mcp_core.pending import register_pending_tools
from stutters_mcp_core.policy import InstanceRules, SafetyPolicy
from stutters_mcp_core.prompts import registered_prompts
from stutters_mcp_core.roles import RoleMiddleware
from stutters_mcp_core.secrets import SecretErrorMiddleware, scrub_error, scrub_url
from stutters_mcp_core.settings import BaseServerSettings
from stutters_mcp_core.spec.store import SpecSource, SpecStore
from stutters_mcp_core.spec.tools import register_spec_tools
from stutters_mcp_core.targets import TargetState, target_name, target_settings
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


CONFIRM_MODE_INSTRUCTIONS = {
    "auto": """
Confirmation (MCP_CONFIRM_MODE=auto): if your client supports elicitation, the preview call itself asks
the user to approve the change in a dialog and applies it on accept (status 'applied', or 'declined');
there is no token to repeat. Clients without elicitation get the confirm-token flow above.
""",
    "elicit": """
Confirmation (MCP_CONFIRM_MODE=elicit): only the user confirms changes, in the client's dialog that the
preview call opens (status 'applied' on accept, 'declined' otherwise). Confirm tokens are not accepted,
except for two-person changes confirmed by a second user.
""",
}


@dataclass(frozen=True)
class ServerSpec:
    name: str  # distribution / FastMCP server name, e.g. "ise-mcp-server"
    prefix: str  # tool prefix, e.g. "ise"
    product: str  # human name, e.g. "Cisco ISE"
    env_prefix: str  # e.g. "ISE_"
    instructions: str
    write_instructions: str = DEFAULT_WRITE_INSTRUCTIONS
    # The server package's version for <prefix>_status; defaults to the installed distribution `name`.
    version: str | None = None
    # Product-specific examples for the spec tools' descriptions (neutral text when unset).
    spec_search_example: str | None = None
    spec_path_example: str | None = None
    # How the product's own logs and events stamp time, for <prefix>_status's clock block (C69), e.g.
    # "MnT: appliance local time, no zone"; core's tool convention is always reported.
    log_timestamps: str | None = None


ClientFactory = Callable[[Any], Any]
Registrar = Callable[[ToolRegistrar], None]
StatusFn = Callable[[Any], Awaitable[dict[str, Any]]]
SpecSources = Callable[[Any], list[SpecSource]]


def _server_version(spec: ServerSpec) -> str | None:
    if spec.version:
        return spec.version
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(spec.name)
    except PackageNotFoundError:
        return None


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

    names = settings.target_names()
    multi = len(names) > 1
    # Built now so a bad target configuration fails at startup, not on first use.
    configs: dict[str, BaseServerSettings] = {names[0]: settings}
    for name in names[1:]:
        configs[name] = target_settings(settings, name)

    async def open_target(name: str, cfg: BaseServerSettings) -> dict[str, Any]:
        label = f"{spec.product} [{name}]" if multi else spec.product
        app = client_factory(cfg)
        if cfg.url.startswith("http://"):
            log.warning("%s: URL uses plain HTTP; credentials are sent unencrypted", label)
        if not cfg.verify_ssl and cfg.ca_bundle is None:
            log.warning("%s: VERIFY_SSL=false: the appliance's TLS certificate is not checked", label)
        if status is not None:
            try:
                info = await status(app)
                log.info(
                    "Connected to %s at %s: %s",
                    label,
                    scrub_url(cfg.url),
                    _clean(_brief(mask_secrets(info)), cfg),
                )
            except Exception as e:  # noqa: BLE001 - never refuse to start over the status probe
                # The model can call <prefix>_status to see the problem.
                log.error("%s startup check failed: %s", label, _clean(str(e), cfg))
        bucket: dict[str, Any] = {"client": app, "settings": cfg}
        if spec_sources is not None:
            bucket["specs"] = SpecStore(app, spec_sources(app), cache_dir, cfg.url)
        return bucket

    @lifespan
    async def server_lifespan(server: FastMCP[Any]) -> AsyncIterator[dict[str, Any]]:
        per_target: dict[str, dict[str, Any]] = {}
        try:
            for name, cfg in configs.items():
                per_target[name] = await open_target(name, cfg)
            shared: dict[str, Any] = {
                "previews": PreviewStore(ttl=settings.mcp_confirm_ttl),
                "overrides": OverrideStore.from_settings(settings),
                "policy": policy,
                "roles": roles,
                "confirm_mode": settings.mcp_confirm_mode,  # C67
            }
            state: dict[str, Any] = (
                TargetState(names[0], per_target, shared) if multi else {**per_target[names[0]], **shared}
            )
            listener = None
            if settings.mcp_metrics and settings.mcp_metrics_port is not None:
                listener = await metrics.start_listener(settings.mcp_metrics_host, settings.mcp_metrics_port)
            try:
                yield state
            finally:
                if listener is not None:
                    await listener.stop()
        finally:
            for bucket in per_target.values():
                await _aclose(bucket["client"])

    instructions = spec.instructions.rstrip() + "\n"
    if settings.mcp_enable_writes:
        instructions += spec.write_instructions
    if settings.mcp_enable_dangerous:
        instructions += DANGEROUS_INSTRUCTIONS
    policy = settings.safety_policy()
    roles = settings.role_map()
    if settings.mcp_enable_writes and policy.active:
        instructions += _policy_instructions(policy)
    if settings.mcp_enable_writes and settings.mcp_confirm_mode != "token":
        instructions += CONFIRM_MODE_INSTRUCTIONS[settings.mcp_confirm_mode]

    mcp: FastMCP[Any] = FastMCP(
        name=spec.name,
        instructions=instructions,
        version=__version__,
        lifespan=server_lifespan,
        mask_error_details=False,
        auth=auth,
        middleware=[AuditMiddleware(spec.name, roles)],
    )
    if settings.mcp_parse_json_args:
        from stutters_mcp_core.json_args import JsonArgsMiddleware

        # C72: first in the chain, so the audit log and previews see the parsed (and masked) structure
        mcp.middleware.insert(0, JsonArgsMiddleware(mcp))
    if settings.mcp_caller_rate_limit is not None or settings.mcp_caller_rate_limit_by_role:
        from stutters_mcp_core.inbound_limits import CallerRateLimitMiddleware, parse_role_limits

        mcp.add_middleware(
            CallerRateLimitMiddleware(
                spec.name,
                spec.prefix,
                settings.mcp_caller_rate_limit,
                parse_role_limits(settings.mcp_caller_rate_limit_by_role),
                roles,
                names=names if multi else None,  # a fan-out read counts once per instance (C74)
            )
        )
    mcp.add_middleware(SecretErrorMiddleware(settings))
    if roles is not None:
        mcp.add_middleware(RoleMiddleware(roles, mcp))
    if settings.mcp_enable_writes and settings.mcp_confirm_mode != "token":
        from stutters_mcp_core.confirm import ConfirmInputMiddleware

        mcp.add_middleware(ConfirmInputMiddleware())  # C67: confirm dialogs on 2026-07-28 clients

    if settings.mcp_metrics:
        metrics.enable(lambda name: name in getattr(mcp, "_stutters_tiers", {}))
        metrics.set_info(spec.name, _server_version(spec) or "unknown", __version__)
        if settings.mcp_metrics_port is None:
            _register_metrics_route(mcp, auth)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        """Liveness for Docker/load balancers. Unauthenticated, so it reveals nothing upstream."""
        return JSONResponse({"status": "ok"})

    if settings.mcp_tasks:
        _enable_tasks(mcp, settings)
    mcp._stutters_policy = policy  # type: ignore[attr-defined]
    reg = ToolRegistrar(mcp, settings, spec.prefix)
    reg.instance_settings = list(configs.values())
    _register_status(reg, spec, settings, status)
    instance_two_person = any(InstanceRules.from_settings(c).two_person for c in configs.values())
    override_two_person = settings.mcp_runtime_overrides and settings.mcp_override_two_person
    if (policy.two_person or instance_two_person or override_two_person) and settings.mcp_enable_writes:
        _register_review(reg, spec)
        register_pending_tools(reg, spec.prefix)  # C66: list / cancel what awaits a second person
    for fn in register:
        fn(reg)
    if spec_sources is not None:
        register_spec_tools(
            reg,
            product=spec.product,
            search_example=spec.spec_search_example,
            path_example=spec.spec_path_example,
        )
    return mcp


def _enable_tasks(mcp: FastMCP[Any], settings: BaseServerSettings) -> None:
    """Register the SEP-2663 tasks extension (fastmcp-tasks; Docket in memory or Redis/Valkey)."""
    try:
        from fastmcp_tasks import TasksExtension
    except ImportError:
        raise ValueError(
            "MCP_TASKS=true needs the tasks extra: install stutters-mcp-core[tasks] (fastmcp-tasks)"
        ) from None
    mcp.add_extension(TasksExtension(url=settings.mcp_tasks_url))


def _register_metrics_route(mcp: FastMCP[Any], auth: AuthProvider | None) -> None:
    """GET /metrics on the MCP HTTP app, behind the same bearer tokens as MCP itself."""

    @mcp.custom_route("/metrics", methods=["GET"])
    async def metrics_route(request: Request) -> Response:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        token = token.strip()
        if auth is None or scheme.lower() != "bearer" or not token or await auth.verify_token(token) is None:
            return PlainTextResponse(
                "metrics need Authorization: Bearer <one of MCP_AUTH_TOKENS>\n",
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return PlainTextResponse(metrics.REGISTRY.render(), media_type=metrics.CONTENT_TYPE)


def _clean(text: str, settings: BaseServerSettings) -> str:
    """Error text for status: credential shapes and the configured secret values masked."""
    return scrub_error(text, settings)


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
            "(they can inspect it with the review_change tool; list_pending_changes shows what is "
            "waiting and cancel_pending_change withdraws or rejects one)."
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
        target_cfg: BaseServerSettings = ctx.lifespan_context.get("settings") or settings
        if status is not None:
            try:
                out[spec.prefix] = mask_secrets(await status(get_client(ctx)))
            except UpstreamError as e:
                out[spec.prefix] = {"error": _clean(str(e), target_cfg)}
            except ValueError as e:  # e.g. missing credentials
                out[spec.prefix] = {"error": _clean(f"Configuration problem: {e}", target_cfg)}
        tiers = reg.tiers
        state = ctx.lifespan_context
        target_info = (
            {"instances": state.names, "instance": target_name() or state.default}
            if isinstance(state, TargetState)
            else {}
        )
        out["server"] = {
            "name": spec.name,
            "server_version": _server_version(spec),
            "core_version": __version__,
            "url": scrub_url(target_cfg.url),
            "tls_verify": target_cfg.verify_ssl or target_cfg.ca_bundle is not None,
            "writes_enabled": settings.mcp_enable_writes,
            "dangerous_enabled": settings.mcp_enable_dangerous,
            "confirm_mode": settings.mcp_confirm_mode,
            "tools": {t.value: sum(1 for v in tiers.values() if v is t) for t in Tier},
            **target_info,
        }
        out["server"]["clock"] = clock_status(target_cfg.url, product_convention=spec.log_timestamps)
        from stutters_mcp_core.mock import detect  # here: `python -m stutters_mcp_core.mock` stays clean

        demo = await detect(target_cfg.url)  # offline demo mode (C64): plain-HTTP URLs only
        if demo:
            out["server"].update(demo)
        roles = ctx.lifespan_context.get("roles")
        if roles is not None:
            out["server"]["access"] = roles.describe(current_caller())
        rules = InstanceRules.from_settings(target_cfg)
        if rules.active:
            out["server"]["instance_rules"] = rules.describe()
        if reg.task_tools:
            backend = settings.mcp_tasks_url.split("://", 1)[0]
            out["server"]["tasks"] = {"backend": backend, "tools": sorted(reg.task_tools)}
        if reg.policy.active:
            out["server"]["safety"] = reg.policy.describe()
            if reg.withheld:
                out["server"]["safety"]["withheld_tools"] = sorted(reg.withheld)
        overrides = ctx.lifespan_context.get("overrides")
        if isinstance(overrides, OverrideStore) and (overrides.enabled or overrides.active()):
            out["server"]["overrides"] = overrides.describe()
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

    name = f"{spec.prefix}_review_change"
    overrides: dict[str, str] = getattr(reg.mcp, "_stutters_role_tiers", {})
    overrides[name] = "write"  # with per-user roles: only callers who could approve may review
    reg.mcp._stutters_role_tiers = overrides  # type: ignore[attr-defined]

    @reg.read(title="Review a pending change", name=name, description=description)
    async def review_change(
        ctx: Context,
        token: Annotated[
            str,
            Field(
                description="The confirm token from the requester's preview (pv_...), or the preview id "
                "(pc_...) from the list_pending_changes tool"
            ),
        ],
    ) -> dict[str, Any]:
        store = get_previews(ctx)
        found = store.find(token.strip())
        token, pending = found if found is not None else (token, None)
        if pending is None or not pending.two_person or pending.preview is None:
            raise InvalidRequest(
                "Unknown or expired two-person token. Ask the requester to preview the change again."
            )
        tool_name, _, on_target = pending.tool.partition("@")
        return {
            "tool": tool_name,
            **({"instance": on_target} if on_target else {}),
            "preview_id": store.preview_id(token),
            "requested_by": pending.caller,
            "expires_in_seconds": store.remaining(pending),
            **pending.preview,
            "how_to_approve": (
                f"If the user approves, call {tool_name} with exactly these arguments"
                f"{f' (instance={on_target!r})' if on_target else ''} plus "
                f'confirm="{token}". You must be a different user than {pending.caller!r}. Masked '
                "(secret) or truncated arguments must be supplied in full by the requester: any "
                "difference refuses the token."
            ),
        }
