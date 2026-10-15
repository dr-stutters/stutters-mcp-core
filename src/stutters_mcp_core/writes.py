"""Write tiers and preview-then-confirm (ADR-002).

Registering tools:

    reg = ToolRegistrar(mcp, settings, prefix="ise")
    @reg.read(title="List NADs")
    async def ise_list_nads(...): ...
    @reg.write(title="Create NAD")                 # only registered if MCP_ENABLE_WRITES
    async def ise_create_nad(..., confirm: Confirm = None): ...
    @reg.dangerous(title="Apply ANC policy")       # also needs MCP_ENABLE_DANGEROUS
    async def ise_anc_apply(..., confirm: Confirm = None): ...

Inside a write tool:

    plan = Plan(f"Create NAD {name}", details={...}, fingerprint=None, blockers=[...])
    return await preview_or_apply(ctx, tool="ise_create_nad", args={...}, confirm=confirm,
                                  plan=plan, apply=lambda: client.post(...))

The first call (no confirm) changes nothing and returns the preview + a single-use token; the second
call with identical args and that token applies it, after re-checking that the target state
(fingerprint) hasn't changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, TypeVar

from fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from stutters_mcp_core.errors import InvalidRequest
from stutters_mcp_core.formatting import mask_secrets
from stutters_mcp_core.inbound_auth import current_caller
from stutters_mcp_core.policy import SafetyPolicy
from stutters_mcp_core.secrets import scrub_text
from stutters_mcp_core.targets import target_name

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from stutters_mcp_core.settings import BaseServerSettings

audit = logging.getLogger("stutters_mcp_core.audit")

F = TypeVar("F", bound=Callable[..., Any])

Confirm = Annotated[
    str | None,
    Field(
        description="Leave empty to get a preview (nothing changes). To apply, repeat the identical call "
        "with the confirm token returned by the preview, after the user has approved it."
    ),
]


class Tier(StrEnum):
    READ = "read"
    WRITE = "write"
    DANGEROUS = "dangerous"


def tier_enabled(settings: BaseServerSettings, tier: Tier) -> bool:
    if tier is Tier.READ:
        return True
    if tier is Tier.WRITE:
        return settings.mcp_enable_writes
    return settings.mcp_enable_writes and settings.mcp_enable_dangerous


def annotations_for(
    tier: Tier, *, title: str | None, destructive: bool | None, idempotent: bool | None
) -> ToolAnnotations:
    if tier is Tier.READ:
        return ToolAnnotations(
            title=title,
            read_only_hint=True,
            idempotent_hint=True if idempotent is None else idempotent,
            open_world_hint=True,
        )
    return ToolAnnotations(
        title=title,
        read_only_hint=False,
        destructive_hint=(tier is Tier.DANGEROUS) if destructive is None else destructive,
        idempotent_hint=bool(idempotent),
        open_world_hint=True,
    )


class ToolRegistrar:
    """Registers tools with tier-appropriate annotations, skipping tiers that are disabled, and
    records every tool's tier (used by the contract linter and the status tool)."""

    def __init__(self, mcp: FastMCP[Any], settings: BaseServerSettings, prefix: str) -> None:
        self.mcp = mcp
        self.settings = settings
        self.prefix = prefix
        registry: dict[str, Tier] = getattr(mcp, "_stutters_tiers", {})
        mcp._stutters_tiers = registry  # type: ignore[attr-defined]
        self.tiers = registry
        policy: SafetyPolicy | None = getattr(mcp, "_stutters_policy", None)
        if policy is None:
            policy = SafetyPolicy.from_settings(settings)
            mcp._stutters_policy = policy  # type: ignore[attr-defined]
        self.policy = policy
        withheld: dict[str, Tier] = getattr(mcp, "_stutters_withheld", {})
        mcp._stutters_withheld = withheld  # type: ignore[attr-defined]
        self.withheld = withheld  # tools MCP_DENY_TOOLS / MCP_ALLOW_TOOLS kept out
        self.core_tools = {f"{prefix}_status", f"{prefix}_review_change"}  # never withheld
        self.target_names: list[str] = settings.target_names()  # >1: tools get an `instance` argument
        task_tools: set[str] = getattr(mcp, "_stutters_task_tools", set())
        mcp._stutters_task_tools = task_tools  # type: ignore[attr-defined]
        self.task_tools = task_tools  # tools registered as MCP background tasks (MCP_TASKS)

    def _register(
        self,
        tier: Tier,
        *,
        title: str | None,
        name: str | None,
        destructive: bool | None,
        idempotent: bool | None,
        tags: set[str] | None,
        description: str | None = None,
        task: bool = False,
    ) -> Callable[[F], F]:
        def deco(fn: F) -> F:
            tool_name = name or fn.__name__
            if not tool_name.startswith(self.prefix + "_"):
                raise ValueError(f"Tool {tool_name!r} must start with {self.prefix}_")
            if not tier_enabled(self.settings, tier):
                return fn
            if not self.policy.registers(tool_name, tier.value, always=tool_name in self.core_tools):
                self.withheld[tool_name] = tier
                return fn
            if task and not inspect.iscoroutinefunction(fn):
                raise ValueError(
                    f"{tool_name}: task=True needs an async function (background tasks run async)"
                )
            registered: Callable[..., Any] = fn
            if len(self.target_names) > 1:
                from stutters_mcp_core.targets import with_target_argument

                registered = with_target_argument(fn, self.target_names, self.target_names[0])
            extra: dict[str, Any] = {}
            if task and getattr(self.settings, "mcp_tasks", False):
                # MCP background task (C12): clients that support tasks get a task handle and poll;
                # others run the call synchronously as before.
                from fastmcp.utilities.tasks import TaskConfig

                from stutters_mcp_core.secrets import SecretErrorMiddleware
                from stutters_mcp_core.targets import with_task_guard

                scrubber = SecretErrorMiddleware(self.settings)
                registered = with_task_guard(
                    registered,
                    server_has_auth=lambda: self.mcp.auth is not None,
                    scrub=scrubber.scrub,
                )
                extra["task"] = TaskConfig(mode="optional")
                self.task_tools.add(tool_name)
            self.mcp.tool(
                registered,
                name=tool_name,
                description=description,
                annotations=annotations_for(
                    tier, title=title, destructive=destructive, idempotent=idempotent
                ),
                tags={tier.value, *(tags or set())},
                **extra,
            )
            self.tiers[tool_name] = tier
            return fn

        return deco

    def read(
        self,
        *,
        title: str | None = None,
        name: str | None = None,
        idempotent: bool | None = None,
        tags: set[str] | None = None,
        description: str | None = None,
        task: bool = False,
    ) -> Callable[[F], F]:
        return self._register(
            Tier.READ,
            title=title,
            name=name,
            destructive=False,
            idempotent=idempotent,
            tags=tags,
            description=description,
            task=task,
        )

    def write(
        self,
        *,
        title: str | None = None,
        name: str | None = None,
        destructive: bool | None = None,
        idempotent: bool | None = None,
        tags: set[str] | None = None,
        description: str | None = None,
        task: bool = False,
    ) -> Callable[[F], F]:
        return self._register(
            Tier.WRITE,
            title=title,
            name=name,
            destructive=destructive,
            idempotent=idempotent,
            tags=tags,
            description=description,
            task=task,
        )

    def prompt(
        self,
        *,
        title: str | None = None,
        name: str | None = None,
        description: str | None = None,
        uses: Iterable[str] | None = None,
        tools: Iterable[str] = (),
        tier: Tier = Tier.READ,
        tags: set[str] | None = None,
        example: dict[str, str] | None = None,
    ) -> Callable[[F], F]:
        """Register a workflow prompt `<prefix>_<workflow>` (see stutters_mcp_core.prompts)."""
        from stutters_mcp_core.prompts import PromptRegistrar

        return PromptRegistrar(self.mcp, self.settings, self.prefix).prompt(
            title=title,
            name=name,
            description=description,
            uses=uses,
            tools=tools,
            tier=tier,
            tags=tags,
            example=example,
        )

    def dangerous(
        self,
        *,
        title: str | None = None,
        name: str | None = None,
        idempotent: bool | None = None,
        tags: set[str] | None = None,
        description: str | None = None,
        task: bool = False,
    ) -> Callable[[F], F]:
        return self._register(
            Tier.DANGEROUS,
            title=title,
            name=name,
            destructive=True,
            idempotent=idempotent,
            tags=tags,
            description=description,
            task=task,
        )


# --- preview / confirm --------------------------------------------------------------------


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class Plan:
    """What a write would do: a one-line summary, details for the user, anything that would make it
    fail, and a fingerprint of the current state it depends on (re-checked at confirm time).

    `blast_radius`: how many things the change touches (devices deployed to, endpoints affected, rules
    rewritten...), when the tool can count them. Shown in the preview and checked against
    MCP_MAX_BLAST_RADIUS / MCP_BLAST_RADIUS_OVERRIDES; None means "not counted" and is never limited."""

    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    fingerprint: Any = None
    blockers: list[str] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    blast_radius: int | None = None


@dataclass
class _Pending:
    tool: str
    args: str
    fingerprint: str
    caller: str
    expires: float
    two_person: bool = False
    preview: dict[str, Any] | None = None  # shown to the approver by <prefix>_review_change


class PreviewStore:
    def __init__(self, ttl: float = 600) -> None:
        self.ttl = ttl
        self._pending: dict[str, _Pending] = {}

    def _purge(self) -> None:
        now = time.monotonic()
        for token in [t for t, p in self._pending.items() if p.expires < now]:
            del self._pending[token]

    def issue(
        self,
        tool: str,
        args: dict[str, Any],
        fingerprint: Any,
        caller: str,
        *,
        two_person: bool = False,
        preview: dict[str, Any] | None = None,
    ) -> str:
        self._purge()
        token = "pv_" + secrets.token_urlsafe(12)
        self._pending[token] = _Pending(
            tool,
            _digest(args),
            _digest(fingerprint),
            caller,
            time.monotonic() + self.ttl,
            two_person=two_person,
            preview=preview,
        )
        return token

    def peek(self, token: str) -> _Pending | None:
        """The pending change behind a token, without consuming it (None if unknown or expired)."""
        self._purge()
        return self._pending.get(token)

    def remaining(self, pending: _Pending) -> int:
        return max(0, int(pending.expires - time.monotonic()))

    def redeem(self, token: str, tool: str, args: dict[str, Any], fingerprint: Any, caller: str) -> _Pending:
        """Consume a token or raise explaining why it can't be used. Any mismatch also burns the
        token, so a fresh preview is always required afterwards.

        A two-person token (MCP_TWO_PERSON_TOOLS) must be redeemed by a caller other than the one who
        previewed; the requester trying to confirm it is refused without burning the token."""
        self._purge()
        pending = self._pending.get(token)
        if pending is None:
            raise InvalidRequest(
                f"Unknown or expired confirm token (tokens last {int(self.ttl // 60)} minutes and work "
                "once). Call the tool again without confirm to get a fresh preview."
            )
        if pending.two_person and pending.caller == caller:
            raise InvalidRequest(
                "Confirm token refused: this change needs two-person approval (MCP_TWO_PERSON_TOOLS), so "
                f"a different user than {caller!r} must confirm it. Nothing was changed; the token is "
                "still valid for the approver."
            )
        problem = None
        if pending.caller != caller and not pending.two_person:
            problem = "it was issued to a different user"
        elif pending.tool != tool:
            problem = f"it was issued for {pending.tool}"
        elif pending.args != _digest(args):
            problem = "the arguments differ from the previewed call"
        elif pending.fingerprint != _digest(fingerprint):
            problem = "the objects involved changed since the preview"
        del self._pending[token]
        if problem:
            raise InvalidRequest(f"Confirm token refused: {problem}. Nothing was changed; preview again.")
        return pending


def get_previews(ctx: Context) -> PreviewStore:
    store: PreviewStore = ctx.lifespan_context["previews"]
    return store


def _policy_for(ctx: Context) -> SafetyPolicy | None:
    lc = ctx.lifespan_context
    policy = lc.get("policy")
    if policy is None and "settings" in lc:
        policy = lc["settings"].safety_policy()
    if policy is not None and not isinstance(policy, SafetyPolicy):
        raise TypeError("lifespan_context['policy'] must be a SafetyPolicy")
    return policy


def _tier_of(ctx: Context, tool: str) -> str | None:
    try:
        tiers: dict[str, Tier] = getattr(ctx.fastmcp, "_stutters_tiers", {})
    except Exception:  # noqa: BLE001 - no server attached (unit tests of the helper)
        return None
    tier = tiers.get(tool)
    return tier.value if tier is not None else None


async def preview_or_apply(
    ctx: Context,
    *,
    tool: str,
    args: dict[str, Any],
    confirm: str | None,
    plan: Plan,
    apply: Callable[[], Awaitable[Any]],
) -> dict[str, Any]:
    """Return a preview (issuing a token) when `confirm` is None; otherwise check the token against
    the freshly computed plan and apply. `args` must be every argument except `confirm`.

    The safety rails (stutters_mcp_core.policy) are applied here: dry-run and change-window refusals,
    blast-radius limits and two-person confirmation. With no rails configured the output is unchanged."""
    store = get_previews(ctx)
    caller = current_caller()
    target = target_name()
    token_tool = tool if target is None else f"{tool}@{target}"  # a token only works on its target
    policy = _policy_for(ctx)
    tier = _tier_of(ctx, tool)
    info: dict[str, Any] = {}
    blockers = list(plan.blockers)
    refusals: list[str] = []
    two_person = False
    if policy is not None and policy.active:
        info, policy_blockers, refusals = policy.for_write(tool, tier, plan.blast_radius)
        blockers += policy_blockers
        two_person = bool(info.get("two_person"))
    extra: dict[str, Any] = {}
    if target is not None:
        extra["instance"] = target
    if info:
        extra["policy"] = info
    elif plan.blast_radius is not None:
        extra["blast_radius"] = plan.blast_radius
    identity = caller_identity(ctx, caller)
    if identity is not None:
        extra["role"] = identity["role"]

    if confirm is None:
        out: dict[str, Any] = {"status": "preview", "action": plan.summary}
        if target is not None:
            out["instance"] = target
        if plan.details:
            out["preview"] = plan.details
        if plan.side_effects:
            out["side_effects"] = plan.side_effects
        if plan.blast_radius is not None:
            out["blast_radius"] = plan.blast_radius
        if info:
            out["policy"] = info
        if identity is not None:
            out["requested_by"] = identity
        if blockers:
            out["status"] = "blocked"
            out["blockers"] = blockers
            out["note"] = "This change can't be applied as it stands; nothing was changed."
            _emit(ctx, "blocked", tool, caller, plan.summary, blockers=blockers, **extra)
            return out
        if policy is not None and policy.dry_run:
            out["note"] = (
                "Dry-run mode (MCP_DRY_RUN=true): this preview is all the server will do. Nothing was "
                "changed and no confirm token is issued."
            )
            _emit(ctx, "preview", tool, caller, plan.summary, **extra)
            return out
        token = store.issue(token_tool, args, plan.fingerprint, caller, two_person=two_person)
        out["confirm"] = token
        out["expires_in_seconds"] = int(store.ttl)
        if two_person:
            out["how_to_apply"] = (
                "Nothing has been changed. This change needs TWO-PERSON approval: a different user must "
                f"review it (`{tool.split('_', 1)[0]}_review_change` with this token shows the preview) and "
                f'then call {tool} with exactly the same arguments plus confirm="{token}". '
                "The requester can't confirm it."
            )
        else:
            out["how_to_apply"] = (
                "Nothing has been changed. Show this preview to the user; if they approve, call "
                f'{tool} again with exactly the same arguments plus confirm="{token}".'
            )
        if refusals:
            out["warning"] = "The confirm will be refused while " + "; ".join(refusals) + "."
        pending = store.peek(token)
        if pending is not None and two_person:
            pending.preview = {
                k: v for k, v in out.items() if k not in ("confirm", "how_to_apply", "expires_in_seconds")
            }
            pending.preview = mask_secrets(pending.preview)
            pending.preview["args"] = _shown_args(args)
        _emit(ctx, "preview", tool, caller, plan.summary, **extra)
        return out

    if refusals:
        _emit(ctx, "refused", tool, caller, plan.summary, reasons=refusals, **extra)
        raise InvalidRequest(f"Can't apply: {'; '.join(refusals)}. Nothing was changed.")
    if blockers:
        _emit(ctx, "refused", tool, caller, plan.summary, reasons=blockers, **extra)
        raise InvalidRequest(f"Can't apply: {'; '.join(blockers)}. Nothing was changed.")
    pending = store.redeem(confirm, token_tool, args, plan.fingerprint, caller)
    if pending.two_person:
        extra["requested_by"] = pending.caller
        extra["approved_by"] = caller
    try:
        result = await apply()
    except asyncio.CancelledError:
        # e.g. tasks/cancel on a background task mid-apply: the change may be partly done upstream.
        _emit(ctx, "apply_cancelled", tool, caller, plan.summary, **extra)
        raise
    except Exception as e:
        _emit(ctx, "apply_failed", tool, caller, plan.summary, error=scrub_text(str(e))[:300], **extra)
        raise
    _emit(ctx, "applied", tool, caller, plan.summary, **extra)
    out = {"status": "applied", "action": plan.summary}
    if target is not None:
        out["instance"] = target
    if pending.two_person:
        out["requested_by"] = pending.caller
        out["approved_by"] = caller
    if result is not None:
        out["result"] = result
    return out


def _emit(ctx: Context, event: str, tool: str, caller: str, summary: str, **extra: Any) -> None:
    """One audit event for a write, plus the mcp_write_events_total metric."""
    from stutters_mcp_core.metrics import record_write

    audit.info(dumps_event(event, tool, caller, summary, **extra))
    try:
        server = str(ctx.fastmcp.name)
    except Exception:  # noqa: BLE001 - no server attached (unit tests of the helper)
        server = ""
    record_write(server, tool, event)


def _shown_args(args: dict[str, Any]) -> dict[str, Any]:
    from stutters_mcp_core.audit import redact_args

    return redact_args(args)


def caller_identity(ctx: Context | None, caller: str) -> dict[str, Any] | None:
    """{"caller": name, "role": role} when per-user roles are configured (G2), else None."""
    if ctx is None:
        return None
    roles = ctx.lifespan_context.get("roles") if hasattr(ctx, "lifespan_context") else None
    if roles is None:
        return None
    return {"caller": caller, "role": roles.role_for(caller).value}


def dumps_event(event: str, tool: str, caller: str, summary: str, **extra: Any) -> str:
    return json.dumps(
        {"event": event, "tool": tool, "caller": caller, "action": summary, **extra}, default=str
    )


# --- diff helpers -------------------------------------------------------------------------


def field_changes(current: dict[str, Any], desired: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{field: {"from": old, "to": new}} for each field in `desired` whose value differs.
    Nested dicts are compared recursively and reported with dotted keys."""
    changes: dict[str, dict[str, Any]] = {}

    def walk(cur: Any, new: Any, prefix: str) -> None:
        if isinstance(new, dict) and isinstance(cur, dict):
            for k, v in new.items():
                walk(cur.get(k), v, f"{prefix}{k}.")
            return
        if cur != new:
            changes[prefix.rstrip(".")] = {"from": cur, "to": new}

    walk(current, desired, "")
    return changes
