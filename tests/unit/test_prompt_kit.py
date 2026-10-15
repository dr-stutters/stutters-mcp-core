"""C9 prompt test kit (manifests, cross-server references, snapshots) and G4 secret files."""

import json
from pathlib import Path
from typing import Any

import pytest

from stutters_mcp_core.cli import run_cli
from stutters_mcp_core.manifest import build_manifest, parse_tool_manifest
from stutters_mcp_core.testing import (
    assert_inbound_references,
    assert_prompt_contract,
    assert_prompt_snapshots,
    inbound_reference_problems,
    load_tool_manifest,
    load_tool_manifests,
    prompt_contract_problems,
    prompt_snapshot_problems,
    read_tool_manifests,
)

from .demo import SPEC, DemoSettings, create_server, make_settings

ALL_TIERS = {"mcp_enable_writes": True, "mcp_enable_dangerous": True}


def _other_manifest(refs: list[str]) -> dict[str, Any]:
    return {
        "manifest": 1,
        "server": "other-mcp-server",
        "prefix": "other",
        "version": "1.0.0",
        "tools": {"other_list_things": {"tier": "read"}},
        "prompts": {"other_flow": {"uses": ["other", "demo"], "tier": "read", "references": refs}},
    }


# --- manifest + CLI ------------------------------------------------------------------------


def test_cli_tools_json_writes_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # no .env; DEMO_URL unset -> placeholder URL, no appliance contact
    monkeypatch.delenv("DEMO_URL", raising=False)
    out = tmp_path / "demo.json"
    run_cli(SPEC, DemoSettings, create_server, argv=["tools", "--json", "--out", str(out)])
    data = json.loads(out.read_text())
    assert data["manifest"] == 1 and data["prefix"] == "demo" and data["server"] == "demo-mcp-server"
    assert data["tools"]["demo_wipe"]["tier"] == "dangerous"  # all tiers on by default
    assert data["tools"]["demo_create_item"] == {"tier": "write", "title": "Create item"}
    audit = data["prompts"]["demo_audit_items"]
    assert audit["uses"] == ["demo", "other"] and audit["arguments"] == ["kind", "max_items"]
    assert audit["references"] == ["demo_get_item", "demo_list_items", "other_list_things"]
    assert data["prompts"]["demo_cleanup_items"]["tier"] == "write"
    m = load_tool_manifest(out)
    assert {"demo_status", "demo_wipe", "demo_audit_items"} <= m.names
    assert out.read_text().endswith("}\n")


def test_cli_tools_text_and_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEMO_URL", "https://demo.test")
    monkeypatch.setenv("MCP_ENABLE_WRITES", "true")  # --tier overrides the environment
    run_cli(SPEC, DemoSettings, create_server, argv=["tools", "--tier", "read"])
    text = capsys.readouterr().out
    assert "read      demo_list_items" in text and "demo_create_item" not in text
    assert "prompts:" in text and "demo_audit_items  uses=demo,other" in text
    assert "demo_cleanup_items" not in text


async def test_manifest_parse_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a stutters tool manifest"):
        parse_tool_manifest({"tools": {}})
    with pytest.raises(ValueError, match="'server' and 'prefix'"):
        parse_tool_manifest({"manifest": 1, "tools": {}})
    with pytest.raises(ValueError, match="must be objects"):
        parse_tool_manifest({"manifest": 1, "server": "s", "prefix": "s", "tools": []})
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_tool_manifest(bad)
    (tmp_path / "bad.json").unlink()
    d = tmp_path / "manifests"
    d.mkdir()
    built = await build_manifest(create_server(make_settings()), server="demo-mcp-server", prefix="demo")
    (d / "demo.json").write_text(json.dumps(built))
    (d / "other.json").write_text(json.dumps(_other_manifest([])))
    loaded = load_tool_manifests(d)
    assert set(loaded) == {"demo", "other"} and loaded["other"] == {"other_list_things", "other_flow"}
    (d / "dup.json").write_text(json.dumps(_other_manifest([])))
    with pytest.raises(ValueError, match="two manifests for prefix 'other'"):
        load_tool_manifests(d)


# --- cross-server references -----------------------------------------------------------------


async def test_external_tools_check_references() -> None:
    server = create_server(make_settings(**ALL_TIERS))
    await assert_prompt_contract(server, "demo", external_tools={"other": ["other_list_things"]})
    problems = await prompt_contract_problems(server, "demo", external_tools={"other": ["other_renamed"]})
    assert problems == [
        "prompt demo_audit_items: references 'other_list_things', which the other manifest does not list"
    ]
    # Manifest prefixes count as known, so `uses` typos are still found when only manifests are given.
    problems = await prompt_contract_problems(server, "demo", external_tools={"fmc": []})
    assert "prompt demo_audit_items: `uses` names unknown server prefix 'other'" in problems


async def test_inbound_references(tmp_path: Path) -> None:
    server = create_server(make_settings(**ALL_TIERS))
    path = tmp_path / "other.json"
    path.write_text(json.dumps(_other_manifest(["other_list_things", "demo_get_item", "demo_gone"])))
    problems = await inbound_reference_problems(server, "demo", read_tool_manifests(path))
    assert problems == [
        "other-mcp-server prompt other_flow: references 'demo_gone', which this server does not register"
    ]
    path.write_text(json.dumps(_other_manifest(["demo_get_item", "demo_audit_items"])))
    await assert_inbound_references(server, "demo", tmp_path)


# --- snapshots -------------------------------------------------------------------------------


async def test_prompt_snapshots_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UPDATE_PROMPT_SNAPSHOTS", raising=False)
    server = create_server(make_settings(**ALL_TIERS))
    samples = {"demo_audit_items": {"kind": "router"}}
    snaps = tmp_path / "snaps"

    first = await prompt_snapshot_problems(server, snaps, samples=samples)
    assert len(first) == 2 and all("new snapshot written" in p for p in first)
    text = (snaps / "demo_audit_items.md").read_text()
    assert text.startswith("# demo_audit_items\n\ntitle: Audit items\n")
    assert 'arguments: {"kind": "router", "max_items": "1"}' in text
    assert "## user\n\n1. Call `demo_list_items` and keep the router items (at most 1)." in text
    await assert_prompt_snapshots(server, snaps, samples=samples)  # now matches

    changed = await prompt_snapshot_problems(server, snaps, samples={"demo_audit_items": {"kind": "switch"}})
    assert len(changed) == 1 and "rendering differs" in changed[0]
    assert "-1. Call `demo_list_items` and keep the router items" in changed[0]
    assert "+1. Call `demo_list_items` and keep the switch items" in changed[0]

    (snaps / "demo_removed.md").write_text("old\n")
    stale = await prompt_snapshot_problems(server, snaps, samples=samples)
    assert stale == [
        f"{snaps / 'demo_removed.md'}: no such prompt any more (UPDATE_PROMPT_SNAPSHOTS=1 deletes it)"
    ]

    monkeypatch.setenv("UPDATE_PROMPT_SNAPSHOTS", "1")
    assert (
        await prompt_snapshot_problems(server, snaps, samples={"demo_audit_items": {"kind": "switch"}}) == []
    )
    assert not (snaps / "demo_removed.md").exists()
    assert "switch items" in (snaps / "demo_audit_items.md").read_text()
    with pytest.raises(AssertionError, match="rendering differs"):
        await assert_prompt_snapshots(server, snaps, samples=samples, update=False)


# --- G4: <VAR>_FILE secrets -------------------------------------------------------------------


def test_password_file_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "pw"
    secret.write_text("s3cret\n")
    monkeypatch.setenv("DEMO_URL", "https://demo.test")
    monkeypatch.setenv("DEMO_PASSWORD_FILE", str(secret))
    s = DemoSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.password_value() == "s3cret" and "s3cret" not in repr(s)

    monkeypatch.setenv("DEMO_PASSWORD", "plain")
    with pytest.raises(ValueError, match="set DEMO_PASSWORD or DEMO_PASSWORD_FILE, not both"):
        DemoSettings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.delenv("DEMO_PASSWORD")
    monkeypatch.setenv("DEMO_PASSWORD_FILE", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="DEMO_PASSWORD_FILE: cannot read"):
        DemoSettings(_env_file=None)  # type: ignore[call-arg]


def test_token_file_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "tok"
    secret.write_text("Bearer abc\r\n")
    env = tmp_path / ".env"
    env.write_text(f"DEMO_URL=https://demo.test\nDEMO_TOKEN_FILE={secret}\n")
    for var in ("DEMO_URL", "DEMO_TOKEN", "DEMO_TOKEN_FILE", "DEMO_PASSWORD_FILE"):
        monkeypatch.delenv(var, raising=False)
    s = DemoSettings(_env_file=str(env))  # type: ignore[call-arg]
    assert s.token_value() == "abc"  # file value still goes through the field validators
    assert DemoSettings(_env_file=None, url="https://x.test").token is None  # type: ignore[call-arg]


def test_auth_tokens_file_is_left_to_its_own_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = tmp_path / "tokens"
    tokens.write_text("alice:sha256:" + "a" * 64 + "\n")
    monkeypatch.setenv("MCP_AUTH_TOKENS_FILE", str(tokens))
    s = make_settings()
    assert s.mcp_auth_tokens is None and s.auth_token_hashes() == {"alice": "a" * 64}


# --- registered examples and placeholders ---------------------------------------------------


async def test_prompt_example_is_used_by_kit_and_manifest(tmp_path: Path) -> None:
    from typing import Annotated

    from fastmcp import FastMCP
    from pydantic import Field

    from stutters_mcp_core.prompts import PromptRegistrar, tool_ref

    mcp: FastMCP[Any] = FastMCP("x")
    reg = PromptRegistrar(mcp, None, "demo")
    with pytest.raises(ValueError, match="must be strings"):
        reg.prompt(example={"port": 23})  # type: ignore[dict-item]

    @reg.prompt(uses=["demo", "other"], example={"mac": "AA:BB:CC:DD:EE:FF"})
    def demo_find(
        mac: Annotated[str, Field(description="Endpoint MAC address")],
        port: Annotated[int | None, Field(ge=1, le=65535, description="A port")] = None,
    ) -> str:
        """Find an endpoint by MAC and hand over to the other server for its switch port."""
        if ":" not in mac:
            raise ValueError("not a MAC")
        return f"Call {tool_ref('other_find')} for {mac} port {port}."

    await assert_prompt_contract(mcp, "demo", external_tools={"other": ["other_find"]})
    m = await build_manifest(mcp, server="x", prefix="demo")
    assert m["prompts"]["demo_find"]["rendered"] is True
    assert m["prompts"]["demo_find"]["references"] == ["other_find"]
    await assert_prompt_snapshots(mcp, tmp_path, update=True)
    assert 'arguments: {"mac": "AA:BB:CC:DD:EE:FF", "port": "1"}' in (tmp_path / "demo_find.md").read_text()

    @reg.prompt()
    def demo_needs_real_args(mac: Annotated[str, Field(description="Endpoint MAC address")]) -> str:
        """A prompt without an example whose placeholder arguments do not validate."""
        raise ValueError("not a MAC")

    m = await build_manifest(mcp, server="x", prefix="demo")
    assert m["prompts"]["demo_needs_real_args"] == {
        "uses": ["demo"],
        "tier": "read",
        "arguments": ["mac"],
        "references": [],
        "rendered": False,
    }


def test_init_value_skips_secret_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_URL", "https://demo.test")
    monkeypatch.setenv("DEMO_PASSWORD_FILE", str(tmp_path / "missing"))
    s = DemoSettings(_env_file=None, password="given")  # type: ignore[call-arg,arg-type]
    assert s.password_value() == "given"
