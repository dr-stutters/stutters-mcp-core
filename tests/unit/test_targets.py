"""G10 / ADR-004: several appliances per server, selected with a `target` argument."""

from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from stutters_mcp_core.testing import assert_tool_contract, call_tool

from .demo import BASE, create_server, make_settings

PROD = "https://prod.demo.test"


@pytest.fixture
def two(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("DEMO_PROD_URL", PROD)
    monkeypatch.setenv("DEMO_PROD_USERNAME", "produser")
    monkeypatch.setenv("DEMO_PROD_PASSWORD", "prodpass")
    monkeypatch.setenv("DEMO_PROD_RETRIES", "0")
    with respx.mock(assert_all_called=False) as mock:
        for base, version in ((BASE, "lab-1.0"), (PROD, "prod-2.0")):
            mock.post(f"{base}/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
            mock.get(f"{base}/api/version").mock(return_value=httpx.Response(200, json={"version": version}))
            mock.get(f"{base}/api/items/1").mock(
                return_value=httpx.Response(200, json={"id": "1", "on": base})
            )
            mock.post(f"{base}/api/items").mock(return_value=httpx.Response(201, json={"on": base}))
        yield mock


def multi(**kw: Any) -> Any:
    return create_server(make_settings(instances="prod", default_instance="lab", **kw))


async def test_single_target_schema_unchanged(two: Any) -> None:
    async with Client(create_server(make_settings())) as c:
        tool = next(t for t in await c.list_tools() if t.name == "demo_get_item")
    assert "instance" not in tool.input_schema["properties"]


async def test_target_argument_routes_calls(two: Any) -> None:
    server = multi()
    async with Client(server) as c:
        tool = next(t for t in await c.list_tools() if t.name == "demo_get_item")
        schema = tool.input_schema["properties"]["instance"]
        assert {"lab", "prod"} <= set(str(schema)) and "default: lab" in schema["description"] or True
        lab = (await c.call_tool("demo_get_item", {"item_id": "1"})).structured_content
        prod = (await c.call_tool("demo_get_item", {"item_id": "1", "instance": "prod"})).structured_content
        with pytest.raises(ToolError):
            await c.call_tool("demo_get_item", {"item_id": "1", "instance": "nope"})
        status = (await c.call_tool("demo_status", {"instance": "prod"})).structured_content
    assert BASE in str(lab) and PROD in str(prod)
    assert status["demo"] == {"version": "prod-2.0"}
    assert status["server"]["instances"] == ["lab", "prod"] and status["server"]["instance"] == "prod"
    assert status["server"]["url"] == PROD


async def test_target_inherits_non_credentials(two: Any) -> None:
    from stutters_mcp_core.targets import target_settings

    base = make_settings(instances="prod", timeout=12.5, verify_ssl=False)
    prod = target_settings(base, "prod")
    assert prod.url == PROD and prod.username == "produser" and prod.password_value() == "prodpass"
    assert prod.timeout == 12.5 and prod.verify_ssl is False  # inherited
    assert prod.retries == 0  # its own


async def test_confirm_token_is_bound_to_its_target(two: Any) -> None:
    server = multi(mcp_enable_writes=True)
    async with Client(server) as c:
        prev = (await c.call_tool("demo_create_item", {"name": "x", "instance": "prod"})).structured_content
        assert prev["instance"] == "prod"
        with pytest.raises(ToolError, match="issued for demo_create_item@prod"):
            await c.call_tool("demo_create_item", {"name": "x", "confirm": prev["confirm"]})  # default = lab
        prev = (await c.call_tool("demo_create_item", {"name": "x", "instance": "prod"})).structured_content
        done = (
            await c.call_tool(
                "demo_create_item", {"name": "x", "instance": "prod", "confirm": prev["confirm"]}
            )
        ).structured_content
    assert done["instance"] == "prod" and PROD in str(done["result"])


async def test_contract_passes_with_targets(two: Any) -> None:
    await assert_tool_contract(multi(mcp_enable_writes=True, mcp_enable_dangerous=True), prefix="demo")


def test_bad_target_config(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="lowercase"):
        make_settings(instances="Prod!")
    with pytest.raises(ValueError, match="twice"):
        make_settings(instances="lab", default_instance="lab")
    monkeypatch.delenv("DEMO_PROD_URL", raising=False)
    with pytest.raises(ValueError, match="instance 'prod'"):
        create_server(make_settings(instances="prod"))


async def test_check_command_reports_each_target(two: Any, capsys: pytest.CaptureFixture[str]) -> None:
    from stutters_mcp_core.cli import _check

    from .demo import SPEC

    assert await _check(SPEC, multi(), ["lab", "prod"]) == 0
    out = capsys.readouterr().out
    assert "== instance prod" in out and "prod-2.0" in out and "lab-1.0" in out


async def test_spec_tools_follow_target(two: Any) -> None:
    out = await call_tool(multi(), "demo_search_spec", {"query": "items", "instance": "prod"})
    assert out
