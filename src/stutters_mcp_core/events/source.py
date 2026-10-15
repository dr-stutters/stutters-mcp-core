"""Event sources: background tasks that feed the EventBuffer.

Subclass `EventSource` and implement `run()`: connect, call `self.set_connected()`, then loop calling
`self.emit(event)`. Return or raise when the connection ends; the supervisor restarts `run()` with
exponential backoff (and jitter), records the error, and keeps going until the server stops.
Raise `SourceConfigError` for problems a retry can't fix (missing secret): the source then stays
`failed` without retrying.

Most first sources poll: use `PollingEventSource` with a `poll(since)` function instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from stutters_mcp_core.events.model import Event, utcnow

if TYPE_CHECKING:
    from stutters_mcp_core.events.buffer import EventBuffer

log = logging.getLogger(__name__)


class SourceConfigError(Exception):
    """A configuration problem retrying won't fix; the source stops in state 'failed'."""


def _iso(t: datetime | None) -> str | None:
    return t.isoformat(timespec="seconds") if t else None


class EventSource(ABC):
    """A supervised background task feeding events into the buffer.

    `kind`: "poll" (asks the product periodically), "stream" (holds a connection open: websocket,
    pxGrid, eStreamer), "push" (the product connects to us: webhook, syslog).
    `enabled=False` lists the source in `<prefix>_event_sources` with `disabled_reason` but never
    starts it, so the model can tell the user how to turn it on.
    """

    kind: str = "stream"

    def __init__(
        self,
        name: str,
        *,
        description: str = "",
        enabled: bool = True,
        disabled_reason: str = "",
        backoff_initial: float = 1.0,
        backoff_max: float = 60.0,
        healthy_after: float = 30.0,
    ) -> None:
        if not name or not name.replace("-", "_").replace("_", "").isalnum() or name != name.lower():
            raise ValueError(f"Event source name {name!r} must be lower-case letters, digits, - or _")
        self.name = name
        self.description = description
        self.enabled = enabled
        self.disabled_reason = disabled_reason
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.healthy_after = healthy_after
        self.buffer: EventBuffer | None = None
        self.state = "disabled" if not enabled else "stopped"
        self.connected = False
        self.emitted = 0
        self.errors = 0
        self.restarts = 0
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.started_at: datetime | None = None
        self.connected_at: datetime | None = None
        self.drops: dict[str, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._next_retry: float | None = None
        self._emitted_this_run = False

    # --- for subclasses ----------------------------------------------------------------------

    @abstractmethod
    async def run(self) -> None:
        """Connect and deliver events until the connection ends (return) or fails (raise)."""

    def emit(self, event: Event) -> Event | None:
        """Store one event (stamped with this source's name). Returns it, or None if it was a duplicate."""
        if self.buffer is None:
            raise RuntimeError(f"event source {self.name!r} is not attached to a buffer")
        event.source = self.name
        stored = self.buffer.add(event)
        if stored is not None:
            self.emitted += 1
            self._emitted_this_run = True
        return stored

    def drop(self, reason: str, n: int = 1) -> None:
        """Count events that were lost or refused before reaching the buffer (rate limit, oversize,
        parse error, sender not allowed...)."""
        self.drops[reason] = self.drops.get(reason, 0) + n

    def set_connected(self, connected: bool = True) -> None:
        self.connected = connected
        if connected:
            self.state = "connected"
            self.connected_at = utcnow()

    def extra_status(self) -> dict[str, Any]:
        """Source-specific status fields (listen address, poll interval...)."""
        return {}

    # --- lifecycle (called by the EventHub) --------------------------------------------------

    def start(self, buffer: EventBuffer) -> None:
        self.buffer = buffer
        if not self.enabled or self._task is not None:
            return
        self._stopping = False
        self.started_at = utcnow()
        self._task = asyncio.create_task(self._supervise(), name=f"event-source:{self.name}")

    async def stop(self, timeout: float = 5.0) -> None:  # noqa: ASYNC109 - bounded shutdown
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
                await asyncio.wait_for(task, timeout)
        self.connected = False
        if self.enabled and self.state != "failed":
            self.state = "stopped"

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _supervise(self) -> None:
        backoff = self.backoff_initial
        while not self._stopping:
            self.state = "connecting"
            self._emitted_this_run = False
            began = time.monotonic()
            try:
                await self.run()
                log.info("event source %s ended; restarting", self.name)
            except asyncio.CancelledError:
                raise
            except SourceConfigError as e:
                self._record_error(e)
                self.state = "failed"
                self.connected = False
                log.error("event source %s disabled: %s", self.name, e)
                return
            except Exception as e:  # noqa: BLE001 - any failure is retried with backoff
                self._record_error(e)
                log.warning("event source %s failed: %s", self.name, self.last_error)
            self.connected = False
            if self._stopping:
                break
            if self._emitted_this_run or time.monotonic() - began >= self.healthy_after:
                backoff = self.backoff_initial
            delay = backoff * random.uniform(0.8, 1.2)  # noqa: S311 - jitter, not crypto
            backoff = min(self.backoff_max, backoff * 2)
            self.state = "reconnecting"
            self.restarts += 1
            self._next_retry = time.monotonic() + delay
            await asyncio.sleep(delay)
            self._next_retry = None

    def _record_error(self, e: BaseException) -> None:
        self.errors += 1
        self.last_error = (f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)[:300]
        self.last_error_at = utcnow()

    def status(self) -> dict[str, Any]:
        stats = self.buffer.stats(self.name).as_dict() if self.buffer else {}
        out: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "description": self.description or None,
            "state": self.state,
            "connected": self.connected,
            "events": self.emitted,
            **stats,
            "errors": self.errors,
            "last_error": self.last_error,
            "last_error_at": _iso(self.last_error_at),
            "restarts": self.restarts,
            "drops": dict(self.drops) or None,
            "connected_since": _iso(self.connected_at) if self.connected else None,
        }
        if self._next_retry is not None:
            out["retry_in_seconds"] = max(0, round(self._next_retry - time.monotonic(), 1))
        if not self.enabled:
            out["disabled_reason"] = self.disabled_reason or None
        out.update(self.extra_status())
        return {k: v for k, v in out.items() if v is not None}


PollFn = Callable[[datetime | None], Awaitable[Iterable[Event]]]


def _fingerprint(ev: Event) -> str:
    raw = json.dumps([ev.type, ev.time.isoformat() if ev.time else None, ev.summary, ev.keys], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class PollingEventSource(EventSource):
    """Calls `poll(since)` every `interval` seconds and emits events it hasn't seen before.

    - `since` is the start time of the previous successful poll (None the first time), so the poll
      can ask the product for a time window. Overlap is fine: events are deduplicated by `Event.id`,
      or by `id_of(event)`, or by a hash of type + time + summary + keys.
    - `emit_initial=False` swallows whatever the first poll returns (history), so only changes after
      startup become events. The default emits it (the buffer then starts with recent history).
    - A poll that raises is retried with the supervisor's backoff; the seen-set survives retries.
    - Pass `poll=` or override `async def poll(self, since)` in a subclass.
    """

    kind = "poll"

    def __init__(
        self,
        name: str,
        poll: PollFn | None = None,
        *,
        interval: float = 60.0,
        emit_initial: bool = True,
        id_of: Callable[[Event], str | None] | None = None,
        seen_size: int = 20_000,
        **kwargs: Any,
    ) -> None:
        super().__init__(name, **kwargs)
        if interval <= 0:
            raise ValueError("interval must be positive")
        self._poll_fn = poll
        self.interval = interval
        self.emit_initial = emit_initial
        self.id_of = id_of
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_size = seen_size
        self._since: datetime | None = None
        self._first = True
        self.polls = 0
        self.last_poll_at: datetime | None = None
        self.last_poll_seconds: float | None = None

    async def poll(self, since: datetime | None) -> Iterable[Event]:
        if self._poll_fn is None:
            raise NotImplementedError("pass poll= or override poll()")
        return await self._poll_fn(since)

    def _ident(self, ev: Event) -> str:
        ident = self.id_of(ev) if self.id_of else None
        return ident or ev.id or _fingerprint(ev)

    async def poll_once(self) -> int:
        """One poll: returns how many new events were emitted."""
        started = utcnow()
        t0 = time.monotonic()
        events = list(await self.poll(self._since))
        self.last_poll_seconds = round(time.monotonic() - t0, 3)
        self.polls += 1
        self.last_poll_at = started
        if all(e.time is not None for e in events):
            events.sort(key=lambda e: e.when)
        new = 0
        for ev in events:
            ident = self._ident(ev)
            if ident in self._seen:
                continue
            self._seen[ident] = None
            if len(self._seen) > self._seen_size:
                self._seen.popitem(last=False)
            if self._first and not self.emit_initial:
                continue
            if ev.id is None:
                ev.id = ident
            if self.emit(ev) is not None:
                new += 1
        self._first = False
        self._since = started
        return new

    async def run(self) -> None:
        while True:
            await self.poll_once()
            self.set_connected()
            await asyncio.sleep(self.interval)

    def extra_status(self) -> dict[str, Any]:
        return {
            "interval_seconds": self.interval,
            "polls": self.polls,
            "last_poll_at": _iso(self.last_poll_at),
            "last_poll_seconds": self.last_poll_seconds,
        }
