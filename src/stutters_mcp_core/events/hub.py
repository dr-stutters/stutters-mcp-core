"""EventHub: owns the buffer and the sources, starts and stops them with the server lifespan, routes
webhook requests, and registers the generic event tools and resource."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastmcp import Context
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from stutters_mcp_core.events.buffer import EventBuffer
from stutters_mcp_core.events.source import EventSource
from stutters_mcp_core.events.webhook import WebhookListener, WebhookReceiver

if TYPE_CHECKING:
    from stutters_mcp_core.settings import BaseServerSettings

log = logging.getLogger(__name__)

EventSources = Callable[[Any], list[EventSource]]


@dataclass(frozen=True)
class EventOptions:
    """Names of the generic event tools. "{prefix}" is replaced by the server prefix; None leaves a
    tool out (e.g. when the server already has a tool by that name and wants none, or its own).

    Catalyst Center already had an assurance tool called `catc_list_events`; it renames that one
    (to `catc_list_assurance_events`) and keeps the generic name, so every server reads its event
    buffer with `<prefix>_list_events`. A server that can't rename passes
    `EventOptions(list_tool="{prefix}_list_buffered_events")`."""

    list_tool: str | None = "{prefix}_list_events"
    sources_tool: str | None = "{prefix}_event_sources"
    resource: bool = True
    recent: int = 50

    def name(self, template: str | None, prefix: str) -> str | None:
        return template.replace("{prefix}", prefix) if template else None


class EventHub:
    def __init__(self, prefix: str, settings: BaseServerSettings, buffer: EventBuffer | None = None) -> None:
        self.prefix = prefix
        self.settings = settings
        self.buffer = buffer or EventBuffer(settings.mcp_event_buffer_size)
        self.sources: dict[str, EventSource] = {}
        self.listener: WebhookListener | None = None
        self.mounted_on_mcp_app = False  # set by build_server when the route is on the MCP app

    # --- lifecycle ---------------------------------------------------------------------------

    async def start(self, sources: list[EventSource]) -> None:
        await self.stop()
        names: set[str] = set()
        for src in sources:
            if src.name in names:
                raise ValueError(f"Two event sources are named {src.name!r}")
            names.add(src.name)
        self.sources = {s.name: s for s in sources}
        hooks = [s for s in sources if isinstance(s, WebhookReceiver) and s.enabled]
        path = self.settings.mcp_webhook_path
        if hooks and self.settings.mcp_webhook_port is not None:
            listener = WebhookListener(
                self.asgi_app(), self.settings.mcp_webhook_host, self.settings.mcp_webhook_port
            )
            try:
                await listener.start()
                self.listener = listener
                where = f"http://{listener.host}:{listener.port}"
            except OSError as e:
                log.error("webhook listener: %s", e)
                where = ""
                for h in hooks:
                    h.last_error = f"listener failed: {e}"[:300]
        elif hooks and self.mounted_on_mcp_app:
            where = f"MCP HTTP app ({self.settings.mcp_host}:{self.settings.mcp_port})"
        else:
            where = ""
            if hooks:
                log.warning(
                    "webhook receivers are enabled but not served: set MCP_TRANSPORT=http or MCP_WEBHOOK_PORT"
                )
        for h in hooks:
            h.listen = (
                f"POST {where}{path}/{h.name}"
                if where
                else "not served: needs MCP_TRANSPORT=http or MCP_WEBHOOK_PORT"
            )
        for src in sources:
            src.start(self.buffer)

    async def stop(self) -> None:
        for src in self.sources.values():
            await src.stop()
        if self.listener is not None:
            await self.listener.stop()
            self.listener = None

    # --- webhooks ----------------------------------------------------------------------------

    async def handle_webhook(self, request: Request) -> Response:
        src = self.sources.get(str(request.path_params.get("source", "")))
        if not isinstance(src, WebhookReceiver) or not src.enabled:
            return JSONResponse({"error": "unknown event source"}, status_code=404)
        return await src.handle(request)

    def asgi_app(self) -> Any:
        from starlette.applications import Starlette
        from starlette.routing import Route

        return Starlette(
            routes=[
                Route(f"{self.settings.mcp_webhook_path}/{{source}}", self.handle_webhook, methods=["POST"])
            ]
        )

    # --- status ------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "sources": [s.status() for s in self.sources.values()],
            "buffer": self.buffer.summary(),
        }

    def brief(self) -> dict[str, Any]:
        return {
            "sources": {s.name: s.state for s in self.sources.values()},
            "stored": len(self.buffer),
            "latest_seq": self.buffer.latest_seq,
        }


def get_events(ctx: Context) -> EventHub:
    """The server's EventHub (only when build_server got `event_sources=`)."""
    hub: EventHub = ctx.lifespan_context["events"]
    return hub
