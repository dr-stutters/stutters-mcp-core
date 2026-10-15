import asyncio
import socket
from datetime import UTC, datetime
from typing import Any

import pytest

from stutters_mcp_core.events import Event, EventBuffer, SyslogMessage, SyslogReceiver, parse_syslog
from stutters_mcp_core.events.syslog import default_syslog_event

from .test_events import wait_for

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


# --- parsing ------------------------------------------------------------------------------


def test_rfc5424_full() -> None:
    raw = (
        "<165>1 2026-10-15T09:30:01.123+10:00 ise01 CISE_RADIUS 1234 5400 "
        '[exampleSDID@32473 iut="3" eventSource="App" eventID="1011"][meta x="a\\"b\\]c"] '
        "﻿An application event log entry"
    )
    m = parse_syslog(raw)
    assert m.format == "rfc5424" and m.facility == 20 and m.severity == 5 and m.severity_name == "notice"
    assert m.timestamp == datetime(2026, 10, 14, 23, 30, 1, 123000, tzinfo=UTC)
    assert (m.hostname, m.app_name, m.procid, m.msgid) == ("ise01", "CISE_RADIUS", "1234", "5400")
    assert m.structured_data["exampleSDID@32473"]["eventID"] == "1011"
    assert m.structured_data["meta"]["x"] == 'a"b]c'
    assert m.message == "An application event log entry"


def test_rfc5424_nil_values_and_no_message() -> None:
    m = parse_syslog("<34>1 - - - - - -")
    assert m.format == "rfc5424" and m.timestamp is None and m.hostname is None
    assert m.structured_data == {} and m.message == ""
    m = parse_syslog(b"<34>1 2026-10-15T09:30:01Z host app - ID47 - hello world\n")
    assert m.msgid == "ID47" and m.message == "hello world" and m.procid is None


def test_rfc5424_malformed_falls_back() -> None:
    m = parse_syslog("<34>1 2026-10-15T09:30:01Z host app - ID47 [broken sd")
    assert m.format == "rfc3164" and m.severity == 2  # still keeps PRI


def test_rfc3164_classic() -> None:
    m = parse_syslog("<34>Oct 11 22:14:15 mymachine su[230]: 'su root' failed for lonvick", now=NOW)
    assert m.format == "rfc3164" and m.facility == 4 and m.severity == 2
    assert m.timestamp == datetime(2026, 10, 11, 22, 14, 15, tzinfo=UTC)
    assert (m.hostname, m.app_name, m.procid) == ("mymachine", "su", "230")
    assert m.message == "'su root' failed for lonvick"


def test_rfc3164_space_padded_day_and_year_rollover() -> None:
    m = parse_syslog("<13>Dec  5 23:59:59 host app: x", now=datetime(2027, 1, 2, tzinfo=UTC))
    assert m.timestamp == datetime(2026, 12, 5, 23, 59, 59, tzinfo=UTC)


def test_cisco_ios_format() -> None:
    m = parse_syslog(
        "<189>123: *Oct 15 10:00:00.123: %SYS-5-CONFIG_I: Configured from console by admin on vty0", now=NOW
    )
    assert m.timestamp == datetime(2026, 10, 15, 10, 0, 0, 123000, tzinfo=UTC)
    assert m.hostname is None and m.cisco_facility == "SYS" and m.cisco_mnemonic == "CONFIG_I"
    assert m.msgid == "CONFIG_I" and m.message.startswith("%SYS-5-CONFIG_I:")


def test_ftd_format() -> None:
    raw = (
        "<166>Oct 15 2026 10:00:00 ftd1 : %FTD-6-430002: EventPriority: Low, DeviceUUID: x, "
        "SrcIP: 10.1.1.10, DstIP: 8.8.8.8"
    )
    m = parse_syslog(raw, now=NOW)
    assert m.hostname == "ftd1" and m.timestamp == datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
    assert m.cisco_facility == "FTD" and m.cisco_mnemonic == "430002" and m.severity == 6
    assert m.message.startswith("%FTD-6-430002: EventPriority")
    e = default_syslog_event(m)
    assert e is not None and e.type == "syslog.430002" and e.severity == "info" and e.keys["host"] == "ftd1"


def test_asa_style_with_hostname_and_no_pri() -> None:
    m = parse_syslog("%ASA-4-106023: Deny tcp src outside:1.2.3.4/1234 dst inside:10.1.1.1/23")
    assert m.format == "unknown" and m.cisco_mnemonic == "106023" and m.severity == 4
    m = parse_syslog("<999>garbage")
    assert m.format == "unknown" and m.message == "<999>garbage"
    m = parse_syslog("<14>just a message without timestamp")
    assert m.format == "rfc3164" and m.message == "just a message without timestamp" and m.hostname is None


def test_default_event_mapping() -> None:
    m = parse_syslog("<11>1 2026-10-15T09:30:01Z sw1 app - - - link down")
    m.peer = "10.0.0.5"
    e = default_syslog_event(m)
    assert e is not None and e.type == "syslog" and e.severity == "error"
    assert e.keys == {"host": "sw1", "sender": "10.0.0.5"} and e.data["app_name"] == "app"


# --- receiver round trips -----------------------------------------------------------------


async def closed_by_peer(reader: asyncio.StreamReader) -> bool:
    try:
        return await asyncio.wait_for(reader.read(), 2) == b""
    except ConnectionResetError:
        return True


async def start_receiver(**kw: Any) -> tuple[SyslogReceiver, EventBuffer]:
    buf = EventBuffer(1000)
    kw.setdefault("udp_port", 0)
    kw.setdefault("tcp_port", 0)
    rcv = SyslogReceiver("syslog", host="127.0.0.1", **kw)
    rcv.start(buf)
    await wait_for(lambda: rcv.connected)
    return rcv, buf


async def test_udp_round_trip_and_status() -> None:
    rcv, buf = await start_receiver()
    try:
        assert rcv.udp_address is not None
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(b"<34>Oct 11 22:14:15 mymachine su: hello over udp\n", rcv.udp_address)
        sock.sendto(b"<13>1 2026-10-15T09:30:01Z h a - - - second", rcv.udp_address)
        sock.close()
        await wait_for(lambda: len(buf) == 2)
        events = list(buf)
        assert events[0].summary == "hello over udp" and events[0].keys["sender"] == "127.0.0.1"
        assert events[0].data["transport"] == "udp" and events[1].summary == "second"
        st = rcv.status()
        assert st["kind"] == "push" and st["state"] == "connected" and st["messages"] == 2
        assert st["listen"][0].startswith("udp://127.0.0.1:")
    finally:
        await rcv.stop()


async def test_tcp_octet_counted_and_newline_framing_with_partial_writes() -> None:
    rcv, buf = await start_receiver(udp_port=None)
    try:
        assert rcv.tcp_address is not None
        reader, writer = await asyncio.open_connection(*rcv.tcp_address)
        msg1 = b"<34>1 2026-10-15T09:30:01Z h app - - - octet one"
        msg2 = b"<34>Oct 11 22:14:15 host app: newline two"
        msg3 = b"<34>1 2026-10-15T09:30:02Z h app - - - octet\nwith newline inside"
        stream = f"{len(msg1)} ".encode() + msg1 + msg2 + b"\n" + f"{len(msg3)} ".encode() + msg3
        for i in range(0, len(stream), 7):  # dribble the bytes in small pieces
            writer.write(stream[i : i + 7])
            await writer.drain()
            await asyncio.sleep(0)
        writer.write(b"<34>Oct 11 22:14:16 host app: last line without newline")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        await wait_for(lambda: len(buf) == 4)
        assert [e.summary for e in buf] == [
            "octet one",
            "newline two",
            "octet\nwith newline inside",
            "last line without newline",
        ]
        assert all(e.data["transport"] == "tcp" for e in buf)
    finally:
        await rcv.stop()


async def test_sender_allow_list_udp_and_tcp() -> None:
    rcv, buf = await start_receiver(allow="10.9.9.9")
    try:
        assert rcv.udp_address and rcv.tcp_address
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(b"<34>Oct 11 22:14:15 h a: denied", rcv.udp_address)
        sock.close()
        reader, writer = await asyncio.open_connection(*rcv.tcp_address)
        writer.write(b"<34>Oct 11 22:14:15 h a: denied tcp\n")
        await writer.drain()
        assert await closed_by_peer(reader)  # closed by the receiver
        writer.close()
        await wait_for(lambda: rcv.drops.get("sender_not_allowed", 0) == 2)
        assert len(buf) == 0
    finally:
        await rcv.stop()


async def test_udp_rate_limit_and_oversize_drop() -> None:
    rcv, buf = await start_receiver(rate_limit=1.0, max_message=600)
    try:
        assert rcv.udp_address
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for i in range(5):
            sock.sendto(f"<34>Oct 11 22:14:15 h a: m{i}".encode(), rcv.udp_address)
        sock.sendto(b"<34>" + b"x" * 700, rcv.udp_address)
        sock.close()
        await wait_for(lambda: sum(rcv.drops.values()) == 4)
        assert len(buf) == 2  # burst of 2 at rate 1/s
        assert rcv.drops == {"rate_limited": 3, "oversize": 1}
    finally:
        await rcv.stop()


async def test_tcp_oversize_octet_count_closes_and_max_connections() -> None:
    rcv, buf = await start_receiver(udp_port=None, max_message=600, max_connections=1)
    try:
        assert rcv.tcp_address
        r1, w1 = await asyncio.open_connection(*rcv.tcp_address)
        await wait_for(lambda: rcv.status()["tcp_connections"] == 1)
        r2, w2 = await asyncio.open_connection(*rcv.tcp_address)
        assert await closed_by_peer(r2)
        w2.close()
        w1.write(b"99999 <34>too big")
        await w1.drain()
        assert await closed_by_peer(r1)
        w1.close()
        await wait_for(lambda: rcv.drops.get("oversize") == 1)
        assert rcv.drops["too_many_connections"] == 1
        # a newline-framed line beyond the limit is dropped too
        r3, w3 = await asyncio.open_connection(*rcv.tcp_address)
        w3.write(b"<34>" + b"y" * 1000)
        await w3.drain()
        assert await closed_by_peer(r3)
        w3.close()
        assert rcv.drops["oversize"] == 2 and len(buf) == 0
    finally:
        await rcv.stop()


async def test_custom_to_event_filter_and_errors() -> None:
    def to_event(msg: SyslogMessage) -> Event | None:
        if "ignore" in msg.message:
            return None
        if "boom" in msg.message:
            raise RuntimeError("bad parser")
        return Event(type="fw.deny", summary=msg.message, keys={"ip": "1.2.3.4"})

    buf = EventBuffer(10)
    rcv = SyslogReceiver("fw", udp_port=0, tcp_port=None, to_event=to_event)
    rcv.buffer = buf
    rcv.handle_message("<34>Oct 11 22:14:15 h a: keep", "1.1.1.1", "udp")
    rcv.handle_message("<34>Oct 11 22:14:15 h a: ignore me", "1.1.1.1", "udp")
    rcv.handle_message("<34>Oct 11 22:14:15 h a: boom", "1.1.1.1", "udp")
    assert [e.type for e in buf] == ["fw.deny"] and rcv.drops == {"filtered": 1, "parse_error": 1}


async def test_bind_failure_is_retried_and_reported() -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    port = holder.getsockname()[1]
    rcv = SyslogReceiver("syslog", udp_port=None, tcp_port=port, backoff_initial=0.01, backoff_max=0.02)
    rcv.start(EventBuffer(10))
    try:
        await wait_for(lambda: rcv.errors >= 2)
        assert rcv.state in ("reconnecting", "connecting") and "OSError" in (rcv.last_error or "")
        holder.close()
        await wait_for(lambda: rcv.connected)
    finally:
        holder.close()
        await rcv.stop()


def test_needs_a_port() -> None:
    with pytest.raises(ValueError):
        SyslogReceiver("s", udp_port=None, tcp_port=None)
