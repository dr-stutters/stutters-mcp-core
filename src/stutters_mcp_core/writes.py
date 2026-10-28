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
import hmac
import inspect
import json
import logging
import os
import re
import secrets
import time
import typing
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, TypeVar

from fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from stutters_mcp_core.errors import InvalidRequest
from stutters_mcp_core.inbound_auth import current_caller
from stutters_mcp_core.policy import InstanceRules, SafetyPolicy
from stutters_mcp_core.secrets import scrub_text
from stutters_mcp_core.targets import target_name
from stutters_mcp_core.toolsets import ToolsetGate, normalise, tags_for

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from stutters_mcp_core.audit import ArgRedactor
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


def _is_secret_annotation(ann: Any) -> bool:
    """SecretStr / SecretBytes, also inside Optional, a union or Annotated (or as a string annotation)."""
    from pydantic import SecretBytes, SecretStr

    if ann is SecretStr or ann is SecretBytes:
        return True
    if isinstance(ann, str):
        return bool(re.search(r"\bSecret(?:Str|Bytes)\b", ann))
    return any(_is_secret_annotation(a) for a in typing.get_args(ann))


def secret_parameters(fn: Callable[..., Any]) -> set[str]:
    """The parameters of `fn` annotated SecretStr (or SecretBytes, Optional or Annotated forms)."""
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except Exception:  # noqa: BLE001 - unresolvable forward references: fall back to the raw annotations
        hints = {}
    out: set[str] = set()
    for pname, param in inspect.signature(fn).parameters.items():
        if _is_secret_annotation(hints.get(pname, param.annotation)):
            out.add(pname)
    return out


def _register_redactors(tool: str, fn: Callable[..., Any], redact: Mapping[str, ArgRedactor] | None) -> None:
    from stutters_mcp_core.audit import register_arg_redactors

    params = inspect.signature(fn).parameters
    unknown = sorted(set(redact or {}) - set(params))
    if unknown:
        raise ValueError(f"{tool}: redact= names arguments the tool doesn't take: {', '.join(unknown)}")
    redactors: dict[str, ArgRedactor] = {p: True for p in secret_parameters(fn)}
    redactors.update(redact or {})
    register_arg_redactors(tool, redactors)


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
        self.core_tools = {  # never withheld
            f"{prefix}_status",
            f"{prefix}_review_change",
            f"{prefix}_list_pending_changes",
            f"{prefix}_cancel_pending_change",
        }
        self.target_names: list[str] = settings.target_names()  # >1: tools get an `instance` argument
        task_tools: set[str] = getattr(mcp, "_stutters_task_tools", set())
        mcp._stutters_task_tools = task_tools  # type: ignore[attr-defined]
        self.task_tools = task_tools  # tools registered as MCP background tasks (MCP_TASKS)
        # Every instance's settings (build_server sets them), so a fan-out leg's error masks that instance's
        # secrets too.
        self.instance_settings: list[BaseServerSettings] = [settings]

    def fanout_scrub(self, message: str) -> str:
        """Secrets of every instance and credential shapes masked in a fan-out leg's error (C74)."""
        from stutters_mcp_core.secrets import mask_values, scrub_error, secret_values

        values = {v for cfg in self.instance_settings for v in secret_values(cfg)}
        return scrub_error(mask_values(message, values), self.settings)

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
        redact: Mapping[str, ArgRedactor] | None = None,
        toolset: str | Iterable[str] | None = None,
    ) -> Callable[[F], F]:
        sets = normalise(toolset, name or "tool")
        self.toolsets.declare(sets)  # known even when the tier is off, so <P>_TOOLSETS can name it
        if sets:
            tags = {*(tags or set()), *tags_for(sets)}

        def deco(fn: F) -> F:
            tool_name = name or fn.__name__
            if not tool_name.startswith(self.prefix + "_"):
                raise ValueError(f"Tool {tool_name!r} must start with {self.prefix}_")
            _register_redactors(tool_name, fn, redact)  # checked even when the tier is off
            if not tier_enabled(self.settings, tier):
                return fn
            if not self.policy.registers(tool_name, tier.value, always=tool_name in self.core_tools):
                self.withheld[tool_name] = tier
                return fn
            if not self.toolsets.registers(tool_name, sets, always=tool_name in self.core_tools):
                return fn  # C31: not in the toolsets <P>_TOOLSETS loads
            if task and not inspect.iscoroutinefunction(fn):
                raise ValueError(
                    f"{tool_name}: task=True needs an async function (background tasks run async)"
                )
            registered: Callable[..., Any] = fn
            multi = len(self.target_names) > 1
            fanout = multi and tier is Tier.READ  # C74: reads take instance='*' / 'a,b'
            if multi:
                from stutters_mcp_core.targets import with_target_argument

                registered = with_target_argument(
                    fn,
                    self.target_names,
                    self.target_names[0],
                    fanout=fanout,
                    concurrency=getattr(self.settings, "mcp_fanout_concurrency", 4),
                    scrub=self.fanout_scrub,
                )
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
            if fanout:
                # Clients check structured content against the output schema: widen it for the fan-out shape
                from fastmcp.tools import FunctionTool

                from stutters_mcp_core.targets import fanout_output_schema

                own = FunctionTool.from_function(registered, name=tool_name).output_schema
                if own is not None:
                    extra["output_schema"] = fanout_output_schema(own)
            self.mcp.tool(
                registered,
                name=tool_name,
                description=description,
                annotations=annotations_for(
                    tier, title=title, destructive=destructive, idempotent=idempotent
                ),
                tags={tier.value, *(["fanout"] if fanout else []), *(tags or set())},
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
        redact: Mapping[str, ArgRedactor] | None = None,
        toolset: str | Iterable[str] | None = None,
    ) -> Callable[[F], F]:
        """`redact`: arguments to mask in the audit log and previews, beyond secret-named ones and
        SecretStr-annotated parameters (always masked): {"reg_key": True} masks the value,
        {"lines": config_mask.mask_config_value} logs it through a function. `toolset`: the area(s) the
        tool belongs to, for <P>_TOOLSETS (stutters_mcp_core.toolsets)."""
        return self._register(
            Tier.READ,
            title=title,
            name=name,
            destructive=False,
            idempotent=idempotent,
            tags=tags,
            description=description,
            task=task,
            redact=redact,
            toolset=toolset,
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
        redact: Mapping[str, ArgRedactor] | None = None,
        toolset: str | Iterable[str] | None = None,
    ) -> Callable[[F], F]:
        """`redact`: arguments to mask in the audit log and previews, beyond secret-named ones and
        SecretStr-annotated parameters (always masked): {"reg_key": True} masks the value,
        {"lines": config_mask.mask_config_value} logs it through a function. `toolset`: the area(s) the
        tool belongs to, for <P>_TOOLSETS (stutters_mcp_core.toolsets)."""
        return self._register(
            Tier.WRITE,
            title=title,
            name=name,
            destructive=destructive,
            idempotent=idempotent,
            tags=tags,
            description=description,
            task=task,
            redact=redact,
            toolset=toolset,
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
        redact: Mapping[str, ArgRedactor] | None = None,
        toolset: str | Iterable[str] | None = None,
    ) -> Callable[[F], F]:
        """`redact`: arguments to mask in the audit log and previews, beyond secret-named ones and
        SecretStr-annotated parameters (always masked): {"reg_key": True} masks the value,
        {"lines": config_mask.mask_config_value} logs it through a function. `toolset`: the area(s) the
        tool belongs to, for <P>_TOOLSETS (stutters_mcp_core.toolsets)."""
        return self._register(
            Tier.DANGEROUS,
            title=title,
            name=name,
            destructive=True,
            idempotent=idempotent,
            tags=tags,
            description=description,
            task=task,
            redact=redact,
            toolset=toolset,
        )

    @property
    def toolsets(self) -> ToolsetGate:
        """The server's toolset selection and what it loaded (C31), shared by every registrar."""
        return ToolsetGate.for_server(self.mcp, self.settings)


# --- preview / confirm --------------------------------------------------------------------


# Per-process key for the digests that bind a confirm token to its arguments and fingerprint. Keyed so a
# low-entropy PSK or password in a write's arguments can't be brute-forced from a digest; tokens live in
# memory only, so a key that changes on restart costs nothing.
_DIGEST_KEY = os.urandom(32)


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, default=str).encode()
    return hmac.new(_DIGEST_KEY, data, hashlib.sha256).hexdigest()


@dataclass
class Plan:
    """What a write would do: a one-line summary, details for the user, anything that would make it
    fail, and a fingerprint of the current state it depends on (re-checked at confirm time).

    `blast_radius`: how many things the change touches (devices deployed to, endpoints affected, rules
    rewritten...), when the tool can count them. Shown in the preview and checked against
    MCP_MAX_BLAST_RADIUS / MCP_BLAST_RADIUS_OVERRIDES; None means "not counted" and is never limited.

    `blast_unknown`: why the tool can't count it (C29), e.g. "a raw API call: the server can't tell what
    it touches". The preview states 'blast_unknown: not counted (<reason>)' so an operator who set
    MCP_MAX_BLAST_RADIUS sees the limit doesn't apply here. Every dangerous Plan sets one of the two
    (`stutters_mcp_core.testing.blast_radius.assert_dangerous_plans_counted` checks); never both.

    `two_person`: this change always needs a second caller to confirm, whatever MCP_TWO_PERSON_TOOLS
    says (runtime rail overrides, C70).

    `noop` (C71): the target is already in the requested state (the tool compared the current state it
    read for the fingerprint with what was asked for). `preview_or_apply` then returns
    `{"status": "no_change", ...}` with no confirm token, never calls `apply`, writes a `noop` audit event
    and skips the rails (blast-radius limits, two-person, change window, dry-run, elicitation): there is
    nothing to confirm. `noop_reason` says why, e.g. "NTP servers already 192.0.2.1, 192.0.2.2".
    `Plan.blockers` still win over `noop` (a request that can't be valid isn't "already applied")."""

    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    fingerprint: Any = None
    blockers: list[str] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    blast_radius: int | None = None
    two_person: bool = False
    blast_unknown: str | None = None
    noop: bool = False
    noop_reason: str | None = None

    def __post_init__(self) -> None:
        if self.noop_reason is not None and not self.noop:
            raise ValueError("Plan: noop_reason is only meaningful with noop=True")
        if self.blast_unknown is not None:
            if self.blast_radius is not None:
                raise ValueError("Plan: set blast_radius or blast_unknown, not both")
            if not self.blast_unknown.strip():
                raise ValueError("Plan: blast_unknown needs a reason (why the blast radius can't be counted)")


# Test hook (C29): stutters_mcp_core.testing.blast_radius pushes a list here and preview_or_apply appends
# (tool, plan) to it. A module list rather than a ContextVar so in-memory clients' server tasks see it.
_plan_recorders: list[list[tuple[str, Plan]]] = []


def _blast_unknown_text(plan: Plan, info: dict[str, Any]) -> str | None:
    """'not counted (<reason>)', noting a configured blast-radius limit that can't apply."""
    if plan.blast_unknown is None:
        return None
    text = f"not counted ({plan.blast_unknown.strip()})"
    limited = (
        info.get("max_blast_radius") is not None
        or (info.get("instance_rules") or {}).get("max_blast_radius") is not None
    )
    if limited:
        text += "; the blast-radius limit (MCP_MAX_BLAST_RADIUS) can't check this change"
    return text


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

    def preview_id(self, token: str) -> str:
        """A stable public id for a pending change (C66): lets callers list and cancel it without the
        listing handing out confirm tokens. Keyed digest of the token, so it can't be reversed."""
        return "pc_" + hmac.new(_DIGEST_KEY, token.encode(), hashlib.sha256).hexdigest()[:12]

    def awaiting_approval(self) -> list[tuple[str, _Pending]]:
        """Unexpired two-person changes waiting for a second person, soonest to expire first."""
        self._purge()
        waiting = [(t, p) for t, p in self._pending.items() if p.two_person]
        return sorted(waiting, key=lambda tp: tp[1].expires)

    def find(self, ref: str) -> tuple[str, _Pending] | None:
        """(token, pending) for a confirm token or a preview id, without consuming it."""
        self._purge()
        if ref in self._pending:
            return ref, self._pending[ref]
        for token, pending in self._pending.items():
            if hmac.compare_digest(self.preview_id(token), ref):
                return token, pending
        return None

    def cancel(self, token: str) -> _Pending | None:
        """Drop a pending change so its token can no longer be confirmed."""
        return self._pending.pop(token, None)

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


def _rails(
    ctx: Context, tool: str, tier: str | None, plan: Plan, target: str | None
) -> tuple[SafetyPolicy | None, dict[str, Any], list[str], list[str], bool]:
    """(policy, info, blockers, refusals, two_person) from the safety rails and the instance rules."""
    policy = _policy_for(ctx)
    info: dict[str, Any] = {}
    blockers = list(plan.blockers)
    refusals: list[str] = []
    two_person = plan.two_person
    if policy is not None and policy.active:
        info, policy_blockers, refusals = policy.for_write(tool, tier, plan.blast_radius)
        blockers += policy_blockers
        two_person = two_person or bool(info.get("two_person"))
    instance_settings = ctx.lifespan_context.get("settings")
    if instance_settings is not None:
        rules = InstanceRules.from_settings(instance_settings)
        if rules.active:
            label = f"instance {target!r}" if target is not None else "this appliance"
            rule_info, rule_blockers, rule_refusals = rules.for_write(label, plan.blast_radius)
            info = {**info, "instance_rules": rule_info}
            blockers += rule_blockers
            refusals += rule_refusals
            two_person = two_person or rules.two_person
    return policy, info, blockers, refusals, two_person


async def preview_or_apply(
    ctx: Context,
    *,
    tool: str,
    args: dict[str, Any],
    confirm: str | None,
    plan: Plan,
    apply: Callable[[], Awaitable[Any]],
    replan: Callable[[], Awaitable[Plan]] | None = None,
) -> dict[str, Any]:
    """Return a preview (issuing a token) when `confirm` is None; otherwise check the token against
    the freshly computed plan and apply. `args` must be every argument except `confirm`.

    The safety rails (stutters_mcp_core.policy) are applied here: dry-run and change-window refusals,
    blast-radius limits and two-person confirmation. With no rails configured the output is unchanged.

    With MCP_CONFIRM_MODE=auto|elicit and a client that supports elicitation (stutters_mcp_core.confirm),
    the preview call asks the user to confirm in the client and applies on accept. `replan`, when given,
    recomputes the plan after the user accepts so a target that changed while the dialog was open is
    refused like a token whose fingerprint no longer matches."""
    from stutters_mcp_core.confirm import (
        ask_user,
        confirm_mode,
        elicit_channel,
        elicit_message,
        input_answer,
        request_input,
    )

    for recorder in _plan_recorders:
        recorder.append((tool, plan))
    store = get_previews(ctx)
    caller = current_caller()
    target = target_name()
    if plan.noop and not plan.blockers:
        return _no_change(ctx, store, tool, caller, target, plan, confirm)
    token_tool = tool if target is None else f"{tool}@{target}"  # a token only works on its target
    tier = _tier_of(ctx, tool)
    policy, info, blockers, refusals, two_person = _rails(ctx, tool, tier, plan, target)
    mode = confirm_mode(ctx)
    extra: dict[str, Any] = {}
    if target is not None:
        extra["instance"] = target
    if info:
        extra["policy"] = info
    elif plan.blast_radius is not None:
        extra["blast_radius"] = plan.blast_radius
    if plan.blast_unknown is not None:
        extra["blast_unknown"] = plan.blast_unknown
    unknown_text = _blast_unknown_text(plan, info)
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
        if unknown_text is not None:
            out["blast_unknown"] = unknown_text
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
        if mode != "token" and not two_person:
            if refusals:
                if mode == "elicit":
                    out["warning"] = "This change can't be confirmed while " + "; ".join(refusals) + "."
                    out["note"] = "Nothing was changed. Preview again when the change can be confirmed."
                    _emit(ctx, "preview", tool, caller, plan.summary, confirm_mode=mode, **extra)
                    return out
                channel = None  # auto: the token flow below explains the refusal
            else:
                channel = elicit_channel(ctx)
            if channel == "input":
                answered = input_answer(ctx)
                if answered is not None:  # the client's retry carrying the user's answer
                    answer, held = answered
                    if answer == "accept":
                        store.redeem(held, token_tool, args, plan.fingerprint, caller)  # fresh plan
                        return await _apply_elicited(
                            ctx, tool, caller, target, plan, apply, None, tier, extra
                        )
                    store.cancel(held)
                    return _declined(ctx, out, tool, caller, plan, answer, store.ttl, extra)
                held = store.issue(token_tool, args, plan.fingerprint, caller)
                if request_input(elicit_message(tool, out, _shown_args(args, tool)), held):
                    _emit(ctx, "preview", tool, caller, plan.summary, confirm_mode="elicit", **extra)
                    out["note"] = "Waiting for the user's answer in the client's confirmation dialog."
                    return out  # replaced by the InputRequiredResult (ConfirmInputMiddleware)
                store.cancel(held)
                channel = None
            if channel == "push":
                _emit(ctx, "preview", tool, caller, plan.summary, confirm_mode="elicit", **extra)
                answer = await ask_user(
                    ctx, elicit_message(tool, out, _shown_args(args, tool)), wait_seconds=float(store.ttl)
                )
                if answer == "accept":
                    return await _apply_elicited(ctx, tool, caller, target, plan, apply, replan, tier, extra)
                if answer != "error" or mode == "elicit":
                    return _declined(ctx, out, tool, caller, plan, answer, store.ttl, extra)
                # auto mode and the dialog failed: fall back to a token below.
            elif channel is None and mode == "elicit":
                out["status"] = "blocked"
                out["blockers"] = [
                    "MCP_CONFIRM_MODE=elicit: changes are confirmed by the user in the client's "
                    "confirmation dialog (MCP elicitation), and this client doesn't support it"
                ]
                out["note"] = (
                    "Nothing was changed. Use a client that supports elicitation, or ask the operator "
                    "for MCP_CONFIRM_MODE=auto (tokens for clients without it)."
                )
                _emit(ctx, "blocked", tool, caller, plan.summary, blockers=out["blockers"], **extra)
                return out
        token = store.issue(token_tool, args, plan.fingerprint, caller, two_person=two_person)
        out["confirm"] = token
        out["expires_in_seconds"] = int(store.ttl)
        if two_person:
            out["preview_id"] = store.preview_id(token)
            out["how_to_apply"] = (
                "Nothing has been changed. This change needs TWO-PERSON approval: a different user must "
                f"review it (`{tool.split('_', 1)[0]}_review_change` with this token or the preview id "
                "shows the preview) and "
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
                k: v
                for k, v in out.items()
                if k not in ("confirm", "how_to_apply", "expires_in_seconds", "preview_id")
            }
            from stutters_mcp_core.audit import redact_value

            pending.preview = redact_value(pending.preview)  # config secrets in details too (C28)
            pending.preview["args"] = _shown_args(args, tool)
        _emit(ctx, "preview", tool, caller, plan.summary, **extra)
        return out

    if mode == "elicit":
        token_owner = store.peek(confirm)
        if token_owner is None or not token_owner.two_person:
            _emit(ctx, "refused", tool, caller, plan.summary, reasons=["confirm_mode=elicit"], **extra)
            raise InvalidRequest(
                "Confirm tokens are not accepted here (MCP_CONFIRM_MODE=elicit): call the tool without "
                "confirm and the user confirms the change in the client's dialog. Nothing was changed."
            )
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
    result = await _apply_change(ctx, tool, caller, plan, apply, extra)
    out = {"status": "applied", "action": plan.summary}
    if target is not None:
        out["instance"] = target
    if pending.two_person:
        out["requested_by"] = pending.caller
        out["approved_by"] = caller
    if result is not None:
        out["result"] = result
    return out


NO_CHANGE_NOTE = (
    "The target is already in the requested state: nothing was changed and no confirm token is needed."
)


def _no_change(
    ctx: Context,
    store: PreviewStore,
    tool: str,
    caller: str,
    target: str | None,
    plan: Plan,
    confirm: str | None,
) -> dict[str, Any]:
    """C71: the answer for a Plan(noop=True): no token, no apply, no rails (nothing to limit or approve).
    A confirm for an earlier preview of the same change is dropped: the target got there anyway."""
    extra: dict[str, Any] = {}
    if target is not None:
        extra["instance"] = target
    if plan.noop_reason:
        extra["reason"] = plan.noop_reason
    dropped = False
    if confirm is not None:
        # Only a token issued for this change (same tool and target, this caller or a two-person
        # approver) is dropped: a token for another tool or user is left alone.
        token_tool = tool if target is None else f"{tool}@{target}"
        pending = store.peek(confirm)
        if (
            pending is not None
            and pending.tool == token_tool
            and (pending.caller == caller or pending.two_person)
        ):
            store.cancel(confirm)
            dropped = True
            extra["confirm_dropped"] = True
    out: dict[str, Any] = {
        "status": "no_change",
        "action": plan.summary,
        "summary": plan.noop_reason or "Already in the requested state.",
    }
    if target is not None:
        out["instance"] = target
    if plan.details:
        out["details"] = plan.details
    out["note"] = NO_CHANGE_NOTE
    if dropped:
        out["note"] += " The confirm token given was dropped."
    _emit(ctx, "noop", tool, caller, plan.summary, **extra)
    return out


def _declined(
    ctx: Context,
    out: dict[str, Any],
    tool: str,
    caller: str,
    plan: Plan,
    answer: str,
    ttl: float,
    extra: dict[str, Any],
) -> dict[str, Any]:
    _emit(ctx, "declined", tool, caller, plan.summary, answer=answer, **extra)
    out["status"] = "blocked" if answer == "error" else "declined"
    out["note"] = {
        "decline": "The user declined the change in the confirmation dialog.",
        "cancel": "The user dismissed the confirmation dialog.",
        "timeout": f"Nobody answered the confirmation dialog within {int(ttl)} s.",
        "error": "The client's confirmation dialog failed (MCP_CONFIRM_MODE=elicit).",
    }[answer] + " Nothing was changed; call the tool again to preview it afresh."
    return out


async def _apply_elicited(
    ctx: Context,
    tool: str,
    caller: str,
    target: str | None,
    plan: Plan,
    apply: Callable[[], Awaitable[Any]],
    replan: Callable[[], Awaitable[Plan]] | None,
    tier: str | None,
    extra: dict[str, Any],
) -> dict[str, Any]:
    """Apply after the user accepted in the elicitation dialog, re-checking what may have changed while
    the dialog was open: the rails (a change window can close) and, with `replan`, the target state."""
    if replan is not None:
        fresh = await replan()
        if fresh.blockers or _digest(fresh.fingerprint) != _digest(plan.fingerprint):
            reasons = fresh.blockers or ["the objects involved changed while the dialog was open"]
            _emit(ctx, "refused", tool, caller, plan.summary, reasons=reasons, **extra)
            raise InvalidRequest(f"Can't apply: {'; '.join(reasons)}. Nothing was changed; preview again.")
        plan = fresh
    _, _, blockers, refusals, _ = _rails(ctx, tool, tier, plan, target)
    if refusals or blockers:
        reasons = refusals + blockers
        _emit(ctx, "refused", tool, caller, plan.summary, reasons=reasons, **extra)
        raise InvalidRequest(f"Can't apply: {'; '.join(reasons)}. Nothing was changed.")
    result = await _apply_change(ctx, tool, caller, plan, apply, {**extra, "confirmed_via": "elicit"})
    out: dict[str, Any] = {"status": "applied", "action": plan.summary}
    if target is not None:
        out["instance"] = target
    out["confirmed_via"] = "elicit"
    if result is not None:
        out["result"] = result
    return out


async def _apply_change(
    ctx: Context,
    tool: str,
    caller: str,
    plan: Plan,
    apply: Callable[[], Awaitable[Any]],
    extra: dict[str, Any],
) -> Any:
    """Run `apply` with the audit events around it and return its result."""
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
    return result


def _emit(ctx: Context, event: str, tool: str, caller: str, summary: str, **extra: Any) -> None:
    """One audit event for a write, plus the mcp_write_events_total metric."""
    from stutters_mcp_core.metrics import record_write

    audit.info(dumps_event(event, tool, caller, summary, **extra))
    try:
        server = str(ctx.fastmcp.name)
    except Exception:  # noqa: BLE001 - no server attached (unit tests of the helper)
        server = ""
    record_write(server, tool, event)


def _shown_args(args: dict[str, Any], tool: str | None = None) -> dict[str, Any]:
    from stutters_mcp_core.audit import redact_args

    return redact_args(args, tool=tool)


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


def field_changes(
    current: dict[str, Any], desired: dict[str, Any], *, replace: bool = False
) -> dict[str, dict[str, Any]]:
    """{field: {"from": old, "to": new}} for each field in `desired` whose value differs.
    Nested dicts are compared recursively and reported with dotted keys.

    With `replace=True` (the update sends the whole object, e.g. PUT), keys present in `current` but
    missing from `desired` are reported too, as {"from": old, "to": None, "removed": True}, so a
    preview never hides what the change deletes."""
    changes: dict[str, dict[str, Any]] = {}

    def walk(cur: Any, new: Any, prefix: str) -> None:
        if isinstance(new, dict) and isinstance(cur, dict):
            for k, v in new.items():
                walk(cur.get(k), v, f"{prefix}{k}.")
            if replace:
                for k in cur.keys() - new.keys():
                    if cur[k] not in (None, "", [], {}):
                        changes[f"{prefix}{k}"] = {"from": cur[k], "to": None, "removed": True}
            return
        if cur != new:
            changes[prefix.rstrip(".")] = {"from": cur, "to": new}

    walk(current, desired, "")
    return changes
