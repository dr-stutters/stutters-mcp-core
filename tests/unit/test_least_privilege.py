"""G3: the least-privilege contract helper, against the demo server with a mocked appliance."""

from typing import Any

import httpx
import pytest
import respx

from stutters_mcp_core.testing.least_privilege import RO_CALLER, assert_read_tier_works, read_tier_report

from .demo import BASE, create_server, make_settings


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        mock.get("/api/version").mock(return_value=httpx.Response(200, json={"version": "1"}))
        mock.get("/api/items").mock(return_value=httpx.Response(200, json={"items": [], "total": 0}))
        denied = httpx.Response(403, json={"message": "role lacks items:read"})
        mock.get("/api/items/1").mock(return_value=denied)
        yield mock


def ro_server() -> Any:
    return create_server(
        make_settings(mcp_enable_writes=True, mcp_enable_dangerous=True, mcp_roles=f"{RO_CALLER}:ro")
    )


async def test_report_classifies_tools(api: Any) -> None:
    report = await read_tier_report(ro_server(), {"demo_get_item": {"item_id": "1"}}, skip={"demo_api_call"})
    assert "demo_list_items" in report.ok and "demo_status" in report.ok
    assert "demo_get_item" in report.denied and "denied access" in report.denied["demo_get_item"]
    assert {"demo_create_item", "demo_wipe"} <= set(report.hidden_writes)
    assert "demo_get_definition" in report.skipped  # needs an argument we didn't give


async def test_assert_fails_on_denial(api: Any) -> None:
    with pytest.raises(AssertionError, match="DENIED demo_get_item"):
        await assert_read_tier_works(ro_server(), {"demo_get_item": {"item_id": "1"}})
    api.get("/api/items/1").mock(return_value=httpx.Response(200, json={"id": "1"}))
    report = await assert_read_tier_works(ro_server(), {"demo_get_item": {"item_id": "1"}})
    assert "demo_get_item" in report.ok


async def test_write_tool_visible_to_ro_is_a_failure(api: Any) -> None:
    # without roles, the "read-only" caller would see write tools: the helper flags that
    server = create_server(make_settings(mcp_enable_writes=True))
    report = await read_tier_report(server)
    assert "demo_create_item" in report.denied
