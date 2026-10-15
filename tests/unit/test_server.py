import json
import logging
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from stutters_mcp_core.testing import assert_tool_contract, call_tool, tool_contract_problems

from .demo import BASE, create_server, make_settings


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        mock.get("/api/version").mock(return_value=httpx.Response(200, json={"version": "1.2.3"}))
        yield mock


async def test_status_tool_reports_product_and_server(api: Any) -> None:
    data = await call_tool(create_server(make_settings()), "demo_status")
    assert data["demo"] == {"version": "1.2.3"}
    server = data["server"]
    assert server["writes_enabled"] is False
    assert server["tools"] == {"read": 6, "write": 0, "dangerous": 0}
    assert server["spec_sources"] == ["main"]


async def test_status_reports_upstream_error(api: Any) -> None:
    api.get("/api/version").mock(return_value=httpx.Response(401, json={"message": "nope"}))
    data = await call_tool(create_server(make_settings()), "demo_status")
    assert "denied access" in data["demo"]["error"]


async def test_startup_survives_missing_credentials() -> None:
    from stutters_mcp_core import BaseServerSettings

    async def status(app: object) -> dict[str, str]:
        make_settings(username=None).require_credentials("DEMO_")
        return {}

    from stutters_mcp_core import ServerSpec, build_server

    spec = ServerSpec("x-mcp-server", "x", "X", "X_", "Tools for X (test).")
    server = build_server(
        spec, make_settings(), client_factory=lambda s: object(), register=[], status=status
    )
    data = await call_tool(server, "x_status")
    assert "DEMO_USERNAME" in data["x"]["error"]
    assert BaseServerSettings  # imported for clarity


async def test_tiers_control_registration(api: Any) -> None:
    async def names(**kw: Any) -> set[str]:
        async with Client(create_server(make_settings(**kw))) as c:
            return {t.name for t in await c.list_tools()}

    ro = await names()
    rw = await names(mcp_enable_writes=True)
    rwd = await names(mcp_enable_writes=True, mcp_enable_dangerous=True)
    assert "demo_create_item" not in ro and "demo_create_item" in rw
    assert "demo_wipe" not in rw and "demo_wipe" in rwd


def test_dangerous_requires_writes() -> None:
    with pytest.raises(ValueError, match="also requires MCP_ENABLE_WRITES"):
        make_settings(mcp_enable_dangerous=True)


@pytest.mark.parametrize("kw", [{}, {"mcp_enable_writes": True, "mcp_enable_dangerous": True}])
async def test_demo_server_passes_contract(api: Any, kw: dict[str, Any]) -> None:
    await assert_tool_contract(create_server(make_settings(**kw)), prefix="demo")


async def test_contract_catches_bad_tools() -> None:
    from fastmcp import FastMCP

    bad: FastMCP[Any] = FastMCP("bad")

    @bad.tool
    def other_list_things(x: int) -> int:
        """short"""
        return x

    problems = await tool_contract_problems(bad, "demo")
    text = "\n".join(problems)
    assert "start with 'demo_'" in text and "description length" in text and "readOnlyHint" in text


async def test_list_paging_and_login(api: Any) -> None:
    items = [{"id": i, "name": f"n{i}", "secret": "s"} for i in range(5)]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Token"] == "T"
        off, lim = int(request.url.params["offset"]), int(request.url.params["limit"])
        return httpx.Response(200, json={"items": items[off : off + lim], "total": 5})

    api.get("/api/items").mock(side_effect=handler)
    data = await call_tool(create_server(make_settings()), "demo_list_items", {"limit": 3, "offset": 1})
    assert [r["id"] for r in data["results"]] == [1, 2, 3]
    assert data["total"] == 5 and data["next_offset"] == 4
    assert data["results"][0]["secret"] == "********"


async def test_markdown_list(api: Any) -> None:
    api.get("/api/items").mock(
        return_value=httpx.Response(200, json={"items": [{"id": 1, "name": "a"}], "total": 1})
    )
    text = await call_tool(create_server(make_settings()), "demo_list_items", {"response_format": "markdown"})
    assert "| id | name |" in text and "total: 1" in text


async def test_preview_then_apply(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    create = api.post("/api/items").mock(return_value=httpx.Response(201, json={"id": 7}))
    server = create_server(make_settings(mcp_enable_writes=True))
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    async with Client(server) as c:
        preview = (await c.call_tool("demo_create_item", {"name": "r1"})).structured_content
        assert preview["status"] == "preview" and create.call_count == 0
        token = preview["confirm"]
        with pytest.raises(ToolError, match="arguments differ"):
            await c.call_tool("demo_create_item", {"name": "r2", "confirm": token})
        with pytest.raises(ToolError, match="Unknown or expired"):  # burnt by the mismatch
            await c.call_tool("demo_create_item", {"name": "r1", "confirm": token})
        token = (await c.call_tool("demo_create_item", {"name": "r1"})).structured_content["confirm"]
        applied = (await c.call_tool("demo_create_item", {"name": "r1", "confirm": token})).structured_content
    assert applied == {"status": "applied", "action": "Create item r1", "result": {"id": 7}}
    assert create.call_count == 1
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "stutters_mcp_core.audit"]
    assert {"preview", "applied"} <= {e.get("event") for e in events}
    assert any(e.get("event") == "tool_call" and e["tool"] == "demo_create_item" for e in events)


async def test_spec_tools(api: Any) -> None:
    server = create_server(make_settings(mcp_cache_dir=""))
    hits = await call_tool(server, "demo_search_spec", {"query": "item create"})
    assert hits["results"][0]["name"] == "POST /api/items"
    text = await call_tool(server, "demo_get_definition", {"name": "POST /api/items"})
    d = json.loads(text) if isinstance(text, str) else text
    assert d["operations"][0]["request_required"] == ["name"]
    assert "enum: router|switch" in d["operations"][0]["request_fields"]["kind"]


async def test_api_call_read_only_and_write(api: Any) -> None:
    api.get("/api/items/3").mock(return_value=httpx.Response(200, json={"id": 3, "psk": "k", "note": None}))
    post = api.post("/api/items").mock(return_value=httpx.Response(201, json={"id": 9}))
    ro = create_server(make_settings())
    async with Client(ro) as c:
        tool = next(t for t in await c.list_tools() if t.name == "demo_api_call")
        assert "method" not in tool.input_schema["properties"]
    api.get("/api/t1/items").mock(return_value=httpx.Response(200, json=[]))
    assert json.loads(await call_tool(ro, "demo_api_call", {"path": "/api/{tenant}/items"})) == []
    assert json.loads(await call_tool(ro, "demo_api_call", {"path": "/api/items/3"})) == {
        "id": 3,
        "psk": "********",
        "note": None,
    }
    with pytest.raises(ToolError, match="Invalid path"):
        await call_tool(ro, "demo_api_call", {"path": "https://evil/x"})
    with pytest.raises(ToolError, match="Invalid path"):
        await call_tool(ro, "demo_api_call", {"path": "/api/../secret"})

    with pytest.raises(ToolError, match="Invalid path"):
        await call_tool(ro, "demo_api_call", {"path": "/api/%2e%2e/secret"})
    writes_only = create_server(make_settings(mcp_enable_writes=True))
    async with Client(writes_only) as c:
        tool = next(t for t in await c.list_tools() if t.name == "demo_api_call")
        assert "method" not in tool.input_schema["properties"]  # raw writes need the dangerous tier

    rw = create_server(make_settings(mcp_enable_writes=True, mcp_enable_dangerous=True))
    async with Client(rw) as c:
        args = {"method": "POST", "path": "/api/items", "body": {"name": "x"}}
        prev = json.loads((await c.call_tool("demo_api_call", args)).content[0].text)
        assert prev["status"] == "preview" and post.call_count == 0
        done = json.loads(
            (await c.call_tool("demo_api_call", {**args, "confirm": prev["confirm"]})).content[0].text
        )
    assert done["status"] == "applied" and post.call_count == 1


async def test_cli_check_and_token(
    api: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from stutters_mcp_core.cli import _check, run_cli

    from .demo import SPEC

    assert await _check(SPEC, create_server(make_settings())) == 0
    assert '"version": "1.2.3"' in capsys.readouterr().out

    run_cli(SPEC, type(make_settings()), create_server, argv=["token", "alice"])
    out = capsys.readouterr().out
    assert "alice:sha256:" in out


async def test_get_definition_examples_flag(api: Any) -> None:
    server = create_server(make_settings(mcp_cache_dir=""))
    async with Client(server) as c:
        tool = next(t for t in await c.list_tools() if t.name == "demo_get_definition")
    assert "examples" in tool.input_schema["properties"]
    text = await call_tool(server, "demo_get_definition", {"name": "POST /api/items", "examples": True})
    d = json.loads(text) if isinstance(text, str) else text
    assert d["operations"][0]["examples"] == "none in the spec"


def test_audit_log_rotation(tmp_path: Any) -> None:
    import logging

    from stutters_mcp_core.audit import audit as audit_logger
    from stutters_mcp_core.audit import configure_audit_log

    path = tmp_path / "audit.log"
    configure_audit_log(str(path), max_mb=0.001, backups=2)  # ~1 KB
    try:
        for i in range(100):
            audit_logger.info(json.dumps({"event": "tool_call", "n": i, "pad": "x" * 50}))
        files = sorted(p.name for p in tmp_path.iterdir())
        assert files == ["audit.log", "audit.log.1", "audit.log.2"]
        assert all(path.stat().st_size < 2048 for path in tmp_path.iterdir())
    finally:
        configure_audit_log("off")
        audit_logger.handlers.clear()
        audit_logger.propagate = True
        audit_logger.setLevel(logging.NOTSET)


def test_audit_rotation_settings() -> None:
    s = make_settings(mcp_audit_log_max_mb="", mcp_audit_log_backups=3)
    assert s.mcp_audit_log_max_mb is None and s.mcp_audit_log_backups == 3
    with pytest.raises(ValueError):
        make_settings(mcp_audit_log_max_mb=0)


async def test_status_reports_server_version(api: Any) -> None:
    from dataclasses import replace

    from stutters_mcp_core import build_server

    from .demo import SPEC, DemoApp

    server = build_server(
        replace(SPEC, version="9.9.9"), make_settings(), client_factory=DemoApp, register=[]
    )
    data = await call_tool(server, "demo_status")
    assert data["server"]["server_version"] == "9.9.9"
    unknown = await call_tool(create_server(make_settings()), "demo_status")
    assert unknown["server"]["server_version"] is None  # demo-mcp-server isn't an installed distribution
