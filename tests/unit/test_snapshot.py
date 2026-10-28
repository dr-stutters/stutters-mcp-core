"""C53/C78: the snapshot contract (builder, parse, compare), the snapshot tools and suite_change_check."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError

from stutters_mcp_core import InvalidRequest, ToolRegistrar, build_server, suite
from stutters_mcp_core.prompts import rendered_text
from stutters_mcp_core.snapshot import (
    MAX_CHANGES,
    SCHEMA_VERSION,
    Metric,
    Snapshot,
    compare,
    config_digest,
    digest,
    parse,
    parse_expected,
    register_snapshot_tools,
)
from stutters_mcp_core.suite import pack_problems
from stutters_mcp_core.suite.access_review import PENDING_TOOLS
from stutters_mcp_core.suite.decommission_site import PENDING_TOOLS as DECOMMISSION_PENDING
from stutters_mcp_core.testing import assert_prompt_contract, suite_pack_problems

from . import demo
from .demo import SPEC, DemoApp, make_settings

FIXTURES = Path(__file__).parent / "fixtures" / "snapshots"
ALL_TIERS = {"mcp_enable_writes": True, "mcp_enable_dangerous": True}
SNAPSHOT_TOOLS = [
    f"{p}_{t}" for p in ("catsdwan", "ise", "catc", "fmc") for t in ("snapshot", "compare_snapshot")
]


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def _snap(**items: dict[str, Any]) -> Snapshot:
    """A one-section 'demo' snapshot: items keyed by name."""
    s = Snapshot("demo", "demo-mcp-server", scope={"site": "s1"})
    sec = s.section("links", "Links", metrics={"loss_pct": Metric("lower", 1, limit=5)})
    for key, fields in items.items():
        sec.item(key, **fields)
    return s


# --- builder and blob -------------------------------------------------------------------------------


def test_blob_shape_and_digest() -> None:
    s = Snapshot("demo", "demo-mcp-server", scope={"site": "s1", "empty": ""}, server_version="1.2.3")
    sec = s.section("links", "Links", metrics={"loss_pct": Metric("lower", 1.0, limit=5.0)})
    sec.item("a", ok=True, state="up", loss_pct=0.0, latency_ms=12.3456, label="Link A", note=None)
    sec.item("b", label="b")
    blob = s.blob()
    assert (
        blob["snapshot"] == 1 and blob["prefix"] == "demo"
    )  # health only: still schema 1 and blob["scope"] == {"site": "s1"}
    assert blob["server_version"] == "1.2.3" and blob["taken_at"].endswith("Z")
    links = blob["sections"]["links"]
    assert links["metrics"] == {"loss_pct": {"better": "lower", "limit": 5, "tolerance": 1}}
    assert links["items"]["a"] == {
        "ok": True,
        "state": "up",
        "loss_pct": 0,
        "latency_ms": 12.346,
        "label": "Link A",
    }
    assert links["items"]["b"] == {}  # a label equal to the key is dropped
    assert blob["digest"] == digest(blob) and blob["digest"].startswith("sha256:")
    # The digest survives a JSON round trip, which is how the blob comes back from the client.
    assert digest(json.loads(json.dumps(blob))) == blob["digest"]


def test_builder_rejects_bad_input() -> None:
    s = Snapshot("demo", "demo-mcp-server")
    with pytest.raises(ValueError, match="snake_case"):
        s.section("Bad-Name", "x")
    sec = s.section("links", "Links")
    with pytest.raises(ValueError, match="added twice"):
        s.section("links", "Links")
    with pytest.raises(ValueError, match="must be a scalar"):
        sec.item("a", extra=[1])  # type: ignore[arg-type]
    sec.item("a", ok=True)
    with pytest.raises(ValueError, match="added twice"):
        sec.item("a")
    with pytest.raises(ValueError, match="1-200 characters"):
        sec.item("")


def test_truncation_keeps_unhealthy_items_and_fits_budget() -> None:
    s = Snapshot("demo", "demo-mcp-server")
    sec = s.section("links", "Links", max_items=10)
    for i in range(30):
        sec.item(f"link-{i:02d}", ok=i not in (25, 29), state="up")
    blob = s.blob()
    items = blob["sections"]["links"]["items"]
    assert len(items) == 10 and blob["sections"]["links"]["truncated"] == 20
    assert "link-25" in items and "link-29" in items and "link-00" in items

    big = Snapshot("demo", "demo-mcp-server")
    sec = big.section("links", "Links", max_items=1000)
    for i in range(400):
        sec.item(f"link-{i:03d}", ok=True, description="x" * 80)
    blob = big.blob(max_chars=8_000)
    assert len(json.dumps(blob, separators=(",", ":"))) <= 8_000
    assert blob["sections"]["links"]["truncated"] > 0
    with pytest.raises(ValueError, match="does not fit"):
        big.blob(max_chars=500)


def test_section_failure_is_recorded() -> None:
    s = Snapshot("demo", "demo-mcp-server")
    s.section("links", "Links").fail("HTTP 503 from /dataservice/device/bfd/sessions " + "x" * 400)
    sec = s.blob()["sections"]["links"]
    assert sec["error"].startswith("HTTP 503") and len(sec["error"]) == 300 and sec["items"] == {}


# --- parse ---------------------------------------------------------------------------------------------


def test_parse_errors() -> None:
    blob = _snap(a={"ok": True}).blob()
    assert parse(json.dumps(blob), prefix="demo") == blob
    with pytest.raises(InvalidRequest, match="not JSON"):
        parse('{"snapshot": 1, "sections": {"a"', what="before")
    with pytest.raises(InvalidRequest, match="not a snapshot blob"):
        parse({"sections": {}})
    with pytest.raises(InvalidRequest, match="schema 3 is not supported"):
        parse({**blob, "snapshot": 3})
    with pytest.raises(InvalidRequest, match="schema True is not supported"):
        parse({**blob, "snapshot": True})
    with pytest.raises(InvalidRequest, match="kind 'config'; schema 1 knows health"):
        parse({**blob, "sections": {"links": {"kind": "config", "items": {}}}})
    with pytest.raises(InvalidRequest, match="kind 'other'"):
        parse({**blob, "snapshot": 2, "sections": {"links": {"kind": "other", "items": {}}}})
    with pytest.raises(InvalidRequest, match="taken by 'demo', not 'ise'"):
        parse(blob, prefix="ise")
    with pytest.raises(InvalidRequest, match="'sections' must be an object"):
        parse({**blob, "sections": []})
    with pytest.raises(InvalidRequest, match="section 'links' must be an object"):
        parse({**blob, "sections": {"links": {"items": []}}})
    with pytest.raises(InvalidRequest, match="item links/a must be an object"):
        parse({**blob, "sections": {"links": {"items": {"a": 1}}}})


# --- compare -------------------------------------------------------------------------------------------


def test_compare_recorded_inet_failure_names_bfd_drop_and_sla_miss_only() -> None:
    before = _fixture("catsdwan-br1-before.json")
    after = _fixture("catsdwan-br1-after-inet-down.json")
    result = compare(before, after, prefix="catsdwan")
    assert result["verdict"] == "4 major changes; 11 items unchanged."
    assert result["counts"] == {"major": 4, "minor": 0, "info": 0}
    assert [(c["section"], c["kind"], c["item"]) for c in result["changes"]] == [
        ("alarms", "added", "br1|bfd-state-change|biz-internet"),
        ("app_route_sla", "degraded", "br1|biz-internet|hub1|biz-internet"),
        ("bfd", "degraded", "br1|biz-internet|br2|biz-internet"),
        ("bfd", "degraded", "br1|biz-internet|hub1|biz-internet"),
    ]
    assert result["changes"][1]["what"] == (
        "App-route SLA br1|biz-internet|hub1|biz-internet: healthy -> unhealthy; "
        "loss_pct 0 -> 100 (past the limit 5)"
    )
    assert (
        result["changes"][0]["what"]
        == "Active alarms br1 BFD down on biz-internet: new and unhealthy (major)"
    )
    assert "notes" not in result and "integrity" not in result
    # The reverse direction is the recovery.
    back = compare(after, before)
    assert back["counts"] == {"major": 0, "minor": 0, "info": 4}
    assert {c["kind"] for c in back["changes"]} == {"recovered", "removed"}
    # Same blob twice: nothing changed.
    same = compare(before, json.dumps(before))
    assert same["verdict"] == "No change (14 items compared)." and same["changes"] == []


def test_compare_item_rules() -> None:
    before = _snap(
        gone_ok={"ok": True},
        gone_bad={"ok": False},
        gone_plain={},
        recovers={"ok": False, "state": "down"},
        renamed={"state": "a"},
        version={"ok": True, "version": "17.15"},
        worse={"ok": True, "loss_pct": 1},
        better={"ok": True, "loss_pct": 4},
        back={"ok": True, "loss_pct": 9},
        noise={"ok": True, "loss_pct": 0},
        flag={"ok": None},
    ).blob()
    after = _snap(
        recovers={"ok": True, "state": "up"},
        renamed={"state": "b"},
        version={"ok": True, "version": "17.18"},
        worse={"ok": True, "loss_pct": 3},
        better={"ok": True, "loss_pct": 1},
        back={"ok": True, "loss_pct": 2},
        noise={"ok": True, "loss_pct": 0.5},
        flag={"ok": True},
        new_ok={"ok": True},
        new_bad={"ok": False, "state": "down"},
        new_plain={},
    ).blob()
    result = compare(before, after)
    by_item = {c["item"]: (c["severity"], c["kind"], c["what"]) for c in result["changes"]}
    assert by_item == {
        "gone_ok": ("major", "removed", "Links gone_ok: gone (was healthy)"),
        "new_bad": ("major", "added", "Links new_bad: new and unhealthy (down)"),
        "gone_plain": ("minor", "removed", "Links gone_plain: gone"),
        "new_plain": ("minor", "added", "Links new_plain: new"),
        "renamed": ("minor", "changed", "Links renamed: state a -> b"),
        "version": ("minor", "changed", "Links version: version 17.15 -> 17.18"),
        "worse": ("minor", "worse", "Links worse: loss_pct 1 -> 3"),
        "flag": ("minor", "changed", "Links flag: ok None -> True"),
        "gone_bad": ("info", "removed", "Links gone_bad: gone (was unhealthy)"),
        "new_ok": ("info", "added", "Links new_ok: new"),
        "recovers": ("info", "recovered", "Links recovers: unhealthy -> healthy; state down -> up"),
        "better": ("info", "improved", "Links better: loss_pct 4 -> 1"),
        "back": ("info", "recovered", "Links back: loss_pct 9 -> 2 (back within the limit 5)"),
    }
    assert [c["severity"] for c in result["changes"]] == ["major"] * 2 + ["minor"] * 6 + ["info"] * 5
    assert result["unchanged_items"] == 1  # noise: 0 -> 0.5 is within the tolerance


def test_compare_notes_integrity_and_errors() -> None:
    before = _snap(a={"ok": True}).blob()
    after_snap = _snap(a={"ok": True})
    after_snap.scope = {"site": "s2"}
    after_snap.section("vpn", "VPN tunnels").fail("timed out")
    after = after_snap.blob()
    retyped = copy.deepcopy(before)
    retyped["sections"]["links"]["items"]["a"]["ok"] = False  # edited after it was taken
    result = compare(retyped, after)
    assert result["integrity"] == {"before": "altered"}
    notes = " ".join(result["notes"])
    assert "before blob differs from what the server returned" in notes
    assert "Scopes differ" in notes and "VPN tunnels: only in the after snapshot" in notes

    both = copy.deepcopy(after)
    both["sections"]["vpn"]["error"] = None  # readable before ...
    both["digest"] = digest(both)
    result = compare(both, after)  # ... unreadable after
    assert (
        result["changes"][0]["kind"] == "unreadable"
        and "could not be read: timed out" in result["changes"][0]["what"]
    )

    with pytest.raises(InvalidRequest, match="before is from 'demo' and after from 'ise'"):
        compare(before, {**before, "prefix": "ise"})


def test_compare_truncated_sections_skip_added_and_removed() -> None:
    b = Snapshot("demo", "demo-mcp-server")
    sec = b.section("links", "Links", max_items=3)
    for i in range(5):
        sec.item(f"l{i}", ok=True)
    a = Snapshot("demo", "demo-mcp-server")
    sec = a.section("links", "Links", max_items=3)
    for i in range(1, 6):
        sec.item(f"l{i}", ok=i != 2)
    result = compare(b.blob(), a.blob())
    assert [(c["item"], c["kind"]) for c in result["changes"]] == [("l2", "degraded")]
    assert any("truncated" in n for n in result["notes"])


def test_compare_caps_changes() -> None:
    n = MAX_CHANGES + 7
    b = Snapshot("demo", "demo-mcp-server")
    a = Snapshot("demo", "demo-mcp-server")
    sb, sa = b.section("links", "Links", max_items=n), a.section("links", "Links", max_items=n)
    for i in range(n):
        sb.item(f"l{i:03d}", ok=True)
        sa.item(f"l{i:03d}", ok=False)
    result = compare(b.blob(), a.blob())
    assert len(result["changes"]) == MAX_CHANGES and result["changes_omitted"] == 7
    assert result["counts"]["major"] == n


# --- tools helper ----------------------------------------------------------------------------------------


def _snapshot_server(state: dict[str, Any]) -> FastMCP[Any]:
    async def take(ctx: Context, site: str) -> Snapshot:
        state["calls"].append(site)
        if state.get("wrong_prefix"):
            return Snapshot("ise", "x")
        return _snap(link1={"ok": state["up"], "state": "up" if state["up"] else "down"})

    def register(reg: ToolRegistrar) -> None:
        register_snapshot_tools(
            reg, take, product="Demo", covers="links and their loss", site_doc="Demo site name; empty = all"
        )

    return build_server(SPEC, make_settings(), client_factory=DemoApp, register=[demo.register, register])


async def test_snapshot_tools_round_trip() -> None:
    state: dict[str, Any] = {"up": True, "calls": []}
    server = _snapshot_server(state)
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert tools["demo_snapshot"].annotations.read_only_hint is True  # type: ignore[union-attr]
        assert tools["demo_compare_snapshot"].annotations.read_only_hint is True  # type: ignore[union-attr]
        assert "links and their loss" in (tools["demo_snapshot"].description or "")
        blob = (await client.call_tool("demo_snapshot", {"site": "s1"})).structured_content
        assert blob is not None and blob["prefix"] == "demo" and blob["digest"] == digest(blob)

        state["up"] = False  # the change
        result = (await client.call_tool("demo_compare_snapshot", {"before": blob})).structured_content
        assert result is not None and result["counts"]["major"] == 1
        assert result["changes"][0]["what"] == "Links link1: healthy -> unhealthy; state up -> down"
        assert state["calls"] == ["s1", "s1"]  # the fresh 'after' used the before's scope

        after = (await client.call_tool("demo_snapshot", {})).structured_content
        as_text = (
            await client.call_tool(
                "demo_compare_snapshot", {"before": json.dumps(blob), "after": json.dumps(after)}
            )
        ).structured_content
        assert as_text is not None and as_text["counts"]["major"] == 1

        with pytest.raises(ToolError, match="taken by 'ise', not 'demo'"):
            await client.call_tool("demo_compare_snapshot", {"before": {**blob, "prefix": "ise"}})
        state["wrong_prefix"] = True
        with pytest.raises(ToolError, match="returned a 'ise' snapshot on the demo server"):
            await client.call_tool("demo_snapshot", {})


# --- suite_change_check ------------------------------------------------------------------------------------


def _suite_server() -> FastMCP[Any]:
    return build_server(
        SPEC, make_settings(**ALL_TIERS), client_factory=DemoApp, register=[demo.register, suite.register]
    )


async def _render(args: dict[str, str]) -> str:
    async with Client(_suite_server()) as client:
        return rendered_text(await client.get_prompt("suite_change_check", args))


def test_core_pack_is_clean() -> None:
    assert "suite_change_check" in suite.PACK
    assert pack_problems() == []
    sp = suite.PACK["suite_change_check"]
    assert sp.requires == ["catsdwan", "ise", "catc", "fmc"] and sp.tools == SNAPSHOT_TOOLS


async def test_change_check_pre_and_post() -> None:
    pre = await _render({"site": "br1", "phase": "pre", "servers": "catsdwan,ise"})
    assert "- catc leg skipped: server not connected; not checked: Catalyst Center" in pre
    assert "- fmc leg skipped" in pre
    assert '1. Call `catsdwan_snapshot` with site="br1". Keep the blob' in pre
    assert '2. Call `ise_snapshot` with site="br1".' in pre and "catc_snapshot" not in pre

    post = await _render(
        {"site": "br1", "phase": "post", "change": "banner on br1", "servers": "fmc-mcp-server"}
    )
    assert "1. Call `fmc_compare_snapshot` with before = the blob fmc's snapshot tool returned" in post
    assert "catsdwan_compare_snapshot" not in post and "'Expected' list" in post and "(banner on br1)" in post
    assert "call no write tool" in post

    assert "expected = the configuration objects the planned change itself touches" in post
    assert "'Changed outside the change'" in post and "`expected_unchanged`" in post
    named = await _render({"phase": "post", "servers": "fmc", "expected": "acp|mcp-allow-web,\n mcp-host-1"})
    assert 'expected="acp|mcp-allow-web, mcp-host-1", and no `after`' in named

    given = await _render({"phase": "POST", "before": '{"snapshot": 1}', "servers": "ise"})
    assert "pre-phase blobs given at the end of this message" in given
    assert (
        given.endswith('Pre-phase blobs:\n{"snapshot": 1}')
        and 'site="" (everything each server sees)' in given
    )

    unknown = await _render({"site": "br1"})
    assert "First check which of these tools you have: `catsdwan_*`, `ise_*`, `catc_*`, `fmc_*`." in unknown

    nothing = await _render({"servers": "cml"})
    assert nothing.endswith(
        "None of these servers is connected, so there is nothing to run: say so and stop."
    )

    with pytest.raises(Exception, match="phase must be 'pre' or 'post'"):
        await _render({"phase": "during"})


async def test_change_check_passes_the_prompt_kit(tmp_path: Path) -> None:
    server = _suite_server()
    await assert_prompt_contract(server, "demo", known_prefixes={"other"})
    # No peer manifests: nothing to check beyond registration.
    assert await suite_pack_problems(server, "demo") == []
    # A peer manifest without its snapshot tools yet: reported, unless pending.
    (tmp_path / "ise.json").write_text(
        json.dumps({"manifest": 1, "server": "ise-mcp-server", "prefix": "ise", "tools": {"ise_status": {}}})
    )
    # Only this prompt's legs: other pack prompts have ise legs of their own.
    problems = [p for p in await suite_pack_problems(server, "demo", tmp_path) if "suite_change_check" in p]
    assert problems == [
        "suite prompt suite_change_check: ise leg calls 'ise_snapshot', which the ise manifest does not list",
        "suite prompt suite_change_check: ise leg calls 'ise_compare_snapshot', "
        "which the ise manifest does not list",
    ]
    problems = await suite_pack_problems(server, "demo", tmp_path, pending=SNAPSHOT_TOOLS)
    assert [p for p in problems if "suite_change_check" in p] == []


SUITE = Path(__file__).resolve().parents[3]
OWN_MANIFEST = {
    "catsdwan": "cat-sdwan-mcp-server",
    "ise": "ise-mcp-server",
    "catc": "catc-mcp-server",
    "fmc": "fmc-mcp-server",
    "cml": "cml-mcp-server",
}


async def test_pack_against_suite_checkout_manifests() -> None:
    """With the other repos checked out next to this one (~/MCP-Suite), check the pack against each
    server's own committed manifest. Skipped in CI, where only this repo is checked out."""
    paths = [
        SUITE / repo / "tests" / "manifests" / f"{prefix}.json"
        for prefix, repo in OWN_MANIFEST.items()
        if (SUITE / repo / "tests" / "manifests" / f"{prefix}.json").exists()
    ]
    if not paths:
        pytest.skip("no sibling server checkouts with manifests")
    server = _suite_server()
    present = {p.stem for p in paths}
    waived = (*SNAPSHOT_TOOLS, *PENDING_TOOLS, *DECOMMISSION_PENDING)
    pending = [t for t in waived if t.split("_", 1)[0] in present]
    problems = await suite_pack_problems(server, "demo", *paths, pending=pending)
    # A waiver for a tool a server now ships is reported: that is the signal to drop it here.
    assert all("is in `pending` but" in p for p in problems), problems


# --- schema 2: config sections (C78) ---------------------------------------------------------------------

RULE_BODY = {
    "name": "mcp-allow-web",
    "action": "ALLOW",
    "destinationPorts": {"objects": [{"name": "HTTPS", "id": "a1"}]},
    "metadata": {"timestamp": 1761600000, "lastUser": {"name": "admin"}},
    "links": {"self": "https://fmc.example.com/api/x"},
}


def _config_snap(rules: dict[str, Any], *, health: bool = True) -> Snapshot:
    """A demo v2 snapshot: a health section plus an access-rule config section (key -> rule body)."""
    s = Snapshot("demo", "demo-mcp-server", scope={"site": "s1"})
    if health:
        s.section("links", "Links").item("link1", ok=True, state="up")
    sec = s.section("access_rules", "Access rules", kind="config")
    for key, body in rules.items():
        sec.config(key, digest=config_digest(body, ignore=("metadata", "links")), label=body.get("name"))
    return s


def test_config_digest_ignores_volatile_keys_at_any_depth() -> None:
    d = config_digest(RULE_BODY, ignore=("metadata", "links"))
    assert d.startswith("sha256:") and len(d) == len("sha256:") + 16
    touched = {**RULE_BODY, "metadata": {"timestamp": 1761700000}, "links": {}}
    assert config_digest(touched, ignore=("metadata", "links")) == d
    reordered = dict(reversed(list(RULE_BODY.items())))
    assert config_digest(reordered, ignore=("metadata", "links")) == d
    nested = {"rules": [RULE_BODY]}
    assert config_digest(nested, ignore=("metadata", "links")) == config_digest(
        {"rules": [touched]}, ignore=("metadata", "links")
    )
    assert config_digest({**RULE_BODY, "action": "BLOCK"}, ignore=("metadata", "links")) != d
    assert config_digest(RULE_BODY) != d  # nothing ignored: the timestamp counts


def test_config_section_builder_and_blob() -> None:
    s = Snapshot("demo", "demo-mcp-server")
    sec = s.section("templates", "Templates per device", kind="config")
    sec.config("edge1|mcp-banner", version=3, label="mcp-banner on edge1")
    sec.config("edge2|mcp-banner", version="3.1", digest="sha256:00", label="edge2|mcp-banner")
    blob = s.blob()
    assert blob["snapshot"] == SCHEMA_VERSION == 2
    assert blob["sections"]["templates"] == {
        "title": "Templates per device",
        "kind": "config",
        "items": {
            "edge1|mcp-banner": {"version": 3, "label": "mcp-banner on edge1"},
            "edge2|mcp-banner": {"version": "3.1", "digest": "sha256:00"},
        },
    }
    assert parse(json.dumps(blob)) == blob and blob["digest"] == digest(blob)
    with pytest.raises(ValueError, match="needs a digest or a version"):
        sec.config("edge3|x")
    with pytest.raises(ValueError, match="added twice"):
        sec.config("edge1|mcp-banner", version=4)
    with pytest.raises(ValueError, match="is a config section: add objects with config"):
        sec.item("x", ok=True)
    with pytest.raises(ValueError, match="is a health section: add items with item"):
        s.section("links", "Links").config("x", version=1)
    with pytest.raises(ValueError, match="kind must be one of"):
        s.section("other", "Other", kind="state")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no metrics"):
        s.section("cfg2", "C", kind="config", metrics={"x": Metric("lower")})


def test_parse_expected() -> None:
    assert parse_expected(None) == [] and parse_expected("") == []
    assert parse_expected(" a, b\nc,,a ") == ["a", "b", "c"]
    assert parse_expected(["x", " y ", ""]) == ["x", "y"]


def test_compare_v2_unexpected_extra_object_vs_expected_rule() -> None:
    """The C78 live-proof story on synthetic blobs: the change edits one rule; someone also adds an
    object. With expected=the rule, only the extra object is left as an unexpected change."""
    other = {"name": "mcp-deny-telnet", "action": "BLOCK"}
    before = _config_snap({"acp|mcp-allow-web": RULE_BODY, "acp|mcp-deny-telnet": other}).blob()
    edited = {**RULE_BODY, "action": "BLOCK", "metadata": {"timestamp": 1761700000}}
    extra = {"name": "mcp-extra", "action": "ALLOW"}
    after = _config_snap(
        {"acp|mcp-allow-web": edited, "acp|mcp-deny-telnet": other, "acp|mcp-extra": extra}
    ).blob()

    plain = compare(before, after)
    assert [(c["kind"], c["item"]) for c in plain["changes"]] == [
        ("changed", "acp|mcp-allow-web"),
        ("added", "acp|mcp-extra"),
    ]
    assert plain["counts"] == {"major": 0, "minor": 2, "info": 0} and "expected" not in plain
    assert plain["changes"][1] == {
        "severity": "minor",
        "kind": "added",
        "section": "access_rules",
        "item": "acp|mcp-extra",
        "what": "Access rules mcp-extra: new object",
        "config": True,
        "label": "mcp-extra",
    }

    for name in ("acp|mcp-allow-web", "access_rules/acp|mcp-allow-web", "mcp-allow-web"):
        result = compare(before, after, expected=[name])
        assert [(c["kind"], c["item"]) for c in result["changes"]] == [("added", "acp|mcp-extra")], name
        assert result["counts"] == {"major": 0, "minor": 1, "info": 0}
        assert [(c["severity"], c["kind"], c["item"]) for c in result["expected"]] == [
            ("expected", "changed", "acp|mcp-allow-web")
        ]
        assert result["expected"][0]["what"].startswith("Access rules mcp-allow-web: digest sha256:")
        assert result["verdict"] == "1 minor change; 2 items unchanged; 1 expected config change."
        assert "expected_unchanged" not in result and "notes" not in result

    # Both named: nothing unexpected is left.
    both = compare(before, after, expected="mcp-allow-web, mcp-extra")
    assert both["changes"] == [] and len(both["expected"]) == 2
    assert both["verdict"] == "No unexpected change (2 items unchanged); 2 expected config changes."

    # An expected object that did not change, and one that is in neither blob, are named.
    missed = compare(before, after, expected=["mcp-deny-telnet", "mcp-nope"])
    assert missed["expected_unchanged"] == ["mcp-deny-telnet"]
    assert missed["expected_not_found"] == ["mcp-nope"] and missed["expected"] == []
    notes = " ".join(missed["notes"])
    assert "Expected but unchanged: mcp-deny-telnet" in notes and "in neither snapshot: mcp-nope" in notes

    # Removal and a rename are config changes too; same blob twice is no change.
    gone = compare(after, before)
    assert ("removed", "acp|mcp-extra") in [(c["kind"], c["item"]) for c in gone["changes"]]
    assert compare(after, json.dumps(after))["verdict"] == "No change (4 items compared)."
    renamed = _config_snap({"acp|mcp-deny-telnet": {**other, "name": "mcp-deny-telnet-2"}}, health=False)
    r = compare(_config_snap({"acp|mcp-deny-telnet": other}, health=False).blob(), renamed.blob())
    assert "renamed mcp-deny-telnet -> mcp-deny-telnet-2" in r["changes"][0]["what"]


def test_compare_v2_version_change_and_truncated_config() -> None:
    def tmpl(versions: dict[str, int], max_items: int = 100) -> dict[str, Any]:
        s = Snapshot("demo", "demo-mcp-server")
        sec = s.section("templates", "Template versions", kind="config", max_items=max_items)
        for k, v in versions.items():
            sec.config(k, version=v)
        return s.blob()

    result = compare(tmpl({"edge1|base": 3, "edge2|base": 3}), tmpl({"edge1|base": 4, "edge2|base": 3}))
    assert result["changes"][0]["what"] == "Template versions edge1|base: version 3 -> 4"
    cut = compare(
        tmpl({f"e{i}|base": 1 for i in range(5)}, max_items=3),
        tmpl({f"e{i}|base": 1 for i in range(1, 6)}, max_items=3),
    )
    assert cut["changes"] == [] and any("truncated" in n for n in cut["notes"])


def test_compare_v1_blob_against_v2_blob() -> None:
    """A v1 (health-only) blob taken before an upgrade still compares with a v2 blob: the health section is
    diffed and the new config section is noted as only in the after snapshot."""
    v1 = _fixture("catsdwan-br1-before.json")
    assert v1["snapshot"] == 1
    v2 = copy.deepcopy(v1)
    v2["snapshot"] = 2
    v2["sections"]["config_groups"] = {
        "title": "Configuration groups",
        "kind": "config",
        "items": {"mcp-branch": {"version": 7, "digest": "sha256:0011223344556677"}},
    }
    v2["digest"] = digest(v2)
    result = compare(v1, v2, prefix="catsdwan", expected=["mcp-branch"])
    assert result["changes"] == [] and result["expected"] == []
    notes = " ".join(result["notes"])
    assert "Configuration groups: only in the after snapshot; not compared." in notes
    assert "in neither snapshot: mcp-branch" in notes

    mixed = copy.deepcopy(v2)
    mixed["sections"]["config_groups"].pop("kind")
    mixed["digest"] = digest(mixed)
    r = compare(v2, mixed)
    assert r["changes"] == [] and any("config section in one snapshot only" in n for n in r["notes"])


async def test_compare_tool_takes_expected() -> None:
    state: dict[str, Any] = {"rules": {"acp|mcp-allow-web": RULE_BODY}}

    async def take(ctx: Context, site: str) -> Snapshot:
        return _config_snap(state["rules"])

    def register(reg: ToolRegistrar) -> None:
        register_snapshot_tools(reg, take, product="Demo", covers="links, rules", site_doc="Demo site")

    server = build_server(SPEC, make_settings(), client_factory=DemoApp, register=[demo.register, register])
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert "expected" in (tools["demo_compare_snapshot"].input_schema["properties"])
        blob = (await client.call_tool("demo_snapshot", {"site": "s1"})).structured_content
        state["rules"] = {
            "acp|mcp-allow-web": {**RULE_BODY, "action": "BLOCK"},
            "acp|mcp-extra": {"name": "mcp-extra"},
        }
        result = (
            await client.call_tool("demo_compare_snapshot", {"before": blob, "expected": "mcp-allow-web"})
        ).structured_content
        assert result is not None
        assert [c["item"] for c in result["changes"]] == ["acp|mcp-extra"]
        assert [c["item"] for c in result["expected"]] == ["acp|mcp-allow-web"]
