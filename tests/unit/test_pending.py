"""Pending two-person changes (C66): <prefix>_list_pending_changes and <prefix>_cancel_pending_change."""

import json
import logging
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from stutters_mcp_core.testing import as_caller
from stutters_mcp_core.writes import PreviewStore

from .demo import BASE, create_server, make_settings

RWD = {"mcp_enable_writes": True, "mcp_enable_dangerous": True}
TWO = {"mcp_two_person_tools": "@dangerous", **RWD}


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        mock.get("/api/version").mock(return_value=httpx.Response(200, json={"version": "1.2.3"}))
        mock.post("/api/items").mock(return_value=httpx.Response(201, json={"id": "1"}))
        mock.delete("/api/items").mock(return_value=httpx.Response(204))
        yield mock


def audit_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "stutters_mcp_core.audit"]


async def call(c: Client, name: str, args: dict[str, Any] | None = None) -> Any:
    return (await c.call_tool(name, args or {})).structured_content


def test_store_preview_ids_find_and_cancel() -> None:
    store = PreviewStore(ttl=600)
    one = store.issue("demo_wipe", {}, None, "alice", two_person=True)
    two = store.issue("demo_create_item", {"name": "x"}, None, "alice")
    pid = store.preview_id(one)
    assert pid.startswith("pc_") and pid == store.preview_id(one) and one not in pid
    assert [t for t, _ in store.awaiting_approval()] == [one]  # one-person previews are not listed
    assert store.find(pid) == (one, store.peek(one))
    assert store.find(one) == (one, store.peek(one))
    assert store.find("pc_000000000000") is None
    assert store.cancel(one) is not None and store.find(pid) is None
    assert store.cancel(one) is None
    assert store.peek(two) is not None


def test_store_listing_drops_expired() -> None:
    store = PreviewStore(ttl=600)
    token = store.issue("demo_wipe", {}, None, "alice", two_person=True)
    pending = store.peek(token)
    assert pending is not None
    pending.expires = 0
    assert store.awaiting_approval() == []


async def test_tools_only_with_two_person(api: Any) -> None:
    async with Client(create_server(make_settings(**RWD))) as c:
        names = {t.name for t in await c.list_tools()}
    assert "demo_list_pending_changes" not in names and "demo_cancel_pending_change" not in names
    async with Client(create_server(make_settings(**TWO))) as c:
        names = {t.name for t in await c.list_tools()}
    assert {"demo_list_pending_changes", "demo_cancel_pending_change"} <= names
    # a two-person instance rule registers them too, and MCP_ALLOW_TOOLS never withholds them
    async with Client(
        create_server(make_settings(two_person=True, mcp_allow_tools="demo_list_*", **RWD))
    ) as c:
        names = {t.name for t in await c.list_tools()}
    assert {"demo_list_pending_changes", "demo_cancel_pending_change", "demo_review_change"} <= names


async def test_list_review_and_approve_by_preview_id(api: Any) -> None:
    async with Client(create_server(make_settings(**TWO))) as c:
        with as_caller("alice"):
            prev = await call(c, "demo_wipe")
            token, pid = prev["confirm"], prev["preview_id"]
            assert pid.startswith("pc_") and "preview id" in prev["how_to_apply"]
            await call(c, "demo_create_item", {"name": "x"})  # one-person preview: not listed
            mine = await call(c, "demo_list_pending_changes", {"mine": True})
            assert mine["total"] == 1 and mine["results"][0]["you_can_approve"] is False
        with as_caller("bob"):
            listing = await call(c, "demo_list_pending_changes")
            assert listing["total"] == 1
            row = listing["results"][0]
            assert row["preview_id"] == pid and row["tool"] == "demo_wipe"
            assert row["action"] == "Delete ALL items" and row["requested_by"] == "alice"
            assert row["blast_radius"] == 3 and row["you_can_approve"] is True
            assert 0 < row["expires_in_seconds"] <= 600
            assert token not in json.dumps(listing)  # the listing never hands out tokens
            assert (await call(c, "demo_list_pending_changes", {"mine": True}))["total"] == 0
            review = await call(c, "demo_review_change", {"token": pid})
            assert review["preview_id"] == pid and f'confirm="{token}"' in review["how_to_approve"]
            assert "preview_id" not in review.get("preview", {})
            applied = await call(c, "demo_wipe", {"confirm": token})
        assert applied["status"] == "applied" and applied["approved_by"] == "bob"
        empty = await call(c, "demo_list_pending_changes")
        assert empty["total"] == 0 and "Nothing is waiting" in empty["note"]


@pytest.mark.parametrize("canceller, as_who", [("alice", "requester"), ("bob", "approver")])
async def test_cancel_burns_the_token(
    api: Any, caplog: pytest.LogCaptureFixture, canceller: str, as_who: str
) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    async with Client(create_server(make_settings(**TWO))) as c:
        with as_caller("alice"):
            prev = await call(c, "demo_wipe")
        with as_caller(canceller):
            out = await call(
                c, "demo_cancel_pending_change", {"preview_id": prev["preview_id"], "reason": "wrong site"}
            )
            assert out["status"] == "cancelled" and out["cancelled_as"] == as_who
            assert out["requested_by"] == "alice" and out["cancelled_by"] == canceller
            with pytest.raises(ToolError, match="No pending two-person change"):
                await call(c, "demo_cancel_pending_change", {"preview_id": prev["preview_id"]})
        with as_caller("bob"), pytest.raises(ToolError, match="Unknown or expired"):
            await call(c, "demo_wipe", {"confirm": prev["confirm"]})
    api_calls = [r for r in api.calls if r.request.method == "DELETE"]
    assert api_calls == []
    cancelled = [e for e in audit_events(caplog) if e["event"] == "cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["tool"] == "demo_wipe" and cancelled[0]["reason"] == "wrong site"
    assert cancelled[0]["cancelled_by"] == canceller and cancelled[0]["preview_id"] == prev["preview_id"]


async def test_cancel_by_token_and_not_one_person_previews(api: Any) -> None:
    async with Client(create_server(make_settings(**TWO))) as c:
        with as_caller("alice"):
            prev = await call(c, "demo_wipe")
            one = await call(c, "demo_create_item", {"name": "x"})
            with pytest.raises(ToolError, match="No pending two-person change"):
                await call(c, "demo_cancel_pending_change", {"preview_id": one["confirm"]})
            out = await call(c, "demo_cancel_pending_change", {"preview_id": prev["confirm"]})
            assert out["preview_id"] == prev["preview_id"] and "reason" not in out
            # the one-person token still works for its owner
            await call(c, "demo_create_item", {"name": "x", "confirm": one["confirm"]})


async def test_pending_tools_need_write_role(api: Any) -> None:
    server = create_server(make_settings(mcp_roles="alice:admin, bob:rw, ci-*:ro", **TWO))
    async with Client(server) as c:
        with as_caller("ci-x"):
            names = {t.name for t in await c.list_tools()}
            assert "demo_list_pending_changes" not in names and "demo_cancel_pending_change" not in names
            with pytest.raises(ToolError):
                await call(c, "demo_list_pending_changes")
        with as_caller("bob"):
            names = {t.name for t in await c.list_tools()}
            assert {"demo_list_pending_changes", "demo_cancel_pending_change"} <= names
