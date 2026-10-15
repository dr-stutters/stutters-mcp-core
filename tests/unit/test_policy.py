"""Safety rails (G7): dry-run, change windows, deny/allow lists, blast radius, two-person confirm."""

import json
import logging
from datetime import datetime
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from stutters_mcp_core import policy as policy_mod
from stutters_mcp_core.formatting import MASK
from stutters_mcp_core.policy import ChangeWindow, SafetyPolicy, ToolSelector
from stutters_mcp_core.testing import as_caller, call_tool

from .demo import BASE, create_server, make_settings

RW = {"mcp_enable_writes": True}
RWD = {"mcp_enable_writes": True, "mcp_enable_dangerous": True}


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        mock.get("/api/version").mock(return_value=httpx.Response(200, json={"version": "1.2.3"}))
        mock.post("/api/items").mock(return_value=httpx.Response(201, json={"id": "1"}))
        mock.delete("/api/items").mock(return_value=httpx.Response(204))
        yield mock


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def audit_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "stutters_mcp_core.audit"]


# --- parsing ------------------------------------------------------------------------------


def test_selector_globs_and_tiers() -> None:
    sel = ToolSelector.parse("*_delete_*, demo_wipe ,@dangerous", setting="X")
    assert sel.matches("ise_delete_nad", "write")
    assert sel.matches("demo_wipe", None)
    assert sel.matches("fmc_deploy", "dangerous")
    assert not sel.matches("fmc_list_devices", "read")
    assert sel.describe() == ["*_delete_*", "demo_wipe", "@dangerous"]
    with pytest.raises(ValueError, match="unknown tier"):
        ToolSelector.parse("@admin", setting="X")
    with pytest.raises(ValueError, match="not a tool name"):
        ToolSelector.parse("rm -rf /", setting="X")


def test_weekly_window_across_midnight_in_a_zone() -> None:
    w = ChangeWindow.parse("Mon-Fri 22:00-02:00 Australia/Sydney")
    # Friday 23:30 Sydney (AEDT, +11) is inside; Saturday 01:30 too (window opened Friday).
    assert w.state(at("2026-10-16T12:30:00+00:00"))["open"] is True
    sat = w.state(at("2026-10-16T14:30:00+00:00"))
    assert sat["open"] is True and sat["closes_at"] == "2026-10-17T02:00+11:00"
    # Saturday 22:30 is not: no window opens on Saturday. Next is Monday 22:00.
    closed = w.state(at("2026-10-17T11:30:00+00:00"))
    assert closed == {
        "window": "Mon-Fri 22:00-02:00 Australia/Sydney",
        "open": False,
        "next_open": "2026-10-19T22:00+11:00",
    }


def test_window_lists_and_24h() -> None:
    w = ChangeWindow.parse("Sat,Sun 00:00-00:00; daily 12:00-13:00")
    assert w.state(at("2026-10-17T09:00:00+00:00"))["open"] is True  # Saturday UTC
    assert w.state(at("2026-10-15T12:30:00+00:00"))["open"] is True  # Thursday lunch
    assert w.state(at("2026-10-15T14:00:00+00:00"))["next_open"] == "2026-10-16T12:00+00:00"


def test_cron_window() -> None:
    w = ChangeWindow.parse("0 22 * * 1-5 4h Australia/Sydney")
    assert w.state(at("2026-10-16T12:30:00+00:00"))["open"] is True
    assert w.state(at("2026-10-17T11:30:00+00:00"))["next_open"] == "2026-10-19T22:00+11:00"
    sunday = ChangeWindow.parse("30 1 * * 7 1h")  # 7 = Sunday
    assert sunday.state(at("2026-10-18T01:45:00+00:00"))["open"] is True
    assert ChangeWindow.parse("*/15 * * * * 5m").state(at("2026-10-15T10:31:00+00:00"))["open"] is True


@pytest.mark.parametrize(
    "spec, error",
    [
        ("Mon-Fri 22:00", "time range"),
        ("Funday 10:00-11:00", "bad day"),
        ("Mon 25:00-26:00", "bad time"),
        ("Mon 10:00-11:00 Mars/Olympus", "time zone"),
        ("0 22 * * 1-5 9d", "at most 7d"),
        ("0 99 * * * 1h", "out of range"),
        ("whenever", "windows look like"),
    ],
)
def test_bad_windows_fail_settings(spec: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        make_settings(mcp_change_window=spec)


def test_blast_limits_exact_before_glob() -> None:
    p = SafetyPolicy.from_settings(
        make_settings(mcp_max_blast_radius=20, mcp_blast_radius_overrides="fmc_*=50,fmc_deploy=5")
    )
    assert p.blast_limit("fmc_deploy") == 5
    assert p.blast_limit("fmc_create_object") == 50
    assert p.blast_limit("ise_x") == 20
    with pytest.raises(ValueError, match="tool_or_glob=N"):
        make_settings(mcp_blast_radius_overrides="fmc_deploy:5")


def test_default_settings_have_no_active_policy() -> None:
    assert not SafetyPolicy.from_settings(make_settings()).active
    assert not SafetyPolicy.from_settings(make_settings(mcp_change_window="", mcp_deny_tools=" ")).active


# --- behaviour through the server ---------------------------------------------------------


async def test_no_rails_preview_is_unchanged(api: Any) -> None:
    out = await call_tool(create_server(make_settings(**RW)), "demo_create_item", {"name": "r1"})
    assert set(out) == {"status", "action", "preview", "confirm", "expires_in_seconds", "how_to_apply"}


async def test_dry_run_previews_but_never_applies(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    server = create_server(make_settings(mcp_dry_run=True, **RW))
    async with Client(server) as c:
        prev = (await c.call_tool("demo_create_item", {"name": "r1"})).structured_content
        assert prev["status"] == "preview" and "confirm" not in prev
        assert prev["policy"] == {"dry_run": True} and "Dry-run" in prev["note"]
        with pytest.raises(ToolError, match="dry-run mode"):
            await c.call_tool("demo_create_item", {"name": "r1", "confirm": "pv_forged"})
        status = (await c.call_tool("demo_status", {})).structured_content
    assert api.routes[2].call_count == 0
    assert status["server"]["safety"] == {"dry_run": True}
    assert [e["event"] for e in audit_events(caplog) if e["event"] != "tool_call"] == ["preview", "refused"]


async def test_change_window(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    server = create_server(make_settings(mcp_change_window="Mon-Fri 22:00-02:00 UTC", **RW))
    monkeypatch.setattr(policy_mod, "_now", lambda: at("2026-10-17T12:00:00+00:00"))  # Saturday
    async with Client(server) as c:
        prev = (await c.call_tool("demo_create_item", {"name": "r1"})).structured_content
        assert prev["policy"]["change_window"]["open"] is False
        assert "outside the change window" in prev["warning"]
        with pytest.raises(ToolError, match="outside the change window.*next opens 2026-10-19T22:00"):
            await c.call_tool("demo_create_item", {"name": "r1", "confirm": prev["confirm"]})
        monkeypatch.setattr(policy_mod, "_now", lambda: at("2026-10-19T23:00:00+00:00"))  # Monday night
        applied = (
            await c.call_tool("demo_create_item", {"name": "r1", "confirm": prev["confirm"]})
        ).structured_content
    assert applied["status"] == "applied"


async def test_deny_and_allow_lists_withhold_tools(api: Any) -> None:
    async def names(**kw: Any) -> set[str]:
        async with Client(create_server(make_settings(**RWD, **kw))) as c:
            return {t.name for t in await c.list_tools()}

    denied = await names(mcp_deny_tools="@dangerous,demo_create_*")
    assert "demo_wipe" not in denied and "demo_create_item" not in denied
    assert "demo_list_items" in denied
    allowed = await names(mcp_allow_tools="demo_list_*")
    assert allowed == {"demo_list_items", "demo_status"}  # status is always there
    status = await call_tool(
        create_server(make_settings(**RWD, mcp_allow_tools="demo_list_*")), "demo_status"
    )
    assert "demo_wipe" in status["server"]["safety"]["withheld_tools"]


async def test_blast_radius_blocks_over_limit(api: Any) -> None:
    server = create_server(make_settings(mcp_max_blast_radius=2, **RWD))
    prev = await call_tool(server, "demo_wipe")
    assert prev["status"] == "blocked" and "confirm" not in prev
    assert prev["blast_radius"] == 3
    assert prev["policy"] == {"blast_radius": 3, "max_blast_radius": 2}
    assert "exceeds the limit of 2" in prev["blockers"][0]
    ok = await call_tool(
        create_server(make_settings(mcp_max_blast_radius=2, mcp_blast_radius_overrides="demo_wipe=3", **RWD)),
        "demo_wipe",
    )
    assert ok["status"] == "preview" and ok["policy"]["max_blast_radius"] == 3


async def test_blast_radius_shown_without_rails(api: Any) -> None:
    prev = await call_tool(create_server(make_settings(**RWD)), "demo_wipe")
    assert prev["blast_radius"] == 3 and "policy" not in prev


async def test_two_person_confirm(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    server = create_server(make_settings(mcp_two_person_tools="@dangerous", **RWD))
    async with Client(server) as c:
        with as_caller("alice"):
            prev = (await c.call_tool("demo_wipe", {})).structured_content
            assert prev["policy"]["two_person"] is True and "TWO-PERSON" in prev["how_to_apply"]
            token = prev["confirm"]
            with pytest.raises(ToolError, match="different user than 'alice'"):
                await c.call_tool("demo_wipe", {"confirm": token})
        with as_caller("bob"):
            review = (await c.call_tool("demo_review_change", {"token": token})).structured_content
            assert review["tool"] == "demo_wipe" and review["requested_by"] == "alice"
            assert review["action"] == "Delete ALL items" and review["args"] == {}
            applied = (await c.call_tool("demo_wipe", {"confirm": token})).structured_content
        # write-tier tools are not covered by @dangerous: same-user confirm still works
        with as_caller("alice"):
            t2 = (await c.call_tool("demo_create_item", {"name": "x"})).structured_content["confirm"]
            await c.call_tool("demo_create_item", {"name": "x", "confirm": t2})
    assert applied["status"] == "applied"
    assert applied["requested_by"] == "alice" and applied["approved_by"] == "bob"
    done = [e for e in audit_events(caplog) if e["event"] == "applied" and e["tool"] == "demo_wipe"]
    assert done[0]["requested_by"] == "alice" and done[0]["approved_by"] == "bob"


async def test_review_tool_only_with_two_person(api: Any) -> None:
    async with Client(create_server(make_settings(**RWD))) as c:
        assert "demo_review_change" not in {t.name for t in await c.list_tools()}
    server = create_server(make_settings(mcp_two_person_tools="demo_wipe", **RWD))
    with pytest.raises(ToolError, match="Unknown or expired two-person token"):
        await call_tool(server, "demo_review_change", {"token": "pv_nope"})


async def test_review_masks_secret_args(api: Any) -> None:
    from stutters_mcp_core.writes import _shown_args

    assert _shown_args({"name": "n", "password": "hunter2"}) == {"name": "n", "password": MASK}


async def test_contract_still_passes_with_rails(api: Any) -> None:
    from stutters_mcp_core.testing import assert_tool_contract

    settings = make_settings(mcp_two_person_tools="@dangerous", mcp_dry_run=True, **RWD)
    await assert_tool_contract(create_server(settings), prefix="demo")
