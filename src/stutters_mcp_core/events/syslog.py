"""Syslog receiver: UDP and/or TCP listeners, RFC 5424 and RFC 3164 (BSD, Cisco/FTD flavours) parsing.

    SyslogReceiver.from_settings(settings, to_event=ftd_event)   # MCP_SYSLOG_* settings

- TCP accepts both framings of RFC 6587: octet counting ("123 <34>1 ...") and newline-delimited,
  detected per message. TCP senders over the rate limit are slowed down (backpressure); UDP datagrams
  over the limit are dropped and counted.
- Sender allow-list (`MCP_SYSLOG_ALLOW_IPS`): datagrams from other addresses are dropped, TCP
  connections are closed at once. Behind the SSH-tunnelled CML relay every TCP sender appears as the
  tunnel endpoint (127.0.0.1), so allow-list that.
- `to_event(msg) -> Event | None` turns a parsed message into an event (None ignores it). The default
  gives type "syslog" (or "syslog.<cisco mnemonic>", e.g. "syslog.430002"), keys host and sender.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from stutters_mcp_core.events.limits import AllowList, PerKeyLimiter
from stutters_mcp_core.events.model import Event, utcnow
from stutters_mcp_core.events.source import EventSource

if TYPE_CHECKING:
    from stutters_mcp_core.settings import BaseServerSettings

log = logging.getLogger(__name__)

SEVERITY_NAMES = ("emergency", "alert", "critical", "error", "warning", "notice", "info", "debug")
# Map syslog severities onto the Event severity vocabulary.
_EVENT_SEVERITY = ("critical", "critical", "critical", "error", "warning", "notice", "info", "debug")

_PRI_RE = re.compile(r"^<(\d{1,3})>")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1
    )
}
# "Oct 15 10:00:00", "Oct  5 10:00:00.123", "Oct 15 2026 10:00:00", optional leading '*'/'.' (IOS) and
# a trailing time zone and/or colon.
_TS3164_RE = re.compile(
    r"^[*.]?(?P<mon>[A-Za-z]{3}) +(?P<day>\d{1,2})(?: (?P<year>\d{4}))? "
    r"(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})(?:\.(?P<frac>\d{1,6}))?"
    r"(?: (?P<tz>UTC|GMT|[A-Z]{3,4}))?:? "
)
_TAG_RE = re.compile(r"^(?P<tag>[^\s:\[\]]{1,48})(?:\[(?P<pid>[^\]]{1,32})\])?: ?")
_CISCO_RE = re.compile(r"%(?P<fac>[A-Z][A-Z0-9_]*)-(?P<sev>[0-7])-(?P<mn>[A-Z0-9_]+)")


@dataclass
class SyslogMessage:
    raw: str
    message: str
    facility: int | None = None
    severity: int | None = None
    timestamp: datetime | None = None
    hostname: str | None = None
    app_name: str | None = None
    procid: str | None = None
    msgid: str | None = None
    structured_data: dict[str, dict[str, str]] = field(default_factory=dict)
    format: str = "unknown"  # rfc5424 | rfc3164 | unknown
    cisco_facility: str | None = None  # "FTD" from "%FTD-6-430002"
    cisco_mnemonic: str | None = None  # "430002"
    peer: str = ""
    transport: str = ""

    @property
    def severity_name(self) -> str | None:
        return SEVERITY_NAMES[self.severity] if self.severity is not None else None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat() if self.timestamp else None
        d["severity_name"] = self.severity_name
        return {k: v for k, v in d.items() if v not in (None, {}, "")}


def _nil(v: str) -> str | None:
    return None if v == "-" else v


def _parse_sd(text: str) -> tuple[dict[str, dict[str, str]], str]:
    """Parse RFC 5424 STRUCTURED-DATA at the start of `text`; returns (data, rest)."""
    if text.startswith("-"):
        return {}, text[1:]
    out: dict[str, dict[str, str]] = {}
    i, n = 0, len(text)
    while i < n and text[i] == "[":
        j = i + 1
        while j < n and text[j] not in " ]":
            j += 1
        sd_id = text[i + 1 : j]
        params: dict[str, str] = {}
        while j < n and text[j] == " ":
            j += 1
            eq = text.find("=", j)
            if eq < 0 or eq + 1 >= n or text[eq + 1] != '"':
                raise ValueError("bad SD-PARAM")
            name = text[j:eq]
            k = eq + 2
            value: list[str] = []
            while k < n and text[k] != '"':
                if text[k] == "\\" and k + 1 < n and text[k + 1] in '"\\]':
                    k += 1
                value.append(text[k])
                k += 1
            if k >= n:
                raise ValueError("unterminated SD-PARAM value")
            params[name] = "".join(value)
            j = k + 1
        if j >= n or text[j] != "]":
            raise ValueError("unterminated SD-ELEMENT")
        out[sd_id] = params
        i = j + 1
    if not out:
        raise ValueError("bad STRUCTURED-DATA")
    return out, text[i:]


def _parse_5424(raw: str, pri: int, rest: str) -> SyslogMessage | None:
    parts = rest.split(" ", 6)
    if len(parts) < 7 or not parts[0].isdigit():
        return None
    version, ts, host, app, procid, msgid, tail = parts
    try:
        sd, msg = _parse_sd(tail)
    except ValueError:
        return None
    timestamp = None
    if ts != "-":
        try:
            timestamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            timestamp = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp
            timestamp = timestamp.astimezone(UTC)
        except ValueError:
            return None
    msg = msg[1:] if msg.startswith(" ") else msg
    msg = msg.removeprefix("\ufeff")
    return SyslogMessage(
        raw=raw,
        message=msg,
        facility=pri >> 3,
        severity=pri & 7,
        timestamp=timestamp,
        hostname=_nil(host),
        app_name=_nil(app),
        procid=_nil(procid),
        msgid=_nil(msgid),
        structured_data=sd,
        format="rfc5424",
    )


def _ts_3164(m: re.Match[str], now: datetime) -> datetime | None:
    month = _MONTHS.get(m["mon"].lower())
    if month is None:
        return None
    year = int(m["year"]) if m["year"] else now.year
    frac = int((m["frac"] or "0").ljust(6, "0"))
    try:
        ts = datetime(year, month, int(m["day"]), int(m["h"]), int(m["m"]), int(m["s"]), frac, tzinfo=UTC)
    except ValueError:
        return None
    if not m["year"] and ts > now + timedelta(days=1):  # December message read in January
        ts = ts.replace(year=year - 1)
    return ts


def parse_syslog(raw: str | bytes, *, now: datetime | None = None) -> SyslogMessage:
    """Parse one syslog message. Never raises: anything unrecognised comes back with
    format="unknown" and the whole line as `message`. RFC 3164 timestamps carry no zone or year;
    they are taken as UTC in the current year."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    raw = raw.rstrip("\r\n\x00").removeprefix("\ufeff")
    now = now or utcnow()
    m = _PRI_RE.match(raw)
    if not m or int(m.group(1)) > 191:
        return _with_cisco(SyslogMessage(raw=raw, message=raw.strip()))
    pri = int(m.group(1))
    rest = raw[m.end() :]
    if rest[:1].isdigit() and (msg := _parse_5424(raw, pri, rest)) is not None:
        return _with_cisco(msg)
    out = SyslogMessage(raw=raw, message=rest.strip(), facility=pri >> 3, severity=pri & 7, format="rfc3164")
    # Cisco IOS sequence number: "<189>123: *Oct 15 ..."
    seq = re.match(r"^\d+: ", rest)
    if seq:
        rest = rest[seq.end() :]
    ts = _TS3164_RE.match(rest)
    if ts:
        out.timestamp = _ts_3164(ts, now)
        rest = rest[ts.end() :]
        # Hostname, unless the next token is already the tag ("TAG: msg" / "%FAC-..." / ": msg").
        if rest.startswith(": "):
            rest = rest[2:]
        else:
            first = rest.split(" ", 1)[0]
            if first and not first.endswith(":") and not first.startswith("%") and " " in rest:
                out.hostname = first
                rest = rest[len(first) + 1 :]
                if rest.startswith(": "):  # FTD: "<166>Oct 15 2026 10:00:00 ftd1 : %FTD-6-..."
                    rest = rest[2:]
    if not rest.startswith("%"):
        tag = _TAG_RE.match(rest)
        if tag:
            out.app_name, out.procid = tag["tag"], tag["pid"]
            rest = rest[tag.end() :]
    out.message = rest.strip()
    return _with_cisco(out)


def _with_cisco(msg: SyslogMessage) -> SyslogMessage:
    m = _CISCO_RE.search(msg.message[:200])
    if m:
        msg.cisco_facility, msg.cisco_mnemonic = m["fac"], m["mn"]
        if msg.severity is None:
            msg.severity = int(m["sev"])
        if msg.msgid is None:
            msg.msgid = m["mn"]
    return msg


def default_syslog_event(msg: SyslogMessage) -> Event | None:
    """Generic mapping: type "syslog" or "syslog.<cisco mnemonic>", keys host/sender."""
    kind = f"syslog.{msg.cisco_mnemonic.lower()}" if msg.cisco_mnemonic else "syslog"
    return Event(
        type=kind,
        summary=msg.message[:300],
        time=msg.timestamp,
        keys={"host": msg.hostname or "", "sender": msg.peer},
        severity=_EVENT_SEVERITY[msg.severity] if msg.severity is not None else None,
        data=msg.to_dict(),
    )


ToEvent = Callable[[SyslogMessage], Event | None]


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, receiver: SyslogReceiver) -> None:
        self.receiver = receiver

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        self.receiver._handle_datagram(data, str(addr[0]))

    def error_received(self, exc: Exception) -> None:
        log.debug("syslog UDP error: %s", exc)


class SyslogReceiver(EventSource):
    """UDP and/or TCP syslog listener feeding the event buffer (an EventSource of kind "push").

    Port 0 binds an ephemeral port (tests); `udp_address` / `tcp_address` give the bound addresses.
    A port of None disables that transport."""

    kind = "push"

    def __init__(
        self,
        name: str = "syslog",
        *,
        host: str = "127.0.0.1",
        udp_port: int | None = 5514,
        tcp_port: int | None = 5514,
        allow: str | list[str] | None = None,
        rate_limit: float | None = 500.0,
        max_message: int = 16_384,
        max_connections: int = 64,
        idle_timeout: float = 600.0,
        to_event: ToEvent | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("description", "Syslog receiver")
        super().__init__(name, **kwargs)
        if udp_port is None and tcp_port is None:
            raise ValueError("SyslogReceiver needs a UDP or TCP port")
        self.host = host
        self.udp_port = udp_port
        self.tcp_port = tcp_port
        self.allow = AllowList(allow)
        self.limiter = PerKeyLimiter(rate_limit)
        self.max_message = max_message
        self.max_connections = max_connections
        self.idle_timeout = idle_timeout
        self.to_event = to_event or default_syslog_event
        self.udp_address: tuple[str, int] | None = None
        self.tcp_address: tuple[str, int] | None = None
        self.messages = 0
        self._connections = 0
        self._writers: set[asyncio.StreamWriter] = set()

    @classmethod
    def from_settings(
        cls,
        settings: BaseServerSettings,
        *,
        name: str = "syslog",
        to_event: ToEvent | None = None,
        **kwargs: Any,
    ) -> SyslogReceiver:
        """A receiver configured by MCP_SYSLOG_*; disabled (but listed) unless MCP_SYSLOG_ENABLED=true."""
        kwargs.setdefault("enabled", settings.mcp_syslog_enabled)
        kwargs.setdefault(
            "disabled_reason", "set MCP_SYSLOG_ENABLED=true (and the MCP_SYSLOG_* port/allow-list)"
        )
        return cls(
            name,
            host=settings.mcp_syslog_host,
            udp_port=settings.mcp_syslog_udp_port,
            tcp_port=settings.mcp_syslog_tcp_port,
            allow=settings.mcp_syslog_allow_ips,
            rate_limit=settings.mcp_syslog_rate_limit,
            max_message=settings.mcp_syslog_max_message,
            max_connections=settings.mcp_syslog_max_connections,
            to_event=to_event,
            **kwargs,
        )

    # --- message handling ----------------------------------------------------------------

    def handle_message(self, raw: bytes | str, peer: str, transport: str) -> Event | None:
        """Parse, convert and store one message (also usable directly in tests)."""
        msg = parse_syslog(raw)
        msg.peer, msg.transport = peer, transport
        self.messages += 1
        try:
            event = self.to_event(msg)
        except Exception:  # noqa: BLE001 - a bad message must not stop the listener
            log.warning("syslog to_event failed for a message from %s", peer, exc_info=True)
            self.drop("parse_error")
            return None
        if event is None:
            self.drop("filtered")
            return None
        return self.emit(event)

    def _handle_datagram(self, data: bytes, peer: str) -> None:
        if not self.allow.allows(peer):
            self.drop("sender_not_allowed")
            return
        if len(data) > self.max_message:
            self.drop("oversize")
            return
        if not self.limiter.allow(peer):
            self.drop("rate_limited")
            return
        text = data.decode("utf-8", errors="replace").strip("\r\n\x00 ")
        if text:
            self.handle_message(text, peer, "udp")

    async def _handle_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername")
        peer = str(peername[0]) if peername else ""
        if not self.allow.allows(peer):
            self.drop("sender_not_allowed")
            writer.close()
            return
        if self._connections >= self.max_connections:
            self.drop("too_many_connections")
            writer.close()
            return
        self._connections += 1
        self._writers.add(writer)
        try:
            await self._read_frames(reader, peer)
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._connections -= 1
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _read_frames(self, reader: asyncio.StreamReader, peer: str) -> None:
        buf = bytearray()
        eof = False
        while True:
            # Drop separators between frames.
            while bytes(buf[:1]) in (b"\n", b"\r", b"\x00", b" "):
                del buf[:1]
            frame: bytes | None = None
            if buf[:1].isdigit():
                space = buf.find(b" ", 0, 12)
                if space > 0 and buf[:space].isdigit():
                    length = int(buf[:space])
                    if length > self.max_message:
                        self.drop("oversize")
                        return  # can't resynchronise an octet-counted stream; close it
                    if len(buf) >= space + 1 + length:
                        frame = bytes(buf[space + 1 : space + 1 + length])
                        del buf[: space + 1 + length]
                elif space < 0 and len(buf) > 12:
                    self.drop("bad_framing")
                    return
            elif buf:
                nl = buf.find(b"\n")
                if nl >= 0:
                    frame = bytes(buf[:nl])
                    del buf[: nl + 1]
                elif len(buf) > self.max_message:
                    self.drop("oversize")
                    return
                elif eof:
                    frame = bytes(buf)
                    buf.clear()
            if frame is not None:
                await self._pace(peer)
                text = frame.decode("utf-8", errors="replace").strip("\r\n\x00 ")
                if text:
                    self.handle_message(text, peer, "tcp")
                continue
            if eof:
                if buf:
                    self.drop("truncated")
                return
            chunk = await asyncio.wait_for(reader.read(65_536), self.idle_timeout)
            if not chunk:
                eof = True
            buf.extend(chunk)

    async def _pace(self, peer: str) -> None:
        """TCP backpressure: wait for the sender's rate-limit token instead of dropping."""
        bucket = self.limiter.bucket(peer)
        if bucket is None:
            return
        while not bucket.take():  # noqa: ASYNC110 - waiting on a clock, not on another task
            await asyncio.sleep(bucket.wait_time())

    # --- lifecycle -----------------------------------------------------------------------

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        udp: asyncio.DatagramTransport | None = None
        tcp: asyncio.Server | None = None
        try:
            if self.udp_port is not None:
                udp, _ = await loop.create_datagram_endpoint(
                    lambda: _UdpProtocol(self), local_addr=(self.host, self.udp_port)
                )
                sock = udp.get_extra_info("sockname")
                self.udp_address = (str(sock[0]), int(sock[1]))
            if self.tcp_port is not None:
                tcp = await asyncio.start_server(
                    self._handle_tcp, self.host, self.tcp_port, limit=self.max_message + 16
                )
                sock = tcp.sockets[0].getsockname()
                self.tcp_address = (str(sock[0]), int(sock[1]))
            if self.allow.open:
                log.warning("syslog receiver %s accepts any sender; set MCP_SYSLOG_ALLOW_IPS", self.name)
            log.info(
                "syslog receiver %s listening: udp=%s tcp=%s", self.name, self.udp_address, self.tcp_address
            )
            self.set_connected()
            await asyncio.Event().wait()  # until cancelled
        finally:
            if udp is not None:
                udp.close()
            if tcp is not None:
                tcp.close()
                for w in list(self._writers):
                    w.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(tcp.wait_closed(), 2)

    def extra_status(self) -> dict[str, Any]:
        listen = []
        if self.udp_port is not None:
            listen.append(f"udp://{self.host}:{self.udp_address[1] if self.udp_address else self.udp_port}")
        if self.tcp_port is not None:
            listen.append(f"tcp://{self.host}:{self.tcp_address[1] if self.tcp_address else self.tcp_port}")
        return {
            "listen": listen,
            "allowed_senders": str(self.allow),
            "messages": self.messages,
            "tcp_connections": self._connections,
        }
