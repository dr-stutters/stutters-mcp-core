"""Prometheus metrics (roadmap G5): `/metrics` in the Prometheus text format. Off by default.

    MCP_METRICS=true                 collect and serve metrics
    MCP_METRICS_PORT=9464            serve them on a separate, unauthenticated listener (stdio too),
    MCP_METRICS_HOST=127.0.0.1       bound to this address (default loopback)

Without MCP_METRICS_PORT, an HTTP server serves `GET /metrics` on its MCP port, and the scraper must send
`Authorization: Bearer <token>` with one of the MCP_AUTH_TOKENS (Prometheus:
`authorization.credentials_file`).

Series (all prefixed `mcp_`; `server` is the server name, `product` the appliance, e.g. "ISE"):
    mcp_info{server,version,core_version} 1
    mcp_tool_calls_total{server,tool,outcome}             outcome: ok | error
    mcp_tool_duration_seconds{server,tool}                histogram
    mcp_write_events_total{server,tool,event}             preview | applied | apply_failed | blocked | refused
    mcp_upstream_requests_total{product,method,status}    status: HTTP code, or timeout | error
    mcp_upstream_duration_seconds{product,method}         histogram
    mcp_upstream_retries_total{product,reason}            reason: HTTP code or timeout | error
    mcp_rate_limit_waits_total{product}                   requests that waited for the client-side limiter
    mcp_rate_limit_wait_seconds_total{product}

Label values never include ids, paths or arguments, so cardinality stays bounded by the tool list.
Collection is a few dictionary updates per call and happens whether or not metrics are served; the
module keeps one process-wide registry (one server per process).
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

TOOL_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600)
UPSTREAM_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
RATE_WAIT_THRESHOLD = 0.005  # seconds; shorter waits are scheduling noise

Labels = tuple[tuple[str, str], ...]


@dataclass
class _Histogram:
    buckets: tuple[float, ...]
    counts: list[int] = field(default_factory=list)
    total: float = 0.0
    count: int = 0

    def __post_init__(self) -> None:
        self.counts = [0] * len(self.buckets)

    def observe(self, value: float) -> None:
        self.total += value
        self.count += 1
        for i, b in enumerate(self.buckets):
            if value <= b:
                self.counts[i] += 1


@dataclass(frozen=True)
class _Family:
    name: str
    kind: str  # counter | gauge | histogram
    help: str
    buckets: tuple[float, ...] = ()


FAMILIES = {
    f.name: f
    for f in (
        _Family("mcp_info", "gauge", "Server and core versions."),
        _Family("mcp_tool_calls_total", "counter", "MCP tool calls by outcome."),
        _Family("mcp_tool_duration_seconds", "histogram", "MCP tool call duration.", TOOL_BUCKETS),
        _Family("mcp_write_events_total", "counter", "Preview-first write events."),
        _Family("mcp_upstream_requests_total", "counter", "Requests to the appliance API by status."),
        _Family(
            "mcp_upstream_duration_seconds", "histogram", "Appliance API request duration.", UPSTREAM_BUCKETS
        ),
        _Family("mcp_upstream_retries_total", "counter", "Retried appliance API requests."),
        _Family(
            "mcp_rate_limit_waits_total", "counter", "Requests that waited for the client-side rate limiter."
        ),
        _Family("mcp_rate_limit_wait_seconds_total", "counter", "Time spent waiting for the rate limiter."),
    )
}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt_labels(labels: Labels, extra: Iterable[tuple[str, str]] = ()) -> str:
    items = [*labels, *extra]
    if not items:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in items) + "}"


def _fmt_value(v: float) -> str:
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    return repr(float(v)) if not float(v).is_integer() else str(int(v))


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, dict[Labels, float]] = {}
        self._hists: dict[str, dict[Labels, _Histogram]] = {}

    def reset(self) -> None:
        with self._lock:
            self._values.clear()
            self._hists.clear()

    def inc(self, name: str, labels: dict[str, str], amount: float = 1.0) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            series = self._values.setdefault(name, {})
            series[key] = series.get(key, 0.0) + amount

    def set(self, name: str, labels: dict[str, str], value: float) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values.setdefault(name, {})[key] = value

    def observe(self, name: str, labels: dict[str, str], value: float) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            series = self._hists.setdefault(name, {})
            hist = series.get(key)
            if hist is None:
                hist = series[key] = _Histogram(FAMILIES[name].buckets)
            hist.observe(value)

    def value(self, name: str, **labels: str) -> float:
        """Current value of a counter/gauge series (tests)."""
        return self._values.get(name, {}).get(tuple(sorted(labels.items())), 0.0)

    def histogram_count(self, name: str, **labels: str) -> int:
        hist = self._hists.get(name, {}).get(tuple(sorted(labels.items())))
        return hist.count if hist else 0

    def render(self) -> str:
        """The Prometheus text exposition format (version 0.0.4)."""
        lines: list[str] = []
        with self._lock:
            for name, fam in FAMILIES.items():
                if fam.kind == "histogram":
                    hists = self._hists.get(name)
                    if not hists:
                        continue
                    lines += [f"# HELP {name} {fam.help}", f"# TYPE {name} histogram"]
                    for labels, h in sorted(hists.items()):
                        for b, c in zip(h.buckets, h.counts, strict=True):
                            lines.append(f"{name}_bucket{_fmt_labels(labels, [('le', _fmt_value(b))])} {c}")
                        lines.append(f"{name}_bucket{_fmt_labels(labels, [('le', '+Inf')])} {h.count}")
                        lines.append(f"{name}_sum{_fmt_labels(labels)} {_fmt_value(h.total)}")
                        lines.append(f"{name}_count{_fmt_labels(labels)} {h.count}")
                else:
                    values = self._values.get(name)
                    if not values:
                        continue
                    lines += [f"# HELP {name} {fam.help}", f"# TYPE {name} {fam.kind}"]
                    for labels, v in sorted(values.items()):
                        lines.append(f"{name}{_fmt_labels(labels)} {_fmt_value(v)}")
        return "\n".join(lines) + "\n"


REGISTRY = Registry()
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


# --- recording helpers (called from audit, writes and the HTTP client) --------------------------


def record_tool(server: str, tool: str, outcome: str, seconds: float) -> None:
    REGISTRY.inc("mcp_tool_calls_total", {"server": server, "tool": tool, "outcome": outcome})
    REGISTRY.observe("mcp_tool_duration_seconds", {"server": server, "tool": tool}, seconds)


def record_write(server: str, tool: str, event: str) -> None:
    REGISTRY.inc("mcp_write_events_total", {"server": server, "tool": tool, "event": event})


def record_upstream(product: str, method: str, status: str, seconds: float) -> None:
    REGISTRY.inc("mcp_upstream_requests_total", {"product": product, "method": method, "status": status})
    REGISTRY.observe("mcp_upstream_duration_seconds", {"product": product, "method": method}, seconds)


def record_retry(product: str, reason: str) -> None:
    REGISTRY.inc("mcp_upstream_retries_total", {"product": product, "reason": reason})


def record_rate_wait(product: str, seconds: float) -> None:
    if seconds < RATE_WAIT_THRESHOLD:
        return
    REGISTRY.inc("mcp_rate_limit_waits_total", {"product": product})
    REGISTRY.inc("mcp_rate_limit_wait_seconds_total", {"product": product}, seconds)


def set_info(server: str, version: str, core_version: str) -> None:
    REGISTRY.set("mcp_info", {"server": server, "version": version, "core_version": core_version}, 1)


# --- separate listener (MCP_METRICS_PORT) -------------------------------------------------------


class Listener:
    def __init__(self, server: object, task: object) -> None:
        self.server = server
        self.task = task

    async def stop(self) -> None:
        import asyncio

        self.server.should_exit = True  # type: ignore[attr-defined]
        try:
            await asyncio.wait_for(self.task, timeout=5)  # type: ignore[arg-type]
        except (TimeoutError, asyncio.CancelledError):
            self.task.cancel()  # type: ignore[attr-defined]


_listener: Listener | None = None


async def start_listener(host: str, port: int) -> Listener | None:
    """Serve GET /metrics (and nothing else) on host:port, unauthenticated: bind it to loopback or a
    monitoring network only. Started once per process; returns None if already running."""
    import asyncio
    import contextlib

    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    global _listener
    if _listener is not None and not _listener.task.done():  # type: ignore[attr-defined]
        return None

    async def metrics_endpoint(request: Request) -> PlainTextResponse:
        return PlainTextResponse(REGISTRY.render(), media_type=CONTENT_TYPE)

    app = Starlette(routes=[Route("/metrics", metrics_endpoint, methods=["GET"])])
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False, lifespan="off")

    class _Server(uvicorn.Server):
        def capture_signals(self) -> Any:  # the MCP server owns SIGINT/SIGTERM
            return contextlib.nullcontext()

    server = _Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):  # wait up to ~5 s for the bind
        if server.started or task.done():
            break
        await asyncio.sleep(0.05)
    if task.done():
        exc = task.exception()
        import logging

        logging.getLogger(__name__).error("metrics listener on %s:%s failed: %s", host, port, exc)
        return None
    _listener = Listener(server, task)
    return _listener
