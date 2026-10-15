"""G5: Prometheus metrics: series, /metrics auth, separate listener."""

import asyncio
import socket
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from stutters_mcp_core import metrics
from stutters_mcp_core.inbound_auth import NamedTokenVerifier, hash_token
from stutters_mcp_core.metrics import REGISTRY

from .demo import BASE, create_server, make_settings

TOKEN = "metrics-scraper-token-0123456789abcdef"


@pytest.fixture(autouse=True)
def fresh_registry() -> Any:
    REGISTRY.reset()
    metrics.enable()
    yield
    metrics.disable()
    REGISTRY.reset()


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        mock.get("/api/version").mock(return_value=httpx.Response(200, json={"version": "1.2.3"}))
        mock.get("/api/items/1").mock(return_value=httpx.Response(200, json={"id": "1"}))
        mock.get("/api/items/2").mock(return_value=httpx.Response(404))
        mock.post("/api/items").mock(return_value=httpx.Response(201, json={"id": "9"}))
        yield mock


def test_render_format() -> None:
    REGISTRY.inc("mcp_tool_calls_total", {"server": "s", "tool": 't"x', "outcome": "ok"})
    REGISTRY.observe("mcp_tool_duration_seconds", {"server": "s", "tool": "t"}, 0.3)
    text = REGISTRY.render()
    assert "# TYPE mcp_tool_calls_total counter" in text
    assert 'mcp_tool_calls_total{outcome="ok",server="s",tool="t\\"x"} 1' in text
    assert 'mcp_tool_duration_seconds_bucket{server="s",tool="t",le="0.25"} 0' in text
    assert 'mcp_tool_duration_seconds_bucket{server="s",tool="t",le="0.5"} 1' in text
    assert 'mcp_tool_duration_seconds_bucket{server="s",tool="t",le="+Inf"} 1' in text
    assert 'mcp_tool_duration_seconds_count{server="s",tool="t"} 1' in text


async def test_series_from_tool_calls(api: Any) -> None:
    server = create_server(make_settings(mcp_enable_writes=True, mcp_metrics=True))
    async with Client(server) as c:
        await c.call_tool("demo_get_item", {"item_id": "1"})
        with pytest.raises(ToolError):
            await c.call_tool("demo_get_item", {"item_id": "2"})
        with pytest.raises(ToolError):
            await c.call_tool("demo_no_such_tool", {})
        prev = (await c.call_tool("demo_create_item", {"name": "x"})).structured_content
        await c.call_tool("demo_create_item", {"name": "x", "confirm": prev["confirm"]})
    s = "demo-mcp-server"
    assert REGISTRY.value("mcp_tool_calls_total", server=s, tool="demo_get_item", outcome="ok") == 1
    assert REGISTRY.value("mcp_tool_calls_total", server=s, tool="demo_get_item", outcome="error") == 1
    assert REGISTRY.histogram_count("mcp_tool_duration_seconds", server=s, tool="demo_get_item") == 2
    assert REGISTRY.value("mcp_tool_calls_total", server=s, tool="_unknown", outcome="error") == 1
    assert REGISTRY.value("mcp_write_events_total", server=s, tool="demo_create_item", event="preview") == 1
    assert REGISTRY.value("mcp_write_events_total", server=s, tool="demo_create_item", event="applied") == 1
    assert REGISTRY.value("mcp_upstream_requests_total", product="Demo", method="GET", status="200") >= 1
    assert REGISTRY.value("mcp_upstream_requests_total", product="Demo", method="GET", status="404") == 1
    assert REGISTRY.value("mcp_upstream_requests_total", product="Demo", method="POST", status="201") == 1
    assert REGISTRY.value("mcp_info", server=s, version="unknown", core_version=metrics_core_version()) == 1


def metrics_core_version() -> str:
    from stutters_mcp_core import __version__

    return __version__


async def test_retries_and_timeouts_counted() -> None:
    from stutters_mcp_core.http import ApiClient

    with respx.mock(base_url=BASE) as mock:
        mock.get("/x").mock(side_effect=[httpx.Response(503), httpx.Response(200, json={})])
        mock.get("/t").mock(side_effect=httpx.ConnectTimeout("slow"))
        client = ApiClient(BASE, product="P", env_prefix="P_", retries=1)
        await client.get("/x")
        with pytest.raises(ToolError):
            await client.get("/t")
        await client.aclose()
    assert REGISTRY.value("mcp_upstream_retries_total", product="P", reason="503") == 1
    assert REGISTRY.value("mcp_upstream_retries_total", product="P", reason="timeout") == 1
    assert REGISTRY.value("mcp_upstream_requests_total", product="P", method="GET", status="timeout") == 2


def test_rate_wait_threshold() -> None:
    metrics.record_rate_wait("P", 0.001)
    assert REGISTRY.value("mcp_rate_limit_waits_total", product="P") == 0
    metrics.record_rate_wait("P", 0.5)
    assert REGISTRY.value("mcp_rate_limit_waits_total", product="P") == 1
    assert REGISTRY.value("mcp_rate_limit_wait_seconds_total", product="P") == 0.5


async def test_metrics_route_needs_a_token(api: Any) -> None:
    auth = NamedTokenVerifier({"prom": hash_token(TOKEN)})
    app = create_server(make_settings(mcp_metrics=True), auth=auth).http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
        assert (await http.get("/metrics")).status_code == 401
        assert (await http.get("/metrics", headers={"Authorization": "Bearer wrong"})).status_code == 401
        ok = await http.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    assert ok.status_code == 200 and "mcp_info" in ok.text
    assert ok.headers["content-type"].startswith("text/plain; version=0.0.4")


async def test_metrics_off_by_default(api: Any) -> None:
    auth = NamedTokenVerifier({"prom": hash_token(TOKEN)})
    app = create_server(make_settings(), auth=auth).http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
        r = await http.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code in (401, 404) and "mcp_info" not in r.text


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_separate_listener() -> None:
    port = free_port()
    metrics.set_info("x", "1", "2")
    listener = await metrics.start_listener("127.0.0.1", port)
    assert listener is not None
    try:
        async with httpx.AsyncClient() as http:
            r = await http.get(f"http://127.0.0.1:{port}/metrics")
            assert r.status_code == 200 and 'mcp_info{core_version="2",server="x",version="1"} 1' in r.text
            assert (await http.get(f"http://127.0.0.1:{port}/mcp")).status_code == 404
    finally:
        await listener.stop()
        metrics._listener = None
    await asyncio.sleep(0)


async def test_port_in_use_does_not_kill_the_server() -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert await metrics.start_listener("127.0.0.1", port) is None


def test_disabled_records_nothing() -> None:
    metrics.disable()
    metrics.record_tool("s", "t", "ok", 0.1)
    metrics.record_upstream("P", "GET", "200", 0.1)
    assert REGISTRY.render() == "\n"


def test_method_label_bounded() -> None:
    metrics.record_upstream("P", "BREW", "418", 0.1)
    assert REGISTRY.value("mcp_upstream_requests_total", product="P", method="OTHER", status="418") == 1


async def test_apply_failed_counted(api: Any) -> None:
    api.post("/api/items").mock(return_value=httpx.Response(500))
    server = create_server(make_settings(mcp_enable_writes=True, mcp_metrics=True, retries=0))
    async with Client(server) as c:
        prev = (await c.call_tool("demo_create_item", {"name": "x"})).structured_content
        with pytest.raises(ToolError):
            await c.call_tool("demo_create_item", {"name": "x", "confirm": prev["confirm"]})
    labels = {"server": "demo-mcp-server", "tool": "demo_create_item", "event": "apply_failed"}
    assert REGISTRY.value("mcp_write_events_total", **labels) == 1
