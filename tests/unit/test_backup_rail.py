"""C73: MCP_REQUIRE_BACKUP_HOURS: covered previews show the newest backup's age and block when it is too old
or unknown; the C70 override (<prefix>_waive_backup_check) lifts the blocker for a while."""

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

from stutters_mcp_core import Confirm, Plan, ToolRegistrar, build_server, get_client, preview_or_apply
from stutters_mcp_core import backup_rail as br_mod
from stutters_mcp_core import overrides as ov_mod
from stutters_mcp_core.backup_rail import BackupAge, BackupRail, format_age, register_backup_age
from stutters_mcp_core.testing import as_caller, assert_tool_contract, call_tool

from . import demo
from .demo import BASE, make_settings

T0 = datetime(2026, 10, 28, 3, 0, tzinfo=UTC)
PROD = "https://prod.demo.test"
RWD = {"mcp_enable_writes": True, "mcp_enable_dangerous": True}


class Hook:
    """A fake backup_age hook: `mode` is fresh, stale, none or raise."""

    def __init__(self) -> None:
        self.mode = "fresh"
        self.calls = 0

    async def __call__(self, ctx: Context) -> BackupAge:
        self.calls += 1
        if self.mode == "raise":
            raise RuntimeError("backup list failed: password=hunter2")
        if self.mode == "none":
            return BackupAge(None, "demo backups", "no backup found")
        hours = 2 if self.mode == "fresh" else 75
        return BackupAge.from_time(T0 - timedelta(hours=hours), "demo backups", "nightly", now=T0)


@pytest.fixture
def api() -> Any:
    with respx.mock(assert_all_called=False) as mock:
        for base in (BASE, PROD):
            mock.post(f"{base}/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
            mock.get(f"{base}/api/version").mock(return_value=httpx.Response(200, json={"version": "1"}))
            mock.delete(f"{base}/api/items").mock(return_value=httpx.Response(200, json={"deleted": 3}))
            mock.post(f"{base}/api/items").mock(return_value=httpx.Response(201, json={"id": "9"}))
            mock.put(f"{base}/api/ntp").mock(return_value=httpx.Response(200, json={"ok": True}))
        yield mock


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(ov_mod, "_now", lambda: T0)
    monkeypatch.setattr(br_mod, "_now", lambda: T0)


def register_noop(reg: ToolRegistrar) -> None:
    @reg.dangerous(title="Set NTP")
    async def demo_set_ntp(
        ctx: Context,
        servers: Annotated[list[str], Field(description="NTP servers, in order")],
        confirm: Confirm = None,
    ) -> dict[str, Any]:
        """Set the appliance's NTP servers (no change when they already match 192.0.2.1)."""
        app: demo.DemoApp = get_client(ctx)
        same = servers == ["192.0.2.1"]
        return await preview_or_apply(
            ctx,
            tool="demo_set_ntp",
            args={"servers": servers},
            confirm=confirm,
            plan=Plan("Set NTP", noop=same, blast_radius=1),
            apply=lambda: app.api.put("/api/ntp", {"servers": servers}),
        )


def server(hook: Hook | None = None, **kw: Any) -> FastMCP[Any]:
    def reg_hook(reg: ToolRegistrar) -> None:
        if hook is not None:
            register_backup_age(reg, hook)

    return build_server(
        demo.SPEC,
        make_settings(**{**RWD, **kw}),
        client_factory=demo.DemoApp,
        register=[demo.register, register_noop, reg_hook],
        status=demo.DemoApp.status,
    )


def audit(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "stutters_mcp_core.audit"]


# --- helpers ------------------------------------------------------------------------------


def test_from_time_and_format() -> None:
    assert BackupAge.from_time(T0 - timedelta(hours=3), "s", now=T0).seconds == 3 * 3600
    naive = BackupAge.from_time(datetime(2026, 10, 28, 1, 0), "s", now=T0)
    assert naive.seconds == 7200 and naive.taken_at == "2026-10-28T01:00:00+00:00"
    ms = int((T0 - timedelta(days=2)).timestamp() * 1000)  # epoch milliseconds, as Cisco APIs return
    assert BackupAge.from_time(ms, "s", now=T0).seconds == 2 * 86400
    assert BackupAge.from_time(str(ms), "s", now=T0).seconds == 2 * 86400
    assert BackupAge.from_time("2026-10-27T03:00:00Z", "s", now=T0).seconds == 86400
    assert BackupAge.from_time(None, "s").seconds is None
    bad = BackupAge.from_time("last tuesday", "s")
    assert bad.seconds is None and "unreadable" in (bad.detail or "")
    assert [format_age(s) for s in (5, 40 * 60, 5 * 3600 + 720, 3 * 86400 + 4 * 3600)] == [
        "<1m",
        "40m",
        "5h 12m",
        "3d 4h",
    ]


def test_rail_settings() -> None:
    rail = BackupRail.from_settings(make_settings(mcp_require_backup_hours=24))
    assert rail.hours == 24 and rail.covers("demo_wipe", "dangerous")
    assert not rail.covers("demo_create_item", "write")  # write tier unaffected by default
    assert not rail.covers("demo_list_items", "read")
    assert rail.covers("demo_unknown", None)  # unknown tier: fail closed
    assert not rail.covers("demo_waive_backup_check", "write")
    picked = BackupRail.from_settings(make_settings(mcp_require_backup_tools="demo_create_*"))
    assert picked.covers("demo_create_item", "write") and not picked.covers("demo_wipe", "dangerous")
    assert rail.limit_for(make_settings(require_backup_hours=72)) == 72
    assert rail.limit_for(make_settings(require_backup_hours=0)) is None  # 0 = off for this appliance
    with pytest.raises(ValueError, match="MCP_REQUIRE_BACKUP_TOOLS"):
        make_settings(mcp_require_backup_tools="@nope")
    assert make_settings(mcp_require_backup_hours="").mcp_require_backup_hours is None


# --- previews -----------------------------------------------------------------------------


async def test_off_by_default(api: Any) -> None:
    hook = Hook()
    prev = await call_tool(server(hook), "demo_wipe")
    assert prev["status"] == "preview" and "backup" not in prev and hook.calls == 0
    status = await call_tool(server(hook), "demo_status")
    assert status["server"]["rails"]["backup"] == {
        "required": False,
        "max_age_hours": None,
        "tools": ["@dangerous"],
        "hook": True,
    }


async def test_fresh_backup_allows_and_applies(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    hook = Hook()
    mcp = server(hook, mcp_require_backup_hours=24)
    async with Client(mcp) as c:
        prev = (await c.call_tool("demo_wipe", {})).structured_content
        assert prev["status"] == "preview" and prev["confirm"]
        assert prev["backup"] == {
            "max_age_hours": 24,
            "source": "demo backups",
            "detail": "nightly",
            "taken_at": "2026-10-28T01:00:00+00:00",
            "age_hours": 2.0,
            "age": "2h 0m",
            "ok": True,
        }
        done = (await c.call_tool("demo_wipe", {"confirm": prev["confirm"]})).structured_content
    assert done["status"] == "applied" and hook.calls == 2  # re-checked at confirm
    events = [e for e in audit(caplog) if e["event"] != "tool_call"]
    assert [e["event"] for e in events] == ["preview", "applied"] and events[0]["backup"]["ok"] is True


async def test_stale_backup_blocks(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    hook = Hook()
    hook.mode = "stale"
    prev = await call_tool(server(hook, mcp_require_backup_hours=24), "demo_wipe")
    assert prev["status"] == "blocked" and "confirm" not in prev
    assert prev["backup"]["age"] == "3d 3h" and prev["backup"]["ok"] is False
    assert "is 3d 3h old, more than the 24 h MCP_REQUIRE_BACKUP_HOURS" in prev["blockers"][0]
    assert "demo_waive_backup_check" in prev["blockers"][0]
    assert [e["event"] for e in audit(caplog) if e["event"] != "tool_call"] == ["blocked"]


async def test_backup_going_stale_refuses_the_confirm(api: Any) -> None:
    hook = Hook()
    async with Client(server(hook, mcp_require_backup_hours=24)) as c:
        prev = (await c.call_tool("demo_wipe", {})).structured_content
        hook.mode = "stale"
        with pytest.raises(ToolError, match="MCP_REQUIRE_BACKUP_HOURS"):
            await c.call_tool("demo_wipe", {"confirm": prev["confirm"]})
    assert not any(call.request.method == "DELETE" for call in api.calls)


@pytest.mark.parametrize(
    ("mode", "why"),
    [
        ("none", "demo backups: no backup found"),
        ("raise", "the backup-age check failed: backup list failed"),
        ("nohook", "this server has no backup-age check"),
    ],
)
async def test_unknown_age_blocks(api: Any, mode: str, why: str) -> None:
    hook = Hook()
    hook.mode = mode
    mcp = server(None if mode == "nohook" else hook, mcp_require_backup_hours=24)
    prev = await call_tool(mcp, "demo_wipe")
    assert prev["status"] == "blocked" and prev["backup"]["age"] == "unknown"
    assert f"the age of the newest backup is unknown ({why}" in prev["blockers"][0]
    assert "hunter2" not in json.dumps(prev)  # hook errors are scrubbed
    if mode == "nohook":
        status = await call_tool(mcp, "demo_status")
        assert status["server"]["rails"]["backup"]["hook"] is False
        assert "no backup-age check" in status["server"]["rails"]["backup"]["note"]


async def test_write_tier_unaffected_and_noop_bypasses(api: Any) -> None:
    hook = Hook()
    hook.mode = "stale"
    mcp = server(hook, mcp_require_backup_hours=24)
    prev = await call_tool(mcp, "demo_create_item", {"name": "x"})
    assert prev["status"] == "preview" and "backup" not in prev
    same = await call_tool(mcp, "demo_set_ntp", {"servers": ["192.0.2.1"]})
    assert same["status"] == "no_change" and hook.calls == 0  # nothing to change: no rail, no hook call
    other = await call_tool(mcp, "demo_set_ntp", {"servers": ["192.0.2.9"]})
    assert other["status"] == "blocked" and hook.calls == 1


async def test_tools_selector(api: Any) -> None:
    hook = Hook()
    hook.mode = "stale"
    mcp = server(hook, mcp_require_backup_hours=24, mcp_require_backup_tools="demo_create_*")
    created = await call_tool(mcp, "demo_create_item", {"name": "x"})
    assert created["status"] == "blocked"  # a write tool named explicitly is covered
    wipe = await call_tool(mcp, "demo_wipe")
    assert wipe["status"] == "preview" and "backup" not in wipe  # not in the selector
    status = await call_tool(mcp, "demo_status")
    assert status["server"]["rails"]["backup"]["tools"] == ["demo_create_*"]


async def test_per_instance_limits(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_PROD_URL", PROD)
    monkeypatch.setenv("DEMO_PROD_USERNAME", "produser")
    monkeypatch.setenv("DEMO_PROD_PASSWORD", "prodpass")
    monkeypatch.setenv("DEMO_PROD_REQUIRE_BACKUP_HOURS", "100")
    hook = Hook()
    hook.mode = "stale"  # 75 h old on both
    mcp = server(hook, mcp_require_backup_hours=24, instances="prod", default_instance="lab")
    async with Client(mcp) as c:
        lab = (await c.call_tool("demo_wipe", {})).structured_content
        prod = (await c.call_tool("demo_wipe", {"instance": "prod"})).structured_content
        lab_status = (await c.call_tool("demo_status", {"instance": "lab"})).structured_content
        prod_status = (await c.call_tool("demo_status", {"instance": "prod"})).structured_content
    assert lab["status"] == "blocked" and lab["backup"]["max_age_hours"] == 24
    assert prod["status"] == "preview" and prod["backup"]["max_age_hours"] == 100
    assert prod_status["server"]["rails"]["backup"]["instance_hours"] == 100
    assert prod_status["server"]["rails"]["backup"]["server_hours"] == 24
    assert "instance_hours" not in lab_status["server"]["rails"]["backup"]


async def test_instance_only_and_zero_turns_off(api: Any) -> None:
    hook = Hook()
    hook.mode = "stale"
    only = await call_tool(server(hook, require_backup_hours=48), "demo_wipe")
    assert only["status"] == "blocked" and only["backup"]["max_age_hours"] == 48
    off = await call_tool(server(hook, mcp_require_backup_hours=24, require_backup_hours=0), "demo_wipe")
    assert off["status"] == "preview" and "backup" not in off


async def test_dry_run_still_shows_the_age(api: Any) -> None:
    prev = await call_tool(server(Hook(), mcp_require_backup_hours=24, mcp_dry_run=True), "demo_wipe")
    assert prev["status"] == "preview" and prev["backup"]["ok"] is True and "confirm" not in prev


# --- waiver (C70 override) ----------------------------------------------------------------


WAIVE = {"tools": ["demo_wipe"], "until": "1h", "reason": "backup server down, change approved CHG-1"}


async def test_waiver_tool_only_with_overrides_and_rail(api: Any) -> None:
    async def names(mcp: FastMCP[Any]) -> set[str]:
        async with Client(mcp) as c:
            return {t.name for t in await c.list_tools()}

    assert "demo_waive_backup_check" not in await names(server(Hook(), mcp_require_backup_hours=24))
    assert "demo_waive_backup_check" not in await names(server(Hook(), mcp_runtime_overrides=True))
    on = server(Hook(), mcp_require_backup_hours=24, mcp_runtime_overrides=True)
    assert "demo_waive_backup_check" in await names(on)
    await assert_tool_contract(on, "demo")


async def test_waiver_lifts_the_blocker(api: Any, clock: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    hook = Hook()
    hook.mode = "stale"
    mcp = server(hook, mcp_require_backup_hours=24, mcp_runtime_overrides=True)
    async with Client(mcp) as c:
        with as_caller("alice"):
            ask = (await c.call_tool("demo_waive_backup_check", WAIVE)).structured_content
            configured = ask["preview"]["configured"]
            assert ask["status"] == "preview" and configured["waived_tools"] == ["demo_wipe"]
            assert configured["max_age_hours"] == 24
        with as_caller("bob"):  # two-person by default
            done = (
                await c.call_tool("demo_waive_backup_check", {**WAIVE, "confirm": ask["confirm"]})
            ).structured_content
        assert done["status"] == "applied" and done["approved_by"] == "bob"
        with as_caller("alice"):
            prev = (await c.call_tool("demo_wipe", {})).structured_content
            assert prev["status"] == "preview" and prev["backup"]["ok"] is False
            assert prev["backup"]["waived"]["reason"] == WAIVE["reason"]
            assert "waiver is active" in prev["backup"]["note"]
            applied = (await c.call_tool("demo_wipe", {"confirm": prev["confirm"]})).structured_content
            assert applied["status"] == "applied"
            other = (await c.call_tool("demo_set_ntp", {"servers": ["192.0.2.9"]})).structured_content
            assert other["status"] == "blocked"  # the waiver names demo_wipe only
            status = (await c.call_tool("demo_status", {})).structured_content
            assert status["server"]["overrides"]["active"][0]["scope"] == "backup"
    events = [e for e in audit(caplog) if e.get("tool") == "demo_wipe" and e["event"] != "tool_call"]
    assert events[0]["backup"]["waived"] == WAIVE["reason"]


async def test_waiver_previews_block_bad_requests(api: Any, clock: Any) -> None:
    mcp = server(Hook(), mcp_require_backup_hours=24, mcp_runtime_overrides=True)
    nomatch = await call_tool(mcp, "demo_waive_backup_check", {**WAIVE, "tools": ["demo_create_*"]})
    assert nomatch["status"] == "blocked" and "matches none of the tools" in nomatch["blockers"][0]
    bad = await call_tool(mcp, "demo_waive_backup_check", {**WAIVE, "tools": ["rm -rf"]})
    assert bad["status"] == "blocked" and "not a tool name or glob" in bad["blockers"][0]
    short = await call_tool(mcp, "demo_waive_backup_check", {**WAIVE, "reason": "x"})
    assert short["status"] == "blocked" and any("reason is required" in b for b in short["blockers"])
    star = await call_tool(mcp, "demo_waive_backup_check", {**WAIVE, "tools": ["*"]})
    assert star["status"] == "preview"
    assert set(star["preview"]["configured"]["waived_tools"]) == {"demo_set_ntp", "demo_wipe"}


async def test_waiver_not_blocked_by_the_rail_it_lifts(api: Any, clock: Any) -> None:
    hook = Hook()
    hook.mode = "stale"
    mcp = server(
        hook,
        mcp_require_backup_hours=24,
        mcp_require_backup_tools="*",
        mcp_runtime_overrides=True,
        mcp_override_two_person=False,
    )
    ask = await call_tool(mcp, "demo_waive_backup_check", WAIVE)
    assert ask["status"] == "preview" and "backup" not in ask


def test_instance_limit_not_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    from stutters_mcp_core.targets import target_settings

    monkeypatch.setenv("DEMO_PROD_URL", PROD)
    base = make_settings(instances="prod", require_backup_hours=12)
    assert target_settings(base, "prod").require_backup_hours is None
