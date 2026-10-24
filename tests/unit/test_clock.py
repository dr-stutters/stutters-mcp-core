"""C69: appliance clock offset from the HTTP Date header, and the timestamp convention in <prefix>_status."""

import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
import respx

from stutters_mcp_core import clock
from stutters_mcp_core.http import ApiClient
from stutters_mcp_core.testing import call_tool

from . import demo
from .demo import BASE, make_settings


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    clock.clear()
    yield
    clock.clear()


def _date(ts: float) -> str:
    return format_datetime(datetime.fromtimestamp(ts, UTC), usegmt=True)


def test_offset_from_date_header() -> None:
    now = 1_790_000_000.0  # whole second: the header loses nothing
    s = clock.observe("https://ise.example:9060/ers", _date(now + 42), sent=now - 0.2, received=now + 0.2)
    assert s is not None
    assert s.offset_seconds == 42.5  # +0.5: the header truncates to whole seconds
    assert s.uncertainty_seconds == 0.7
    assert s.appliance_time == datetime.fromtimestamp(now + 42, UTC)
    # One sample per host, whatever the port or path.
    assert clock.sample_for("https://ISE.example") == s


@pytest.mark.parametrize(
    "header",
    [None, "", "not a date", "Mon, 99 Foo 2026 25:61:00 GMT", "x, 99999999999 Jan 2026 00:00:00 GMT"],
)
def test_unreadable_date_is_ignored(header: str | None) -> None:
    assert clock.observe("https://x.example", header, sent=1.0, received=2.0) is None
    assert clock.sample_for("https://x.example") is None


def test_status_block_ok_ahead_behind_unknown() -> None:
    unknown = clock.clock_status("https://a.example", product_convention="MnT: local time")
    assert unknown["offset_seconds"] is None and "unknown" in unknown["note"]
    assert unknown["timestamps"] == {"tools": clock.TIMESTAMP_CONVENTION, "product": "MnT: local time"}

    now = 1_790_000_000.0
    clock.observe("https://a.example", _date(now + 1), sent=now, received=now)
    ok = clock.clock_status("https://a.example")
    assert ok["drift"] == "ok" and "warning" not in ok and ok["offset_seconds"] == 1.5
    assert ok["timestamps"] == {"tools": clock.TIMESTAMP_CONVENTION}

    clock.observe("https://a.example", _date(now + 300), sent=now, received=now)
    ahead = clock.clock_status("https://a.example")
    assert ahead["drift"] == "ahead"
    assert "5.0 min ahead of this server" in ahead["warning"] and "late" in ahead["warning"]

    clock.observe("https://a.example", _date(now - 30), sent=now, received=now)
    behind = clock.clock_status("https://a.example")
    assert behind["drift"] == "behind" and "29.5 s behind" in behind["warning"]


def test_slow_exchange_widens_ok_band() -> None:
    now = 1_790_000_000.0
    clock.observe("https://slow.example", _date(now + 10), sent=now - 10, received=now + 10)
    st = clock.clock_status("https://slow.example")
    assert st["uncertainty_seconds"] == 10.5 and st["drift"] == "ok"


def test_to_server_time() -> None:
    t = datetime(2026, 10, 24, 3, 13, 20, tzinfo=UTC)
    assert clock.to_server_time(t, 42.0) == datetime(2026, 10, 24, 3, 12, 38, tzinfo=UTC)
    assert clock.to_server_time(t, None) == t


async def test_api_client_records_offset() -> None:
    skew = 3600.0
    with respx.mock(base_url="https://fmc.example") as mock:
        mock.get("/api/x").mock(
            side_effect=lambda req: httpx.Response(200, json={}, headers={"Date": _date(time.time() + skew)})
        )
        async with ApiClient("https://fmc.example/api", product="FMC") as api:
            await api.get("/x")
    sample = clock.sample_for("https://fmc.example")
    assert sample is not None and abs(sample.offset_seconds - skew) < 2


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        mock.get("/api/version").mock(
            side_effect=lambda req: httpx.Response(
                200, json={"version": "1.2.3"}, headers={"Date": _date(time.time() - 120)}
            )
        )
        yield mock


async def test_status_tool_reports_clock(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    data = await call_tool(demo.create_server(make_settings()), "demo_status")
    c = data["server"]["clock"]
    assert c["source"] == "HTTP Date header" and c["drift"] == "behind"
    assert -121 < c["offset_seconds"] < -118
    assert c["timestamps"] == {"tools": clock.TIMESTAMP_CONVENTION}

    monkeypatch.setattr(demo, "SPEC", replace(demo.SPEC, log_timestamps="Demo events: epoch ms, UTC"))
    data = await call_tool(demo.create_server(make_settings()), "demo_status")
    assert data["server"]["clock"]["timestamps"]["product"] == "Demo events: epoch ms, UTC"
