"""A budget for a server's tool surface (C31): what a client loads before the first turn.

Every connected server sends its tool descriptions and instructions to the model on every turn, so their
size is a cost the suite pays per server. The budget is an explicit number a server commits in its tests;
growth past it fails CI until the same PR raises the number (an explicit bump a reviewer sees):

    from stutters_mcp_core.testing import SurfaceBudget, assert_tool_contract, tool_contract_problems

    BUDGET = SurfaceBudget(descriptions=48_600, instructions=6_200, tools=153, per_tool=900)

    async def test_contract():                # all tiers on, no <P>_TOOLSETS: the whole surface
        server = create_server(settings(writes=True, dangerous=True))
        assert await tool_contract_problems(server, "fmc", budget=BUDGET) == []

    async def test_surface_budget():          # or on its own
        await assert_surface_budget(server, BUDGET)

`headroom=0.05` also fails when a budget is more than 5% above what is used, so a budget raised once
can't hide later growth; `surface_size(server)` prints the current numbers (per toolset too) to set it.
Characters, not tokens: the count is stable across tokenizers and needs no model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Client, FastMCP

from stutters_mcp_core.toolsets import toolsets_of


@dataclass(frozen=True)
class SurfaceBudget:
    """Limits on a server's tool surface; None = not checked.

    `descriptions`: total characters of every tool description. `instructions`: characters of the server
    instructions (as built with the test's settings). `tools`: number of tools. `per_tool`: characters of
    one description (the contract's own 30..1500 range still applies); `per_tool_overrides` gives single
    tools their own limit. `headroom`: the largest fraction a total budget may sit above actual use."""

    descriptions: int | None = None
    instructions: int | None = None
    tools: int | None = None
    per_tool: int | None = None
    per_tool_overrides: Mapping[str, int] = field(default_factory=dict)
    headroom: float | None = None

    def __post_init__(self) -> None:
        for name in ("descriptions", "instructions", "tools", "per_tool"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"SurfaceBudget.{name} must be >= 0")
        if self.headroom is not None and self.headroom < 0:
            raise ValueError("SurfaceBudget.headroom must be >= 0")


@dataclass(frozen=True)
class SurfaceSize:
    """What a server's tool surface costs: counts and characters, per tool and per toolset."""

    tools: int
    descriptions: int
    instructions: int
    per_tool: dict[str, int]
    toolsets: dict[str, dict[str, int]]  # toolset -> {"tools": n, "descriptions": chars}

    def largest(self, n: int = 10) -> list[tuple[str, int]]:
        return sorted(self.per_tool.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def size_of(tools: Iterable[Any], instructions: str | None) -> SurfaceSize:
    """The surface size of MCP tool definitions (from `Client.list_tools()`) and instructions."""
    per_tool: dict[str, int] = {}
    toolsets: dict[str, dict[str, int]] = {}
    for t in tools:
        chars = len((t.description or "").strip())
        per_tool[t.name] = chars
        tags = (t.meta or {}).get("fastmcp", {}).get("tags", []) or []
        for name in toolsets_of(tags):
            entry = toolsets.setdefault(name, {"tools": 0, "descriptions": 0})
            entry["tools"] += 1
            entry["descriptions"] += chars
    return SurfaceSize(
        tools=len(per_tool),
        descriptions=sum(per_tool.values()),
        instructions=len((instructions or "").strip()),
        per_tool=per_tool,
        toolsets=dict(sorted(toolsets.items())),
    )


async def surface_size(mcp: FastMCP[Any]) -> SurfaceSize:
    """The surface a client connected to `mcp` loads (build it with all tiers on for the whole surface)."""
    async with Client(mcp) as client:
        tools = await client.list_tools()
    return size_of(tools, mcp.instructions)


def _over(what: str, used: int, limit: int, field_name: str) -> str:
    return (
        f"surface budget: {what} {used:,} > budget {limit:,}: shorten it, or raise "
        f"SurfaceBudget({field_name}={used}) in the same change (an explicit bump)"
    )


def surface_budget_problems(size: SurfaceSize, budget: SurfaceBudget) -> list[str]:
    """Where `size` breaks `budget`, as human-readable strings (empty when within it)."""
    problems: list[str] = []
    totals = (
        ("tool descriptions total", size.descriptions, budget.descriptions, "descriptions"),
        ("server instructions", size.instructions, budget.instructions, "instructions"),
        ("tool count", size.tools, budget.tools, "tools"),
    )
    for what, used, limit, name in totals:
        if limit is None:
            continue
        if used > limit:
            problems.append(_over(what, used, limit, name))
        elif budget.headroom is not None and limit > used * (1 + budget.headroom):
            problems.append(
                f"surface budget: {name} budget {limit:,} is more than {budget.headroom:.0%} above the "
                f"{used:,} used: lower it to SurfaceBudget({name}={used}) so growth fails CI"
            )
    for tool in sorted(budget.per_tool_overrides):
        if tool not in size.per_tool:
            problems.append(f"surface budget: per_tool_overrides names {tool}, which is not registered")
    for tool, chars in sorted(size.per_tool.items()):
        limit = budget.per_tool_overrides.get(tool, budget.per_tool)
        if limit is not None and chars > limit:
            problems.append(f"{tool}: description length {chars} > per-tool budget {limit}")
    return problems


async def assert_surface_budget(mcp: FastMCP[Any], budget: SurfaceBudget) -> None:
    problems = surface_budget_problems(await surface_size(mcp), budget)
    assert not problems, "Tool surface over budget:\n  " + "\n  ".join(problems)
