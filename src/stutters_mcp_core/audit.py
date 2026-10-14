"""Structured audit trail: one JSON line per tool call (who, what, how long, outcome).

Write tools additionally log "preview" / "applied" / "apply_failed" events from writes.py. All of it
goes to the `stutters_mcp_core.audit` logger, routed by MCP_AUDIT_LOG (stderr | <file path> | off).
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from stutters_mcp_core.formatting import MASK, is_secret_key
from stutters_mcp_core.inbound_auth import current_caller

audit = logging.getLogger("stutters_mcp_core.audit")

MAX_ARG_CHARS = 300


def redact_args(args: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if is_secret_key(k) and v not in (None, ""):
            out[k] = MASK
            continue
        text = json.dumps(v, default=str)
        out[k] = v if len(text) <= MAX_ARG_CHARS else text[:MAX_ARG_CHARS] + "...(truncated)"
    return out


class AuditMiddleware(Middleware):
    def __init__(self, server_name: str) -> None:
        self.server_name = server_name

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        start = time.monotonic()
        event: dict[str, Any] = {
            "event": "tool_call",
            "server": self.server_name,
            "tool": context.message.name,
            "caller": current_caller(),
            "args": redact_args(context.message.arguments),
        }
        try:
            result = await call_next(context)
        except Exception as e:
            event.update(outcome="error", error_type=type(e).__name__, error=str(e)[:300])
            raise
        else:
            event["outcome"] = "ok"
            return result
        finally:
            event["duration_ms"] = int((time.monotonic() - start) * 1000)
            audit.info(json.dumps(event, default=str))


def configure_audit_log(destination: str) -> None:
    """Route the audit logger: 'stderr' (default), 'off', or a file path (appended)."""
    audit.propagate = False
    audit.handlers.clear()
    if destination.lower() in ("off", "none", "false", ""):
        audit.addHandler(logging.NullHandler())
        return
    handler: logging.Handler
    if destination.lower() == "stderr":
        handler = logging.StreamHandler(sys.stderr)
    else:
        handler = logging.FileHandler(destination)
    handler.setFormatter(logging.Formatter('{"ts":"%(asctime)s","audit":%(message)s}'))
    audit.addHandler(handler)
    audit.setLevel(logging.INFO)
