import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from stutters_mcp_core.events import (
    Event,
    EventBuffer,
    EventSource,
    PollingEventSource,
    SourceConfigError,
    parse_time,
)
from stutters_mcp_core.events.limits import AllowList, PerKeyLimiter, TokenBucket, parse_networks

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def ev(type_: str = "a.b", source: str = "s1", **kw: Any) -> Event:
    e = Event(type=type_, **kw)
    e.source = source
    return e


# --- model --------------------------------------------------------------------------------


def test_parse_time_formats() -> None:
    assert parse_time("15m", now=NOW) == NOW - timedelta(minutes=15)
    assert parse_time("2h", now=NOW) == NOW - timedelta(hours=2)
    assert parse_time("1 day ago", now=NOW) == NOW - timedelta(days=1)
    assert parse_time("30s", now=NOW) == NOW - timedelta(seconds=30)
    assert parse_time("2026-10-15T09:30:00Z") == datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
    assert parse_time("2026-10-15T09:30:00") == datetime(2026, 10, 15, 9, 30, tzinfo=UTC)  # naive = UTC
    assert parse_time("2026-10-15T19:30:00+10:00") == datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
    assert parse_time("1760520600") == datetime.fromtimestamp(1760520600, UTC)
    assert parse_time(None) is None and parse_time("  ") is None
    assert parse_time(datetime(2026, 1, 1)) == datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="ISO 8601"):
        parse_time("yesterday-ish")


def test_event_normalisation_and_dict() -> None:
    e = Event(
        type=" Auth.FAIL ",
        keys={"MAC": " AA:BB ", "ip": None, "user": ""},  # type: ignore[dict-item]
        severity="Error",
        time=datetime(2026, 10, 15, 22, 0),
    )
    assert e.type == "auth.fail" and e.keys == {"mac": "AA:BB"} and e.severity == "error"
    assert e.time is not None and e.time.tzinfo is UTC
    e.received = e.time + timedelta(seconds=2.5)
    assert e.lag_seconds() == 2.5
    d = e.to_dict()
    assert d["type"] == "auth.fail" and "data" not in d and "id" not in d
    e.data = {"x": 1}
    assert e.to_dict(include_data=True)["data"] == {"x": 1}
    assert Event(type="").type == "event"


# --- buffer -------------------------------------------------------------------------------


def test_buffer_eviction_seq_and_stats() -> None:
    buf = EventBuffer(3)
    for i in range(5):
        buf.add(ev(source="a" if i < 4 else "b", summary=str(i)))
    assert [e.seq for e in buf] == [3, 4, 5] and buf.latest_seq == 5
    assert buf.stats("a").evicted == 2 and buf.stats("a").received == 4 and buf.stats("b").received == 1
    assert buf.summary()["evicted"] == 2 and buf.summary()["oldest_seq"] == 3
    assert buf.stats("nope").received == 0
    buf.clear()
    assert len(buf) == 0 and buf.add(ev()) is not None and buf.latest_seq == 6  # seq never reused
    with pytest.raises(ValueError):
        EventBuffer(0)


def test_buffer_capacity_one_and_empty_queries() -> None:
    buf = EventBuffer(1)
    assert buf.query().items == [] and buf.summary()["oldest_seq"] is None
    buf.add(ev(summary="x"))
    buf.add(ev(summary="y"))
    assert [e.summary for e in buf] == ["y"]


def test_buffer_dedupe_by_source_and_id_with_window() -> None:
    buf = EventBuffer(100, dedupe_window=2)
    assert buf.add(ev(id="1")) is not None
    assert buf.add(ev(id="1")) is None
    assert buf.add(ev(id="1", source="other")) is not None  # ids are per source
    assert buf.add(ev()) is not None and buf.add(ev()) is not None  # no id: never deduplicated
    buf.add(ev(id="2"))
    buf.add(ev(id="3"))  # window of 2: "1" forgotten
    assert buf.add(ev(id="1")) is not None
    assert buf.stats("s1").duplicates == 1


def test_buffer_filters() -> None:
    buf = EventBuffer(100)
    base = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
    buf.add(ev("auth.fail", "ise", keys={"mac": "aa:bb", "ip": "10.1.1.1"}, severity="error", time=base))
    buf.add(ev("auth.pass", "ise", keys={"mac": "AA:BB"}, severity="info", time=base + timedelta(minutes=5)))
    buf.add(ev("session.start", "ise", keys={"user": "bob"}, time=base + timedelta(minutes=10)))
    buf.add(ev("health.alert", "fmc", keys={"device": "ftd1"}, summary="Disk FULL", time=base))

    def seqs(**kw: Any) -> list[int]:
        return [e.seq for e in buf.matching(**kw)]

    assert seqs(source="ise") == [1, 2, 3]
    assert seqs(source="ISE, fmc") == [1, 2, 3, 4]
    assert seqs(type="auth.*") == [1, 2] and seqs(type="auth.") == [1, 2]
    assert seqs(type="auth.fail,health.alert") == [1, 4]
    assert seqs(type="auth") == []  # exact unless a prefix pattern
    assert seqs(key="mac=aa:bb") == [1, 2]  # case-insensitive value
    assert seqs(key="10.1.1.1") == [1] and seqs(key="ip=10.1.1.2") == []
    assert seqs(severity="error,info") == [1, 2]
    assert seqs(since=base + timedelta(minutes=5)) == [2, 3]
    assert seqs(until=base) == [1, 4]
    assert seqs(text="disk full") == [4]
    assert seqs(after_seq=2) == [3, 4]
    page = buf.query(limit=2, offset=1, source="ise")
    assert [e.seq for e in page.items] == [2, 1] and page.total == 3 and page.next_offset is None
    oldest_first = buf.query(limit=2, newest_first=False)
    assert [e.seq for e in oldest_first.items] == [1, 2] and oldest_first.next_offset == 2


def test_buffer_consumer_cursors() -> None:
    buf = EventBuffer(100)
    for i in range(5):
        buf.add(ev("x" if i % 2 else "y"))
    assert [e.seq for e in buf.read_new("a", limit=2)] == [1, 2]
    assert [e.seq for e in buf.read_new("a")] == [3, 4, 5]
    assert buf.read_new("a") == [] and buf.cursor("a") == 5
    assert [e.seq for e in buf.read_new("b", type="x")] == [2, 4]
    assert buf.cursor("b") == 5  # everything examined
    buf.add(ev("y"))
    assert buf.read_new("b", type="x") == [] and buf.cursor("b") == 6


# --- limits -------------------------------------------------------------------------------


def test_allow_list_and_limits() -> None:
    allow = AllowList("10.0.0.0/8, 198.18.128.11 ::1")
    assert allow.allows("10.2.3.4") and allow.allows("198.18.128.11") and allow.allows("::1")
    assert allow.allows("::ffff:10.1.1.1")  # IPv4-mapped
    assert not allow.allows("198.18.128.12") and not allow.allows(None) and not allow.allows("bogus")
    assert AllowList("").allows("1.2.3.4") and AllowList(None).open and str(AllowList()) == "any"
    with pytest.raises(ValueError, match="10.0.0.300"):
        parse_networks("10.0.0.300")
    bucket = TokenBucket(rate=1, burst=2)
    assert bucket.take() and bucket.take() and not bucket.take() and bucket.wait_time() > 0
    limiter = PerKeyLimiter(rate=1, burst=1, max_keys=2)
    assert limiter.allow("a") and not limiter.allow("a") and limiter.allow("b")
    limiter.allow("c")  # evicts "a"
    assert limiter.allow("a")
    assert PerKeyLimiter(None).allow("x")


# --- sources ------------------------------------------------------------------------------


class FlakySource(EventSource):
    def __init__(self, failures: int, **kw: Any) -> None:
        super().__init__("flaky", backoff_initial=0.01, backoff_max=0.04, **kw)
        self.failures = failures
        self.runs = 0

    async def run(self) -> None:
        self.runs += 1
        if self.runs <= self.failures:
            raise ConnectionError(f"refused #{self.runs}")
        self.set_connected()
        self.emit(Event(type="up", summary=f"run {self.runs}"))
        await asyncio.Event().wait()


async def wait_for(cond: Any, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


async def test_source_reconnects_with_backoff_then_stops() -> None:
    buf = EventBuffer(10)
    src = FlakySource(failures=3)
    src.start(buf)
    await wait_for(lambda: src.connected)
    st = src.status()
    assert st["state"] == "connected" and st["errors"] == 3 and st["restarts"] == 3
    assert st["last_error"] == "ConnectionError: refused #3" and st["events"] == 1
    assert st["received"] == 1 and "connected_since" in st
    assert [e.source for e in buf] == ["flaky"]
    await src.stop()
    assert src.status()["state"] == "stopped" and not src.running and not src.connected


async def test_source_backoff_grows_and_caps() -> None:
    src = FlakySource(failures=100)
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(d: float) -> None:
        delays.append(d)
        await real_sleep(0)

    src.start(EventBuffer(10))
    import stutters_mcp_core.events.source as source_mod

    orig = source_mod.asyncio.sleep
    source_mod.asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        await wait_for(lambda: len(delays) >= 5)
    finally:
        source_mod.asyncio.sleep = orig
        await src.stop()
    assert delays[1] > delays[0] * 1.3
    assert max(delays) <= 0.04 * 1.2 + 1e-9


async def test_config_error_fails_without_retry() -> None:
    class Bad(EventSource):
        async def run(self) -> None:
            raise SourceConfigError("no secret")

    src = Bad("bad", backoff_initial=0.01)
    src.start(EventBuffer(10))
    await wait_for(lambda: src.state == "failed")
    await asyncio.sleep(0.05)
    assert src.errors == 1 and src.restarts == 0 and "no secret" in (src.last_error or "")
    await src.stop()
    assert src.state == "failed"


async def test_disabled_source_never_starts() -> None:
    src = FlakySource(failures=0, enabled=False, disabled_reason="set X=true")
    src.start(EventBuffer(10))
    assert not src.running and src.status()["state"] == "disabled"
    assert src.status()["disabled_reason"] == "set X=true"
    await src.stop()
    assert src.state == "disabled"


def test_source_name_validation_and_emit_without_buffer() -> None:
    for bad in ("Bad", "has space", "", "a/b"):
        with pytest.raises(ValueError):
            PollingEventSource(bad, poll=None)
    src = PollingEventSource("ok-name_1", poll=None)
    with pytest.raises(RuntimeError):
        src.emit(Event(type="x"))
    with pytest.raises(ValueError):
        PollingEventSource("p", poll=None, interval=0)


async def test_polling_dedupes_sorts_and_passes_since() -> None:
    t0 = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
    calls: list[datetime | None] = []
    batches = [
        [Event("x", "b", time=t0 + timedelta(seconds=2), id="2"), Event("x", "a", time=t0, id="1")],
        [
            Event("x", "b", time=t0 + timedelta(seconds=2), id="2"),
            Event("x", "c", time=t0 + timedelta(seconds=3)),
        ],
        [Event("x", "c", time=t0 + timedelta(seconds=3))],  # same fingerprint, no id
    ]

    async def poll(since: datetime | None) -> list[Event]:
        calls.append(since)
        return batches[len(calls) - 1]

    buf = EventBuffer(100)
    src = PollingEventSource("p", poll, interval=60)
    src.buffer = buf
    assert await src.poll_once() == 2
    assert await src.poll_once() == 1
    assert await src.poll_once() == 0
    assert [e.summary for e in buf] == ["a", "b", "c"]  # sorted by time within a poll
    assert calls[0] is None and calls[1] is not None and calls[2] is not None and calls[2] >= calls[1]
    assert src.extra_status()["polls"] == 3


async def test_polling_emit_initial_false_and_error_recovery() -> None:
    n = 0

    async def poll(since: datetime | None) -> list[Event]:
        nonlocal n
        n += 1
        if n == 2:
            raise TimeoutError("upstream slow")
        return [Event("x", f"e{i}", id=str(i)) for i in range(n)]

    buf = EventBuffer(100)
    src = PollingEventSource("p", poll, interval=0.01, emit_initial=False, backoff_initial=0.01)
    src.start(buf)
    await wait_for(lambda: n >= 4)
    await src.stop()
    # poll 1 (history e0) swallowed; poll 2 failed; poll 3 brings e1, e2; poll 4 brings e3
    assert [e.summary for e in buf][:3] == ["e1", "e2", "e3"]
    assert src.errors == 1 and "upstream slow" in (src.last_error or "")


async def test_polling_subclass_override() -> None:
    class Sub(PollingEventSource):
        async def poll(self, since: datetime | None) -> list[Event]:
            return [Event("sub", "hello", id="1")]

    src = Sub("sub", interval=10)
    src.buffer = EventBuffer(10)
    assert await src.poll_once() == 1
    with pytest.raises(NotImplementedError):
        await PollingEventSource("p").poll(None)
