"""C62: suite_asbuilt_drift (NetBox interfaces, VLANs and cables vs Catalyst Center)."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from stutters_mcp_core import build_server, suite
from stutters_mcp_core.prompts import find_tool_refs, rendered_text
from stutters_mcp_core.suite import pack_problems
from stutters_mcp_core.suite.asbuilt_drift import parse_checks
from stutters_mcp_core.testing import assert_prompt_contract, suite_pack_problems

from . import demo
from .demo import SPEC, DemoApp, make_settings

ALL_TIERS = {"mcp_enable_writes": True, "mcp_enable_dangerous": True}
NAME = "suite_asbuilt_drift"


def _server() -> FastMCP[Any]:
    return build_server(
        SPEC, make_settings(**ALL_TIERS), client_factory=DemoApp, register=[demo.register, suite.register]
    )


async def _render(args: dict[str, str]) -> str:
    async with Client(_server()) as client:
        return rendered_text(await client.get_prompt(NAME, args))


def test_in_pack_and_clean() -> None:
    sp = suite.PACK[NAME]
    assert sp.requires == ["netbox", "catc"]
    assert sp.tools == [
        "netbox_site_summary",
        "netbox_list_objects",
        "catc_list_devices",
        "catc_list_device_interfaces",
        "catc_get_physical_topology",
        "catc_get_vlan_topology",
    ]
    assert pack_problems() == []


def test_parse_checks() -> None:
    assert parse_checks("") == ["interfaces", "vlans", "cables"]
    assert parse_checks("ALL") == ["interfaces", "vlans", "cables"]
    assert parse_checks("cables, interfaces") == ["interfaces", "cables"]
    with pytest.raises(ValueError, match="unknown ports"):
        parse_checks("ports,vlans")


async def test_both_servers_full_walk() -> None:
    text = await _render({"site": "branch-1", "servers": "netbox,catc"})
    assert "skipped" not in text
    assert "As-built drift for site branch-1: checks interfaces, vlans, cables." in text
    assert "`netbox_list_objects`('dcim.devices', filters={'site': 'branch-1'}" in text
    assert "`catc_list_devices` with site='branch-1'" in text
    assert "by serial, then management IP, then hostname" in text
    assert "Gi=GigabitEthernet" in text and "'dcim.interfaces'" in text and "'dcim.cables'" in text
    assert "`catc_get_physical_topology`" in text and "`catc_get_vlan_topology`" in text
    assert "'NetBox' when" in text and "'network' when" in text and "'site visit' when" in text
    assert text.rstrip().endswith("call no write tool, and propose each fix without applying it.")
    # Every leg tool appears; nothing else.
    assert set(find_tool_refs(text, suite.PEERS)) == set(suite.PACK[NAME].tools)


async def test_checks_device_and_site_mapping() -> None:
    text = await _render(
        {
            "site": "br-1=Global/Lab/Branch-1",
            "device": "edge1",
            "checks": "cables",
            "servers": "netbox-mcp, catc-mcp-server",
        }
    )
    assert "checks cables." in text and "(only device edge1)" in text
    assert "filters={'site': 'br-1'}" in text and "site='Global/Lab/Branch-1'" in text
    assert "'dcim.interfaces'" not in text and "`catc_get_vlan_topology`" not in text
    assert "NetBox site 'br-1' vs Catalyst Center site 'Global/Lab/Branch-1'" in text


async def test_one_side_only_stops() -> None:
    text = await _render({"site": "branch-1", "servers": "catc"})
    assert "- netbox leg skipped: server not connected; not checked: the documented intent" in text
    assert "only catc (Catalyst Center) connected there is nothing to compare" in text
    assert find_tool_refs(text, suite.PEERS) == []
    text = await _render({"site": "branch-1", "servers": "netbox"})
    assert "- catc leg skipped" in text and "Do not report netbox's data on its own as drift." in text
    nothing = await _render({"site": "branch-1", "servers": "fmc"})
    assert nothing.endswith("nothing to run: say so and stop.")


async def test_unknown_servers_checks_tools_first() -> None:
    text = await _render({"site": "branch-1"})
    assert "First check which of these tools you have: `netbox_*`, `catc_*`." in text
    assert "1. If either the `netbox_*` or the `catc_*` tools are missing, there is nothing" in text


async def test_bad_checks_rejected() -> None:
    with pytest.raises(Exception, match="checks: unknown"):
        await _render({"site": "branch-1", "checks": "routes"})


async def test_prompt_kit_and_manifests(tmp_path: Path) -> None:
    server = _server()
    await assert_prompt_contract(server, "demo", known_prefixes={"other"})
    # Peer manifests holding every leg tool: clean. A catc manifest without the topology tool: reported.
    netbox = {"netbox_site_summary": {}, "netbox_list_objects": {}}
    (tmp_path / "netbox.json").write_text(
        json.dumps({"manifest": 1, "server": "netbox-mcp", "prefix": "netbox", "tools": netbox})
    )
    catc = {t: {} for t in suite.PACK[NAME].tools if t.startswith("catc_")}
    (tmp_path / "catc.json").write_text(
        json.dumps({"manifest": 1, "server": "catc-mcp-server", "prefix": "catc", "tools": catc})
    )
    problems = [p for p in await suite_pack_problems(server, "demo", tmp_path) if NAME in p]
    assert problems == []
    del catc["catc_get_physical_topology"]
    (tmp_path / "catc.json").write_text(
        json.dumps({"manifest": 1, "server": "catc-mcp-server", "prefix": "catc", "tools": catc})
    )
    problems = [p for p in await suite_pack_problems(server, "demo", tmp_path) if NAME in p]
    assert problems == [
        f"suite prompt {NAME}: catc leg calls 'catc_get_physical_topology', which the catc manifest does "
        "not list"
    ]


SUITE = Path(__file__).resolve().parents[3]


async def test_leg_tools_exist_in_sibling_manifests() -> None:
    """Local only (skipped in CI): every leg tool is in the committed catc manifest and the hand-written
    netbox manifest (cat-sdwan's copy)."""
    paths = [
        p
        for p in (
            SUITE / "catc-mcp-server" / "tests" / "manifests" / "catc.json",
            SUITE / "cat-sdwan-mcp-server" / "tests" / "manifests" / "netbox.json",
        )
        if p.exists()
    ]
    if not paths:
        pytest.skip("no sibling server checkouts with manifests")
    problems = [p for p in await suite_pack_problems(_server(), "demo", *paths) if NAME in p]
    assert problems == []
