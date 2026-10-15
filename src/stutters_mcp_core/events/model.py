"""The event record shared by every source, and time-filter parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

SEVERITIES = ("critical", "error", "warning", "notice", "info", "debug")


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(t: datetime) -> datetime:
    """Naive datetimes are taken to be UTC."""
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


@dataclass
class Event:
    """One event from any source.

    - `type`: dotted and lower-case, most general first: "auth.fail", "health.alert", "syslog.430002".
      Filters match exact types or prefixes ("auth.*").
    - `time`: when it happened by the source's clock (UTC). Defaults to when it was received.
    - `keys`: correlation keys, lower-case names: mac, ip, user, device, site, rule... Values are strings.
    - `id`: the source's own id, when it has one; the buffer drops a second event with the same
      (source, id), so a poller and a webhook can deliver the same thing safely.
    - `seq` and `received` are set by the buffer; `source` by the EventSource that emits it.
    """

    type: str
    summary: str = ""
    time: datetime | None = None
    keys: dict[str, str] = field(default_factory=dict)
    severity: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    id: str | None = None
    source: str = ""
    seq: int = 0
    received: datetime | None = None

    def __post_init__(self) -> None:
        self.type = self.type.strip().lower() or "event"
        if self.time is not None:
            self.time = as_utc(self.time)
        self.keys = {
            str(k).strip().lower(): str(v).strip()
            for k, v in self.keys.items()
            if v is not None and str(v).strip()
        }
        if self.severity is not None:
            self.severity = self.severity.strip().lower() or None

    @property
    def when(self) -> datetime:
        """Occurrence time, falling back to receive time."""
        return self.time or self.received or utcnow()

    def lag_seconds(self) -> float | None:
        if self.time is None or self.received is None:
            return None
        return round((self.received - self.time).total_seconds(), 3)

    def to_dict(self, *, include_data: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "seq": self.seq,
            "time": self.when.isoformat(timespec="seconds"),
            "source": self.source,
            "type": self.type,
            "severity": self.severity,
            "summary": self.summary,
            "keys": self.keys or None,
        }
        if self.id is not None:
            out["id"] = self.id
        if include_data and self.data:
            out["data"] = self.data
        return {k: v for k, v in out.items() if v is not None}


_REL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days)\s*(ago)?\s*$", re.I)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_time(value: str | datetime | None, *, now: datetime | None = None) -> datetime | None:
    """A filter time: a datetime, an ISO 8601 string ("2026-10-15T09:30:00Z"; naive means UTC), a
    relative age ("15m", "2h", "1d", "30s", "10 min ago") or a Unix timestamp. None passes through."""
    if value is None or isinstance(value, datetime):
        return as_utc(value) if isinstance(value, datetime) else None
    text = value.strip()
    if not text:
        return None
    now = now or utcnow()
    m = _REL_RE.match(text)
    if m:
        return now - timedelta(seconds=float(m.group(1)) * _UNITS[m.group(2)[0].lower()])
    try:
        return datetime.fromtimestamp(float(text), UTC)
    except ValueError:
        pass
    try:
        return as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        raise ValueError(
            f"Can't read time {value!r}: use ISO 8601 (2026-10-15T09:30:00Z) or an age such as 15m, 2h, 1d"
        ) from None
