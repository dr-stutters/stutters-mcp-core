"""C31: toolsets (<P>_TOOLSETS include/exclude at registration) and the tool-surface budget lint."""

import logging
from typing import Any

import pytest
from fastmcp import Client, Context, FastMCP
from pydantic import ValidationError

from stutters_mcp_core import Confirm, ServerSpec, ToolRegistrar, build_server
from stutters_mcp_core.manifest import build_manifest
from stutters_mcp_core.testing import (
    SurfaceBudget,
    assert_surface_budget,
    call_tool,
    surface_budget_problems,
    surface_size,
    tool_contract_problems,
)
from stutters_mcp_core.toolsets import ToolsetSelection, normalise

from .demo import make_settings

SPEC = ServerSpec("ts-mcp-server", "demo", "Toolset Demo", "DEMO_", "Tools for a toolset demo (tests).")
DESC = "A synthetic tool for the toolset tests; it returns a fixed answer and touches nothing."


def register(reg: ToolRegistrar) -> None:
    @reg.read(title="Trace", toolset="troubleshoot", description=DESC)
    async def demo_trace(ctx: Context) -> dict[str, Any]:
        return {"ok": True}

    @reg.read(title="Find object", toolset=("objects", "troubleshoot"), description=DESC)
    async def demo_find_object(ctx: Context) -> dict[str, Any]:
        return {"ok": True}

    @reg.read(title="Show policy", toolset="policy", description=DESC)
    async def demo_show_policy(ctx: Context) -> dict[str, Any]:
        return {"ok": True}

    @reg.read(title="Untagged", description=DESC)
    async def demo_untagged(ctx: Context) -> dict[str, Any]:
        return {"ok": True}

    @reg.write(title="Set object", toolset="objects", description=DESC)
    async def demo_set_object(ctx: Context, confirm: Confirm = None) -> dict[str, Any]:
        return {"ok": True}

    @reg.dangerous(title="Deploy", toolset="deploy", description=DESC)
    async def demo_deploy(ctx: Context, confirm: Confirm = None) -> dict[str, Any]:
        return {"ok": True}


def server(**kw: Any) -> FastMCP[Any]:
    return build_server(SPEC, make_settings(**kw), client_factory=lambda s: object(), register=[register])


async def names(mcp: FastMCP[Any]) -> set[str]:
    async with Client(mcp) as c:
        return {t.name for t in await c.list_tools()}


ALL = {
    "demo_status",
    "demo_trace",
    "demo_find_object",
    "demo_show_policy",
    "demo_untagged",
    "demo_set_object",
    "demo_deploy",
}


# --- parsing -----------------------------------------------------------------------------------


def test_parse_forms() -> None:
    assert not ToolsetSelection.parse(None).active
    assert not ToolsetSelection.parse(" all ").active
    sel = ToolsetSelection.parse("Troubleshoot, objects,")
    assert sel.include == {"troubleshoot", "objects"} and not sel.exclude
    sel = ToolsetSelection.parse("-policy,!deploy")
    assert sel.include is None and sel.exclude == {"policy", "deploy"}
    sel = ToolsetSelection.parse("all,-policy")
    assert sel.include is None and sel.exclude == {"policy"}


@pytest.mark.parametrize(
    ("text", "message"),
    [("-all", "'-all'"), ("trouble shoot", "not a toolset name"), ("a,-a", "both included and excluded")],
)
def test_parse_rejects(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ToolsetSelection.parse(text, var="DEMO_TOOLSETS")


def test_setting_is_validated_with_its_variable_name() -> None:
    with pytest.raises(ValidationError, match="DEMO_TOOLSETS"):
        make_settings(toolsets="bad name")
    assert make_settings(toolsets="  ").toolsets is None


def test_tool_toolset_names_are_checked() -> None:
    assert normalise(("a", "a", "b"), "demo_x") == ("a", "b")
    with pytest.raises(ValueError, match="demo_x: toolset 'All'"):
        normalise("All", "demo_x")
    with pytest.raises(ValueError, match="not 'all'"):
        normalise("all", "demo_x")


# --- registration ------------------------------------------------------------------------------


async def test_unset_loads_everything_and_tags_toolsets() -> None:
    mcp = server(mcp_enable_writes=True, mcp_enable_dangerous=True)
    assert await names(mcp) == ALL
    tool = await mcp.get_tool("demo_find_object")
    assert tool is not None
    assert {"read", "toolset:objects", "toolset:troubleshoot"} <= tool.tags
    assert "Only some toolsets" not in (mcp.instructions or "")


async def test_include_loads_only_those_plus_untagged() -> None:
    mcp = server(mcp_enable_writes=True, mcp_enable_dangerous=True, toolsets="troubleshoot")
    assert await names(mcp) == {"demo_status", "demo_untagged", "demo_trace", "demo_find_object"}
    assert set(mcp._stutters_tiers) == {  # type: ignore[attr-defined]
        "demo_status",
        "demo_untagged",
        "demo_trace",
        "demo_find_object",
    }
    assert "DEMO_TOOLSETS=troubleshoot" in (mcp.instructions or "")
    assert "not loaded: deploy, objects, policy" not in (mcp.instructions or "")  # objects is half-loaded
    assert "not loaded: deploy, policy" in (mcp.instructions or "")


async def test_exclude_wins_over_include() -> None:
    mcp = server(mcp_enable_writes=True, mcp_enable_dangerous=True, toolsets="troubleshoot,objects,-policy")
    assert await names(mcp) == ALL - {"demo_show_policy", "demo_deploy"}
    mcp = server(mcp_enable_writes=True, mcp_enable_dangerous=True, toolsets="troubleshoot,-objects")
    assert await names(mcp) == {"demo_status", "demo_untagged", "demo_trace"}


async def test_exclude_only() -> None:
    mcp = server(mcp_enable_writes=True, mcp_enable_dangerous=True, toolsets="-deploy")
    assert await names(mcp) == ALL - {"demo_deploy"}


async def test_tiers_still_apply() -> None:
    mcp = server(toolsets="objects,deploy")  # writes off: the write/dangerous tools stay out
    assert await names(mcp) == {"demo_status", "demo_untagged", "demo_find_object"}


async def test_unknown_name_warns_and_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="stutters_mcp_core.toolsets"):
        mcp = server(toolsets="troubleshoot,trubleshoot,-nothing")
    assert "DEMO_TOOLSETS names unknown toolset(s) nothing, trubleshoot" in caplog.text
    assert "known: deploy, objects, policy, troubleshoot" in caplog.text
    data = await call_tool(mcp, "demo_status")
    assert data["server"]["toolsets"]["unknown"] == ["nothing", "trubleshoot"]


async def test_tier_off_toolset_is_not_unknown(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="stutters_mcp_core.toolsets"):
        server(toolsets="deploy")  # declared by a dangerous tool, which is off
    assert "unknown" not in caplog.text


# --- status --------------------------------------------------------------------------------------


async def test_status_reports_toolsets() -> None:
    mcp = server(mcp_enable_writes=True, mcp_enable_dangerous=True, toolsets="troubleshoot,objects")
    report = (await call_tool(mcp, "demo_status"))["server"]["toolsets"]
    assert report == {
        "setting": "troubleshoot,objects",
        "available": ["deploy", "objects", "policy", "troubleshoot"],
        "loaded": {"objects": 2, "troubleshoot": 2},
        "hidden_tools": 2,
        "not_loaded": ["deploy", "policy"],
    }
    full = (await call_tool(server(), "demo_status"))["server"]["toolsets"]
    assert full == {
        "setting": "all",
        "available": ["deploy", "objects", "policy", "troubleshoot"],
        "loaded": {"objects": 1, "policy": 1, "troubleshoot": 2},
    }


async def test_status_has_no_toolsets_block_without_toolsets() -> None:
    plain = build_server(SPEC, make_settings(), client_factory=lambda s: object(), register=[])
    assert "toolsets" not in (await call_tool(plain, "demo_status"))["server"]


async def test_manifest_records_toolsets() -> None:
    manifest = await build_manifest(server(), server="ts-mcp-server", prefix="demo")
    assert manifest["tools"]["demo_find_object"]["toolsets"] == ["objects", "troubleshoot"]
    assert "toolsets" not in manifest["tools"]["demo_untagged"]


# --- budget lint ---------------------------------------------------------------------------------


async def test_surface_size_counts_tools_descriptions_instructions_and_toolsets() -> None:
    mcp = server(mcp_enable_writes=True, mcp_enable_dangerous=True)
    size = await surface_size(mcp)
    assert size.tools == 7
    assert size.per_tool["demo_trace"] == len(DESC)
    assert size.descriptions == sum(size.per_tool.values())
    assert size.instructions == len((mcp.instructions or "").strip())
    assert size.toolsets["troubleshoot"] == {"tools": 2, "descriptions": 2 * len(DESC)}
    assert size.largest(1)[0][0] == "demo_status"


async def test_budget_passes_at_the_recorded_numbers() -> None:
    mcp = server(mcp_enable_writes=True, mcp_enable_dangerous=True)
    size = await surface_size(mcp)
    budget = SurfaceBudget(
        descriptions=size.descriptions,
        instructions=size.instructions,
        tools=size.tools,
        per_tool=max(size.per_tool.values()),
        headroom=0.05,
    )
    assert await tool_contract_problems(mcp, "demo", budget=budget) == []
    await assert_surface_budget(mcp, budget)


async def test_budget_fails_on_growth_without_a_bump() -> None:
    mcp = server(mcp_enable_writes=True, mcp_enable_dangerous=True)
    size = await surface_size(mcp)
    budget = SurfaceBudget(
        descriptions=size.descriptions - 1,
        instructions=size.instructions - 1,
        tools=size.tools - 1,
        per_tool=len(DESC) - 1,
        per_tool_overrides={"demo_status": 10_000},
    )
    problems = await tool_contract_problems(mcp, "demo", budget=budget)
    assert any(
        p.startswith(f"surface budget: tool descriptions total {size.descriptions:,} > budget")
        and f"SurfaceBudget(descriptions={size.descriptions})" in p
        for p in problems
    )
    assert any("server instructions" in p and "explicit bump" in p for p in problems)
    assert any("tool count 7 > budget 6" in p for p in problems)
    assert f"demo_trace: description length {len(DESC)} > per-tool budget {len(DESC) - 1}" in problems
    assert not any(p.startswith("demo_status:") for p in problems)  # its own override
    with pytest.raises(AssertionError, match="Tool surface over budget"):
        await assert_surface_budget(mcp, budget)


async def test_budget_headroom_flags_a_stale_budget() -> None:
    size = await surface_size(server())
    stale = SurfaceBudget(descriptions=size.descriptions * 2, headroom=0.10)
    (problem,) = surface_budget_problems(size, stale)
    assert "more than 10% above" in problem and f"SurfaceBudget(descriptions={size.descriptions})" in problem
    assert surface_budget_problems(size, SurfaceBudget(descriptions=size.descriptions * 2)) == []


async def test_budget_override_for_an_unknown_tool_is_a_problem() -> None:
    size = await surface_size(server())
    assert surface_budget_problems(size, SurfaceBudget(per_tool_overrides={"demo_gone": 5})) == [
        "surface budget: per_tool_overrides names demo_gone, which is not registered"
    ]


def test_budget_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="descriptions"):
        SurfaceBudget(descriptions=-1)
    with pytest.raises(ValueError, match="headroom"):
        SurfaceBudget(headroom=-0.1)


async def test_no_budget_keeps_the_contract_unchanged() -> None:
    mcp = server(mcp_enable_writes=True, mcp_enable_dangerous=True)
    assert await tool_contract_problems(mcp, "demo") == []
