"""C48 suite prompt pack: definition, registration on every server, skipped legs, prompt kit checks."""

import json
from pathlib import Path
from typing import Annotated, Any

import pytest
from fastmcp import Client, FastMCP
from pydantic import Field

from stutters_mcp_core import Tier, ToolRegistrar, build_server, suite
from stutters_mcp_core.manifest import build_manifest
from stutters_mcp_core.prompts import rendered_text, tool_ref
from stutters_mcp_core.suite import (
    SERVERS_ARG,
    SuiteLeg,
    SuitePrompt,
    numbered,
    pack_problems,
    parse_servers,
    plan_legs,
    suite_prompt,
)
from stutters_mcp_core.testing import (
    assert_suite_pack,
    inbound_reference_problems,
    prompt_contract_problems,
    read_tool_manifests,
    suite_pack_problems,
)

from . import demo
from .demo import SPEC, DemoApp, make_settings

ALL_TIERS = {"mcp_enable_writes": True, "mcp_enable_dangerous": True}

LEGS = (
    SuiteLeg("demo", "the item inventory", ("demo_list_items", "demo_get_item")),
    SuiteLeg("other", "the other things", ("other_list_things",)),
)


@pytest.fixture
def pack(monkeypatch: pytest.MonkeyPatch) -> dict[str, SuitePrompt]:
    monkeypatch.setitem(suite.PEERS, "demo", "Demo appliance")
    monkeypatch.setitem(suite.PEERS, "other", "Other appliance")
    out: dict[str, SuitePrompt] = {}

    @suite_prompt(title="Demo check", legs=LEGS, example={"kind": "router"}, pack=out)
    def suite_demo_check(
        kind: Annotated[str, Field(description="Item kind to check: router or switch")],
        servers: SERVERS_ARG = "",
    ) -> str:
        """Check items across the demo and other servers; skips the legs whose server is absent."""
        plan = plan_legs(LEGS, servers)
        steps = []
        if plan.has("demo"):
            steps.append(
                f"Call {tool_ref('demo_list_items')} for {kind} items, then {tool_ref('demo_get_item')}."
            )
        if plan.has("other"):
            steps.append(f"Call {tool_ref('other_list_things')}.")
        return plan.header + "\n\n" + numbered(steps)

    @suite_prompt(title="Demo fix", legs=LEGS[:1], tier=Tier.WRITE, pack=out)
    def suite_demo_fix(servers: SERVERS_ARG = "") -> str:
        """Fix demo items with preview-first writes; needs only the demo server."""
        plan = plan_legs(LEGS[:1], servers)
        return plan.header + "\n\n" + numbered([f"Call {tool_ref('demo_list_items')}."] if plan.legs else [])

    return out


def _server(pack: dict[str, SuitePrompt], **kw: Any) -> FastMCP[Any]:
    return build_server(
        SPEC,
        make_settings(**kw),
        client_factory=DemoApp,
        register=[demo.register, lambda reg: suite.register(reg, pack)],
    )


def _other_manifest(tools: list[str], prompts: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "manifest": 1,
        "server": "other-mcp-server",
        "prefix": "other",
        "tools": {t: {"tier": "read"} for t in tools},
        "prompts": prompts or {},
    }


async def _render(server: FastMCP[Any], name: str, args: dict[str, str]) -> str:
    async with Client(server) as client:
        return rendered_text(await client.get_prompt(name, args))


# --- registration and rendering -----------------------------------------------------------------


async def test_register_publishes_requires_and_suite_meta(pack: dict[str, SuitePrompt]) -> None:
    server = _server(pack, **ALL_TIERS)
    async with Client(server) as client:
        prompts = {p.name: p for p in await client.list_prompts()}
    meta = prompts["suite_demo_check"].meta or {}
    assert (
        meta["suite"] is True and meta["requires"] == ["demo", "other"] and meta["uses"] == ["demo", "other"]
    )
    assert meta["tools"] == ["demo_list_items", "demo_get_item", "other_list_things"]
    assert "suite" in meta["fastmcp"]["tags"] and "workflow" in meta["fastmcp"]["tags"]
    assert {"suite_demo_check", "suite_demo_fix", "demo_audit_items"} <= set(prompts)
    # Write-tier suite prompts are hidden when writes are off, like the server's own prompts.
    async with Client(_server(pack)) as client:
        assert "suite_demo_fix" not in {p.name for p in await client.list_prompts()}
    settings = make_settings(**ALL_TIERS)
    with pytest.raises(ValueError, match="registered twice"):
        suite.register(ToolRegistrar(_server(pack, **ALL_TIERS), settings, "demo"), pack)


async def test_renders_with_and_without_peers(pack: dict[str, SuitePrompt]) -> None:
    server = _server(pack, **ALL_TIERS)
    only_demo = await _render(server, "suite_demo_check", {"kind": "switch", "servers": "demo"})
    assert only_demo.startswith(
        "This suite workflow uses these servers: demo (Demo appliance), other (Other appliance).\n"
        "Skipped legs (server not connected); say so in the first lines of your answer:\n"
        "- other leg skipped: server not connected; not checked: the other things.\n"
    )
    assert "`demo_list_items` for switch items" in only_demo and "other_list_things" not in only_demo

    both = await _render(server, "suite_demo_check", {"kind": "switch", "servers": "demo-mcp-server, other"})
    assert "skipped" not in both and "`other_list_things`" in both and "`demo_get_item`" in both

    unknown = await _render(server, "suite_demo_check", {"kind": "switch"})
    assert "First check which of these tools you have: `demo_*`, `other_*`." in unknown
    assert "'<server> leg skipped: server not connected" in unknown and "`other_list_things`" in unknown

    none = await _render(server, "suite_demo_check", {"kind": "switch", "servers": "fmc"})
    assert "None of these servers is connected" in none and "demo_list_items" not in none


def test_parse_servers() -> None:
    assert parse_servers("") is None and parse_servers(" , ") is None
    assert parse_servers("cat-sdwan-mcp-server, ISE ,catc-mcp fmc") == {"catsdwan", "ise", "catc", "fmc"}
    plan = plan_legs(LEGS, "other")
    assert plan.known and [leg.peer for leg in plan.legs] == ["other"] and plan.has("other")
    assert [leg.peer for leg in plan.skipped] == ["demo"] and not plan.has("demo")


def test_suite_prompt_definition_errors(pack: dict[str, SuitePrompt]) -> None:
    def ok(servers: SERVERS_ARG = "") -> str:
        """A docstring that is long enough for the description check."""
        return ""

    with pytest.raises(ValueError, match="at least one leg"):
        suite_prompt(title="x", legs=[], pack={})
    with pytest.raises(ValueError, match="not a suite server prefix"):
        suite_prompt(title="x", legs=[SuiteLeg("nope", "x")], pack={})
    with pytest.raises(ValueError, match="not a demo tool"):
        suite_prompt(title="x", legs=[SuiteLeg("demo", "x", ("other_list_things",))], pack={})
    with pytest.raises(ValueError, match="must be named suite_"):
        suite_prompt(title="x", legs=LEGS, pack={})(ok)
    with pytest.raises(ValueError, match="defined twice"):
        suite_prompt(title="x", legs=LEGS, pack=pack)(pack["suite_demo_check"].fn)

    def suite_no_servers(kind: str) -> str:
        return kind

    with pytest.raises(ValueError, match="must take a `servers"):
        suite_prompt(title="x", legs=LEGS, pack={})(suite_no_servers)


# --- pack checks ------------------------------------------------------------------------------------


def test_pack_problems(pack: dict[str, SuitePrompt]) -> None:
    assert pack_problems(pack) == []
    assert pack_problems() == []  # the pack core ships

    bad: dict[str, SuitePrompt] = {}

    @suite_prompt(title="Leaky", legs=LEGS, pack=bad)
    def suite_leaky(kind: str, servers: SERVERS_ARG = "") -> str:
        """Ignores the leg plan: always calls every server and never names a skipped leg."""
        return (
            f"Call {tool_ref('demo_list_items')}, {tool_ref('other_list_things')}, {tool_ref('demo_wipe')}."
        )

    problems = pack_problems(bad)
    assert "suite prompt suite_leaky: argument 'kind' has no Field(description=...)" in problems
    assert "suite prompt suite_leaky: references 'demo_wipe', which no leg declares" in problems
    assert (
        "suite prompt suite_leaky: with servers='demo' still calls other_list_things (skipped legs)"
        in problems
    )
    assert "suite prompt suite_leaky: with servers='other' does not name every skipped leg" in problems


async def test_prompt_contract_accepts_suite_prompts(pack: dict[str, SuitePrompt]) -> None:
    server = _server(pack, **ALL_TIERS)
    # suite_* names, and `uses` without the server's own prefix, are fine for pack prompts; leg tools
    # are left to the suite check, so a renamed peer tool is not reported twice.
    problems = await prompt_contract_problems(
        server, "demo", known_prefixes={"other"}, external_tools={"other": ["other_renamed"]}
    )
    assert [p for p in problems if "suite_" in p] == []


async def test_suite_pack_problems_against_manifests(pack: dict[str, SuitePrompt], tmp_path: Path) -> None:
    server = _server(pack, **ALL_TIERS)
    (tmp_path / "other.json").write_text(json.dumps(_other_manifest(["other_list_things"])))
    assert await suite_pack_problems(server, "demo", tmp_path, pack=pack) == []

    # A peer renamed a tool a leg calls.
    (tmp_path / "other.json").write_text(json.dumps(_other_manifest(["other_list_all_things"])))
    problems = await suite_pack_problems(server, "demo", tmp_path, pack=pack)
    assert problems == [
        "suite prompt suite_demo_check: other leg calls 'other_list_things', "
        "which the other manifest does not list"
    ]
    # ... known and waived until the peer ships it; a stale or unknown waiver is reported.
    assert await suite_pack_problems(server, "demo", tmp_path, pending=["other_list_things"], pack=pack) == []
    problems = await suite_pack_problems(
        server, "demo", tmp_path, pending=["demo_get_item", "demo_nothing"], pack=pack
    )
    assert "suite prompt suite_demo_check: 'demo_get_item' is in `pending` but demo has it now" in problems
    assert "`pending` names 'demo_nothing', which no suite prompt leg calls" in problems

    # Peers without a manifest are not checked; this server's own leg tools always are.
    pack_missing: dict[str, SuitePrompt] = {}
    suite_prompt(title="x", legs=[SuiteLeg("demo", "x", ("demo_gone",))], pack=pack_missing)(
        pack["suite_demo_fix"].fn
    )
    problems = await suite_pack_problems(server, "demo", pack=pack_missing)
    assert problems == [
        "suite prompt suite_demo_fix: demo leg calls 'demo_gone', which this server does not list"
    ]

    # A server that did not register the pack.
    bare = build_server(SPEC, make_settings(**ALL_TIERS), client_factory=DemoApp, register=[demo.register])
    problems = await suite_pack_problems(bare, "demo", pack=pack)
    assert problems[0].startswith("suite prompt suite_demo_check: not registered (add suite.register")


async def test_assert_suite_pack_uses_core_pack(tmp_path: Path) -> None:
    server = build_server(
        SPEC, make_settings(**ALL_TIERS), client_factory=DemoApp, register=[demo.register, suite.register]
    )
    await assert_suite_pack(server, "demo", tmp_path)


async def test_manifest_marks_suite_prompts_and_inbound_skips_them(
    pack: dict[str, SuitePrompt], tmp_path: Path
) -> None:
    server = _server(pack, **ALL_TIERS)
    built = await build_manifest(server, server="demo-mcp-server", prefix="demo")
    entry = built["prompts"]["suite_demo_check"]
    assert entry["suite"] is True and entry["uses"] == ["demo", "other"] and entry["rendered"] is True
    assert entry["references"] == ["demo_list_items", "demo_get_item", "other_list_things"]
    assert "suite" not in built["prompts"]["demo_audit_items"]

    # Another server's copy of the pack names a demo tool this server lacks: not an inbound problem
    # (the suite check owns it); the same reference from an ordinary prompt still is.
    prompts = {
        "suite_demo_check": {"uses": ["demo", "other"], "references": ["demo_gone"], "suite": True},
        "other_flow": {"uses": ["other", "demo"], "references": ["demo_gone"]},
    }
    (tmp_path / "other.json").write_text(json.dumps(_other_manifest(["other_list_things"], prompts)))
    problems = await inbound_reference_problems(server, "demo", read_tool_manifests(tmp_path))
    assert problems == [
        "other-mcp-server prompt other_flow: references 'demo_gone', which this server does not register"
    ]
