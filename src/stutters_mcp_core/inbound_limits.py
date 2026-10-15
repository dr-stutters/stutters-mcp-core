"""Inbound per-caller rate limiting for tool calls (roadmap C16). Off by default.

    MCP_CALLER_RATE_LIMIT=60                     tool calls per minute per caller identity
    MCP_CALLER_RATE_LIMIT_BY_ROLE="ro=30,admin=120"   per-role values (needs MCP_ROLES), override the default

A token bucket per caller allows short bursts (a quarter of the per-minute budget) and refills steadily.
A refused call gets a clear message with the wait time, an audit event `rate_limited` and the
`mcp_rate_limited_total` metric. `<prefix>_status` is never limited, so a throttled caller can still
diagnose. This protects the appliance (and other callers) from one runaway client; the client-side
limiter (`<P>_RATE_LIMIT`) still paces the server as a whole.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any

import mcp.types as mt
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from stutters_mcp_core.inbound_auth import current_caller

audit = logging.getLogger("stutters_mcp_core.audit")
MAX_CALLERS = 4096


def parse_role_limits(spec: str | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for raw in re.split(r"[,\n]", spec or ""):
        entry = raw.strip()
        if not entry:
            continue
        role, sep, value = entry.partition("=")
        role = role.strip().lower()
        try:
            number = float(value)
        except ValueError:
            number = -1
        if not sep or role not in ("ro", "rw", "admin") or number <= 0:
            raise ValueError(
                f"MCP_CALLER_RATE_LIMIT_BY_ROLE: {entry!r} must look like 'ro=30' (role ro, rw or admin)"
            )
        out[role] = number
    return out


class _Bucket:
    __slots__ = ("tokens", "updated")

    def __init__(self, capacity: float) -> None:
        self.tokens = capacity
        self.updated = time.monotonic()


class CallerRateLimitMiddleware(Middleware):
    def __init__(
        self, server_name: str, prefix: str, per_minute: float | None, by_role: dict[str, float], roles: Any
    ) -> None:
        self.server_name = server_name
        self.exempt = {f"{prefix}_status"}
        self.default = per_minute
        self.by_role = by_role
        self.roles = roles
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def limit_for(self, caller: str) -> float | None:
        if self.roles is not None and self.by_role:
            role = self.roles.role_for(caller).value
            if role in self.by_role:
                return self.by_role[role]
        return self.default

    def check(self, caller: str, now: float | None = None) -> float:
        """0 if the call may proceed, else the seconds until it could."""
        per_minute = self.limit_for(caller)
        if per_minute is None:
            return 0.0
        now = time.monotonic() if now is None else now
        capacity = max(1.0, per_minute / 4)
        rate = per_minute / 60.0
        bucket = self._buckets.get(caller)
        if bucket is None:
            bucket = self._buckets[caller] = _Bucket(capacity)
            if len(self._buckets) > MAX_CALLERS:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(caller)
            bucket.tokens = min(capacity, bucket.tokens + (now - bucket.updated) * rate)
        bucket.updated = now
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return 0.0
        return (1 - bucket.tokens) / rate

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        name = context.message.name
        if name not in self.exempt:
            caller = current_caller()
            wait = self.check(caller)
            if wait > 0:
                from stutters_mcp_core import metrics

                audit.info(
                    json.dumps(
                        {
                            "event": "rate_limited",
                            "tool": name,
                            "caller": caller,
                            "retry_after_s": round(wait, 1),
                        }
                    )
                )
                if metrics.enabled():
                    metrics.REGISTRY.inc("mcp_rate_limited_total", {"server": self.server_name})
                raise ToolError(
                    f"Rate limit for {caller!r} reached ({self.limit_for(caller):g} tool calls per minute). "
                    f"Retry in {wait:.0f} s; batch work with list tools where possible."
                )
        return await call_next(context)
