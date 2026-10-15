"""Per-user roles (G2): ro / rw / admin gate tool tiers per caller; identity in previews and audit."""

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from stutters_mcp_core.roles import Role, RoleMap
from stutters_mcp_core.testing import as_caller, call_tool

from .demo import BASE, create_server, make_settings

RWD = {"mcp_enable_writes": True, "mcp_enable_dangerous": True}
ROLES = "alice:admin, bob:rw, ci-*:ro, github:octocat:rw"


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        mock.get("/api/version").mock(return_value=httpx.Response(200, json={"version": "1.2.3"}))
        mock.post("/api/items").mock(return_value=httpx.Response(201, json={"id": "1"}))
        yield mock


def test_parse_and_lookup() -> None:
    m = RoleMap.parse(ROLES + "\n# comment\nci-admin:admin")
    assert m.role_for("alice") is Role.ADMIN
    assert m.role_for("ci-admin") is Role.ADMIN  # exact name beats the earlier glob
    assert m.role_for("ci-nightly") is Role.RO
    assert m.role_for("github:octocat") is Role.RW
    assert m.role_for("mallory") is Role.RO  # default
    assert m.role_for("local") is Role.ADMIN  # stdio operator unless named
    assert RoleMap.parse("local:ro").role_for("local") is Role.RO
    assert RoleMap.parse("", default="rw").role_for("x") is Role.RW
    assert m.allows("bob", "write") and not m.allows("bob", "dangerous")
    assert not m.allows("bob", None)  # unclassified tools need admin


@pytest.mark.parametrize(
    "spec, error",
    [
        ("alice", "name:role"),
        ("alice:root", "ro, rw or admin"),
        ("a b:ro", "not a caller"),
        ("a:ro,a:rw", "twice"),
    ],
)
def test_bad_roles(spec: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        make_settings(mcp_roles=spec)


def test_roles_file(tmp_path: Path) -> None:
    f = tmp_path / "roles"
    f.write_text("alice:admin\n# ops\nbob:rw\n")
    roles = make_settings(mcp_roles_file=f, mcp_roles="carol:rw").role_map()
    assert roles.role_for("bob") is Role.RW and roles.role_for("carol") is Role.RW
    with pytest.raises(ValueError, match="MCP_ROLES_FILE"):
        make_settings(mcp_roles_file=tmp_path / "missing")
    assert make_settings().role_map() is None


async def tool_names(server: Any, caller: str) -> set[str]:
    async with Client(server) as c:
        with as_caller(caller):
            return {t.name for t in await c.list_tools()}


async def test_roles_hide_and_refuse(api: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    server = create_server(make_settings(mcp_roles=ROLES, **RWD))
    ro, rw, admin = [await tool_names(server, n) for n in ("ci-x", "bob", "alice")]
    assert "demo_list_items" in ro and "demo_create_item" not in ro
    assert "demo_create_item" in rw and "demo_wipe" not in rw
    assert "demo_wipe" in admin
    async with Client(server) as c:
        with as_caller("ci-x"):
            with pytest.raises(ToolError, match="caller 'ci-x' has role 'ro'"):
                await c.call_tool("demo_create_item", {"name": "x"})
            prompts = {p.name for p in await c.list_prompts()}
            assert "demo_audit_items" in prompts and "demo_cleanup_items" not in prompts
        with as_caller("bob"):
            prev = (await c.call_tool("demo_create_item", {"name": "x"})).structured_content
            assert prev["requested_by"] == {"caller": "bob", "role": "rw"}
            await c.call_tool("demo_create_item", {"name": "x", "confirm": prev["confirm"]})
            status = (await c.call_tool("demo_status", {})).structured_content
    assert status["server"]["access"] == {"caller": "bob", "role": "rw", "default_role": "ro", "entries": 4}
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "stutters_mcp_core.audit"]
    assert {
        "event": "denied",
        "tool": "demo_create_item",
        "caller": "ci-x",
        "role": "ro",
        "tier": "write",
    } in events
    calls = [e for e in events if e["event"] == "tool_call"]
    assert all("role" in e for e in calls)
    applied = [e for e in events if e["event"] == "applied"]
    assert applied[0]["caller"] == "bob" and applied[0]["role"] == "rw"


async def test_roles_never_widen_server_tiers(api: Any) -> None:
    server = create_server(make_settings(mcp_roles="alice:admin"))  # writes off
    assert "demo_create_item" not in await tool_names(server, "alice")


async def test_no_roles_no_access_section(api: Any) -> None:
    status = await call_tool(create_server(make_settings(**RWD)), "demo_status")
    assert "access" not in status["server"]
    prev = await call_tool(create_server(make_settings(**RWD)), "demo_create_item", {"name": "x"})
    assert "requested_by" not in prev


async def test_review_change_needs_write_role(api: Any) -> None:
    server = create_server(make_settings(mcp_roles=ROLES, mcp_two_person_tools="@dangerous", **RWD))
    assert "demo_review_change" not in await tool_names(server, "ci-x")
    assert "demo_review_change" in await tool_names(server, "bob")


def test_glob_specificity_and_reserved_local() -> None:
    from stutters_mcp_core.inbound_auth import parse_tokens, valid_token_name

    m = RoleMap.parse("*:admin, ci-*:ro, ops-*:rw, ops-?:ro")
    assert m.role_for("ci-x") is Role.RO  # more specific than *
    assert m.role_for("ops-a") is Role.RO  # tie between ops-* and ops-? (3 literals): lower role
    assert m.role_for("someone") is Role.ADMIN
    assert not valid_token_name("local") and not valid_token_name("LOCAL")
    with pytest.raises(ValueError, match="reserved"):
        parse_tokens("local:" + "x" * 30)


def test_roles_file_errors_name_the_line(tmp_path: Path) -> None:
    f = tmp_path / "roles"
    f.write_text("alice:admin\n# c\nbob\n")
    with pytest.raises(ValueError, match="MCP_ROLES_FILE line 3"):
        make_settings(mcp_roles_file=f)


async def test_unregistered_prompt_is_admin_only(api: Any) -> None:
    server = create_server(make_settings(mcp_roles=ROLES))

    @server.prompt(name="demo_raw_prompt")
    def raw() -> str:
        return "x"

    async with Client(server) as c:
        with as_caller("ci-x"):
            assert "demo_raw_prompt" not in {p.name for p in await c.list_prompts()}
