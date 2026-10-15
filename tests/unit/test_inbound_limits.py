"""C16: inbound per-caller rate limiting."""

import json
import logging
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from stutters_mcp_core.inbound_limits import CallerRateLimitMiddleware, parse_role_limits
from stutters_mcp_core.roles import RoleMap
from stutters_mcp_core.testing import as_caller

from .demo import BASE, create_server, make_settings


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        mock.get("/api/version").mock(return_value=httpx.Response(200, json={"version": "1"}))
        mock.get("/api/items/1").mock(return_value=httpx.Response(200, json={"id": "1"}))
        yield mock


def test_bucket_bursts_then_refills() -> None:
    mw = CallerRateLimitMiddleware("s", "demo", 60, {}, None)  # burst 15, 1/s
    assert all(mw.check("a", now=100.0) == 0 for _ in range(15))
    assert mw.check("a", now=100.0) == pytest.approx(1.0)
    assert mw.check("b", now=100.0) == 0  # other callers unaffected
    assert mw.check("a", now=101.5) == 0


def test_role_limits() -> None:
    roles = RoleMap.parse("ci:ro,ops:admin")
    mw = CallerRateLimitMiddleware("s", "demo", None, parse_role_limits("ro=4"), roles)
    assert mw.limit_for("ci") == 4 and mw.limit_for("ops") is None
    with pytest.raises(ValueError, match="ro=30"):
        parse_role_limits("root=5")


async def test_refusal_message_audit_and_status_exempt(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    server = create_server(make_settings(mcp_caller_rate_limit=4))  # burst 1
    async with Client(server) as c:
        with as_caller("busy"):
            await c.call_tool("demo_get_item", {"item_id": "1"})
            with pytest.raises(ToolError, match="Rate limit for 'busy' reached .4 tool calls per minute"):
                await c.call_tool("demo_get_item", {"item_id": "1"})
            await c.call_tool("demo_status", {})  # never limited
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "stutters_mcp_core.audit"]
    assert any(e["event"] == "rate_limited" and e["caller"] == "busy" for e in events)


async def test_off_by_default(api: Any) -> None:
    async with Client(create_server(make_settings())) as c:
        for _ in range(30):
            await c.call_tool("demo_get_item", {"item_id": "1"})
