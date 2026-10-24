"""C70: runtime, time-boxed rail overrides (core primitive; cml_set_lab_scope is the first consumer)."""

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx
import pytest
import respx
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from stutters_mcp_core import Confirm, ToolRegistrar, build_server
from stutters_mcp_core import overrides as ov_mod
from stutters_mcp_core.overrides import (
    OverrideStore,
    active_override,
    clear_override,
    parse_until,
    set_override,
)
from stutters_mcp_core.testing import as_caller, call_tool

from . import demo
from .demo import BASE, make_settings

T0 = datetime(2026, 10, 24, 3, 0, tzinfo=UTC)
ON = {"mcp_enable_writes": True, "mcp_runtime_overrides": True}
CONFIGURED = ["mcp-*"]


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        mock.get("/api/version").mock(return_value=httpx.Response(200, json={"version": "1.2.3"}))
        yield mock


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(ov_mod, "_now", c)
    return c


def register(reg: ToolRegistrar) -> None:
    @reg.write(title="Widen the scope")
    async def demo_set_scope(
        ctx: Context,
        managed: Annotated[list[str], Field(description="Extra managed lab globs")],
        until: Annotated[str, Field(description="Duration (30m, 2h) or ISO time")],
        reason: Annotated[str, Field(description="Why, and for what work")],
        confirm: Confirm = None,
    ) -> dict[str, Any]:
        """Widen the managed scope for a while (demo consumer of the override primitive)."""
        return await set_override(
            ctx,
            tool="demo_set_scope",
            scope="lab_scope",
            values={"managed": managed},
            until=until,
            reason=reason,
            confirm=confirm,
            current={"managed": CONFIGURED},
            blockers=["'*' would manage every lab"] if "*" in managed else [],
            args={"managed": managed, "until": until, "reason": reason},
        )

    @reg.write(title="Clear the scope override")
    async def demo_clear_scope(ctx: Context, confirm: Confirm = None) -> dict[str, Any]:
        """Remove the scope override now so the configured rail applies again."""
        return await clear_override(ctx, tool="demo_clear_scope", scope="lab_scope", confirm=confirm)

    @reg.read(title="Effective scope")
    async def demo_effective_scope(ctx: Context) -> dict[str, Any]:
        """The managed scope the rail checks right now: configured globs plus a live override."""
        o = active_override(ctx, "lab_scope")
        return {"managed": CONFIGURED + (o.values["managed"] if o else [])}


def server(**kw: Any) -> FastMCP[Any]:
    return build_server(
        demo.SPEC,
        make_settings(**kw),
        client_factory=demo.DemoApp,
        register=[demo.register, register],
        status=demo.DemoApp.status,
    )


ARGS = {"managed": ["lab-x"], "until": "30m", "reason": "rebuild lab-x for the W51 drill"}


def audit(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "stutters_mcp_core.audit"]


def test_parse_until() -> None:
    four = timedelta(hours=4)
    assert parse_until("30m", T0, four) == T0 + timedelta(minutes=30)
    assert parse_until("1h30m", T0, four) == T0 + timedelta(minutes=90)
    assert parse_until("2026-10-24T05:00:00Z", T0, four) == T0 + timedelta(hours=2)
    assert parse_until("2026-10-24T04:00:00", T0, four) == T0 + timedelta(hours=1)  # no zone = UTC
    for bad, msg in [
        ("", "required"),
        ("soon", "neither"),
        ("2026-10-24T02:00:00Z", "not in the future"),
        ("5h", "more than 240 minutes"),
    ]:
        with pytest.raises(ValueError, match=msg):
            parse_until(bad, T0, four)


async def test_off_by_default_blocks(api: Any, clock: Clock) -> None:
    prev = await call_tool(server(mcp_enable_writes=True), "demo_set_scope", ARGS)
    assert prev["status"] == "blocked" and "MCP_RUNTIME_OVERRIDES=false" in prev["blockers"][0]
    status = await call_tool(server(mcp_enable_writes=True), "demo_status")
    assert "overrides" not in status["server"]


async def test_two_person_set_expire_and_audit(
    api: Any, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    async with Client(server(**ON)) as c:
        with as_caller("alice"):
            prev = (await c.call_tool("demo_set_scope", ARGS)).structured_content
            assert prev["status"] == "preview" and "TWO-PERSON" in prev["how_to_apply"]
            assert prev["preview"]["configured"] == {"managed": CONFIGURED}
            assert prev["preview"]["minutes"] == 30 and "Weakens a safety rail" in prev["side_effects"][0]
            with pytest.raises(ToolError, match="different user than 'alice'"):
                await c.call_tool("demo_set_scope", {**ARGS, "confirm": prev["confirm"]})
        with as_caller("bob"):
            review = (await c.call_tool("demo_review_change", {"token": prev["confirm"]})).structured_content
            assert review["requested_by"] == "alice"
            done = (
                await c.call_tool("demo_set_scope", {**ARGS, "confirm": prev["confirm"]})
            ).structured_content
        assert done["status"] == "applied"
        assert done["result"]["requested_by"] == "alice" and done["result"]["approved_by"] == "bob"
        eff = (await c.call_tool("demo_effective_scope", {})).structured_content
        assert eff == {"managed": ["mcp-*", "lab-x"]}
        st = (await c.call_tool("demo_status", {})).structured_content["server"]["overrides"]
        assert st["enabled"] and st["two_person"] and st["max_minutes"] == 240
        assert st["active"][0]["scope"] == "lab_scope" and st["active"][0]["remaining_minutes"] == 30

        clock.now = T0 + timedelta(minutes=31)  # lapses on its own
        eff = (await c.call_tool("demo_effective_scope", {})).structured_content
        assert eff == {"managed": ["mcp-*"]}
        st = (await c.call_tool("demo_status", {})).structured_content["server"]["overrides"]
        assert st["active"] == []
    events = [e["event"] for e in audit(caplog) if e["event"].startswith("override_")]
    assert events == ["override_set", "override_expired"]
    set_event = next(e for e in audit(caplog) if e["event"] == "override_set")
    assert set_event["reason"] == ARGS["reason"] and set_event["approved_by"] == "bob"


async def test_single_user_mode_and_clear(api: Any, clock: Clock, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    async with Client(server(**ON, mcp_override_two_person=False)) as c:
        prev = (await c.call_tool("demo_set_scope", ARGS)).structured_content
        assert "TWO-PERSON" not in prev["how_to_apply"]
        done = (await c.call_tool("demo_set_scope", {**ARGS, "confirm": prev["confirm"]})).structured_content
        assert "approved_by" not in done["result"]
        # replacing shows what it replaces
        again = (await c.call_tool("demo_set_scope", {**ARGS, "until": "1h"})).structured_content
        assert again["preview"]["replaces"]["values"] == {"managed": ["lab-x"]}
        clr = (await c.call_tool("demo_clear_scope", {})).structured_content
        assert clr["status"] == "preview" and clr["preview"]["override"]["scope"] == "lab_scope"
        await c.call_tool("demo_clear_scope", {"confirm": clr["confirm"]})
        assert (await c.call_tool("demo_effective_scope", {})).structured_content == {"managed": ["mcp-*"]}
        with pytest.raises(ToolError, match="nothing to clear"):
            await c.call_tool("demo_clear_scope", {})
        # the token from before the clear is refused: the scope changed under it
        with pytest.raises(ToolError, match="changed since the preview"):
            await c.call_tool("demo_set_scope", {**ARGS, "until": "1h", "confirm": again["confirm"]})
    cleared = [e for e in audit(caplog) if e["event"] == "override_cleared"]
    assert cleared and cleared[0]["cleared_by"] == "local"


async def test_blockers(api: Any, clock: Clock) -> None:
    s = server(**ON, mcp_override_max_minutes=60)
    bad = {"managed": ["*"], "until": "2h", "reason": "x"}
    prev = await call_tool(s, "demo_set_scope", bad)
    text = " | ".join(prev["blockers"])
    assert prev["status"] == "blocked"
    assert (
        "'*' would manage every lab" in text
        and "more than 60 minutes" in text
        and "reason is required" in text
    )


async def test_store_without_build_server(clock: Clock) -> None:
    store = OverrideStore(enabled=True)
    assert store.describe() == {"enabled": True, "max_minutes": 240, "two_person": True, "active": []}
    assert store.get("x") is None
