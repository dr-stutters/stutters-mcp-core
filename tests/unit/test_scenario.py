"""C23 scenario manifests: structure, hand-offs, and the check against the servers' tool manifests."""

import json
from pathlib import Path
from typing import Any

import pytest

from stutters_mcp_core.manifest import parse_tool_manifest
from stutters_mcp_core.testing import assert_scenarios, load_scenario, scenario_problems
from stutters_mcp_core.testing.scenario import parse_scenario, scenario_file_problems

from .demo import create_server, make_settings

DOCS = Path(__file__).resolve().parents[2] / "docs" / "scenarios"


def _manifest(prefix: str, tools: list[str], prompts: dict[str, dict[str, Any]] | None = None) -> Any:
    return parse_tool_manifest(
        {
            "manifest": 1,
            "server": f"{prefix}-mcp-server",
            "prefix": prefix,
            "tools": {t: {"tier": "read"} for t in tools},
            "prompts": prompts or {},
        }
    )


MANIFESTS = [
    _manifest(
        "aaa",
        ["aaa_find_host", "aaa_quarantine"],
        {
            "aaa_incident": {
                "uses": ["aaa", "bbb"],
                "references": ["aaa_find_host", "bbb_block"],
                "rendered": True,
            }
        },
    ),
    _manifest("bbb", ["bbb_block", "bbb_release"]),
]


def _scenario(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "scenario": 1,
        "name": "demo-incident",
        "title": "Demo incident",
        "servers": ["aaa", "bbb"],
        "steps": [
            {"prompt": "aaa_incident", "calls": ["aaa_find_host", "bbb_block"], "leaves": ["host_ip"]},
            {"tool": "aaa_quarantine", "needs": ["host_ip"]},
            {"tool": "bbb_release", "needs": ["host_ip"]},
        ],
        "requires": {"recipes": ["demo-recipe"], "ram_gb": 16, "cml_licences": 4},
    }
    data.update(over)
    return data


def _problems(**over: Any) -> list[str]:
    return scenario_problems(parse_scenario(_scenario(**over)), MANIFESTS)


def test_valid_scenario_passes() -> None:
    sc = parse_scenario(_scenario())
    assert sc.names == ["aaa_incident", "aaa_find_host", "bbb_block", "aaa_quarantine", "bbb_release"]
    assert sc.requires == {"recipes": ["demo-recipe"], "ram_gb": 16, "cml_licences": 4}
    assert scenario_problems(sc, MANIFESTS) == []


def test_renamed_tool_and_prompt_fail() -> None:
    renamed = [
        _manifest("aaa", ["aaa_locate_host", "aaa_quarantine"], {"aaa_incident_v2": {"uses": ["aaa"]}}),
        MANIFESTS[1],
    ]
    problems = scenario_problems(parse_scenario(_scenario()), renamed)
    assert any("has no prompt 'aaa_incident'" in p for p in problems)
    assert any("calls 'aaa_find_host', which the aaa manifest does not list" in p for p in problems)


def test_missing_server_manifest_and_step_outside_servers() -> None:
    problems = scenario_problems(parse_scenario(_scenario()), MANIFESTS[:1])
    assert any("no manifest for server 'bbb'" in p for p in problems)
    steps = [{"tool": "ccc_thing"}]
    problems = _problems(steps=steps)
    assert any("server 'ccc' is not in `servers`" in p for p in problems)


def test_malformed_requires_is_reported() -> None:
    bad = {"ram_gb": "lots", "cml_licences": -1, "recipes": "one", "fmc_slots": 2}
    with pytest.raises(ValueError) as e:
        parse_scenario(_scenario(requires=bad))
    msg = str(e.value)
    assert "`requires`.ram_gb must be a whole number" in msg
    assert "`requires`.cml_licences must be a whole number" in msg
    assert "`requires`.recipes must be a list of strings" in msg
    assert "unknown key 'fmc_slots'" in msg
    with pytest.raises(ValueError, match="`requires` must be a mapping"):
        parse_scenario(_scenario(requires=["recipes"]))


def test_structural_problems_are_all_listed() -> None:
    with pytest.raises(ValueError) as e:
        parse_scenario(
            {
                "scenario": 2,
                "name": "Bad Name",
                "servers": [],
                "extra": 1,
                "steps": [
                    {"prompt": "aaa_x", "tool": "aaa_y"},
                    {"tool": "nounderscore"},
                    "x",
                    {"tool": "a_b", "x": 1},
                ],
            }
        )
    msg = str(e.value)
    for expected in (
        'expected "scenario: 1"',
        "unknown key 'extra'",
        "`name` must be",
        "`servers` must name at least one",
        "step 1: give exactly one of `prompt` or `tool`",
        "step 2: tool 'nounderscore' is not a <prefix>_<name>",
        "step 3 must be a mapping",
        "step 4: unknown key 'x'",
    ):
        assert expected in msg
    with pytest.raises(ValueError, match="`steps` must be a non-empty list"):
        parse_scenario(_scenario(steps=[]))


def test_handoff_needs_an_earlier_step() -> None:
    steps = [
        {"tool": "aaa_quarantine", "needs": ["host_ip"]},
        {"tool": "aaa_find_host", "leaves": ["host_ip"]},
    ]
    problems = _problems(steps=steps)
    assert any("needs 'host_ip', which no earlier step leaves" in p for p in problems)


def test_wrong_kind_uses_and_dropped_reference() -> None:
    steps = [
        {"tool": "aaa_incident"},
        {"prompt": "bbb_block"},
        {"prompt": "aaa_incident", "calls": ["aaa_quarantine"]},
    ]
    problems = _problems(steps=steps, servers=["aaa"])
    assert any("(aaa_incident): is a prompt in the aaa manifest, not a tool" in p for p in problems)
    assert any("server 'bbb' is not in `servers`" in p for p in problems)
    assert any("the prompt uses 'bbb', which is not in `servers`" in p for p in problems)
    assert any("calls 'aaa_quarantine', which the prompt no longer references" in p for p in problems)


def test_pending_and_no_manifest() -> None:
    steps = [{"prompt": "aaa_new_flow", "calls": ["bbb_future"]}, {"tool": "win_create_user"}]
    ok = _problems(
        steps=steps,
        servers=["aaa", "bbb", "win"],
        no_manifest=["win"],
        pending=["aaa_new_flow", "bbb_future"],
    )
    assert ok == []
    stale = _problems(
        steps=[{"tool": "aaa_quarantine"}],
        pending=["aaa_quarantine", "aaa_unused"],
        no_manifest=["bbb", "zzz"],
    )
    assert any("'aaa_quarantine' is in `pending` but exists now" in p for p in stale)
    assert any("`pending` names 'aaa_unused', which no step uses" in p for p in stale)
    assert any("'bbb' is in `no_manifest` but has a manifest now" in p for p in stale)
    assert any("`no_manifest` names 'zzz', which is not in `servers`" in p for p in stale)


def test_files_yaml_json_and_duplicates(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text(json.dumps(_scenario()))
    (tmp_path / "b.yaml").write_text(
        "scenario: 1\nname: demo-incident\nservers: [aaa]\nsteps:\n  - tool: aaa_gone\n"
    )
    (tmp_path / "c.yml").write_text("scenario: [unclosed\n")
    (tmp_path / "notes.md").write_text("ignored")
    assert load_scenario(tmp_path / "b.yaml").steps[0].name == "aaa_gone"
    problems = scenario_file_problems([tmp_path], MANIFESTS)
    assert any("c.yml: invalid YAML" in p for p in problems)
    assert any("scenario name 'demo-incident' is also used by" in p for p in problems)
    assert any("has no tool 'aaa_gone'" in p for p in problems)
    (tmp_path / "empty").mkdir()
    assert scenario_file_problems([tmp_path / "empty"], MANIFESTS)[0].startswith("no scenario manifests")
    with pytest.raises(ValueError, match="invalid JSON"):
        (tmp_path / "d.json").write_text("{")
        load_scenario(tmp_path / "d.json")


async def test_assert_scenarios_with_local_server(tmp_path: Path) -> None:
    other = tmp_path / "manifests"
    other.mkdir()
    other_manifest = {
        "manifest": 1,
        "server": "other-mcp-server",
        "prefix": "other",
        "tools": {"other_list_things": {"tier": "read"}},
        "prompts": {},
    }
    (other / "other.json").write_text(json.dumps(other_manifest))
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    good = {
        "scenario": 1,
        "name": "demo-audit",
        "servers": ["demo", "other"],
        "steps": [{"prompt": "demo_audit_items", "calls": ["demo_get_item"]}, {"tool": "other_list_things"}],
    }
    (scenarios / "demo-audit.json").write_text(json.dumps(good))
    server = create_server(make_settings(mcp_enable_writes=True, mcp_enable_dangerous=True))
    await assert_scenarios(scenarios, manifests=other, server=server, prefix="demo")
    good["steps"] = [{"tool": "demo_renamed"}]
    (scenarios / "demo-audit.json").write_text(json.dumps(good))
    with pytest.raises(AssertionError, match="the demo manifest has no tool 'demo_renamed'"):
        await assert_scenarios(scenarios, manifests=[other], server=server, prefix="demo")
    with pytest.raises(ValueError, match="pass prefix="):
        await assert_scenarios(scenarios, server=server)


def test_committed_scenarios_parse() -> None:
    """The catalogue's manifests are well formed (the servers' CI checks them against real manifests)."""
    files = sorted(DOCS.glob("*.yaml"))
    assert {f.stem for f in files} >= {"incident", "zero-touch-site", "secure-branch", "user-firewall"}
    for f in files:
        sc = load_scenario(f)
        assert sc.name == f.stem and sc.epic
