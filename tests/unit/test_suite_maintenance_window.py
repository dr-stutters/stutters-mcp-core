"""C51: suite_maintenance_window (one window across SD-WAN edges, Catalyst Center switches, ISE)."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from stutters_mcp_core import build_server, suite
from stutters_mcp_core.prompts import find_tool_refs, rendered_text
from stutters_mcp_core.suite import pack_problems
from stutters_mcp_core.suite.maintenance_window import parse_mode
from stutters_mcp_core.testing import assert_prompt_contract, suite_pack_problems

from . import demo
from .demo import SPEC, DemoApp, make_settings

ALL_TIERS = {"mcp_enable_writes": True, "mcp_enable_dangerous": True}
NAME = "suite_maintenance_window"
PEERS = ["catsdwan", "catc", "ise"]


def _server(**tiers: bool) -> FastMCP[Any]:
    return build_server(
        SPEC,
        make_settings(**(tiers or ALL_TIERS)),
        client_factory=DemoApp,
        register=[demo.register, suite.register],
    )


async def _render(args: dict[str, str]) -> str:
    async with Client(_server()) as client:
        return rendered_text(await client.get_prompt(NAME, args))


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end)]


def test_in_pack_and_clean() -> None:
    sp = suite.PACK[NAME]
    assert sp.requires == PEERS and sp.meta()["tier"] == "dangerous"
    for tool in (
        "catsdwan_upgrade_software",
        "catc_distribute_image",
        "catc_activate_image",
        "ise_list_patches",
    ):
        assert tool in sp.tools
    for peer in PEERS:
        assert {f"{peer}_snapshot", f"{peer}_compare_snapshot"} <= set(sp.tools)
    assert pack_problems() == []


def test_parse_mode() -> None:
    assert parse_mode("") == "upgrade"
    assert parse_mode(" Stage ") == "stage"
    assert parse_mode("upgrade") == "upgrade"
    with pytest.raises(ValueError, match="mode must be"):
        parse_mode("activate")


async def test_hidden_unless_dangerous_is_on() -> None:
    async with Client(_server(mcp_enable_writes=True, mcp_enable_dangerous=False)) as client:
        names = {p.name for p in await client.list_prompts()}
    assert NAME not in names and "suite_change_check" in names
    async with Client(_server()) as client:
        assert NAME in {p.name for p in await client.list_prompts()}


async def test_all_servers_upgrade() -> None:
    text = await _render(
        {
            "site": "br2",
            "sdwan_version": "17.18.03",
            "catc_image": "cat9k_iosxe.17.15.04.SPA.bin",
            "ise_patch": "3",
            "window": "Sat 02:00-04:00",
            "servers": ",".join(PEERS),
        }
    )
    assert "skipped" not in text.split("\n\n")[0]
    refs = set(find_tool_refs(text, suite.PEERS))
    assert refs == set(suite.PACK[NAME].tools)
    assert "Maintenance window at site 'br2' across products, mode=upgrade. Dangerous: devices reload" in text

    # Plan before any change: inventory -> plan -> rollback -> pre snapshots -> changes -> post -> report.
    heads = [
        "1. Inventory",
        "2. Plan:",
        "3. Rollback",
        "4. Pre snapshots",
        "5. Changes",
        "6. Post",
        "7. Report",
    ]
    at = [text.index(h) for h in heads]
    assert at == sorted(at)
    assert "the order: ISE checks and backup first (no device changes), then the WAN edges" in text
    assert "then the Catalyst Center switches" in text
    assert "Window: Sat 02:00-04:00." in text and "change nothing before that" in text

    pre = _section(text, "4. Pre snapshots", "5. Changes")
    for peer in PEERS:
        assert f"`{peer}_snapshot`(site='br2')" in pre

    # Changes in order ISE, SD-WAN, (reachability gate), Catalyst Center; each previewed and compared.
    changes = _section(text, "5. Changes", "6. Post")
    order = [
        changes.index(s)
        for s in ("   - ISE", "   - SD-WAN", "   - Before the switches", "   - Catalyst Center")
    ]
    assert order == sorted(order)
    assert "Patch 3" in changes and "Patch Management" in changes
    assert "`ise_create_backup`(repository, name) without confirm" in changes
    assert "version, activate=true): the edge reboots" in changes and "one edge at a time" in changes
    assert changes.index("`catc_distribute_image`") < changes.index("`catc_activate_image`")
    assert "access switches first, one as a canary" in changes
    for peer in PEERS:
        assert f"`{peer}_compare_snapshot`(before=" in changes
    assert (
        "never confirm in a loop" in changes and "Never confirm a preview naming a device outside" in changes
    )
    assert "Target: version 17.18.03" in text and "Target: image 'cat9k_iosxe.17.15.04.SPA.bin'" in text
    assert "Say 'window complete' only when" in text


async def test_stage_mode_reloads_nothing() -> None:
    text = await _render({"site": "br2", "mode": "stage", "ise_patch": "3", "servers": ",".join(PEERS)})
    assert "mode=stage. Dangerous: images are copied and installed; nothing reloads." in text
    assert "`catc_activate_image`" not in text
    assert "activate=true" not in text and "activate=false): installs only" in text
    assert "Rollback per product" not in text and "Before the switches" not in text
    assert "ISE: nothing installed in this window (mode=stage)" in text
    assert "Patch Management" not in text


async def test_defaults_without_targets() -> None:
    text = await _render({"site": "Global/AU/Branch2"})
    first = "First check which of these tools you have: `catsdwan_*`, `catc_*`, `ise_*`."
    assert first in text
    assert "Target: the newest repository version inside the release ceiling" in text
    assert "the site's golden image (golden=true) for each device family" in text
    assert "check only: no target patch given" in text
    assert "ISE: nothing installed in this window (no target patch)" in text
    assert "Window:" not in text


async def test_some_servers_skip_the_rest() -> None:
    text = await _render({"site": "br2", "servers": "cat-sdwan-mcp-server"})
    for peer in ("catc", "ise"):
        assert f"- {peer} leg skipped: server not connected" in text
    refs = find_tool_refs(text, suite.PEERS)
    assert refs and all(r.startswith("catsdwan_") for r in refs)
    assert "the order: the WAN edges" in text and "ISE checks" not in text
    assert "Before the switches" not in text

    text = await _render({"site": "br2", "servers": "catc, ise-mcp-server"})
    assert "- catsdwan leg skipped: server not connected" in text
    assert all(r.startswith(("catc_", "ise_")) for r in find_tool_refs(text, suite.PEERS))
    assert "the order: ISE checks and backup first (no device changes), then the Catalyst Center" in text

    nothing = await _render({"site": "br2", "servers": "fmc"})
    assert nothing.endswith("nothing to run: say so and stop.")


async def test_bad_arguments() -> None:
    with pytest.raises(Exception, match="whole network"):
        await _render({"site": "Global"})
    with pytest.raises(Exception, match="mode must be"):
        await _render({"site": "br2", "mode": "reload"})


async def test_prompt_kit_and_manifests(tmp_path: Path) -> None:
    server = _server()
    await assert_prompt_contract(server, "demo", known_prefixes={"other"})
    sp = suite.PACK[NAME]
    for prefix in PEERS:
        shipped = [t for t in sp.tools if t.startswith(prefix + "_")]
        missing = "catc_activate_image"
        tools = [t for t in shipped if t != missing]
        (tmp_path / f"{prefix}.json").write_text(
            json.dumps(
                {
                    "manifest": 1,
                    "server": f"{prefix}-mcp-server",
                    "prefix": prefix,
                    "tools": dict.fromkeys(tools, {}),
                }
            )
        )
    problems = [p for p in await suite_pack_problems(server, "demo", tmp_path) if NAME in p]
    assert problems == [
        f"suite prompt {NAME}: catc leg calls 'catc_activate_image', which the catc manifest does not list"
    ]


SUITE = Path(__file__).resolve().parents[3]
OWN = {"ise": "ise-mcp-server", "catc": "catc-mcp-server", "catsdwan": "cat-sdwan-mcp-server"}


async def test_against_sibling_manifests() -> None:
    """Local only (skipped in CI): every leg tool is in its server's committed manifest (none pending)."""
    paths = [
        SUITE / repo / "tests" / "manifests" / f"{prefix}.json"
        for prefix, repo in OWN.items()
        if (SUITE / repo / "tests" / "manifests" / f"{prefix}.json").exists()
    ]
    if not paths:
        pytest.skip("no sibling server checkouts with manifests")
    problems = [p for p in await suite_pack_problems(_server(), "demo", *paths) if NAME in p]
    assert problems == []
