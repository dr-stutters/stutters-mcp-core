"""JSON-string list/dict arguments (C72), opt-in with MCP_PARSE_JSON_ARGS=true.

A client keeps the tool list it fetched at session start. After a redeploy adds a list- or dict-valued
argument, that client has no schema for it and sends the value as JSON text
(`redistribute='[{"protocol": "ospf"}]'`), which validation refuses ("Input should be a valid list ...
input_type=str"). With the setting on, a `str` argument whose parameter schema only accepts an array
(list/tuple/set) or an object (dict/BaseModel/TypedDict) and whose text starts with `[` / `{` (matching
that kind) is `json.loads`-ed before validation; the parsed value is then validated as usual. Text that
isn't valid JSON, and parameters that also accept a string, are left alone.

The middleware runs before the audit middleware, so the audit log and previews see (and mask) the parsed
structure rather than one opaque string.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

log = logging.getLogger(__name__)

_OPENERS = {"array": "[", "object": "{"}


def _kinds(schema: Any, defs: dict[str, Any], depth: int = 0) -> set[str] | None:
    """The JSON types a parameter schema accepts, or None when it accepts anything (no type, `Any`)."""
    if not isinstance(schema, dict) or depth > 8:
        return None
    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = defs.get(ref.rsplit("/", 1)[-1]) if ref.startswith("#/$defs/") else None
        return _kinds(target, defs, depth + 1)
    branches = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(branches, list):
        out: set[str] = set()
        for b in branches:
            k = _kinds(b, defs, depth + 1)
            if k is None:
                return None
            out |= k
        return out
    allof = schema.get("allOf")
    if isinstance(allof, list) and len(allof) == 1:
        return _kinds(allof[0], defs, depth + 1)
    t = schema.get("type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {str(x) for x in t}
    if "properties" in schema:
        return {"object"}
    if "items" in schema or "prefixItems" in schema:
        return {"array"}
    if "enum" in schema or "const" in schema:
        return {"enum"}  # never parsed
    return None


def structured_parameters(parameters: dict[str, Any]) -> dict[str, str]:
    """{argument: '[' | '{'} for the parameters of a tool's input schema that take only an array or only
    an object (null allowed), never a string."""
    defs = parameters.get("$defs") or {}
    out: dict[str, str] = {}
    for name, prop in (parameters.get("properties") or {}).items():
        kinds = _kinds(prop, defs)
        if not kinds:
            continue
        kinds = kinds - {"null"}
        if len(kinds) == 1 and (kind := next(iter(kinds))) in _OPENERS:
            out[name] = _OPENERS[kind]
    return out


def parse_json_arguments(
    arguments: dict[str, Any], structured: dict[str, str]
) -> tuple[dict[str, Any], list[str]]:
    """`arguments` with JSON-text values of structured parameters parsed; and the names it parsed."""
    out = dict(arguments)
    parsed: list[str] = []
    for name, opener in structured.items():
        v = out.get(name)
        if not isinstance(v, str):
            continue
        text = v.strip()
        if not text.startswith(opener):
            continue
        try:
            value = json.loads(text)
        except ValueError:
            continue  # validation reports the original string as before
        if isinstance(value, list if opener == "[" else dict):
            out[name] = value
            parsed.append(name)
    return out, parsed


class JsonArgsMiddleware(Middleware):
    """Parses JSON-text list/dict arguments before FastMCP validates them (MCP_PARSE_JSON_ARGS)."""

    def __init__(self, mcp: Any) -> None:
        self.mcp = mcp
        self._structured: dict[str, dict[str, str]] = {}

    async def _for_tool(self, name: str) -> dict[str, str]:
        cached = self._structured.get(name)
        if cached is not None:
            return cached
        tool = await self.mcp.get_tool(name)
        found = structured_parameters(tool.parameters) if tool is not None else {}
        if tool is not None:
            self._structured[name] = found
        return found

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        args = context.message.arguments
        if args and any(isinstance(v, str) for v in args.values()):
            structured = await self._for_tool(context.message.name)
            if structured:
                new, parsed = parse_json_arguments(args, structured)
                if parsed:
                    log.debug("%s: parsed JSON-text arguments %s", context.message.name, ", ".join(parsed))
                    message = context.message.model_copy(update={"arguments": new})
                    context = context.copy(message=message)
        return await call_next(context)
