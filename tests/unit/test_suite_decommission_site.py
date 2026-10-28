"""C52: suite_decommission_site (remove a site everywhere, previewed and confirmed per product)."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from stutters_mcp_core import build_server, suite
from stutters_mcp_core.prompts import find_tool_refs, rendered_text
from stutters_mcp_core.suite import pack_problems
from stutters_mcp_core.suite.decommission_site import PENDING_TOOLS, parse_site, parse_subnets
from stutters_mcp_core.testing import assert_prompt_contract, suite_pack_problems

from . import demo
from .demo import SPEC, DemoApp, make_settings

ALL_TIERS = {"mcp_enable_writes": True, "mcp_enable_dangerous": True}
NAME = "suite_decommission_site"
PEERS = ["catsdwan", "catc", "ise", "fmc", "win"]


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


def test_in_pack_and_clean() -> None:
    sp = suite.PACK[NAME]
    assert sp.requires == PEERS and sp.meta()["tier"] == "dangerous"
    assert set(PENDING_TOOLS) <= set(sp.tools)
    assert pack_problems() == []


def test_parse_site() -> None:
    assert parse_site("  Global/AU/Branch9 ") == "Global/AU/Branch9"
    for bad in ("", "  "):
        with pytest.raises(ValueError, match="site is required"):
            parse_site(bad)
    for whole in ("Global", "global/", "*", "/", "All"):
        with pytest.raises(ValueError, match="whole network"):
            parse_site(whole)


def test_parse_subnets() -> None:
    assert parse_subnets("") == []
    assert parse_subnets("192.0.2.7/24, 198.51.100.0/26 192.0.2.0/24 2001:db8:9::/48") == [
        "192.0.2.0/24",
        "198.51.100.0/26",
        "2001:db8:9::/48",
    ]
    with pytest.raises(ValueError, match="not an IP network"):
        parse_subnets("branch9")
    with pytest.raises(ValueError, match="wider than a site"):
        parse_subnets("0.0.0.0/0")
    with pytest.raises(ValueError, match="wider than a site"):
        parse_subnets("2001:db8::/16")


async def test_hidden_unless_dangerous_is_on() -> None:
    async with Client(_server(mcp_enable_writes=True, mcp_enable_dangerous=False)) as client:
        names = {p.name for p in await client.list_prompts()}
    assert NAME not in names and "suite_access_review" in names
    async with Client(_server()) as client:
        assert NAME in {p.name for p in await client.list_prompts()}


async def test_all_servers() -> None:
    text = await _render(
        {
            "site": "Global/AU/Branch9",
            "subnets": "192.0.2.0/24,198.51.100.0/26",
            "dns_zone": "example.com",
            "servers": ",".join(PEERS),
        }
    )
    assert "skipped" not in text.split("\n\n")[0]
    # dns_zone given: no zone listing needed.
    assert set(suite.PACK[NAME].tools) - set(find_tool_refs(text, suite.PEERS)) == {"win_list_dns_zones"}
    assert set(find_tool_refs(text, suite.PEERS)) <= set(suite.PACK[NAME].tools)
    assert "Decommission site 'Global/AU/Branch9' across products. Dangerous" in text
    assert "subnets: 192.0.2.0/24, 198.51.100.0/26; DNS: zone example.com" in text
    # Removal order: SD-WAN edges, Catalyst Center, ISE, FMC, Windows.
    removal = text.index("Removal, one product at a time")
    rem_text = text[removal : text.index("Verify:")]
    order = [rem_text.index(f"   - {p}") for p in ("SD-WAN", "Catalyst Center", "ISE", "FMC", "Windows")]
    assert order == sorted(order)
    for tool in (
        "catsdwan_remove_device",
        "catc_delete_fabric_site",
        "catc_delete_device",
        "catc_delete_ip_pool",
        "catc_delete_site",
        "ise_delete_nad",
        "ise_delete_object",
        "fmc_delete_object",
        "win_run_powershell_json",
        "win_remove_dns_record",
    ):
        assert f"`{tool}`" in rem_text
    assert "Remove-DhcpServerv4Scope -ScopeId <scope> -Force" in rem_text
    assert "write the preview yourself" in rem_text
    assert "wait for the user to approve that product" in text and "never confirm in a loop" in text
    assert "SD-WAN and Catalyst Center legs found, by IP" in text
    assert "Mark as protected" in text and "controllers and management appliances" in text
    assert "Say 'decommission complete' only when every verify read is empty" in text


async def test_some_servers_skip_the_rest() -> None:
    text = await _render({"site": "br9", "servers": "ise-mcp-server, Windows_Server"})
    for peer in ("catsdwan", "catc", "fmc"):
        assert f"- {peer} leg skipped: server not connected" in text
    refs = find_tool_refs(text, suite.PEERS)
    assert refs and all(r.startswith(("ise_", "win_")) for r in refs)
    assert "found, by IP" not in text and "ise_get_nad" not in text
    assert "subnets: none given (match by site name only)" in text
    assert (
        "`win_list_dns_zones`, then `win_get_dns_records` in every zone (names containing the site)" in text
    )
    assert "scopes named after the site" in text

    nothing = await _render({"site": "br9", "servers": "splunk"})
    assert nothing.endswith("nothing to run: say so and stop.")


async def test_sdwan_fallback_without_remove_device() -> None:
    text = await _render({"site": "100", "servers": "catsdwan"})
    assert "`catsdwan_associate_config_group`(group, remove=[edges])" in text
    assert "device record stays in the Manager" in text
    assert "include_unjoined=true" in text


async def test_unknown_servers_lists_every_leg() -> None:
    text = await _render({"site": "br9"})
    first = "First check which of these tools you have: `catsdwan_*`, `catc_*`, `ise_*`, `fmc_*`, `win_*`."
    assert first in text


async def test_bad_arguments() -> None:
    with pytest.raises(Exception, match="whole network"):
        await _render({"site": "Global"})
    with pytest.raises(Exception, match="not an IP network"):
        await _render({"site": "br9", "subnets": "192.0.2.0/33"})


async def test_prompt_kit_and_pending(tmp_path: Path) -> None:
    server = _server()
    await assert_prompt_contract(server, "demo", known_prefixes={"other"})
    sp = suite.PACK[NAME]
    for prefix in ("catsdwan", "catc", "ise", "fmc"):
        shipped = [t for t in sp.tools if t.startswith(prefix + "_") and t not in PENDING_TOOLS]
        (tmp_path / f"{prefix}.json").write_text(
            json.dumps(
                {
                    "manifest": 1,
                    "server": f"{prefix}-mcp-server",
                    "prefix": prefix,
                    "tools": dict.fromkeys(shipped, {}),
                }
            )
        )
    problems = [p for p in await suite_pack_problems(server, "demo", tmp_path) if NAME in p]
    assert problems == [
        f"suite prompt {NAME}: catsdwan leg calls 'catsdwan_remove_device', which the catsdwan manifest "
        "does not list"
    ]
    found = await suite_pack_problems(server, "demo", tmp_path, pending=list(PENDING_TOOLS))
    problems = [p for p in found if NAME in p]
    assert problems == []


SUITE = Path(__file__).resolve().parents[3]
OWN = {
    "ise": "ise-mcp-server",
    "catc": "catc-mcp-server",
    "fmc": "fmc-mcp-server",
    "catsdwan": "cat-sdwan-mcp-server",
}


async def test_against_sibling_manifests() -> None:
    """Local only (skipped in CI): every leg tool not in PENDING_TOOLS is in its server's own committed
    manifest, and a pending tool a server now ships is reported (drop it from PENDING_TOOLS)."""
    paths = [
        SUITE / repo / "tests" / "manifests" / f"{prefix}.json"
        for prefix, repo in OWN.items()
        if (SUITE / repo / "tests" / "manifests" / f"{prefix}.json").exists()
    ]
    if not paths:
        pytest.skip("no sibling server checkouts with manifests")
    present = {p.stem for p in paths}
    pending = [t for t in PENDING_TOOLS if t.split("_", 1)[0] in present]
    problems = [p for p in await suite_pack_problems(_server(), "demo", *paths, pending=pending) if NAME in p]
    assert problems == []
