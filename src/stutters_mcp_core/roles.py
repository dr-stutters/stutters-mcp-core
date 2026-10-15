"""Per-user roles (roadmap G2): map caller identities to `ro` / `rw` / `admin`, gating tool tiers per caller.

Off unless MCP_ROLES or MCP_ROLES_FILE is set; then every caller gets a role:

    MCP_ROLES="alice:admin, bob:rw, ci-*:ro, github:octocat:rw"
    MCP_ROLES_FILE=/run/secrets/mcp_roles      # same format, one entry per line, '#' comments
    MCP_DEFAULT_ROLE=ro                        # callers no entry matches (default ro)

The identity is the bearer-token name (MCP_AUTH_TOKENS) or, with OAuth, `<provider>:<login>`. Entries
are exact names or shell globs; an exact name wins, then the first matching glob. The stdio caller
(`local`, the operator's own process) is `admin` unless an entry names it.

    ro     read tools only
    rw     read + write tools
    admin  read + write + dangerous tools

Roles narrow what the server enables; they never widen it (a `rw` caller still sees no write tools when
MCP_ENABLE_WRITES is off). Tools a role can't use are hidden from that caller's tool list and refused if
called; prompts follow their declared tier. Identity and role appear in previews (`requested_by`) and in
every audit event (`role`).
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import mcp.types as mt
from fastmcp.exceptions import PromptError, ToolError
from fastmcp.prompts import Prompt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool, ToolResult

from stutters_mcp_core.inbound_auth import current_caller

audit = logging.getLogger("stutters_mcp_core.audit")

LOCAL = "local"


class Role(StrEnum):
    RO = "ro"
    RW = "rw"
    ADMIN = "admin"


ROLE_TIERS: dict[Role, frozenset[str]] = {
    Role.RO: frozenset({"read"}),
    Role.RW: frozenset({"read", "write"}),
    Role.ADMIN: frozenset({"read", "write", "dangerous"}),
}

_PATTERN_RE = re.compile(r"^[A-Za-z0-9_.@:*?\[\]!-]+$")


def _role(value: str, where: str) -> Role:
    try:
        return Role(value.strip().lower())
    except ValueError:
        raise ValueError(f"{where}: role must be ro, rw or admin, not {value.strip()!r}") from None


@dataclass(frozen=True)
class RoleMap:
    entries: tuple[tuple[str, Role], ...]
    default: Role = Role.RO

    @classmethod
    def parse(cls, spec: str, default: str = "ro", *, file_spec: str | None = None) -> RoleMap:
        """`spec` is MCP_ROLES (comma or newline separated); `file_spec` the MCP_ROLES_FILE text."""
        entries: list[tuple[str, Role]] = []
        seen: set[str] = set()
        sources = [(spec, "MCP_ROLES entry #{n}", r"[,\n]")]
        if file_spec is not None:
            sources.append((file_spec, "MCP_ROLES_FILE line {n}", r"\n"))
        for text, label, sep_re in sources:
            for position, raw in enumerate(re.split(sep_re, text), start=1):
                where = label.format(n=position)
                entry = raw.split("#", 1)[0].strip()
                if not entry:
                    continue
                name, sep, role = entry.rpartition(":")
                name = name.strip()
                if not sep or not name:
                    raise ValueError(f"{where} must look like 'name:role' (role ro, rw or admin)")
                if not _PATTERN_RE.match(name):
                    raise ValueError(f"{where}: {name!r} is not a caller name or glob")
                if name in seen:
                    raise ValueError(f"{where}: {name!r} is listed twice")
                seen.add(name)
                entries.append((name, _role(role, where)))
        return cls(tuple(entries), _role(default, "MCP_DEFAULT_ROLE"))

    def role_for(self, caller: str) -> Role:
        """An exact entry; else `admin` for the stdio caller `local` (a name no token can have); else the
        most specific matching glob (most literal characters; on a tie, the lower role); else the default."""
        for name, role in self.entries:
            if name == caller:
                return role
        if caller == LOCAL:
            return Role.ADMIN
        matches = [(name, role) for name, role in self.entries if fnmatch.fnmatchcase(caller, name)]
        if not matches:
            return self.default
        order = list(Role)
        best = max(matches, key=lambda m: (len(re.sub(r"[*?\[\]!]", "", m[0])), -order.index(m[1])))
        return best[1]

    def allows(self, caller: str, tier: str | None) -> bool:
        # A tool registered outside ToolRegistrar has no known tier: only admin may call it.
        return (tier or "dangerous") in ROLE_TIERS[self.role_for(caller)]

    def describe(self, caller: str) -> dict[str, Any]:
        return {
            "caller": caller,
            "role": self.role_for(caller).value,
            "default_role": self.default.value,
            "entries": len(self.entries),
        }


class RoleMiddleware(Middleware):
    """Hides and refuses tools (and prompts) above the caller's role."""

    def __init__(self, roles: RoleMap, mcp: Any) -> None:
        self.roles = roles
        self.mcp = mcp

    def _tool_tier(self, name: str) -> str | None:
        override = getattr(self.mcp, "_stutters_role_tiers", {}).get(name)
        if override is not None:
            return str(override)  # e.g. <prefix>_review_change: only approvers may inspect pending changes
        tier = getattr(self.mcp, "_stutters_tiers", {}).get(name)
        return tier.value if tier is not None else None

    def _prompt_tier(self, name: str) -> str | None:
        meta = getattr(self.mcp, "_stutters_prompts", {}).get(name)
        # A prompt registered outside PromptRegistrar has no declared tier: admin only (fail closed).
        return meta.get("tier") if meta else "dangerous"

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        caller = current_caller()
        return [t for t in tools if self.roles.allows(caller, self._tool_tier(t.name))]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        caller = current_caller()
        name = context.message.name
        tier = self._tool_tier(name)
        if not self.roles.allows(caller, tier):
            role = self.roles.role_for(caller)
            audit.info(
                json.dumps(
                    {"event": "denied", "tool": name, "caller": caller, "role": role.value, "tier": tier}
                )
            )
            raise ToolError(
                f"{name} is a {tier or 'unclassified'} tool and caller {caller!r} has role {role.value!r} "
                f"(allowed: {', '.join(sorted(ROLE_TIERS[role]))}). Ask an administrator for a higher role."
            )
        return await call_next(context)

    async def on_list_prompts(
        self,
        context: MiddlewareContext[mt.ListPromptsRequest],
        call_next: CallNext[mt.ListPromptsRequest, Sequence[Prompt]],
    ) -> Sequence[Prompt]:
        prompts = await call_next(context)
        caller = current_caller()
        return [p for p in prompts if self.roles.allows(caller, self._prompt_tier(p.name))]

    async def on_get_prompt(
        self,
        context: MiddlewareContext[mt.GetPromptRequestParams],
        call_next: CallNext[mt.GetPromptRequestParams, Any],
    ) -> Any:
        caller = current_caller()
        tier = self._prompt_tier(context.message.name) or "dangerous"
        if not self.roles.allows(caller, tier):
            role = self.roles.role_for(caller)
            raise PromptError(
                f"Prompt {context.message.name} needs {tier} tools; "
                f"caller {caller!r} has role {role.value!r}."
            )
        return await call_next(context)
