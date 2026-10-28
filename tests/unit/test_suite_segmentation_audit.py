"""C49: suite_segmentation_audit (segment pairs: intended verdict per layer vs an observed CML test)."""

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from stutters_mcp_core import build_server, suite
from stutters_mcp_core.prompts import find_tool_refs, rendered_text
from stutters_mcp_core.suite import pack_problems, pending_tools
from stutters_mcp_core.suite.segmentation_audit import parse_intent, parse_segments
from stutters_mcp_core.testing import assert_prompt_contract, load_scenario, suite_pack_problems

from . import demo
from .demo import SPEC, DemoApp, make_settings

NAME = "suite_segmentation_audit"
PEERS = ["catsdwan", "ise", "fmc", "catc", "cml"]
SNAPSHOT = Path(__file__).parent / "snapshots" / f"{NAME}.md"
ROOT = Path(__file__).resolve().parents[2]
SUITE = Path(__file__).resolve().parents[3]


def _server(**tiers: bool) -> FastMCP[Any]:
    return build_server(
        SPEC,
        make_settings(**tiers),
        client_factory=DemoApp,
        register=[demo.register, suite.register],
    )


async def _render(args: dict[str, str], **tiers: bool) -> str:
    async with Client(_server(**tiers)) as client:
        return rendered_text(await client.get_prompt(NAME, args))


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end)]


def test_in_pack_and_clean() -> None:
    sp = suite.PACK[NAME]
    assert sp.requires == PEERS and sp.meta()["tier"] == "read"
    for tool in (
        "catsdwan_omp_routes",
        "catsdwan_get_firewall_policy",
        "ise_get_egress_matrix",
        "fmc_rule_lookup",
        "catc_get_sda_fabric",
        "cml_check_reachability",
    ):
        assert tool in sp.tools
    assert NAME not in pending_tools()
    assert pack_problems() == []


def test_parse_segments() -> None:
    assert parse_segments("") == []
    assert parse_segments(" IoT, Corporate ,iot,, PCI-CDE ") == ["IoT", "Corporate", "PCI-CDE"]
    with pytest.raises(ValueError, match="at least two segments"):
        parse_segments("IoT")
    with pytest.raises(ValueError, match="segment 'IoT;drop'"):
        parse_segments("IoT;drop, Corporate")


def test_parse_intent() -> None:
    assert parse_intent("") == []
    assert parse_intent("Corporate->IoT:TCP/443, Corporate -> Servers, Corporate->IoT:tcp/443") == [
        ("Corporate", "IoT", "tcp/443"),
        ("Corporate", "Servers", "any"),
    ]
    assert parse_intent("A->B:udp/1812-1813, A->B:icmp") == [("A", "B", "udp/1812-1813"), ("A", "B", "icmp")]
    for bad, msg in (
        ("Corporate>IoT", "write each allowed flow"),
        ("->IoT", "write each allowed flow"),
        ("IoT->iot", "same segment"),
        ("A->B:http", "service must be"),
    ):
        with pytest.raises(ValueError, match=msg):
            parse_intent(bad)


async def test_read_tier_registers_everywhere() -> None:
    async with Client(_server()) as client:
        assert NAME in {p.name for p in await client.list_prompts()}


async def test_all_servers() -> None:
    sp = suite.PACK[NAME]
    text = await _render({**sp.example, "servers": ",".join(PEERS)})
    assert "skipped" not in text.split("\n\n")[0]
    assert set(find_tool_refs(text, suite.PEERS)) == set(sp.tools)
    assert "Segmentation audit at site 'br1' for PCI DSS 4.0 req 1.3" in text

    heads = [
        "1. Inventory",
        "2. Segments",
        "3. Declared intent",
        "4. Intended verdict",
        "5. Observed verdict",
    ]
    at = [text.index(h) for h in heads]
    assert at == sorted(at)
    assert "only these flows are allowed" in text and "Corporate -> IoT (tcp/443)" in text
    intended = _section(text, "4. Intended verdict", "5. Observed verdict")
    for control in ("sdwan_vpn", "sdwan_firewall", "trustsec_sgacl", "fmc_rule", "sda_extranet"):
        assert f"   - {control}:" in intended
    observed = _section(text, "5. Observed verdict", "6. Status")
    assert "`cml_check_reachability`(lab, nodes=[<test host of A>]" in observed
    assert "use the FMC device trace for the service" in observed
    assert "lab 'secure-branch'" in text

    # The evidence pack: markdown plus one JSON document that parses once placeholders are filled.
    pack = text[text.index("8. Evidence pack") :]
    assert "with the framework 'PCI DSS 4.0 req 1.3'" in pack and "fenced json block" in pack
    for key in ('"pairs"', '"layers"', '"source_tool"', '"observed"', '"status"', '"not_checked"'):
        assert key in pack
    assert "call no write tool" in text and "segmented as intended" in text


async def test_defaults_fully_segmented() -> None:
    text = await _render({})
    first = "First check which of these tools you have: `catsdwan_*`, `ise_*`, `fmc_*`, `catc_*`, `cml_*`."
    assert first in text
    assert "Segmentation audit at every site:" in text
    assert "fully segmented, every pair of different segments is denied" in text
    assert "every segment the inventory finds" in text
    assert "the lab that models these segments (ask if unsure)" in text
    assert "with the framework" not in text


async def test_without_cml_nothing_is_observed() -> None:
    text = await _render({"site": "br1", "servers": "catsdwan, ise-mcp-server, fmc"})
    for peer in ("catc", "cml"):
        assert f"- {peer} leg skipped: server not connected" in text
    refs = find_tool_refs(text, suite.PEERS)
    assert refs and all(r.startswith(("catsdwan_", "ise_", "fmc_")) for r in refs)
    assert "observed verdict is 'not_tested'" in text
    assert "the FMC device trace above is the closest evidence" in text
    assert "sda_vn" not in _section(text, "4. Intended verdict", "5. Observed verdict")


async def test_some_servers_skip_the_rest() -> None:
    text = await _render({"servers": "cat-sdwan-mcp-server, cml"})
    for peer in ("ise", "fmc", "catc"):
        assert f"- {peer} leg skipped: server not connected" in text
    assert all(r.startswith(("catsdwan_", "cml_")) for r in find_tool_refs(text, suite.PEERS))
    assert "the ping proves the deny side only." in text and "FMC device trace" not in text

    only_cml = await _render({"servers": "cml"})
    assert "Intended verdict per pair" not in only_cml and "`cml_check_reachability`" in only_cml

    nothing = await _render({"servers": "netbox"})
    assert nothing.endswith("nothing to run: say so and stop.")


async def test_bad_arguments() -> None:
    with pytest.raises(Exception, match="at least two segments"):
        await _render({"segments": "IoT"})
    with pytest.raises(Exception, match="write each allowed flow"):
        await _render({"intent": "Corporate to IoT"})


async def test_body_snapshot() -> None:
    """The full rendering with every server, as committed (UPDATE_PROMPT_SNAPSHOTS=1 rewrites it)."""
    sp = suite.PACK[NAME]
    text = str(sp.fn(**sp.example, servers=",".join(PEERS))) + "\n"
    if os.environ.get("UPDATE_PROMPT_SNAPSHOTS") == "1":
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(text)
    assert SNAPSHOT.read_text() == text, f"{SNAPSHOT.name} is stale: UPDATE_PROMPT_SNAPSHOTS=1 to rewrite"


async def test_prompt_kit_and_manifests(tmp_path: Path) -> None:
    server = _server()
    await assert_prompt_contract(server, "demo", known_prefixes={"other"})
    sp = suite.PACK[NAME]
    for prefix in PEERS:
        tools = [t for t in sp.tools if t.startswith(prefix + "_") and t != "ise_list_sgt_vn_vlan"]
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
        f"suite prompt {NAME}: ise leg calls 'ise_list_sgt_vn_vlan', which the ise manifest does not list"
    ]


def test_c55_scenario_manifest() -> None:
    sc = load_scenario(ROOT / "docs" / "scenarios" / "edge-security.yaml")
    assert sc.epic == 104
    prompts = [s.name for s in sc.steps if s.kind == "prompt"]
    assert prompts == ["catsdwan_trace_through_firewall", NAME]
    assert "suite" in sc.servers and sc.no_manifest == ("suite",)
    step = next(s for s in sc.steps if s.name == NAME)
    assert set(step.calls) <= set(suite.PACK[NAME].tools)


OWN = {
    "catsdwan": "cat-sdwan-mcp-server",
    "ise": "ise-mcp-server",
    "fmc": "fmc-mcp-server",
    "catc": "catc-mcp-server",
    "cml": "cml-mcp-server",
}


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


def test_c55_scenario_against_sibling_manifests() -> None:
    """Local only: the C55 manifest's steps exist in the servers' committed manifests."""
    from stutters_mcp_core.manifest import read_tool_manifests
    from stutters_mcp_core.testing import scenario_problems

    paths = [SUITE / repo / "tests" / "manifests" / f"{p}.json" for p, repo in OWN.items()]
    if not all(p.exists() for p in paths):
        pytest.skip("no sibling server checkouts with manifests")
    sc = load_scenario(ROOT / "docs" / "scenarios" / "edge-security.yaml")
    assert scenario_problems(sc, read_tool_manifests(*paths)) == []
