"""C29: Plan(blast_unknown=...) and the dangerous-Plan contract helper, on the demo server."""

import json
import logging
from typing import Annotated, Any

import httpx
import pytest
import respx
from fastmcp import Client, Context, FastMCP
from pydantic import Field

from stutters_mcp_core import Confirm, Plan, ToolRegistrar, build_server, get_client, preview_or_apply
from stutters_mcp_core.confirm import elicit_message
from stutters_mcp_core.spec import SpecSource
from stutters_mcp_core.testing import as_caller, call_tool
from stutters_mcp_core.testing.blast_radius import assert_dangerous_plans_counted, blast_radius_report

from .demo import BASE, SPEC, SPEC_DOC, DemoApp, DemoSettings, make_settings
from .demo import register as register_demo

RWD = {"mcp_enable_writes": True, "mcp_enable_dangerous": True}
API_CALL = {"method": "DELETE", "path": "/api/items/1"}


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        mock.get("/api/version").mock(return_value=httpx.Response(200, json={"version": "1"}))
        mock.get("/api/items").mock(
            return_value=httpx.Response(200, json={"items": [{"id": "1"}, {"id": "2"}], "total": 2})
        )
        yield mock


def register_extra(reg: ToolRegistrar) -> None:
    @reg.dangerous(title="Reset item")
    async def demo_reset_item(
        ctx: Context, item_id: Annotated[str, Field(description="Item id")], confirm: Confirm = None
    ) -> dict[str, Any]:
        """Reset one item to factory state. Preview-first."""
        return await preview_or_apply(
            ctx,
            tool="demo_reset_item",
            args={"item_id": item_id},
            confirm=confirm,
            plan=Plan(f"Reset item {item_id}", blast_unknown="the appliance doesn't say what uses an item"),
            apply=lambda: get_client(ctx).api.post(f"/api/items/{item_id}/reset", {}),
        )

    @reg.dangerous(title="Forget items")
    async def demo_forget_items(ctx: Context, confirm: Confirm = None) -> dict[str, Any]:
        """Forget every item the appliance knows (counts them, but forgets to say so). Preview-first."""
        app: DemoApp = get_client(ctx)
        items = (await app.api.get("/api/items"))["items"]
        return await preview_or_apply(
            ctx,
            tool="demo_forget_items",
            args={},
            confirm=confirm,
            plan=Plan(f"Forget {len(items)} items"),
            apply=lambda: app.api.delete("/api/items"),
        )

    @reg.dangerous(title="Broken tool")
    async def demo_broken(ctx: Context, confirm: Confirm = None) -> dict[str, Any]:
        """Fails before it plans anything. Preview-first."""
        await get_client(ctx).api.get("/api/not-mocked")
        return {}


def server(**kw: Any) -> FastMCP[Any]:
    async def load_spec(app: DemoApp) -> dict[str, Any]:
        return SPEC_DOC

    settings: DemoSettings = make_settings(**{**RWD, **kw})
    return build_server(
        SPEC,
        settings,
        client_factory=DemoApp,
        register=[register_demo, register_extra],
        status=DemoApp.status,
        spec_sources=lambda app: [SpecSource("main", load_spec, client=lambda a: a.api)],
    )


def audit_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "stutters_mcp_core.audit"]


# --- Plan(blast_unknown) -----------------------------------------------------------------------


def test_plan_takes_one_of_the_two() -> None:
    with pytest.raises(ValueError, match="not both"):
        Plan("x", blast_radius=2, blast_unknown="why")
    with pytest.raises(ValueError, match="needs a reason"):
        Plan("x", blast_unknown="  ")
    assert Plan("x", blast_unknown="raw call").blast_radius is None


async def test_preview_states_unknown_blast_radius(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    prev = await call_tool(server(), "demo_reset_item", {"item_id": "1"})
    assert prev["status"] == "preview" and "confirm" in prev
    assert prev["blast_unknown"] == "not counted (the appliance doesn't say what uses an item)"
    assert "blast_radius" not in prev
    event = next(e for e in audit_events(caplog) if e["event"] == "preview")
    assert event["blast_unknown"] == "the appliance doesn't say what uses an item"


async def test_unknown_is_stated_but_never_limited(api: Any) -> None:
    prev = await call_tool(server(mcp_max_blast_radius=1), "demo_reset_item", {"item_id": "1"})
    assert prev["status"] == "preview" and "confirm" in prev  # not blocked
    assert prev["policy"] == {"max_blast_radius": 1}
    assert prev["blast_unknown"].startswith("not counted (the appliance")
    assert "MCP_MAX_BLAST_RADIUS" in prev["blast_unknown"]


async def test_core_api_call_declares_unknown(api: Any) -> None:
    raw = await call_tool(server(), "demo_api_call", API_CALL)
    prev = json.loads(raw) if isinstance(raw, str) else raw
    assert prev["status"] == "preview"
    assert prev["blast_unknown"].startswith("not counted (a raw API call")


def test_confirmation_dialog_shows_unknown() -> None:
    text = elicit_message("demo_reset_item", {"action": "Reset", "blast_unknown": "not counted (why)"}, {})
    assert "Blast radius: not counted (why)" in text


# --- the contract helper -----------------------------------------------------------------------

SAMPLES = {"demo_reset_item": {"item_id": "1"}, "demo_api_call": [API_CALL, {**API_CALL, "method": "POST"}]}


async def test_report_counted_declared_missing(api: Any) -> None:
    report = await blast_radius_report(server(), SAMPLES, skip={"demo_broken": "fails on purpose"})
    assert report.counted == {"demo_wipe": [3]}
    assert report.declared["demo_reset_item"] == ["the appliance doesn't say what uses an item"]
    assert len(report.declared["demo_api_call"]) == 2  # both cases were previewed
    assert report.missing == {"demo_forget_items": ["Forget 2 items"]}
    assert report.skipped == {"demo_broken": "fails on purpose"}
    assert not report.not_driven and not report.problems
    assert "demo_create_item" not in report.counted  # write tier isn't checked by default
    with pytest.raises(AssertionError, match="NOT COUNTED demo_forget_items: Plan 'Forget 2 items'"):
        await assert_dangerous_plans_counted(server(), SAMPLES, skip={"demo_broken": "fails on purpose"})


async def test_assert_passes_when_every_plan_counts_or_declares(api: Any) -> None:
    report = await assert_dangerous_plans_counted(
        server(), SAMPLES, skip={"demo_broken": "fails on purpose", "demo_forget_items": "follow-up"}
    )
    assert set(report.counted) | set(report.declared) == {"demo_wipe", "demo_reset_item", "demo_api_call"}
    assert "skipped demo_forget_items: follow-up" in report.summary()


async def test_tools_not_driven_fail_only_when_strict(api: Any) -> None:
    skip = ["demo_forget_items"]
    report = await blast_radius_report(server(), {}, skip=skip)
    assert report.not_driven["demo_reset_item"] == "no sample for item_id"
    assert report.not_driven["demo_broken"].startswith("error before a Plan")
    with pytest.raises(AssertionError, match="NOT DRIVEN demo_reset_item"):
        await assert_dangerous_plans_counted(server(), {}, skip=skip)
    loose = await assert_dangerous_plans_counted(server(), {}, skip=skip, strict=False)
    assert loose.counted == {"demo_wipe": [3]}


async def test_bad_samples_and_disabled_tier(api: Any) -> None:
    report = await blast_radius_report(
        server(),
        {"demo_nope": {}, "demo_wipe": {"confirm": "pv_x"}},
        skip={"demo_broken", "demo_forget_items"},
    )
    assert "sample for demo_nope, which isn't a registered dangerous tool" in report.problems
    assert "sample for demo_wipe carries confirm: previews only" in report.problems
    writes_only = await blast_radius_report(server(mcp_enable_dangerous=False))
    assert writes_only.problems and "MCP_ENABLE_DANGEROUS" in writes_only.problems[0]
    with_writes = await blast_radius_report(
        server(), {"demo_create_item": {"name": "a"}}, tiers=("write",), skip={"demo_cancel_pending_change"}
    )
    assert with_writes.missing.get("demo_create_item") == ["Create item a"]


async def test_two_person_listing_shows_unknown(api: Any) -> None:
    async with Client(server(mcp_two_person_tools="demo_reset_item")) as c:
        with as_caller("alice"):
            await c.call_tool("demo_reset_item", {"item_id": "1"})
        with as_caller("bob"):
            listing = (await c.call_tool("demo_list_pending_changes", {})).structured_content
    assert listing is not None
    row = listing["results"][0]
    assert row["blast_unknown"].startswith("not counted (") and "blast_radius" not in row
