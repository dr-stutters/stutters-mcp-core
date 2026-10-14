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

import hashlib
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, TypeVar

from fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from stutters_mcp_core.errors import InvalidRequest
from stutters_mcp_core.inbound_auth import current_caller

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
    ) -> Callable[[F], F]:
        def deco(fn: F) -> F:
            tool_name = name or fn.__name__
            if not tool_name.startswith(self.prefix + "_"):
                raise ValueError(f"Tool {tool_name!r} must start with {self.prefix}_")
            if not tier_enabled(self.settings, tier):
                return fn
            self.mcp.tool(
                fn,
                name=tool_name,
                description=description,
                annotations=annotations_for(
                    tier, title=title, destructive=destructive, idempotent=idempotent
                ),
                tags={tier.value, *(tags or set())},
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
    ) -> Callable[[F], F]:
        return self._register(
            Tier.READ,
            title=title,
            name=name,
            destructive=False,
            idempotent=idempotent,
            tags=tags,
            description=description,
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
    ) -> Callable[[F], F]:
        return self._register(
            Tier.WRITE,
            title=title,
            name=name,
            destructive=destructive,
            idempotent=idempotent,
            tags=tags,
            description=description,
        )

    def dangerous(
        self,
        *,
        title: str | None = None,
        name: str | None = None,
        idempotent: bool | None = None,
        tags: set[str] | None = None,
        description: str | None = None,
    ) -> Callable[[F], F]:
        return self._register(
            Tier.DANGEROUS,
            title=title,
            name=name,
            destructive=True,
            idempotent=idempotent,
            tags=tags,
            description=description,
        )


# --- preview / confirm --------------------------------------------------------------------


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class Plan:
    """What a write would do: a one-line summary, details for the user, anything that would make it
    fail, and a fingerprint of the current state it depends on (re-checked at confirm time)."""

    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    fingerprint: Any = None
    blockers: list[str] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)


@dataclass
class _Pending:
    tool: str
    args: str
    fingerprint: str
    caller: str
    expires: float


class PreviewStore:
    def __init__(self, ttl: float = 600) -> None:
        self.ttl = ttl
        self._pending: dict[str, _Pending] = {}

    def _purge(self) -> None:
        now = time.monotonic()
        for token in [t for t, p in self._pending.items() if p.expires < now]:
            del self._pending[token]

    def issue(self, tool: str, args: dict[str, Any], fingerprint: Any, caller: str) -> str:
        self._purge()
        token = "pv_" + secrets.token_urlsafe(12)
        self._pending[token] = _Pending(
            tool, _digest(args), _digest(fingerprint), caller, time.monotonic() + self.ttl
        )
        return token

    def redeem(self, token: str, tool: str, args: dict[str, Any], fingerprint: Any, caller: str) -> None:
        """Consume a token or raise explaining why it can't be used. Any mismatch also burns the
        token, so a fresh preview is always required afterwards."""
        self._purge()
        pending = self._pending.get(token)
        if pending is None:
            raise InvalidRequest(
                f"Unknown or expired confirm token (tokens last {int(self.ttl // 60)} minutes and work "
                "once). Call the tool again without confirm to get a fresh preview."
            )
        problem = None
        if pending.caller != caller:
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


def get_previews(ctx: Context) -> PreviewStore:
    store: PreviewStore = ctx.lifespan_context["previews"]
    return store


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
    the freshly computed plan and apply. `args` must be every argument except `confirm`."""
    store = get_previews(ctx)
    caller = current_caller()
    if confirm is None:
        out: dict[str, Any] = {"status": "preview", "action": plan.summary}
        if plan.details:
            out["preview"] = plan.details
        if plan.side_effects:
            out["side_effects"] = plan.side_effects
        if plan.blockers:
            out["status"] = "blocked"
            out["blockers"] = plan.blockers
            out["note"] = "This change can't be applied as it stands; nothing was changed."
            return out
        token = store.issue(tool, args, plan.fingerprint, caller)
        out["confirm"] = token
        out["expires_in_seconds"] = int(store.ttl)
        out["how_to_apply"] = (
            "Nothing has been changed. Show this preview to the user; if they approve, call "
            f'{tool} again with exactly the same arguments plus confirm="{token}".'
        )
        audit.info(dumps_event("preview", tool, caller, plan.summary))
        return out
    if plan.blockers:
        raise InvalidRequest(f"Can't apply: {'; '.join(plan.blockers)}. Nothing was changed.")
    store.redeem(confirm, tool, args, plan.fingerprint, caller)
    try:
        result = await apply()
    except Exception as e:
        audit.info(dumps_event("apply_failed", tool, caller, plan.summary, error=str(e)[:300]))
        raise
    audit.info(dumps_event("applied", tool, caller, plan.summary))
    out = {"status": "applied", "action": plan.summary}
    if result is not None:
        out["result"] = result
    return out


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
