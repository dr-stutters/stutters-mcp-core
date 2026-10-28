"""JSON-string list/dict arguments (C72): MCP_PARSE_JSON_ARGS=true parses them before validation."""

import json
import logging
from typing import Annotated, Any

import httpx
import pytest
import respx
from fastmcp import Client, Context
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from stutters_mcp_core import Confirm, Plan, ToolRegistrar, build_server, preview_or_apply
from stutters_mcp_core.formatting import mask_secrets
from stutters_mcp_core.json_args import parse_json_arguments, structured_parameters

from .demo import BASE, SPEC, DemoApp, make_settings


class Neighbor(BaseModel):
    address: str
    remote_as: int
    neighbor_secret: str | None = None


def register(reg: ToolRegistrar) -> None:
    @reg.read(title="Echo")
    async def demo_echo(
        ctx: Context,
        redistribute: list[dict[str, Any]] | None = None,
        filters: dict[str, str] | None = None,
        neighbor: Neighbor | None = None,
        tags: Annotated[list[str] | str, Field(description="a list, or one tag")] = "",
        note: str = "",
    ) -> dict[str, Any]:
        """Echo the arguments back with their types."""
        return {
            "redistribute": redistribute,
            "filters": filters,
            "neighbor": neighbor.model_dump() if neighbor else None,
            "tags": tags,
            "note": note,
        }

    @reg.write(title="Configure BGP")
    async def demo_configure_bgp(
        ctx: Context, neighbors: list[Neighbor], confirm: Confirm = None
    ) -> dict[str, Any]:
        """Preview-first BGP neighbours."""
        body = [n.model_dump(by_alias=True) for n in neighbors]
        return await preview_or_apply(
            ctx,
            tool="demo_configure_bgp",
            args={"neighbors": body},
            confirm=confirm,
            plan=Plan("Configure BGP", {"neighbors": mask_secrets(body)}),  # as server tools build previews
            apply=lambda: {"ok": True},
        )


def server(**kw: Any) -> Any:
    settings = make_settings(mcp_enable_writes=True, **kw)
    return build_server(SPEC, settings, client_factory=DemoApp, register=[register])


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        yield mock


REDIST = '[{"protocol": "ospf", "process": 1}]'


async def test_off_by_default_refuses_json_text(api: Any) -> None:
    async with Client(server()) as c:
        with pytest.raises(ToolError, match="valid list"):
            await c.call_tool("demo_echo", {"redistribute": REDIST})


async def test_parses_list_dict_and_model_arguments(api: Any) -> None:
    async with Client(server(mcp_parse_json_args=True)) as c:
        out = (
            await c.call_tool(
                "demo_echo",
                {
                    "redistribute": REDIST,
                    "filters": ' {"site": "branch-1"}',
                    "neighbor": '{"address": "192.0.2.1", "remote_as": 65001}',
                    "tags": '["a", "b"]',  # the parameter also takes a string: left alone
                    "note": '["not", "parsed"]',
                },
            )
        ).structured_content
    assert out["redistribute"] == [{"protocol": "ospf", "process": 1}]
    assert out["filters"] == {"site": "branch-1"}
    assert out["neighbor"]["address"] == "192.0.2.1" and out["neighbor"]["remote_as"] == 65001
    assert out["tags"] == '["a", "b"]' and out["note"] == '["not", "parsed"]'


async def test_real_lists_pass_and_bad_json_is_still_refused(api: Any) -> None:
    async with Client(server(mcp_parse_json_args=True)) as c:
        out = (await c.call_tool("demo_echo", {"redistribute": [{"protocol": "static"}]})).structured_content
        assert out["redistribute"] == [{"protocol": "static"}]
        with pytest.raises(ToolError, match="valid list"):
            await c.call_tool("demo_echo", {"redistribute": "[{not json"})
        with pytest.raises(ToolError, match="valid list"):  # wrong opener for the kind: not parsed
            await c.call_tool("demo_echo", {"redistribute": '{"protocol": "ospf"}'})


async def test_audit_and_preview_mask_secrets_inside_parsed_text(
    api: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    text = json.dumps([{"address": "198.51.100.2", "remote_as": 65002, "neighbor_secret": "on"}])
    async with Client(server(mcp_parse_json_args=True)) as c:
        prev = (await c.call_tool("demo_configure_bgp", {"neighbors": text})).structured_content
    assert prev["status"] == "preview"
    assert '"on"' not in json.dumps(prev) and "198.51.100.2" in json.dumps(prev)
    logged = [r.getMessage() for r in caplog.records if r.name == "stutters_mcp_core.audit"]
    assert logged and all('"on"' not in line for line in logged)
    assert any("198.51.100.2" in line for line in logged)  # the audit line saw the parsed list


def test_structured_parameters_reads_the_schema() -> None:
    params = {
        "$defs": {
            "Neighbor": {"type": "object", "properties": {"address": {"type": "string"}}},
            "Mode": {"type": "string", "enum": ["a", "b"]},
        },
        "properties": {
            "a": {"type": "array", "items": {"type": "string"}},
            "b": {"anyOf": [{"type": "object"}, {"type": "null"}]},
            "c": {"$ref": "#/$defs/Neighbor"},
            "d": {"anyOf": [{"type": "array"}, {"type": "string"}]},
            "e": {"$ref": "#/$defs/Mode"},
            "f": {},
            "g": {"type": "string"},
            "h": {"anyOf": [{"type": "array"}, {"type": "object"}]},
        },
    }
    assert structured_parameters(params) == {"a": "[", "b": "{", "c": "{"}
    out, parsed = parse_json_arguments({"a": '["x"]', "b": "{}", "g": "[1]"}, structured_parameters(params))
    assert out == {"a": ["x"], "b": {}, "g": "[1]"} and parsed == ["a", "b"]
