"""C71: Plan(noop=True): a write that finds the target already as requested answers 'no_change'."""

import json
import logging
from typing import Annotated, Any

import httpx
import pytest
import respx
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from stutters_mcp_core import (
    Confirm,
    Plan,
    ToolRegistrar,
    build_server,
    get_client,
    metrics,
    preview_or_apply,
)
from stutters_mcp_core.metrics import REGISTRY
from stutters_mcp_core.spec import SpecSource
from stutters_mcp_core.testing import (
    as_caller,
    assert_tool_contract,
    call_tool,
    noop_result_problems,
    tool_contract_problems,
)
from stutters_mcp_core.testing.blast_radius import blast_radius_report

from .demo import BASE, SPEC, SPEC_DOC, DemoApp, DemoSettings, make_settings
from .demo import register as register_demo

RWD = {"mcp_enable_writes": True, "mcp_enable_dangerous": True}
CURRENT = ["192.0.2.1", "192.0.2.2"]


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        mock.get("/api/version").mock(return_value=httpx.Response(200, json={"version": "1"}))
        mock.get("/api/ntp").mock(return_value=httpx.Response(200, json={"servers": CURRENT}))
        mock.put("/api/ntp").mock(return_value=httpx.Response(200, json={"ok": True}))
        yield mock


def _ntp_tool(reg: ToolRegistrar, name: str, register: Any) -> None:
    async def tool(
        ctx: Context,
        servers: Annotated[list[str], Field(description="NTP servers, in order")],
        confirm: Confirm = None,
    ) -> dict[str, Any]:
        """Set the appliance's NTP servers (desired state: no change when they already match)."""
        app: DemoApp = get_client(ctx)
        current = (await app.api.get("/api/ntp"))["servers"]
        same = current == servers
        plan = Plan(
            f"Set NTP servers to {', '.join(servers)}",
            details={"current": current, "desired": servers},
            fingerprint=current,
            blast_radius=1,
            noop=same,
            noop_reason=f"NTP servers already {', '.join(current)}" if same else None,
        )
        return await preview_or_apply(
            ctx,
            tool=name,
            args={"servers": servers},
            confirm=confirm,
            plan=plan,
            apply=lambda: app.api.put("/api/ntp", {"servers": servers}),
        )

    tool.__name__ = name
    register(title="Set NTP servers")(tool)


def register_ntp(reg: ToolRegistrar) -> None:
    _ntp_tool(reg, "demo_set_ntp", reg.write)
    _ntp_tool(reg, "demo_set_ntp_fleet", reg.dangerous)

    @reg.write(title="Leaky no-op")
    async def demo_leaky_noop(ctx: Context, confirm: Confirm = None) -> dict[str, Any]:
        """A broken tool that claims no change but still hands out a token (contract test)."""
        return {"status": "no_change", "action": "x", "confirm": "pv_leak"}


def server(**kw: Any) -> FastMCP[Any]:
    async def load_spec(app: DemoApp) -> dict[str, Any]:
        return SPEC_DOC

    settings: DemoSettings = make_settings(**{**RWD, **kw})
    return build_server(
        SPEC,
        settings,
        client_factory=DemoApp,
        register=[register_demo, register_ntp],
        status=DemoApp.status,
        spec_sources=lambda app: [SpecSource("main", load_spec, client=lambda a: a.api)],
    )


def audit_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "stutters_mcp_core.audit"]


def put_calls(api: Any) -> int:
    return sum(1 for c in api.calls if c.request.method == "PUT")


# --- Plan --------------------------------------------------------------------------------------


def test_plan_noop_fields() -> None:
    assert Plan("x").noop is False and Plan("x").noop_reason is None
    assert Plan("x", noop=True, noop_reason="already").noop
    with pytest.raises(ValueError, match="noop_reason"):
        Plan("x", noop_reason="already")


# --- preview_or_apply ----------------------------------------------------------------------------


async def test_noop_has_no_token_and_is_audited(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    out = await call_tool(server(), "demo_set_ntp", {"servers": CURRENT})
    assert out["status"] == "no_change"
    assert out["action"] == "Set NTP servers to 192.0.2.1, 192.0.2.2"
    assert out["summary"] == "NTP servers already 192.0.2.1, 192.0.2.2"
    assert out["details"] == {"current": CURRENT, "desired": CURRENT}
    assert not {"confirm", "how_to_apply", "expires_in_seconds", "preview_id", "blast_radius"} & set(out)
    assert "no confirm token" in out["note"]
    assert put_calls(api) == 0
    events = [e for e in audit_events(caplog) if e["event"] != "tool_call"]
    assert [e["event"] for e in events] == ["noop"]
    assert events[0]["tool"] == "demo_set_ntp" and events[0]["reason"] == out["summary"]


async def test_a_real_change_still_previews_then_reruns_as_noop(api: Any) -> None:
    desired = ["192.0.2.1", "198.51.100.7"]
    async with Client(server()) as c:
        prev = (await c.call_tool("demo_set_ntp", {"servers": desired})).structured_content
        assert prev["status"] == "preview" and prev["confirm"].startswith("pv_")
        applied = await c.call_tool("demo_set_ntp", {"servers": desired, "confirm": prev["confirm"]})
        assert applied.structured_content["status"] == "applied"
        api.get("/api/ntp").mock(return_value=httpx.Response(200, json={"servers": desired}))
        again = (await c.call_tool("demo_set_ntp", {"servers": desired})).structured_content
    assert again["status"] == "no_change" and "confirm" not in again
    assert put_calls(api) == 1


async def test_confirm_on_a_target_that_already_matches(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    """Previewed while different, then the target got there by itself: the confirm applies nothing."""
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    desired = ["198.51.100.7"]
    async with Client(server()) as c:
        token = (await c.call_tool("demo_set_ntp", {"servers": desired})).structured_content["confirm"]
        api.get("/api/ntp").mock(return_value=httpx.Response(200, json={"servers": desired}))
        out = (await c.call_tool("demo_set_ntp", {"servers": desired, "confirm": token})).structured_content
        assert out["status"] == "no_change" and "dropped" in out["note"]
        # the token was dropped: it can't be used later, e.g. after the target drifts back
        api.get("/api/ntp").mock(return_value=httpx.Response(200, json={"servers": CURRENT}))
        with pytest.raises(ToolError, match="Unknown or expired confirm token"):
            await c.call_tool("demo_set_ntp", {"servers": desired, "confirm": token})
    assert put_calls(api) == 0
    noop = next(e for e in audit_events(caplog) if e["event"] == "noop")
    assert noop["confirm_dropped"] is True


async def test_noop_leaves_a_token_for_another_tool(api: Any) -> None:
    """A confirm token for a different tool is not this change's: a no-op must not drop it."""
    async with Client(server()) as c:
        other = (await c.call_tool("demo_set_ntp_fleet", {"servers": ["198.51.100.7"]})).structured_content
        out = (
            await c.call_tool("demo_set_ntp", {"servers": CURRENT, "confirm": other["confirm"]})
        ).structured_content
        assert out["status"] == "no_change" and "dropped" not in out["note"]
        applied = await c.call_tool(
            "demo_set_ntp_fleet", {"servers": ["198.51.100.7"], "confirm": other["confirm"]}
        )
    assert applied.structured_content["status"] == "applied"


async def test_noop_metric(api: Any) -> None:
    REGISTRY.reset()
    metrics.enable()
    try:
        async with Client(server(mcp_metrics=True)) as c:
            await c.call_tool("demo_set_ntp", {"servers": CURRENT})
            await c.call_tool("demo_set_ntp", {"servers": ["198.51.100.7"]})
        labels = {"server": "demo-mcp-server", "tool": "demo_set_ntp"}
        assert REGISTRY.value("mcp_write_events_total", **labels, event="noop") == 1
        assert REGISTRY.value("mcp_write_events_total", **labels, event="preview") == 1
    finally:
        metrics.disable()
        REGISTRY.reset()


# --- rails are skipped: nothing to limit or approve ------------------------------------------------


async def test_noop_skips_blast_radius_limit(api: Any) -> None:
    s = server(mcp_max_blast_radius=0)
    blocked = await call_tool(s, "demo_set_ntp", {"servers": ["198.51.100.7"]})
    assert blocked["status"] == "blocked"  # a real change over the limit is still blocked
    out = await call_tool(s, "demo_set_ntp", {"servers": CURRENT})
    assert out["status"] == "no_change" and "policy" not in out and "blockers" not in out


async def test_noop_skips_two_person(api: Any) -> None:
    async with Client(server(mcp_two_person_tools="@dangerous")) as c:
        with as_caller("alice"):
            out = (await c.call_tool("demo_set_ntp_fleet", {"servers": CURRENT})).structured_content
            assert out["status"] == "no_change" and "preview_id" not in out
            real = (await c.call_tool("demo_set_ntp_fleet", {"servers": ["198.51.100.7"]})).structured_content
            assert real["policy"]["two_person"] is True
        with as_caller("bob"):
            listing = (await c.call_tool("demo_list_pending_changes", {})).structured_content
    assert listing is not None and len(listing["results"]) == 1  # only the real change waits


async def test_noop_skips_dry_run_and_elicit(api: Any) -> None:
    out = await call_tool(server(mcp_dry_run=True), "demo_set_ntp", {"servers": CURRENT})
    assert out["status"] == "no_change" and "policy" not in out
    out = await call_tool(server(mcp_confirm_mode="elicit"), "demo_set_ntp", {"servers": CURRENT})
    assert out["status"] == "no_change"


async def test_plan_blockers_win_over_noop(api: Any) -> None:
    async def run(ctx: Context) -> dict[str, Any]:
        return await preview_or_apply(
            ctx,
            tool="demo_x",
            args={},
            confirm=None,
            plan=Plan("x", noop=True, blockers=["bad input"]),
            apply=lambda: _never(),
        )

    reg_server = server()

    @reg_server.tool(name="demo_blocked_noop")
    async def demo_blocked_noop(ctx: Context) -> dict[str, Any]:
        return await run(ctx)

    out = await call_tool(reg_server, "demo_blocked_noop")
    assert out["status"] == "blocked" and out["blockers"] == ["bad input"]


async def _never() -> None:
    raise AssertionError("apply must not run")


# --- contract helpers -----------------------------------------------------------------------------


def test_noop_result_problems() -> None:
    assert noop_result_problems("t", {"status": "no_change", "action": "x"}) == []
    assert noop_result_problems("t", {"status": "no_change", "confirm": "pv_1", "how_to_apply": "."}) == [
        "t: a no-op result must not carry a confirm token (confirm, how_to_apply)"
    ]
    assert noop_result_problems("t", {"status": "preview"}) == [
        "t: no-op call returned status 'preview', expected 'no_change'"
    ]
    assert noop_result_problems("t", "text") == ["t: no-op call returned str, not a result object"]


async def test_tool_contract_checks_noop_calls(api: Any) -> None:
    s = server()
    await assert_tool_contract(s, "demo", noop_calls={"demo_set_ntp": {"servers": CURRENT}})
    problems = await tool_contract_problems(
        s,
        "demo",
        noop_calls={
            "demo_leaky_noop": {},
            "demo_set_ntp": [{"servers": CURRENT}, {"servers": ["198.51.100.7"]}, {"confirm": "pv_x"}],
            "demo_list_items": {},
            "demo_nope": {},
        },
    )
    assert "demo_leaky_noop: a no-op result must not carry a confirm token (confirm)" in problems
    assert "demo_set_ntp: no-op call returned status 'preview', expected 'no_change'" in problems
    assert "demo_set_ntp: a no-op call is made without confirm" in problems
    assert "demo_list_items: no-op calls are for write tools (it has no write tier)" in problems
    assert "demo_nope: no-op call names a tool the server doesn't expose" in problems
    with pytest.raises(AssertionError, match="demo_leaky_noop"):
        await assert_tool_contract(s, "demo", noop_calls={"demo_leaky_noop": {}})


async def test_blast_radius_report_exempts_noop_plans(api: Any) -> None:
    report = await blast_radius_report(
        server(),
        {"demo_set_ntp_fleet": {"servers": CURRENT}},
        skip={"demo_wipe", "demo_api_call"},
    )
    assert report.noop == {"demo_set_ntp_fleet": ["Set NTP servers to 192.0.2.1, 192.0.2.2"]}
    assert "demo_set_ntp_fleet" not in report.missing
    assert "no change demo_set_ntp_fleet" in report.summary()
