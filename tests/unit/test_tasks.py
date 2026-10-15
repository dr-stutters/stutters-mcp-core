"""C12: MCP background tasks (SEP-2663 via fastmcp-tasks) for long-running tools, opt-in."""

import asyncio
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client, Context
from fastmcp_tasks import call_tool_task

from stutters_mcp_core import (
    Confirm,
    Plan,
    ServerSpec,
    ToolRegistrar,
    build_server,
    get_client,
    preview_or_apply,
)
from stutters_mcp_core.testing import as_caller

from .demo import BASE, DemoApp, make_settings

SPEC = ServerSpec("t-mcp-server", "t", "T", "DEMO_", "Tools for the task test server.")


def register(reg: ToolRegistrar) -> None:
    @reg.read(title="Slow report", task=True)
    async def t_slow_report(ctx: Context, seconds: float = 0.05) -> dict[str, Any]:
        """Build a slow report: waits, reports progress and returns which client it used."""
        await ctx.report_progress(1, 2)
        await asyncio.sleep(seconds)
        app: DemoApp = get_client(ctx)
        return {"version": (await app.status())["version"]}

    @reg.write(title="Slow deploy", task=True)
    async def t_slow_deploy(ctx: Context, name: str = "x", confirm: Confirm = None) -> dict[str, Any]:
        """Deploy slowly. Preview-first: returns a confirm token before changing anything."""
        app: DemoApp = get_client(ctx)
        return await preview_or_apply(
            ctx,
            tool="t_slow_deploy",
            args={"name": name},
            confirm=confirm,
            plan=Plan(f"Deploy {name}", blast_radius=1),
            apply=lambda: app.api.post("/api/items", {"name": name}),
        )


def server(**kw: Any) -> Any:
    return build_server(
        SPEC, make_settings(**kw), client_factory=DemoApp, register=[register], status=DemoApp.status
    )


async def run_task(c: Client, name: str, args: dict[str, Any]) -> Any:
    handle = await call_tool_task(c, name, args)
    return (await handle.result()).structured_content


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        mock.get("/api/version").mock(return_value=httpx.Response(200, json={"version": "7.0"}))
        mock.post("/api/items").mock(return_value=httpx.Response(201, json={"id": "9"}))
        yield mock


async def test_tasks_off_by_default(api: Any) -> None:
    async with Client(server()) as c:
        tool = next(t for t in await c.list_tools() if t.name == "t_slow_report")
        assert (await c.call_tool("t_slow_report", {})).structured_content == {"version": "7.0"}
        status = (await c.call_tool("t_status", {})).structured_content
    assert "tasks" not in status["server"]
    assert tool.execution is None or getattr(tool.execution, "task_support", "forbidden") in (
        None,
        "forbidden",
    )


async def test_task_tool_runs_as_background_task(api: Any) -> None:
    async with Client(server(mcp_tasks=True, mcp_enable_writes=True)) as c:
        assert await run_task(c, "t_slow_report", {"seconds": 0.05}) == {"version": "7.0"}
        status = (await c.call_tool("t_status", {})).structured_content
    assert status["server"]["tasks"] == {"backend": "memory", "tools": ["t_slow_deploy", "t_slow_report"]}


async def test_preview_and_confirm_inside_tasks_keep_the_caller(api: Any) -> None:
    async with Client(server(mcp_tasks=True, mcp_enable_writes=True)) as c:
        with as_caller("alice"):
            prev = (
                await (await call_tool_task(c, "t_slow_deploy", {"name": "r1"})).result()
            ).structured_content
            assert prev["status"] == "preview" and prev["blast_radius"] == 1
            done = (
                await (
                    await call_tool_task(c, "t_slow_deploy", {"name": "r1", "confirm": prev["confirm"]})
                ).result()
            ).structured_content
    assert done["status"] == "applied" and api.routes[2].call_count == 1


def test_missing_extra_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real = builtins.__import__

    def fake(name: str, *a: Any, **kw: Any) -> Any:
        if name == "fastmcp_tasks":
            raise ImportError(name)
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(ValueError, match=r"stutters-mcp-core\[tasks\]"):
        server(mcp_tasks=True)
