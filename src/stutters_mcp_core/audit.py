"""Structured audit trail: one JSON line per tool call (who, what, how long, outcome).

Write tools additionally log "preview" / "applied" / "apply_failed" events from writes.py. All of it
goes to the `stutters_mcp_core.audit` logger, routed by MCP_AUDIT_LOG (stderr | <file path> | off).
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable, Mapping
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

# Per-tool argument redactors (C28): {tool: {argument: redactor}}. A redactor is True (mask the whole
# value) or a function returning the value to log (`config_mask.mask_config_value` for config text).
# Filled by ToolRegistrar from `redact=` and from SecretStr-annotated parameters; used by the audit
# `tool_call` event, previews shown in a confirmation dialog and two-person `<prefix>_review_change`.
ArgRedactor = Callable[[Any], Any] | bool
# Argument names that are secrets when a tool takes them (an SNMP community string) but not as fields in
# API responses (a BGP route's `community: 65000:1`), so they stay out of formatting.SECRET_KEY_RE.
ARG_SECRET_KEYS: set[str] = {"community"}
_ARG_REDACTORS: dict[str, dict[str, ArgRedactor]] = {}


def register_arg_redactors(tool: str, redactors: Mapping[str, ArgRedactor]) -> None:
    """Redact these arguments of `tool` wherever its arguments are shown (replaces earlier entries)."""
    clean: dict[str, ArgRedactor] = {k: r for k, r in redactors.items() if r is not False}
    if clean:
        _ARG_REDACTORS[tool] = clean
    else:
        _ARG_REDACTORS.pop(tool, None)


def arg_redactors(tool: str | None) -> dict[str, ArgRedactor]:
    """The redactors registered for `tool` (an `@instance` suffix is ignored); {} when none."""
    if not tool:
        return {}
    return dict(_ARG_REDACTORS.get(tool.partition("@")[0], {}))


def redact_args(
    args: dict[str, Any] | None,
    *,
    tool: str | None = None,
    redactors: Mapping[str, ArgRedactor] | None = None,
) -> dict[str, Any]:
    """`args` as they may be logged or shown to an approver: secret-named arguments masked, the tool's
    registered redactors applied (`tool`, or `redactors` given directly), credential shapes and config
    secrets scrubbed in every string, and each argument cut to MAX_ARG_CHARS."""
    hooks = {**arg_redactors(tool), **(redactors or {})}
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if v in (None, ""):
            out[k] = v
            continue
        hook = hooks.get(k)
        if is_secret_key(k) or k in ARG_SECRET_KEYS or hook is True:
            out[k] = MASK
            continue
        if callable(hook):
            try:
                v = hook(v)
            except Exception:  # noqa: BLE001 - a failing redactor must not leak the value
                out[k] = MASK
                continue
        v = redact_value(v)
        text = json.dumps(v, default=str)
        out[k] = v if len(text) <= MAX_ARG_CHARS else text[:MAX_ARG_CHARS] + "...(truncated)"
    return out


def redact_value(v: Any) -> Any:
    """`v` with every secret-named field masked (nested dicts and lists included) and credential shapes
    in string leaves scrubbed: for request bodies and responses shown to the client (previews, raw API
    call results). Structure and non-secret values are kept."""
    return _scrub_leaves(mask_secrets(v))


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
            "args": redact_args(context.message.arguments, tool=context.message.name),
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

        handler = RotatingFileHandler(
            destination, maxBytes=int(max_mb * 1024 * 1024), backupCount=backups, encoding="utf-8", delay=True
        )
    else:
        handler = logging.FileHandler(destination, encoding="utf-8")
    handler.setFormatter(logging.Formatter('{"ts":"%(asctime)s","audit":%(message)s}'))
    audit.addHandler(handler)
    audit.setLevel(logging.INFO)
