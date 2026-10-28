"""C81: Plan.undo: every write says how to back it out (preview, applied result, `applied` audit event)."""

import json
import logging
import warnings
from typing import Annotated, Any

import httpx
import pytest
import respx
from fastmcp import Client, Context, FastMCP
from pydantic import Field

from stutters_mcp_core import (
    Confirm,
    FromResult,
    Plan,
    ToolRegistrar,
    Undo,
    build_server,
    get_client,
    preview_or_apply,
)
from stutters_mcp_core.formatting import MASK
from stutters_mcp_core.testing import UndoWarning, assert_tool_contract, call_tool, undo_problems
from stutters_mcp_core.writes import undo_applied, undo_preview_text

from .demo import BASE, SPEC, DemoApp, DemoSettings, make_settings

RWD = {"mcp_enable_writes": True, "mcp_enable_dangerous": True}
OLD_SECRET = "0ld-Sh4red-s3cret"
NEW_SECRET = "n3w-Sh4red-s3cret"


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        mock.get("/api/version").mock(return_value=httpx.Response(200, json={"version": "1"}))
        mock.post("/api/rules").mock(
            return_value=httpx.Response(201, json={"id": "r-42", "name": "mcp-allow-dns"})
        )
        mock.delete("/api/rules/r-42").mock(return_value=httpx.Response(204))
        mock.get("/api/nads/sw1").mock(
            return_value=httpx.Response(200, json={"name": "sw1", "shared_secret": OLD_SECRET})
        )
        mock.put("/api/nads/sw1").mock(return_value=httpx.Response(200, json={"name": "sw1"}))
        mock.delete("/api/logs").mock(return_value=httpx.Response(204))
        yield mock


def register_undo(reg: ToolRegistrar) -> None:
    @reg.write(title="Create rule")
    async def demo_undo_create_rule(
        ctx: Context,
        name: Annotated[str, Field(description="Rule name")],
        policy: Annotated[str, Field(description="Access policy the rule goes in")] = "lab",
        confirm: Confirm = None,
    ) -> dict[str, Any]:
        """Create an access rule (its back-out step deletes it by the new id)."""
        app: DemoApp = get_client(ctx)
        plan = Plan(
            f"Create rule {name} in {policy}",
            blast_radius=1,
            undo=Undo("demo_undo_delete_rule", {"policy": policy, "rule": FromResult("id")}),
        )
        return await preview_or_apply(
            ctx,
            tool="demo_undo_create_rule",
            args={"name": name, "policy": policy},
            confirm=confirm,
            plan=plan,
            apply=lambda: app.api.post("/api/rules", {"name": name}),
        )

    @reg.write(title="Delete rule")
    async def demo_undo_delete_rule(
        ctx: Context,
        rule: Annotated[str, Field(description="Rule id")],
        policy: Annotated[str, Field(description="Access policy the rule is in")] = "lab",
        confirm: Confirm = None,
    ) -> dict[str, Any]:
        """Delete an access rule by id (recreating it is a manual step)."""
        app: DemoApp = get_client(ctx)
        plan = Plan(f"Delete rule {rule}", blast_radius=1, undo="recreate the rule from its export")
        return await preview_or_apply(
            ctx,
            tool="demo_undo_delete_rule",
            args={"rule": rule, "policy": policy},
            confirm=confirm,
            plan=plan,
            apply=lambda: app.api.delete(f"/api/rules/{rule}"),
        )

    @reg.write(title="Set NAD secret", redact={"lines": True})
    async def demo_undo_set_secret(
        ctx: Context,
        nad: Annotated[str, Field(description="Network device name")],
        shared_secret: Annotated[str, Field(description="New RADIUS shared secret")],
        lines: Annotated[str | None, Field(description="Extra config lines (masked in logs)")] = None,
        confirm: Confirm = None,
    ) -> dict[str, Any]:
        """Change a NAD's RADIUS shared secret (the back-out step restores the old one)."""
        app: DemoApp = get_client(ctx)
        current = await app.api.get(f"/api/nads/{nad}")
        old = current["shared_secret"]
        plan = Plan(
            f"Set the shared secret of {nad}",
            fingerprint=old,
            blast_radius=1,
            undo=Undo(
                "demo_undo_set_secret",
                lambda result: {"nad": nad, "shared_secret": old, "lines": f"key {old}"},
            ),
        )
        return await preview_or_apply(
            ctx,
            tool="demo_undo_set_secret",
            args={"nad": nad, "shared_secret": shared_secret, "lines": lines},
            confirm=confirm,
            plan=plan,
            apply=lambda: app.api.put(f"/api/nads/{nad}", {"shared_secret": shared_secret}),
        )

    @reg.dangerous(title="Clear logs")
    async def demo_undo_clear_logs(
        ctx: Context,
        blocked: Annotated[bool, Field(description="Simulate a blocker")] = False,
        already: Annotated[bool, Field(description="Simulate an already-clear log")] = False,
        confirm: Confirm = None,
    ) -> dict[str, Any]:
        """Clear the appliance logs (not reversible), or a blocked / no-op variant for the tests."""
        app: DemoApp = get_client(ctx)
        plan = Plan(
            "Clear the logs",
            blast_radius=1,
            blockers=["the log is locked"] if blocked else [],
            noop=already,
            undo="not reversible: cleared logs are gone",
        )
        return await preview_or_apply(
            ctx,
            tool="demo_undo_clear_logs",
            args={"blocked": blocked, "already": already},
            confirm=confirm,
            plan=plan,
            apply=lambda: app.api.delete("/api/logs"),
        )

    @reg.write(title="Rename without undo")
    async def demo_undo_missing(
        ctx: Context,
        name: Annotated[str, Field(description="New name")],
        confirm: Confirm = None,
    ) -> dict[str, Any]:
        """A write tool that gives no back-out step (the contract warns about it)."""
        return await preview_or_apply(
            ctx,
            tool="demo_undo_missing",
            args={"name": name},
            confirm=confirm,
            plan=Plan(f"Rename to {name}"),
            apply=lambda: _none(),
        )

    @reg.write(title="Create with a bad undo path")
    async def demo_undo_bad_path(ctx: Context, confirm: Confirm = None) -> dict[str, Any]:
        """A write whose undo names a result field the appliance doesn't return."""
        app: DemoApp = get_client(ctx)
        return await preview_or_apply(
            ctx,
            tool="demo_undo_bad_path",
            args={},
            confirm=confirm,
            plan=Plan("Create a rule", undo=Undo("demo_undo_delete_rule", {"rule": FromResult("uuid")})),
            apply=lambda: app.api.post("/api/rules", {"name": "x"}),
        )


async def _none() -> None:
    return None


def server(**kw: Any) -> FastMCP[Any]:
    settings: DemoSettings = make_settings(**{**RWD, **kw})
    return build_server(SPEC, settings, client_factory=DemoApp, register=[register_undo])


def audit_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    out = [json.loads(r.getMessage()) for r in caplog.records if r.name == "stutters_mcp_core.audit"]
    return [e for e in out if e["event"] != "tool_call"]


async def preview_then_apply(c: Client[Any], tool: str, args: dict[str, Any]) -> tuple[Any, Any]:
    prev = (await c.call_tool(tool, args)).structured_content
    applied = (await c.call_tool(tool, {**args, "confirm": prev["confirm"]})).structured_content
    return prev, applied


# --- Undo / Plan ---------------------------------------------------------------------------------


def test_plan_undo_validation() -> None:
    assert Plan("x").undo is None
    assert Plan("x", undo="not reversible: gone").undo == "not reversible: gone"
    assert Plan("x", undo=Undo("demo_delete", {"id": "1"})).undo == Undo("demo_delete", {"id": "1"})
    with pytest.raises(ValueError, match="undo text"):
        Plan("x", undo="  ")
    with pytest.raises(ValueError, match="undo must be"):
        Plan("x", undo={"tool": "demo_delete"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="snake_case"):
        Undo("Demo Delete")
    with pytest.raises(ValueError, match="confirm"):
        Undo("demo_delete", {"id": "1", "confirm": "pv_x"})
    with pytest.raises(ValueError, match="mapping or a function"):
        Undo("demo_delete", ["id"])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="path"):
        FromResult(" ")


def test_undo_resolve_from_result() -> None:
    undo = Undo("demo_delete", {"id": FromResult("items.0.id"), "keep": True})
    assert undo.resolve({"items": [{"id": "a1"}]}) == {"id": "a1", "keep": True}
    with pytest.raises(LookupError, match="items.0.id"):
        undo.resolve({"items": []})
    fn = Undo("demo_delete", lambda r: {"id": r["id"]})
    assert fn.resolve({"id": "b2"}) == {"id": "b2"}


def test_undo_texts() -> None:
    undo = Undo("demo_delete_rule", {"policy": "lab", "rule": FromResult("id"), "password": "pw"})
    assert undo_preview_text(undo) == (
        f'back-out: demo_delete_rule(policy="lab", rule=<from the result: id>, password="{MASK}")'
    )
    assert undo_preview_text(Undo("demo_x", lambda r: {})) == "back-out: demo_x(<arguments from the result>)"
    assert undo_preview_text("not reversible: gone ") == "back-out: not reversible: gone"
    applied = undo_applied(undo, {"id": "r-1"})
    assert applied == {
        "tool": "demo_delete_rule",
        "args": {"policy": "lab", "rule": "r-1", "password": MASK},
        "back_out": f'demo_delete_rule(policy="lab", rule="r-1", password="{MASK}")',
    }
    assert undo_applied("manual step", None) == {"back_out": "manual step"}


# --- preview_or_apply ----------------------------------------------------------------------------


async def test_create_names_the_new_id(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    async with Client(server()) as c:
        prev, applied = await preview_then_apply(c, "demo_undo_create_rule", {"name": "mcp-allow-dns"})
    assert prev["status"] == "preview"
    assert prev["undo"] == 'back-out: demo_undo_delete_rule(policy="lab", rule=<from the result: id>)'
    assert applied["status"] == "applied"
    assert applied["undo"] == {
        "tool": "demo_undo_delete_rule",
        "args": {"policy": "lab", "rule": "r-42"},
        "back_out": 'demo_undo_delete_rule(policy="lab", rule="r-42")',
    }
    events = audit_events(caplog)
    assert [e["event"] for e in events] == ["preview", "applied"]
    assert "undo" not in events[0]
    assert events[1]["undo"] == {"tool": "demo_undo_delete_rule", "args": {"policy": "lab", "rule": "r-42"}}


async def test_running_the_undo_reverses_the_create(api: Any) -> None:
    async with Client(server()) as c:
        _, applied = await preview_then_apply(c, "demo_undo_create_rule", {"name": "mcp-allow-dns"})
        undo = applied["undo"]
        _, back = await preview_then_apply(c, undo["tool"], undo["args"])
    assert back["status"] == "applied"
    assert back["undo"] == {"back_out": "recreate the rule from its export"}
    assert api.calls[-1].request.method == "DELETE" and api.calls[-1].request.url.path == "/api/rules/r-42"


async def test_undo_secrets_are_masked(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    async with Client(server()) as c:
        prev, applied = await preview_then_apply(
            c, "demo_undo_set_secret", {"nad": "sw1", "shared_secret": NEW_SECRET}
        )
    assert prev["undo"] == "back-out: demo_undo_set_secret(<arguments from the result>)"
    assert applied["undo"]["args"] == {"nad": "sw1", "shared_secret": MASK, "lines": MASK}
    assert OLD_SECRET not in json.dumps(applied)
    logged = [r.getMessage() for r in caplog.records if r.name == "stutters_mcp_core.audit"]
    assert not any(OLD_SECRET in m or NEW_SECRET in m for m in logged)
    event = next(e for e in audit_events(caplog) if e["event"] == "applied")
    assert event["undo"]["args"]["shared_secret"] == MASK


async def test_not_reversible_text(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    async with Client(server()) as c:
        prev, applied = await preview_then_apply(c, "demo_undo_clear_logs", {})
    assert prev["undo"] == "back-out: not reversible: cleared logs are gone"
    assert applied["undo"] == {"back_out": "not reversible: cleared logs are gone"}
    event = next(e for e in audit_events(caplog) if e["event"] == "applied")
    assert event["undo"] == applied["undo"]


async def test_noop_and_blocked_carry_no_undo(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    s = server()
    noop = await call_tool(s, "demo_undo_clear_logs", {"already": True})
    blocked = await call_tool(s, "demo_undo_clear_logs", {"blocked": True})
    assert noop["status"] == "no_change" and "undo" not in noop
    assert blocked["status"] == "blocked" and "undo" not in blocked
    assert [e["event"] for e in audit_events(caplog)] == ["noop", "blocked"]
    assert not any("undo" in e for e in audit_events(caplog))


async def test_dry_run_preview_shows_the_back_out(api: Any) -> None:
    out = await call_tool(server(mcp_dry_run=True), "demo_undo_create_rule", {"name": "mcp-x"})
    assert "confirm" not in out
    assert out["undo"].startswith("back-out: demo_undo_delete_rule(")


async def test_unfillable_undo_does_not_fail_the_apply(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    async with Client(server()) as c:
        _, applied = await preview_then_apply(c, "demo_undo_bad_path", {})
    assert applied["status"] == "applied"
    assert applied["undo"]["tool"] == "demo_undo_delete_rule" and "args" not in applied["undo"]
    assert "could not be filled" in applied["undo"]["back_out"]


async def test_plain_write_has_no_undo(api: Any) -> None:
    async with Client(server()) as c:
        prev, applied = await preview_then_apply(c, "demo_undo_missing", {"name": "x"})
    assert "undo" not in prev and "undo" not in applied


# --- contract warning ----------------------------------------------------------------------------


def test_undo_problems_lists_tools_without_undo() -> None:
    s = server()
    missing = undo_problems(s, "demo")
    assert "demo_undo_missing" in missing
    assert not {"demo_undo_create_rule", "demo_undo_set_secret", "demo_undo_clear_logs"} & set(missing)
    assert "demo_undo_missing" not in undo_problems(s, "demo", allow={"demo_undo_missing"})


async def test_assert_tool_contract_warns_but_passes() -> None:
    s = server()
    with pytest.warns(UndoWarning, match="demo_undo_missing"):
        await assert_tool_contract(s, "demo")
    with warnings.catch_warnings():
        warnings.simplefilter("error", UndoWarning)
        await assert_tool_contract(s, "demo", undo_allow={"demo_undo_missing"})
        await assert_tool_contract(s, "demo", warn_undo=False)


async def test_a_previewed_undo_counts_for_the_warning(api: Any) -> None:
    """A Plan built in a helper (not in the tool's own source) still counts once it was previewed."""
    from stutters_mcp_core.writes import _undo_tools

    s = server()
    _undo_tools.discard("demo_undo_missing")
    assert "demo_undo_missing" in undo_problems(s, "demo")
    _undo_tools.add("demo_undo_missing")
    try:
        assert "demo_undo_missing" not in undo_problems(s, "demo")
    finally:
        _undo_tools.discard("demo_undo_missing")
