"""Test helpers for servers built on stutters-mcp-core.

    from stutters_mcp_core.testing import assert_tool_contract, call_tool, live_settings

    async def test_contract():
        await assert_tool_contract(create_server(settings(writes=True, dangerous=True)), prefix="ise")

    @pytest.mark.live
    async def test_live_status():
        s = live_settings(Settings, "ISE_")      # skips unless ISE_URL is set and reachable
        ...

pytest is only imported when a helper that needs it is called, so this module is safe to import from
a server's runtime code (it isn't, normally).
"""

from __future__ import annotations

import re
import socket
from typing import Any
from urllib.parse import urlparse

from fastmcp import Client, FastMCP

from stutters_mcp_core.settings import BaseServerSettings

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PAGING_PARAMS = ("limit", "offset", "response_format")
UNDOCUMENTED_OK = {"limit", "offset", "response_format", "confirm", "fields"}
MIN_DESCRIPTION, MAX_DESCRIPTION = 30, 1500


async def tool_contract_problems(mcp: FastMCP[Any], prefix: str) -> list[str]:
    """Everything wrong with the server's tool surface, as human-readable strings."""
    problems: list[str] = []
    async with Client(mcp) as client:
        tools = await client.list_tools()
    if not tools:
        return ["server exposes no tools"]
    for t in tools:
        where = t.name
        if not t.name.startswith(prefix + "_") or not NAME_RE.match(t.name):
            problems.append(f"{where}: name must be snake_case and start with '{prefix}_'")
        desc = (t.description or "").strip()
        if not MIN_DESCRIPTION <= len(desc) <= MAX_DESCRIPTION:
            problems.append(
                f"{where}: description length {len(desc)} not in {MIN_DESCRIPTION}..{MAX_DESCRIPTION}"
            )
        ann = t.annotations
        if ann is None or ann.read_only_hint is None:
            problems.append(f"{where}: missing readOnlyHint annotation (register via ToolRegistrar)")
            continue
        meta = t.meta or {}
        tags = set(meta.get("fastmcp", {}).get("tags", []) or [])
        if not tags & {"read", "write", "dangerous"}:
            problems.append(f"{where}: no tier tag (register via ToolRegistrar)")
        if "read" in tags and not ann.read_only_hint:
            problems.append(f"{where}: read-tier tool must have readOnlyHint=True")
        if tags & {"write", "dangerous"} and ann.read_only_hint:
            problems.append(f"{where}: write-tier tool must have readOnlyHint=False")
        if "dangerous" in tags and not ann.destructive_hint:
            problems.append(f"{where}: dangerous tool must have destructiveHint=True")
        props: dict[str, Any] = (t.input_schema or {}).get("properties", {})
        if tags & {"write", "dangerous"} and "confirm" not in props:
            problems.append(f"{where}: write tools must take a `confirm` parameter (preview-first)")
        if "_list_" in t.name or t.name.startswith(f"{prefix}_list"):
            missing = [p for p in PAGING_PARAMS if p not in props]
            if missing:
                problems.append(f"{where}: list tools need {', '.join(missing)}")
        for pname, schema in props.items():
            if pname in UNDOCUMENTED_OK:
                continue
            if not (isinstance(schema, dict) and (schema.get("description") or schema.get("enum"))):
                problems.append(f"{where}: parameter '{pname}' has no description")
    return problems


async def assert_tool_contract(mcp: FastMCP[Any], prefix: str) -> None:
    problems = await tool_contract_problems(mcp, prefix)
    assert not problems, "Tool contract violations:\n  " + "\n  ".join(problems)


async def call_tool(mcp: FastMCP[Any], name: str, args: dict[str, Any] | None = None) -> Any:
    """Call a tool in-memory and return structured content (or the text content)."""
    async with Client(mcp) as client:
        result = await client.call_tool(name, args or {})
    if result.structured_content is not None:
        return result.structured_content
    return "\n".join(getattr(c, "text", "") for c in result.content)


def reachable(url: str, timeout: float = 3.0) -> bool:
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname or "", port), timeout=timeout):
            return True
    except OSError:
        return False


def live_settings[S: BaseServerSettings](settings_cls: type[S], env_prefix: str, **overrides: Any) -> S:
    """Settings from env/.env for live tests; pytest.skip() if not configured or unreachable."""
    import pytest
    from pydantic import ValidationError

    try:
        settings = settings_cls(**overrides)
    except ValidationError:
        pytest.skip(f"{env_prefix}URL / credentials not configured")
    if not reachable(settings.url):
        pytest.skip(f"{settings.url} is unreachable (lab down or VPN off)")
    return settings
