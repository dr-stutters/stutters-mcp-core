"""Toolsets (C31): load only some areas of a server's tool surface, e.g. FMC_TOOLSETS=troubleshoot,objects.

A server tags each tool with the area(s) it belongs to when it registers it:

    @reg.read(name="fmc_packet_tracer", toolset="troubleshoot", ...)
    @reg.write(name="fmc_create_object", toolset=("objects", "policy"), ...)

The operator then picks areas with `<P>_TOOLSETS` (the product's env prefix: FMC_TOOLSETS, ISE_TOOLSETS...),
comma-separated:

    unset / "" / "all"          every tool (the default; behaviour is exactly as before)
    "troubleshoot,objects"      only tools in those toolsets
    "-dangerous_ops"            every tool except those in `dangerous_ops`
    "all,-policy"               the same form, spelled out
    "troubleshoot,-policy"      tools in `troubleshoot` that are not also in `policy` (an exclusion wins)

Tools with no toolset (including `<prefix>_status`, the spec tools and the two-person tools) are always
registered, as are the server's core tools. A name no tool declares is logged as a warning at startup and
listed under `server.toolsets.unknown` by `<prefix>_status` (a typo would otherwise silently hide tools).
Each tool carries a FastMCP tag `toolset:<name>` next to its tier tag. Toolsets only choose what is
registered; the tier switches (MCP_ENABLE_WRITES...) and MCP_DENY_TOOLS / MCP_ALLOW_TOOLS still apply.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

TAG_PREFIX = "toolset:"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
ALL = "all"


@dataclass(frozen=True)
class ToolsetSelection:
    """A parsed `<P>_TOOLSETS` value. `include is None` means every toolset."""

    include: frozenset[str] | None = None
    exclude: frozenset[str] = frozenset()
    text: str = ""

    @property
    def active(self) -> bool:
        return self.include is not None or bool(self.exclude)

    @classmethod
    def parse(cls, text: str | None, *, var: str = "TOOLSETS") -> ToolsetSelection:
        raw = (text or "").strip()
        if not raw:
            return cls()
        include: set[str] = set()
        exclude: set[str] = set()
        everything = False
        for item in (p.strip() for p in raw.split(",")):
            if not item:
                continue
            negated = item.startswith(("-", "!"))
            name = item[1:].strip().lower() if negated else item.lower()
            if not NAME_RE.match(name):
                raise ValueError(
                    f"{var}: {item!r} is not a toolset name (lowercase letters, digits, '_' or '-'; "
                    "'-name' excludes one)"
                )
            if name == ALL:
                if negated:
                    raise ValueError(f"{var}: '-all' would hide every toolset; unset the variable instead")
                everything = True
            elif negated:
                exclude.add(name)
            else:
                include.add(name)
        overlap = include & exclude
        if overlap:
            raise ValueError(f"{var}: {', '.join(sorted(overlap))} is both included and excluded")
        only = None if everything or not include else frozenset(include)
        return cls(include=only, exclude=frozenset(exclude), text=raw)

    def admits(self, toolsets: Iterable[str]) -> bool:
        """Whether a tool in these toolsets is registered (a tool in none always is)."""
        sets = set(toolsets)
        if not sets:
            return True
        if sets & self.exclude:
            return False
        return self.include is None or bool(sets & self.include)


def normalise(toolset: str | Iterable[str] | None, tool: str) -> tuple[str, ...]:
    """The toolset names a tool declares, checked (raises ValueError naming the tool)."""
    if toolset is None:
        return ()
    names = (toolset,) if isinstance(toolset, str) else tuple(toolset)
    out: list[str] = []
    for n in names:
        if not isinstance(n, str) or not NAME_RE.match(n) or n == ALL:
            raise ValueError(
                f"{tool}: toolset {n!r} must be a lowercase name (letters, digits, '_' or '-'), not 'all'"
            )
        if n not in out:
            out.append(n)
    return tuple(out)


def tags_for(toolsets: Iterable[str]) -> set[str]:
    return {TAG_PREFIX + n for n in toolsets}


def toolsets_of(tags: Iterable[str] | None) -> list[str]:
    """The toolset names in a tool's FastMCP tags."""
    return sorted(t[len(TAG_PREFIX) :] for t in (tags or ()) if t.startswith(TAG_PREFIX))


@dataclass
class ToolsetGate:
    """Per-server toolset state, shared by every ToolRegistrar of one FastMCP server."""

    selection: ToolsetSelection
    var: str = "TOOLSETS"
    declared: set[str] = field(default_factory=set)  # every name any registration declared
    members: dict[str, set[str]] = field(default_factory=dict)  # toolset -> registered tools
    hidden: dict[str, tuple[str, ...]] = field(default_factory=dict)  # tool -> its toolsets
    warned: bool = False

    @classmethod
    def for_server(cls, mcp: Any, settings: Any) -> ToolsetGate:
        gate: ToolsetGate | None = getattr(mcp, "_stutters_toolsets", None)
        if gate is None:
            prefix = str(type(settings).model_config.get("env_prefix", "") or "")
            var = f"{prefix}TOOLSETS"
            gate = cls(ToolsetSelection.parse(getattr(settings, "toolsets", None), var=var), var=var)
            mcp._stutters_toolsets = gate
        return gate

    def declare(self, toolsets: Iterable[str]) -> None:
        self.declared.update(toolsets)

    def registers(self, tool: str, toolsets: tuple[str, ...], *, always: bool = False) -> bool:
        """Whether `tool` is registered; records it either way (for status)."""
        if always or self.selection.admits(toolsets):
            for n in toolsets:
                self.members.setdefault(n, set()).add(tool)
            return True
        self.hidden[tool] = toolsets
        return False

    def unknown(self) -> list[str]:
        """Toolset names in the setting that no tool declares."""
        named = set(self.selection.include or ()) | set(self.selection.exclude)
        return sorted(named - self.declared)

    def warn_unknown(self) -> None:
        missing = self.unknown()
        if missing and not self.warned:
            self.warned = True
            known = ", ".join(sorted(self.declared)) or "none: this server declares no toolsets"
            log.warning("%s names unknown toolset(s) %s (known: %s)", self.var, ", ".join(missing), known)

    def describe(self) -> dict[str, Any] | None:
        """`<prefix>_status.server.toolsets`; None when no tool declares a toolset and none was asked for."""
        if not self.declared and not self.selection.active:
            return None
        out: dict[str, Any] = {
            "setting": self.selection.text or ALL,
            "available": sorted(self.declared),
            "loaded": {n: len(self.members[n]) for n in sorted(self.members)},
        }
        if self.hidden:
            out["hidden_tools"] = len(self.hidden)
            off = {n for sets in self.hidden.values() for n in sets} - set(self.members)
            if off:
                out["not_loaded"] = sorted(off)
        missing = self.unknown()
        if missing:
            out["unknown"] = missing
        return out
