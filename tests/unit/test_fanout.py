"""C74: instance='*' (or 'a,b') on read tools fans out across every configured appliance."""

import asyncio
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from stutters_mcp_core.errors import InvalidRequest, UpstreamError
from stutters_mcp_core.targets import (
    FANOUT_SCHEMA,
    fanout_legs,
    fanout_names,
    fanout_output_schema,
    target_name,
    with_target_argument,
)
from stutters_mcp_core.testing import assert_tool_contract, call_tool, tool_contract_problems
from stutters_mcp_core.writes import ToolRegistrar

from .demo import BASE, create_server, make_settings

PROD = "https://prod.demo.test"


@pytest.fixture
def two(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("DEMO_PROD_URL", PROD)
    monkeypatch.setenv("DEMO_PROD_USERNAME", "produser")
    monkeypatch.setenv("DEMO_PROD_PASSWORD", "prodpass-secret")
    monkeypatch.setenv("DEMO_PROD_RETRIES", "0")
    with respx.mock(assert_all_called=False) as mock:
        for base, version in ((BASE, "lab-1.0"), (PROD, "prod-2.0")):
            mock.post(f"{base}/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
            mock.get(f"{base}/api/version").mock(return_value=httpx.Response(200, json={"version": version}))
            item = httpx.Response(200, json={"id": "1", "on": base})
            mock.get(f"{base}/api/items/1").mock(return_value=item)
            mock.get(f"{base}/api/items").mock(
                return_value=httpx.Response(200, json={"items": [{"name": f"r-{version}"}], "total": 1})
            )
        yield mock


def multi(**kw: Any) -> Any:
    return create_server(make_settings(instances="prod", default_instance="lab", retries=0, **kw))


async def test_star_runs_the_read_on_every_instance(two: Any) -> None:
    async with Client(multi()) as c:
        tool = next(t for t in await c.list_tools() if t.name == "demo_get_item")
        schema = tool.input_schema["properties"]["instance"]
        out = await c.call_tool("demo_get_item", {"item_id": "1", "instance": "*"})
    assert schema.get("x-fanout") is True and "'*'" in schema["description"]
    assert out.structured_content == {
        "instances": {
            "lab": {"result": {"id": "1", "on": BASE}},
            "prod": {"result": {"id": "1", "on": PROD}},
        },
        "partial": False,
    }
    text = out.content[0].text  # type: ignore[union-attr]
    assert "all 2 instances answered" in text and "## instance prod" in text


async def test_one_instance_failing_gives_a_partial_result(two: Any) -> None:
    two.get(f"{PROD}/api/items/1").mock(side_effect=httpx.ConnectError("connection refused"))
    out = await call_tool(multi(), "demo_get_item", {"item_id": "1", "instance": "*"})
    assert out["partial"] is True
    assert out["instances"]["lab"] == {"result": {"id": "1", "on": BASE}}
    assert set(out["instances"]["prod"]) == {"error"} and out["instances"]["prod"]["error"]


async def test_comma_list_picks_instances_in_order(two: Any) -> None:
    async with Client(multi()) as c:
        out = (
            await c.call_tool("demo_get_item", {"item_id": "1", "instance": "prod, lab"})
        ).structured_content
        single = (await c.call_tool("demo_get_item", {"item_id": "1", "instance": "prod"})).structured_content
        with pytest.raises(ToolError):
            await c.call_tool("demo_get_item", {"item_id": "1", "instance": "prod,nope"})
    assert out is not None and list(out["instances"]) == ["prod", "lab"]
    assert single == {"id": "1", "on": PROD}  # one name: the tool's own shape, unchanged


async def test_markdown_list_tool_fans_out_as_text(two: Any) -> None:
    out = await call_tool(multi(), "demo_list_items", {"instance": "*", "response_format": "markdown"})
    assert "r-lab-1.0" in out["instances"]["lab"]["result"]
    assert "r-prod-2.0" in out["instances"]["prod"]["result"]


async def test_status_fans_out(two: Any) -> None:
    out = await call_tool(multi(), "demo_status", {"instance": "*"})
    assert out["instances"]["lab"]["result"]["demo"] == {"version": "lab-1.0"}
    assert out["instances"]["prod"]["result"]["server"]["instance"] == "prod"


async def test_writes_refuse_a_fan_out(two: Any) -> None:
    async with Client(multi(mcp_enable_writes=True, mcp_enable_dangerous=True)) as c:
        tools = {t.name: t for t in await c.list_tools()}
        with pytest.raises(ToolError):
            await c.call_tool("demo_create_item", {"name": "x", "instance": "*"})
        with pytest.raises(ToolError):
            await c.call_tool("demo_wipe", {"instance": "lab,prod"})
    assert "x-fanout" not in tools["demo_create_item"].input_schema["properties"]["instance"]
    assert "fanout" not in str(tools["demo_wipe"].meta)

    async def demo_change(confirm: str | None = None) -> str:
        return "changed"

    wrapped = with_target_argument(demo_change, ["lab", "prod"], "lab")  # fanout=False: a write
    with pytest.raises(InvalidRequest, match="only for read tools"):
        await wrapped(instance="*")


async def test_contract_passes_and_flags_a_fanning_write(two: Any) -> None:
    await assert_tool_contract(multi(mcp_enable_writes=True, mcp_enable_dangerous=True), prefix="demo")

    mcp: FastMCP[Any] = FastMCP("t")
    from stutters_mcp_core.writes import Tier, annotations_for

    async def demo_bad(confirm: str | None = None) -> dict[str, Any]:
        """A write tool that wrongly fans out across instances."""
        return {}

    mcp.tool(
        with_target_argument(demo_bad, ["lab", "prod"], "lab", fanout=True),
        name="demo_bad",
        annotations=annotations_for(Tier.WRITE, title="Bad", destructive=None, idempotent=None),
        tags={"write"},
    )
    problems = await tool_contract_problems(mcp, "demo")
    assert any("only read-only (read-tier) tools may fan out" in p for p in problems)


async def test_concurrency_is_bounded() -> None:
    running = 0
    peak = 0

    async def demo_slow() -> dict[str, Any]:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return {"on": target_name()}

    names = [f"i{n}" for n in range(6)]
    wrapped = with_target_argument(demo_slow, names, "i0", fanout=True, concurrency=2)
    out = await wrapped(instance="*")
    assert peak == 2
    assert out.structured_content["instances"]["i5"] == {"result": {"on": "i5"}}


async def test_sync_tool_fans_out_in_turn() -> None:
    def demo_sync(x: int = 1) -> dict[str, Any]:
        if target_name() == "dr":
            raise UpstreamError("dr is down")
        return {"on": target_name(), "x": x}

    mcp: FastMCP[Any] = FastMCP("t")
    mcp.tool(with_target_argument(demo_sync, ["lab", "prod", "dr"], "lab", fanout=True), name="demo_sync")
    async with Client(mcp) as c:
        out = (await c.call_tool("demo_sync", {"instance": "*", "x": 3})).structured_content
    assert out == {
        "instances": {
            "lab": {"result": {"on": "lab", "x": 3}},
            "prod": {"result": {"on": "prod", "x": 3}},
            "dr": {"error": "dr is down"},
        },
        "partial": True,
    }


async def test_non_object_return_validates_and_errors_are_scrubbed(two: Any) -> None:
    """A str-returning read keeps FastMCP's {"result": ...} wrapping for one instance; a fan-out still passes
    the client's output-schema check, and a leg's error masks that instance's own secrets."""
    settings = make_settings(instances="prod", default_instance="lab")
    mcp: FastMCP[Any] = FastMCP("t")
    reg = ToolRegistrar(mcp, settings, "demo")
    from stutters_mcp_core.targets import target_settings

    reg.instance_settings = [settings, target_settings(settings, "prod")]

    @reg.read(title="Where am I")
    async def demo_where() -> str:
        """Name the instance this call ran on (a str result)."""
        if target_name() == "prod":
            raise UpstreamError("login failed for produser with password prodpass-secret")
        return f"on {target_name()}"

    async with Client(mcp) as c:
        tool = next(t for t in await c.list_tools() if t.name == "demo_where")
        one = await c.call_tool("demo_where", {"instance": "lab"})
        fan = await c.call_tool("demo_where", {"instance": "*"})
    assert tool.output_schema is not None and tool.output_schema["anyOf"][1] == FANOUT_SCHEMA
    assert one.structured_content == {"result": "on lab"} and one.data == "on lab"
    assert fan.structured_content is not None and fan.structured_content["partial"] is True
    assert fan.structured_content["instances"]["lab"] == {"result": "on lab"}
    error = fan.structured_content["instances"]["prod"]["error"]
    assert "prodpass-secret" not in error and "login failed" in error


def test_fanout_names_and_legs() -> None:
    names = ["lab", "prod", "dr"]
    assert fanout_names("*", names) == names
    assert fanout_names(" dr , lab,dr,", names) == ["dr", "lab"]
    assert fanout_names("prod", names) is None and fanout_names(None, names) is None
    with pytest.raises(InvalidRequest, match="Unknown instance 'x'"):
        fanout_names("lab,x", names)
    with pytest.raises(InvalidRequest, match="names no instance"):
        fanout_names(",", names)
    assert fanout_legs("*", names) == 3 and fanout_legs("lab", names) == 1
    assert fanout_legs("lab,x", names) == 1  # unknown names: the call itself is refused, counted once
    assert fanout_output_schema(None) is None
    wrapped = fanout_output_schema({"type": "object", "$defs": {"A": {}}, "x-fastmcp-wrap-result": True})
    assert wrapped is not None and "$defs" in wrapped and wrapped["x-fastmcp-wrap-result"] is True


def test_rate_limit_counts_each_leg() -> None:
    from stutters_mcp_core.inbound_limits import CallerRateLimitMiddleware

    limiter = CallerRateLimitMiddleware("demo", "demo", 8, {}, None, names=["lab", "prod"])  # burst of 2
    assert limiter.check("alice", now=0.0, cost=2) == 0.0  # a fan-out to 2 instances uses the whole burst
    assert limiter.check("alice", now=0.0) > 0  # so the next call waits
    # A fan-out wider than the burst runs on a full bucket and leaves it in debt.
    assert limiter.check("bob", now=0.0, cost=5) == 0.0
    assert limiter.check("bob", now=15.0) > 0  # 15 s refills 2 tokens: still short of the 3 owed + 1


async def test_rate_limit_middleware_charges_fan_out(two: Any) -> None:
    async with Client(multi(mcp_caller_rate_limit=8)) as c:  # burst of 2 calls
        await c.call_tool("demo_get_item", {"item_id": "1", "instance": "*"})
        with pytest.raises(ToolError, match="Rate limit"):
            await c.call_tool("demo_get_item", {"item_id": "1"})


def test_concurrency_setting() -> None:
    assert make_settings().mcp_fanout_concurrency == 4
    assert make_settings(mcp_fanout_concurrency=8).mcp_fanout_concurrency == 8
    with pytest.raises(ValueError):
        make_settings(mcp_fanout_concurrency=0)
