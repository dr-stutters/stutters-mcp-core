from typing import Annotated, Any

import pytest
from fastmcp import Client, FastMCP
from pydantic import Field

from stutters_mcp_core import Tier, ToolRegistrar
from stutters_mcp_core.prompts import PromptRegistrar, find_tool_refs, tool_ref
from stutters_mcp_core.testing import assert_prompt_contract, prompt_contract_problems

from .demo import create_server, make_settings


def test_tool_ref_and_find() -> None:
    assert tool_ref("ise_why_auth_failed") == "`ise_why_auth_failed`"
    with pytest.raises(ValueError):
        tool_ref("WhyAuthFailed")
    text = "Use `ise_a_b`, then `fmc_rule_lookup` and `ise_a_b` again; not `response_format` or ise_bare."
    assert find_tool_refs(text, ["ise", "fmc"]) == ["ise_a_b", "fmc_rule_lookup"]
    assert find_tool_refs(text, ["ise"]) == ["ise_a_b"]
    assert find_tool_refs(text, []) == []


def test_registration_rules() -> None:
    mcp: FastMCP[Any] = FastMCP("x")
    reg = PromptRegistrar(mcp, None, "demo")
    with pytest.raises(ValueError, match="must start with demo_"):
        reg.prompt()(lambda: "x")  # name '<lambda>'
    with pytest.raises(ValueError, match="server prefix"):
        reg.prompt(uses=["ISE!"])
    with pytest.raises(ValueError, match="not a tool name"):
        reg.prompt(tools=["Nope"])

    def demo_once() -> str:
        """A prompt body long enough to register."""
        return "x"

    reg.prompt()(demo_once)
    with pytest.raises(ValueError, match="registered twice"):
        reg.prompt()(demo_once)


async def test_fastmcp_argument_doc_wording_canary() -> None:
    """The linter relies on FastMCP's auto-generated argument text; fail loudly if it changes."""
    from typing import Literal

    from stutters_mcp_core.testing import _AUTO_ARG_DOC, _sample_value

    mcp: FastMCP[Any] = FastMCP("canary")

    @mcp.prompt
    def p(n: int, mode: Literal["a", "b"], flag: bool = False) -> str:
        return f"{n}{mode}{flag}"

    async with Client(mcp) as c:
        args = {a.name: a.description for a in (await c.list_prompts())[0].arguments or []}
    assert all((d or "").startswith(_AUTO_ARG_DOC) for d in args.values())
    assert [_sample_value(args[k]) for k in ("n", "mode", "flag")] == ["1", "a", "true"]
    assert _sample_value(None) == "example"


async def test_demo_prompt_metadata_and_render() -> None:
    server = create_server(make_settings())
    async with Client(server) as c:
        prompts = {p.name: p for p in await c.list_prompts()}
        p = prompts["demo_audit_items"]
        assert p.meta is not None and p.meta["uses"] == ["demo", "other"]
        assert p.meta["tier"] == "read" and "workflow" in p.meta["fastmcp"]["tags"]
        assert [a.name for a in p.arguments or []] == ["kind", "max_items"]
        assert "demo_cleanup_items" not in prompts  # tier=WRITE, writes off
        out = await c.get_prompt("demo_audit_items", {"kind": "router"})
        assert "`demo_list_items`" in out.messages[0].content.text  # type: ignore[union-attr]
    rw = create_server(make_settings(mcp_enable_writes=True))
    async with Client(rw) as c:
        assert "demo_cleanup_items" in {p.name for p in await c.list_prompts()}


@pytest.mark.parametrize("kw", [{}, {"mcp_enable_writes": True, "mcp_enable_dangerous": True}])
async def test_demo_prompts_pass_contract(kw: dict[str, Any]) -> None:
    await assert_prompt_contract(
        create_server(make_settings(**kw)),
        "demo",
        samples={"demo_audit_items": {"kind": "switch"}},
        known_prefixes={"demo", "other"},
    )


async def test_prompt_contract_catches_problems() -> None:
    mcp: FastMCP[Any] = FastMCP("bad")
    reg = ToolRegistrar(mcp, make_settings(), "demo")

    @reg.read()
    async def demo_get_thing(thing: Annotated[str, Field(description="Thing id")]) -> str:
        """Get one thing by id; returns all of its fields as JSON."""
        return thing

    @reg.prompt(uses=["demo"], tools=["demo_missing_tool"])
    def demo_bad_flow(x: str, n: int = 1) -> str:
        """short"""
        return f"Call {tool_ref('demo_get_thing')} then `demo_nope` and `fmc_rule_lookup` with {x} {n}."

    @mcp.prompt(name="other_flow")
    def other_flow() -> str:
        """A prompt registered without the registrar, so it has no metadata at all."""
        return ""

    @reg.prompt(uses=["demo", "zzz"])
    def demo_broken(count: Annotated[int, Field(description="How many")]) -> str:
        """A prompt that raises when it is rendered, to check the render error report."""
        raise RuntimeError("boom")

    problems = "\n".join(await prompt_contract_problems(mcp, "demo", known_prefixes={"demo", "fmc"}))
    assert "demo_bad_flow: description length" in problems
    assert "demo_bad_flow: argument 'x' has no description" in problems
    assert "argument 'n' has no description" in problems
    assert "unknown local tool 'demo_missing_tool'" in problems
    assert "unknown local tool 'demo_nope'" in problems
    assert "'fmc_rule_lookup' but 'fmc' is not in `uses`" in problems
    assert "demo_get_thing'" not in problems
    assert "other_flow: name must" in problems and "other_flow: no `uses`" in problems
    assert "other_flow: renders an empty body" in problems
    assert "demo_broken: does not render" in problems
    assert "unknown server prefix 'zzz'" in problems


def test_tier_values() -> None:
    assert Tier.WRITE.value == "write"


async def test_status_lists_prompts() -> None:
    import httpx
    import respx

    from stutters_mcp_core.testing import call_tool

    from .demo import BASE

    with respx.mock(base_url=BASE, assert_all_called=False) as api:
        api.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        api.get("/api/version").mock(return_value=httpx.Response(200, json={"version": "1"}))
        data = await call_tool(create_server(make_settings()), "demo_status")
    assert data["server"]["prompts"] == ["demo_audit_items"]
