"""C68: argument vocabulary lint (warnings, never failures)."""

import warnings
from typing import Annotated, Any, Literal

import httpx
import pytest
import respx
from fastmcp import FastMCP
from pydantic import Field

from stutters_mcp_core import ToolRegistrar
from stutters_mcp_core.testing import vocabulary_problems, warn_vocabulary
from stutters_mcp_core.vocabulary import (
    ALIASES,
    TERMS,
    VocabularyWarning,
    check_tool,
    vocabulary_findings,
)

from .demo import BASE, create_server, make_settings


def _schema(**props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props}


def test_aliases_point_at_vocabulary_terms() -> None:
    assert set(ALIASES.values()) <= set(TERMS)
    assert not set(ALIASES) & set(TERMS)


def test_alias_findings_name_the_vocabulary_term() -> None:
    found = check_tool(
        "x_list_events",
        _schema(
            hours={"type": "integer"},
            timeout={"type": "number"},
            format={"type": "string"},
            device_id={"type": "string"},
            name={"type": "string"},
        ),
    )
    by_param = {f.param: f for f in found}
    assert set(by_param) == {"hours", "timeout", "format", "device_id"}
    assert by_param["hours"].use == "since_hours" and by_param["hours"].kind == "alias"
    assert by_param["timeout"].use == "timeout_seconds"
    assert by_param["format"].use == "response_format"
    assert str(by_param["device_id"]) == (
        "x_list_events: parameter 'device_id': use 'device' "
        "(a device by host name, IP, serial or id (the tool resolves it))"
    )


def test_type_findings_for_vocabulary_names() -> None:
    found = check_tool(
        "x_deploy",
        _schema(
            wait={"anyOf": [{"type": "integer"}, {"type": "null"}]},
            since_hours={"type": "string"},
            response_format={"type": "string", "enum": ["text", "json"]},
            limit={"type": "integer"},
            confirm={"anyOf": [{"type": "string"}, {"type": "null"}]},
        ),
    )
    msgs = {f.param: f.message for f in found}
    assert set(msgs) == {"wait", "since_hours", "response_format"}
    assert "timeout_seconds" in msgs["wait"]
    assert "integer or number" in msgs["since_hours"]
    assert "['markdown']" in msgs["response_format"]


def test_canonical_names_pass() -> None:
    schema = _schema(
        since_hours={"type": "number"},
        wait={"type": "boolean"},
        timeout_seconds={"anyOf": [{"type": "integer"}, {"type": "null"}]},
        response_format={"enum": ["markdown", "json"], "type": "string"},
        offset={"type": "integer"},
        instance={"anyOf": [{"type": "string"}, {"type": "null"}]},
    )
    assert check_tool("x_ok", schema) == []


def test_deprecated_alias_and_allow_list_are_silent() -> None:
    schema = _schema(
        hours={"type": "integer", "description": "Deprecated: use since_hours."},
        format={"type": "string", "description": "File format: mermaid or drawio"},
        timeout={"type": "number"},
    )
    assert [f.param for f in check_tool("cml_export_topology", schema)] == ["format", "timeout"]
    assert check_tool("cml_export_topology", schema, allow={"cml_export_topology.format", "timeout"}) == []
    assert [f.param for f in check_tool("cml_export_topology", schema, allow={"cml_*.timeout"})] == ["format"]
    assert check_tool("fmc_x", schema, allow={"cml_*.timeout"})[-1].param == "timeout"


def test_findings_sorted_and_schemaless_tools() -> None:
    found = vocabulary_findings(
        [("b_tool", _schema(minutes={"type": "integer"})), ("a_tool", None), ("a_tool2", _schema(page={}))]
    )
    assert [(f.tool, f.use) for f in found] == [("a_tool2", "offset"), ("b_tool", "since_minutes")]


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        mock.get("/api/version").mock(return_value=httpx.Response(200, json={"version": "1.2.3"}))
        yield mock


async def test_demo_server_is_on_vocabulary(api: Any) -> None:
    server = create_server(make_settings(mcp_enable_writes=True, mcp_enable_dangerous=True))
    assert await vocabulary_problems(server, "demo") == []


async def test_warn_vocabulary_warns_and_passes() -> None:
    mcp: FastMCP[Any] = FastMCP("v")
    reg = ToolRegistrar(mcp, make_settings(), "v")

    @reg.read(title="Recent events")
    async def v_recent_events(
        hours: Annotated[int, Field(description="Look back this many hours")] = 24,
        wait: Annotated[int, Field(description="Seconds to wait")] = 0,
        detail: Literal["summary", "full"] = "summary",
    ) -> dict[str, Any]:
        """Recent events on the appliance, newest first, with a summary per event."""
        return {}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        problems = await warn_vocabulary(mcp, "v")
    assert len(problems) == 2
    assert [w.category for w in caught] == [VocabularyWarning, VocabularyWarning]
    assert "since_hours" in str(caught[0].message) and "VOCABULARY.md" in str(caught[0].message)
    assert await vocabulary_problems(mcp, "v", allow={"hours", "v_recent_events.wait"}) == []
