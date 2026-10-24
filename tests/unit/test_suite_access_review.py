"""C63: suite_access_review (privileged access review across platforms)."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from stutters_mcp_core import build_server, suite
from stutters_mcp_core.prompts import find_tool_refs, rendered_text
from stutters_mcp_core.suite import pack_problems
from stutters_mcp_core.suite.access_review import PENDING_TOOLS, parse_stale_days
from stutters_mcp_core.testing import assert_prompt_contract, suite_pack_problems

from . import demo
from .demo import SPEC, DemoApp, make_settings

ALL_TIERS = {"mcp_enable_writes": True, "mcp_enable_dangerous": True}
NAME = "suite_access_review"
PEERS = ["ise", "catc", "fmc", "catsdwan", "cml", "win"]


def _server() -> FastMCP[Any]:
    return build_server(
        SPEC, make_settings(**ALL_TIERS), client_factory=DemoApp, register=[demo.register, suite.register]
    )


async def _render(args: dict[str, str]) -> str:
    async with Client(_server()) as client:
        return rendered_text(await client.get_prompt(NAME, args))


def test_in_pack_and_clean() -> None:
    sp = suite.PACK[NAME]
    assert sp.requires == PEERS and sp.meta()["tier"] == "read"
    assert "win" in suite.PEERS
    assert set(PENDING_TOOLS) <= set(sp.tools)
    assert pack_problems() == []


def test_parse_stale_days() -> None:
    assert parse_stale_days("") == 90 and parse_stale_days(" 30 ") == 30
    with pytest.raises(ValueError, match="whole number"):
        parse_stale_days("ninety")
    with pytest.raises(ValueError, match="between 1 and 3650"):
        parse_stale_days("0")


async def test_all_servers() -> None:
    text = await _render({"servers": ",".join(PEERS), "stale_days": "60"})
    assert "skipped" not in text.split("\n\n")[0]
    assert "Privileged access review (privileged; stale after 60 days)." in text
    for tool in suite.PACK[NAME].tools:
        assert f"`{tool}`" in text
    assert set(find_tool_refs(text, suite.PEERS)) == set(suite.PACK[NAME].tools)
    assert "Get-ADGroupMember -Identity '<group>' -Recursive" in text
    assert "every AD group the other legs (Cisco ISE, Catalyst Center" in text
    assert "no login for 60 days or more" in text and "stale for 60+ days" in text
    assert "not readable with this account" in text and "Never show passwords" in text
    assert text.rstrip().endswith("list fixes (disable, remove from group, change role) as proposals.")


async def test_some_servers_skip_the_rest() -> None:
    text = await _render({"servers": "fmc-mcp-server, Windows_Server", "scope": "all"})
    assert "- ise leg skipped: server not connected; not checked: ISE admin accounts" in text
    for peer in ("catc", "catsdwan", "cml"):
        assert f"- {peer} leg skipped" in text
    refs = find_tool_refs(text, suite.PEERS)
    assert refs and all(r.startswith(("fmc_", "win_")) for r in refs)
    assert "every AD group the other legs (Secure Firewall Management Center) map" in text
    assert "every account, privileged ones flagged" in text

    ad_only = await _render({"servers": "win"})
    assert "any AD group the user names as granting platform admin rights" in ad_only
    nothing = await _render({"servers": "splunk"})
    assert nothing.endswith("nothing to run: say so and stop.")


async def test_unknown_servers_lists_every_leg() -> None:
    text = await _render({})
    assert (
        "First check which of these tools you have: `ise_*`, `catc_*`, `fmc_*`, `catsdwan_*`, `cml_*`, "
        "`win_*`." in text
    )


async def test_bad_arguments() -> None:
    with pytest.raises(Exception, match="scope must be"):
        await _render({"scope": "admins"})
    with pytest.raises(Exception, match="stale_days must be"):
        await _render({"stale_days": "-5"})


async def test_prompt_kit_and_pending(tmp_path: Path) -> None:
    server = _server()
    await assert_prompt_contract(server, "demo", known_prefixes={"other"})
    shipped = {
        "fmc": ["fmc_list_users"],
        "catsdwan": ["catsdwan_list_users", "catsdwan_get_aaa"],
        "catc": ["catc_get_external_authentication"],
        "ise": [],
        "cml": [],
    }
    for prefix, tools in shipped.items():
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
    assert sorted(problems) == sorted(
        f"suite prompt {NAME}: {t.split('_', 1)[0]} leg calls '{t}', which the {t.split('_', 1)[0]} manifest "
        "does not list"
        for t in PENDING_TOOLS
    )
    pending = [t for t in PENDING_TOOLS]
    problems = [p for p in await suite_pack_problems(server, "demo", tmp_path, pending=pending) if NAME in p]
    assert problems == []


SUITE = Path(__file__).resolve().parents[3]
OWN = {
    "ise": "ise-mcp-server",
    "catc": "catc-mcp-server",
    "fmc": "fmc-mcp-server",
    "catsdwan": "cat-sdwan-mcp-server",
    "cml": "cml-mcp-server",
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


def test_windows_server_name_maps_to_win() -> None:
    assert suite.parse_servers("Windows_Server, windows-mcp, ise") == {"win", "ise"}
