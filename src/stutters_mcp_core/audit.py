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

from stutters_mcp_core.formatting import MASK, is_secret_key, mask_secrets
from stutters_mcp_core.inbound_auth import current_caller
from stutters_mcp_core.metrics import record_tool
from stutters_mcp_core.secrets import scrub_text

audit = logging.getLogger("stutters_mcp_core.audit")

MAX_ARG_CHARS = 300


def redact_args(args: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if is_secret_key(k) and v not in (None, ""):
            out[k] = MASK
            continue
        v = _scrub_leaves(mask_secrets(v))
        text = json.dumps(v, default=str)
        out[k] = v if len(text) <= MAX_ARG_CHARS else text[:MAX_ARG_CHARS] + "...(truncated)"
    return out


def _scrub_leaves(v: Any) -> Any:
    if isinstance(v, str):
        return scrub_text(v)
    if isinstance(v, dict):
        return {k: _scrub_leaves(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_scrub_leaves(x) for x in v]
    return v


class AuditMiddleware(Middleware):
    def __init__(self, server_name: str, roles: Any = None) -> None:
        self.server_name = server_name
        self.roles = roles  # stutters_mcp_core.roles.RoleMap when per-user roles are on

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
        if self.roles is not None:
            event["role"] = self.roles.role_for(event["caller"]).value
        try:
            result = await call_next(context)
        except Exception as e:
            event.update(outcome="error", error_type=type(e).__name__, error=scrub_text(str(e))[:300])
            raise
        else:
            event["outcome"] = "ok"
            return result
        finally:
            elapsed = time.monotonic() - start
            event["duration_ms"] = int(elapsed * 1000)
            record_tool(self.server_name, event["tool"], str(event.get("outcome", "error")), elapsed)
            audit.info(json.dumps(event, default=str))


def configure_audit_log(destination: str, *, max_mb: float | None = None, backups: int = 5) -> None:
    """Route the audit logger: 'stderr' (default), 'off', or a file path (appended). With `max_mb`, a file
    rotates when it reaches that size, keeping `backups` old files (<file>.1 ... <file>.N)."""
    audit.propagate = False
    audit.handlers.clear()
    if destination.lower() in ("off", "none", "false", ""):
        audit.addHandler(logging.NullHandler())
        return
    handler: logging.Handler
    if destination.lower() == "stderr":
        handler = logging.StreamHandler(sys.stderr)
    elif max_mb:
        from logging.handlers import RotatingFileHandler

        handler = RotatingFileHandler(destination, maxBytes=int(max_mb * 1024 * 1024), backupCount=backups)
    else:
        handler = logging.FileHandler(destination)
    handler.setFormatter(logging.Formatter('{"ts":"%(asctime)s","audit":%(message)s}'))
    audit.addHandler(handler)
    audit.setLevel(logging.INFO)
