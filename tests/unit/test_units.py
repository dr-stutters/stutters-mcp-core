from pathlib import Path
from typing import Any

import pytest

from stutters_mcp_core.audit import redact_args
from stutters_mcp_core.errors import InvalidRequest, TaskFailed, UpstreamTimeout
from stutters_mcp_core.formatting import compact, list_result, set_max_chars
from stutters_mcp_core.http import Page
from stutters_mcp_core.inbound_auth import parse_tokens
from stutters_mcp_core.spec import SpecIndex, build_index
from stutters_mcp_core.tasks import poll_until
from stutters_mcp_core.writes import field_changes

from .demo import SPEC_DOC, DemoSettings, make_settings

# --- settings ------------------------------------------------------------------------------


def test_settings_prefix_and_unprefixed_server_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_URL", "https://x.test/")
    monkeypatch.setenv("DEMO_PASSWORD", "pw")
    monkeypatch.setenv("DEMO_VERIFY_SSL", "false")
    monkeypatch.setenv("MCP_ENABLE_WRITES", "true")
    monkeypatch.setenv("MCP_PORT", "8033")
    s = DemoSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.url == "https://x.test" and s.password_value() == "pw" and s.verify is False
    assert s.mcp_enable_writes is True and s.mcp_port == 8033


def test_settings_reject_bad_url() -> None:
    with pytest.raises(ValueError, match="must start with"):
        make_settings(url="ftp://x")


def test_cache_dir_default_and_disable() -> None:
    assert make_settings().cache_dir_for("demo-mcp-server") == Path.home() / ".cache" / "demo-mcp-server"
    assert make_settings(mcp_cache_dir="").cache_dir_for("demo-mcp-server") is None


def test_token_scheme_stripped() -> None:
    assert make_settings(token="Bearer abc").token_value() == "abc"


def test_parse_tokens() -> None:
    entries = parse_tokens("alice:" + "x" * 30 + "\nbob:sha256:" + "a" * 64 + "  # comment")
    assert set(entries) == {"alice", "bob"}
    with pytest.raises(ValueError, match="too short"):
        parse_tokens("a:short")


# --- formatting ----------------------------------------------------------------------------


def test_compact_masks_secrets_and_drops_noise() -> None:
    data = {
        "name": "nad1",
        "radiusSharedSecret": "s3",
        "links": [{"href": "x"}],
        "empty": "",
        "n": None,
        "snmp": {"roCommunity": "public", "version": "2c"},
        "enabled": True,
        "passwordRequired": False,
    }
    assert compact(data) == {
        "name": "nad1",
        "radiusSharedSecret": "********",
        "snmp": {"roCommunity": "********", "version": "2c"},
        "enabled": True,
        "passwordRequired": False,
    }


def test_list_result_truncates_to_budget() -> None:
    set_max_chars(2000)
    try:
        page = Page([{"id": i, "blob": "x" * 200} for i in range(50)], offset=0, total=50)
        result = list_result(page)
        payload: Any = result.structured_content
        assert payload["returned"] < 50 and "truncated" in payload["note"]
        assert payload["next_offset"] == payload["returned"]
    finally:
        set_max_chars(40_000)


def test_redact_args() -> None:
    out = redact_args({"password": "p", "name": "x", "body": "y" * 1000})
    assert out["password"] == "********" and out["name"] == "x" and out["body"].endswith("(truncated)")


def test_field_changes() -> None:
    cur = {"name": "a", "tacacs": {"secret": "1", "port": 49}, "x": 1}
    assert field_changes(cur, {"name": "a", "tacacs": {"port": 50}}) == {
        "tacacs.port": {"from": 49, "to": 50}
    }


# --- tasks ---------------------------------------------------------------------------------


async def test_poll_until_done_failed_timeout() -> None:
    states = iter(["running", "running", "done"])

    async def fetch() -> str:
        return next(states)

    assert await poll_until(fetch, done=lambda s: s == "done", interval=0, timeout=5) == "done"

    async def failing() -> dict[str, Any]:
        return {"status": "FAILED", "reason": "device unreachable"}

    with pytest.raises(TaskFailed, match="device unreachable"):
        await poll_until(failing, done=lambda r: False, failed=lambda r: r["reason"], interval=0)

    async def forever() -> str:
        return "running"

    with pytest.raises(UpstreamTimeout, match="still running"):
        await poll_until(forever, done=lambda r: False, interval=0.01, timeout=0.05, describe=lambda r: r)


# --- spec ----------------------------------------------------------------------------------

SWAGGER2 = {
    "swagger": "2.0",
    "basePath": "/ers",
    "info": {"version": "3.5"},
    "paths": {
        "/config/networkdevice": {
            "post": {
                "summary": "Create network device",
                "parameters": [
                    {"in": "body", "name": "body", "schema": {"$ref": "#/definitions/NetworkDevice"}}
                ],
                "responses": {"201": {"description": "ok"}},
            },
            "get": {
                "parameters": [
                    {"in": "query", "name": "filter", "type": "string", "description": "filter expr"}
                ],
                "responses": {"200": {"schema": {"$ref": "#/definitions/SearchResult"}}},
            },
        }
    },
    "definitions": {
        "Base": {"properties": {"id": {"type": "string", "readOnly": True}}},
        "NetworkDevice": {
            "allOf": [{"$ref": "#/definitions/Base"}],
            "required": ["name"],
            "properties": {"name": {"type": "string"}, "profileName": {"type": "string", "default": "Cisco"}},
        },
        "SearchResult": {"properties": {"total": {"type": "integer"}}},
    },
}


def test_swagger2_index_basepath_body_and_allof() -> None:
    idx = build_index(SWAGGER2, source="ers")
    paths = {f"{o.method} {o.path}" for o in idx.operations}
    assert paths == {"POST /ers/config/networkdevice", "GET /ers/config/networkdevice"}
    d = idx.definition("NetworkDevice")
    assert set(d["fields"]) == {"id", "name", "profileName"} and d["required"] == ["name"]
    get = idx.definition("GET /ers/config/networkdevice")["operations"][0]
    assert get["params"][0]["name"] == "filter" and "filter expr" in get["params"][0]["summary"]


def test_index_roundtrip_and_errors() -> None:
    idx = build_index(SPEC_DOC, source="main")
    again = SpecIndex.from_json(idx.to_json())
    assert again.definition("/api/items/{x}")["operations"][0]["operation_id"] == "getItem"
    with pytest.raises(InvalidRequest, match="Did you mean: Item"):
        again.definition("Iten")
    with pytest.raises(ValueError):
        SpecIndex.from_json({**idx.to_json(), "format": 0})
