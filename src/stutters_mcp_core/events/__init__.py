"""Event framework (ADR-003): sources feed a bounded in-memory buffer that the generic tools read.

from stutters_mcp_core.events import Event, PollingEventSource, SyslogReceiver, WebhookReceiver

def event_sources(app) -> list[EventSource]:          # build_server(event_sources=...)
    return [PollingEventSource("issues", app.poll_issues, interval=60),
            SyslogReceiver.from_settings(settings, to_event=ftd_event)]
"""

from stutters_mcp_core.events.buffer import EventBuffer, SourceStats
from stutters_mcp_core.events.hub import EventHub, EventOptions, EventSources, get_events
from stutters_mcp_core.events.limits import AllowList, PerKeyLimiter, TokenBucket
from stutters_mcp_core.events.model import SEVERITIES, Event, parse_time, utcnow
from stutters_mcp_core.events.source import EventSource, PollingEventSource, SourceConfigError
from stutters_mcp_core.events.syslog import SyslogMessage, SyslogReceiver, default_syslog_event, parse_syslog
from stutters_mcp_core.events.webhook import (
    WebhookAuth,
    WebhookReceiver,
    default_webhook_events,
    parse_webhook_secrets,
)

__all__ = [
    "SEVERITIES",
    "AllowList",
    "Event",
    "EventBuffer",
    "EventHub",
    "EventOptions",
    "EventSource",
    "EventSources",
    "PerKeyLimiter",
    "PollingEventSource",
    "SourceConfigError",
    "SourceStats",
    "SyslogMessage",
    "SyslogReceiver",
    "TokenBucket",
    "WebhookAuth",
    "WebhookReceiver",
    "default_syslog_event",
    "default_webhook_events",
    "get_events",
    "parse_syslog",
    "parse_time",
    "parse_webhook_secrets",
    "utcnow",
]
