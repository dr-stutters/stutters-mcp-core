"""Appliance clock offset and the timestamp convention (roadmap C69).

Timeline prompts merge events from several appliances (ISE auth history, Catalyst Center issues, SD-WAN
alarms, FMC audit). When one appliance's clock has drifted, its events land in the wrong order. No product
API reports "my clock", but every HTTP response carries a `Date` header, so core measures the offset on
every request the shared `ApiClient` sends:

    offset = appliance Date - this server's clock at the middle of the exchange

`<prefix>_status` reports it under `server.clock`, with the timestamp convention below and the product's
own convention when the server declares one (`ServerSpec(log_timestamps=...)`):

    "clock": {"source": "HTTP Date header", "offset_seconds": 42.5, "uncertainty_seconds": 0.6,
              "appliance_time": "2026-10-24T03:13:20+00:00", "observed_at": "...", "drift": "ahead",
              "warning": "The appliance clock is 42.5 s ahead of this server: ...",
              "timestamps": {"tools": TIMESTAMP_CONVENTION, "product": "..."}}

`Date` has one-second resolution, so offsets within `DRIFT_THRESHOLD_SECONDS` read as "ok". A server whose
status does not go through `ApiClient` (an SDK, a console) can call `observe()` itself.
"""

from __future__ import annotations

import email.utils
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "DRIFT_THRESHOLD_SECONDS",
    "TIMESTAMP_CONVENTION",
    "ClockSample",
    "clear",
    "clock_status",
    "observe",
    "observe_response",
    "sample_for",
    "to_server_time",
]

DRIFT_THRESHOLD_SECONDS = 5.0

TIMESTAMP_CONVENTION = (
    "Tool output timestamps are ISO 8601. One with Z or an offset is absolute; one without is the "
    "appliance's local time as the product reported it. Timestamps are on the appliance's clock and are "
    "not corrected: before merging timelines across servers, subtract each server's clock.offset_seconds "
    "(positive = that appliance runs ahead)."
)


@dataclass(frozen=True)
class ClockSample:
    offset_seconds: float  # appliance minus this server; positive = the appliance runs ahead
    uncertainty_seconds: float  # half the round trip plus the Date header's half-second resolution
    appliance_time: datetime
    observed_at: datetime  # this server's clock (UTC) at the middle of the exchange


_SAMPLES: dict[str, ClockSample] = {}


def _key(url: str) -> str:
    """Samples are kept per appliance host (lowercase, no port): an appliance's API surfaces on
    different ports share one clock."""
    host = urlsplit(url if "//" in url else f"//{url}").hostname
    return (host or url).lower()


def observe(url: str, date_header: str | None, *, sent: float, received: float) -> ClockSample | None:
    """Record the offset from one response. `sent` / `received` are this server's `time.time()` around
    the exchange. Returns the sample, or None when the header is missing or unreadable."""
    if not date_header:
        return None
    try:
        when = email.utils.parsedate_to_datetime(date_header)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:  # RFC 7231 says GMT; a naive parse means "-0000": treat it as UTC
        when = when.replace(tzinfo=UTC)
    received = max(received, sent)
    middle = (sent + received) / 2
    # The header truncates to whole seconds: the true time is in [when, when + 1).
    appliance = when.timestamp() + 0.5
    sample = ClockSample(
        offset_seconds=round(appliance - middle, 1),
        uncertainty_seconds=round((received - sent) / 2 + 0.5, 1),
        appliance_time=when.astimezone(UTC),
        observed_at=datetime.fromtimestamp(middle, UTC),
    )
    _SAMPLES[_key(url)] = sample
    return sample


def observe_response(url: str, headers: Any, *, sent: float, received: float | None = None) -> None:
    """`observe` from a response's headers (anything with `.get`); used by ApiClient. A passive
    measurement: nothing about a response's Date header may fail the request it came with."""
    try:
        observe(url, headers.get("Date"), sent=sent, received=time.time() if received is None else received)
    except Exception:  # noqa: BLE001 - see the docstring
        return


def sample_for(url: str) -> ClockSample | None:
    """The latest sample for the appliance at `url` (any URL on that host), or None."""
    return _SAMPLES.get(_key(url))


def clear() -> None:
    """Forget every sample (tests)."""
    _SAMPLES.clear()


def to_server_time(timestamp: datetime, offset_seconds: float | None) -> datetime:
    """An appliance timestamp moved onto this server's clock (subtract the offset). None = unchanged."""
    if not offset_seconds:
        return timestamp
    return timestamp - timedelta(seconds=offset_seconds)


def _seconds(value: float) -> str:
    return f"{abs(value):g} s" if abs(value) < 120 else f"{abs(value) / 60:.1f} min"


def clock_status(url: str, *, product_convention: str | None = None) -> dict[str, Any]:
    """The `server.clock` block of `<prefix>_status` for the appliance at `url`."""
    timestamps: dict[str, str] = {"tools": TIMESTAMP_CONVENTION}
    if product_convention:
        timestamps["product"] = product_convention
    sample = sample_for(url)
    if sample is None:
        return {
            "source": "HTTP Date header",
            "offset_seconds": None,
            "note": "No response with a Date header from this appliance yet; the offset is unknown.",
            "timestamps": timestamps,
        }
    offset = sample.offset_seconds
    out: dict[str, Any] = {
        "source": "HTTP Date header",
        "offset_seconds": offset,
        "uncertainty_seconds": sample.uncertainty_seconds,
        "appliance_time": sample.appliance_time.isoformat(timespec="seconds"),
        "observed_at": sample.observed_at.isoformat(timespec="seconds"),
    }
    if abs(offset) <= max(DRIFT_THRESHOLD_SECONDS, sample.uncertainty_seconds):
        out["drift"] = "ok"
    else:
        direction = "ahead of" if offset > 0 else "behind"
        out["drift"] = "ahead" if offset > 0 else "behind"
        out["warning"] = (
            f"The appliance clock is {_seconds(offset)} {direction} this server: its timestamps are "
            f"{'late' if offset > 0 else 'early'} by that much. Subtract offset_seconds before merging "
            "its events with other servers' timelines, and check its NTP configuration."
        )
    out["timestamps"] = timestamps
    return out
