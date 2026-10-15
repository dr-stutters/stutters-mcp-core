"""Bounded in-memory event store: a ring buffer with per-source statistics, dedupe, filters and
consumer cursors. Events live in process memory; a restart empties it (persistence is roadmap C6)."""

from __future__ import annotations

import json
from collections import OrderedDict, deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from stutters_mcp_core.events.model import Event, parse_time, utcnow
from stutters_mcp_core.http.paging import Page


@dataclass
class SourceStats:
    received: int = 0  # accepted into the buffer
    duplicates: int = 0  # dropped: same (source, id) seen recently
    evicted: int = 0  # pushed out of the ring buffer by newer events
    last_seq: int = 0
    last_time: datetime | None = None  # occurrence time of the newest event
    last_received: datetime | None = None
    last_lag: float | None = None  # received - occurred for the newest event, seconds

    def as_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "duplicates": self.duplicates,
            "evicted": self.evicted,
            "last_seq": self.last_seq or None,
            "last_event_at": self.last_time.isoformat(timespec="seconds") if self.last_time else None,
            "last_received_at": (
                self.last_received.isoformat(timespec="seconds") if self.last_received else None
            ),
            "lag_seconds": self.last_lag,
        }


def _split(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    parts = value.split(",") if isinstance(value, str) else list(value)
    return [p.strip().lower() for p in parts if p and p.strip()]


def _type_matcher(patterns: list[str]) -> Any:
    exact = {p for p in patterns if not p.endswith(("*", "."))}
    prefixes = tuple(p.rstrip("*") for p in patterns if p.endswith(("*", ".")))

    def match(t: str) -> bool:
        return t in exact or (bool(prefixes) and t.startswith(prefixes))

    return match


def _key_matcher(spec: str) -> Any:
    """'ip=10.1.1.1' matches that key; a bare value matches any key's value. Case-insensitive."""
    name, sep, value = spec.partition("=")
    if sep:
        name, value = name.strip().lower(), value.strip().lower()
        return lambda keys: keys.get(name, "").lower() == value
    want = spec.strip().lower()
    return lambda keys: any(v.lower() == want for v in keys.values())


class EventBuffer:
    """Ring buffer of at most `capacity` events, oldest evicted first.

    - `add(event)` assigns `seq` (strictly increasing, never reused) and `received`, and returns the
      stored event, or None when it duplicates a recent (source, id).
    - `query(...)` filters by source, type, key, severity, time window and `after_seq`.
    - `read_new(consumer)` returns events added since that consumer's previous call (its cursor).
    """

    def __init__(
        self,
        capacity: int = 5000,
        *,
        dedupe_window: int = 20_000,
        max_bytes: int | None = 64 * 1024 * 1024,
        max_event_bytes: int = 65_536,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        # Memory bounds besides the count: the estimated total size, and one event's `data`
        # (larger data is replaced by a truncated preview).
        self.max_bytes = max_bytes
        self.max_event_bytes = max_event_bytes
        self._bytes = 0
        self._sizes: dict[int, int] = {}
        self._events: deque[Event] = deque()
        self._seq = 0
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._dedupe_window = max(dedupe_window, 1)
        self._stats: dict[str, SourceStats] = {}
        self._cursors: dict[str, int] = {}

    # --- writing ---------------------------------------------------------------------------

    def add(self, event: Event) -> Event | None:
        stats = self._stats.setdefault(event.source, SourceStats())
        if event.id is not None:
            key = (event.source, event.id)
            if key in self._seen:
                self._seen.move_to_end(key)
                stats.duplicates += 1
                return None
            self._seen[key] = None
            while len(self._seen) > self._dedupe_window:
                self._seen.popitem(last=False)
        self._seq += 1
        event.seq = self._seq
        event.received = event.received or utcnow()
        size = self._limit_size(event)
        self._events.append(event)
        self._sizes[event.seq] = size
        self._bytes += size
        while len(self._events) > self.capacity or (
            self.max_bytes is not None and self._bytes > self.max_bytes and len(self._events) > 1
        ):
            old = self._events.popleft()
            self._bytes -= self._sizes.pop(old.seq, 0)
            self._stats.setdefault(old.source, SourceStats()).evicted += 1
        stats.received += 1
        stats.last_seq = event.seq
        stats.last_time = event.when
        stats.last_received = event.received
        stats.last_lag = event.lag_seconds()
        return event

    def _limit_size(self, event: Event) -> int:
        """Estimated memory cost of `event`, truncating oversized `data` first."""
        data_size = len(json.dumps(event.data, default=str)) if event.data else 0
        if data_size > self.max_event_bytes:
            preview = json.dumps(event.data, default=str)[:2000]
            event.data = {"truncated": True, "original_size": data_size, "preview": preview}
            data_size = len(preview) + 80
        if len(event.summary) > 2000:
            event.summary = event.summary[:2000] + "..."
        return 300 + data_size + len(event.summary) + sum(len(k) + len(v) for k, v in event.keys.items())

    def clear(self) -> None:
        """Drop every stored event (statistics and seq numbering are kept)."""
        self._events.clear()
        self._sizes.clear()
        self._bytes = 0

    # --- reading ---------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(list(self._events))

    @property
    def latest_seq(self) -> int:
        return self._seq

    def stats(self, source: str) -> SourceStats:
        return self._stats.get(source, SourceStats())

    def sources(self) -> list[str]:
        return sorted(self._stats)

    def summary(self) -> dict[str, Any]:
        oldest = self._events[0] if self._events else None
        return {
            "stored": len(self._events),
            "capacity": self.capacity,
            "latest_seq": self._seq,
            "oldest_seq": oldest.seq if oldest else None,
            "oldest_event_at": oldest.when.isoformat(timespec="seconds") if oldest else None,
            "evicted": sum(s.evicted for s in self._stats.values()),
            "approx_bytes": self._bytes,
        }

    def matching(
        self,
        *,
        source: str | Iterable[str] | None = None,
        type: str | Iterable[str] | None = None,  # noqa: A002 - matches the tool parameter
        key: str | None = None,
        severity: str | Iterable[str] | None = None,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        after_seq: int | None = None,
        text: str | None = None,
    ) -> list[Event]:
        """Matching events, oldest first. `source`/`type`/`severity` take one value, a comma list or
        an iterable; a type ending in '*' or '.' is a prefix ("auth.*"). `key` is 'name=value' or a
        bare value matched against every key. `since`/`until` filter on occurrence time (ISO 8601 or
        an age like '15m'). `text` is a case-insensitive substring of the summary."""
        sources = set(_split(source))
        types = _split(type)
        type_ok = _type_matcher(types) if types else None
        key_ok = _key_matcher(key) if key and key.strip() else None
        severities = set(_split(severity))
        t_from, t_to = parse_time(since), parse_time(until)
        needle = text.lower() if text else None
        out: list[Event] = []
        for ev in self._events:
            if after_seq is not None and ev.seq <= after_seq:
                continue
            if sources and ev.source.lower() not in sources:
                continue
            if type_ok is not None and not type_ok(ev.type):
                continue
            if severities and (ev.severity or "") not in severities:
                continue
            if t_from is not None and ev.when < t_from:
                continue
            if t_to is not None and ev.when > t_to:
                continue
            if key_ok is not None and not key_ok(ev.keys):
                continue
            if needle is not None and needle not in ev.summary.lower():
                continue
            out.append(ev)
        return out

    def query(self, *, limit: int = 50, offset: int = 0, newest_first: bool = True, **filters: Any) -> Page:
        """A page of matching events (newest first by default); `total` counts all matches."""
        found = self.matching(**filters)
        if newest_first:
            found.reverse()
        return Page(found[offset : offset + limit], offset, len(found))

    def read_new(self, consumer: str, *, limit: int = 1000, **filters: Any) -> list[Event]:
        """Events added since `consumer` last read (oldest first, at most `limit`), advancing its
        cursor past what was returned. A new consumer starts at the beginning of the buffer."""
        cursor = self._cursors.get(consumer, 0)
        matches = self.matching(after_seq=cursor, **filters)
        found = matches[:limit]
        # Everything up to the newest event has been examined unless the limit cut the list short.
        self._cursors[consumer] = found[-1].seq if len(matches) > limit else self._seq
        return found

    def cursor(self, consumer: str) -> int:
        return self._cursors.get(consumer, 0)
