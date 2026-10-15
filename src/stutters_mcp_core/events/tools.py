"""Generic event tools: `<prefix>_list_events`, `<prefix>_event_sources` and the resource
`events://<prefix>/recent`."""

# No `from __future__ import annotations`: FastMCP inspects these signatures at registration time.
import json
from typing import Annotated, Any

from fastmcp.tools import ToolResult
from pydantic import Field

from stutters_mcp_core.errors import InvalidRequest
from stutters_mcp_core.events.hub import EventHub, EventOptions
from stutters_mcp_core.formatting import ResponseFormat, list_result, object_result
from stutters_mcp_core.writes import ToolRegistrar

LIST_DESCRIPTION = """Read recent events from this server's event buffer ({sources}): newest first, \
with seq, time, source, type, severity, summary and correlation keys (mac, ip, user, device...).
Filter by source, type ('auth.fail', or a prefix such as 'auth.*'), key ('ip=10.1.1.1' or a bare \
value matched against every key), severity, a time window (since/until: ISO 8601 or an age like 15m, \
2h, 1d) or summary text. To see only what is new since a previous call, pass after_seq=<latest_seq \
from that call>. The buffer is in memory and bounded; {sources_tool} shows each source's state, lag, \
errors and drops, and how far back the buffer reaches."""


def register_event_tools(reg: ToolRegistrar, hub: EventHub, options: EventOptions, product: str) -> None:
    prefix = reg.prefix
    list_name = options.name(options.list_tool, prefix)
    sources_name = options.name(options.sources_tool, prefix)
    buffer = hub.buffer

    if list_name:

        @reg.read(
            title=f"{product} events",
            name=list_name,
            description=LIST_DESCRIPTION.format(
                sources=f"{product} event sources", sources_tool=sources_name or "the status tool"
            ),
        )
        async def list_events(
            source: Annotated[
                str | None, Field(description="Only these sources (comma-separated names)")
            ] = None,
            type: Annotated[  # noqa: A002 - the natural name for the filter
                str | None, Field(description="Event types, comma-separated; 'auth.*' matches a type prefix")
            ] = None,
            key: Annotated[
                str | None,
                Field(description="Correlation key 'name=value' (ip=10.1.1.1, mac=..., user=...) or a value"),
            ] = None,
            severity: Annotated[
                str | None,
                Field(description="Severities, comma-separated: critical, error, warning, notice, info"),
            ] = None,
            since: Annotated[
                str | None,
                Field(description="Only events at/after this time: ISO 8601 or an age like 15m, 2h, 1d"),
            ] = None,
            until: Annotated[
                str | None, Field(description="Only events at/before this time (same formats)")
            ] = None,
            text: Annotated[
                str | None, Field(description="Case-insensitive text the summary contains")
            ] = None,
            after_seq: Annotated[
                int | None,
                Field(ge=0, description="Only events newer than this seq (latest_seq of an earlier call)"),
            ] = None,
            include_data: Annotated[
                bool, Field(description="Include each event's raw data (larger output)")
            ] = False,
            limit: Annotated[int, Field(ge=1, le=500)] = 50,
            offset: Annotated[int, Field(ge=0)] = 0,
            response_format: ResponseFormat = "markdown",
        ) -> ToolResult:
            try:
                page = buffer.query(
                    limit=limit,
                    offset=offset,
                    source=source,
                    type=type,
                    key=key,
                    severity=severity,
                    since=since,
                    until=until,
                    text=text,
                    after_seq=after_seq,
                )
            except ValueError as e:
                raise InvalidRequest(str(e)) from None
            page.items = [e.to_dict(include_data=include_data) for e in page.items]
            extra: dict[str, Any] = {"latest_seq": buffer.latest_seq}
            if not hub.sources:
                extra["note"] = "No event sources are configured on this server."
            elif not any(s.running for s in hub.sources.values()):
                extra["note"] = f"No event source is running; see {sources_name or 'the status tool'}."
            return list_result(
                page,
                fmt=response_format,
                shape=False,
                columns=["seq", "time", "source", "type", "severity", "summary", "keys"],
                extra=extra,
                hint="narrow the filters or lower the limit",
            )

    if sources_name:

        @reg.read(
            title=f"{product} event sources",
            name=sources_name,
            description=(
                f"Show the {product} event sources feeding {list_name or 'the event buffer'}: state "
                "(connected, reconnecting, disabled and why, failed), events received, last event time and "
                "lag, errors and the last error, drops by reason (rate limit, oversize, auth, duplicates, "
                "buffer evictions), listen addresses of webhook/syslog receivers, and the buffer's size and "
                "oldest event. Use it when events look missing or stale."
            ),
        )
        async def event_sources() -> ToolResult:
            return object_result(hub.status(), shape=False)

    if options.resource:

        @reg.mcp.resource(
            f"events://{prefix}/recent",
            name=f"{prefix}_recent_events",
            description=f"The {options.recent} most recent {product} events (JSON, newest first).",
            mime_type="application/json",
        )
        async def recent_events() -> str:
            page = buffer.query(limit=options.recent)
            return json.dumps(
                {"latest_seq": buffer.latest_seq, "events": [e.to_dict() for e in page.items]}, default=str
            )
