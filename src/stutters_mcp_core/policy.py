"""Safety rails for production (roadmap G7): an opt-in policy layer over the write tiers.

Every rail is off by default; with none configured, behaviour is exactly as before. Configure with:

    MCP_DRY_RUN=true                     previews work, confirms are refused
    MCP_CHANGE_WINDOW="Mon-Fri 22:00-02:00 Australia/Sydney"
                                         confirms outside the window are refused (previews still work)
    MCP_DENY_TOOLS="*_delete_*,@dangerous"
    MCP_ALLOW_TOOLS="ise_*"              denied / not-allowed tools are not registered at all
    MCP_MAX_BLAST_RADIUS=20              a Plan whose blast_radius is larger is blocked
    MCP_BLAST_RADIUS_OVERRIDES="fmc_deploy=5,cml_*=100"
    MCP_TWO_PERSON_TOOLS="@dangerous,fmc_deploy"
                                         the confirm token must be redeemed by a different caller

Tool selectors (deny / allow / two-person) are comma-separated: shell-style globs over tool names
(`*_delete_*`) and tier selectors `@read`, `@write`, `@dangerous`. `<prefix>_status` is always
registered.

Change windows, `;`-separated (any open window allows the change):
    "<days> <HH:MM>-<HH:MM> [Area/City]"   e.g. "Mon-Fri 22:00-02:00 Australia/Sydney", "Sat,Sun 00:00-00:00"
        days: Mon..Sun, ranges and lists, or `daily` / `*`. The window opens on the listed day; an end at or
        before the start runs past midnight (equal = 24 hours). Time zone defaults to UTC.
    "<cron: m h dom mon dow> <duration> [Area/City]"   e.g. "0 22 * * 1-5 4h Australia/Sydney"
        the window opens at each cron match and lasts <duration> (30m, 4h, 1h30m, 2d; at most 7d).
Times are wall-clock in the window's zone (a DST change inside a window shifts its end by an hour).
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from stutters_mcp_core.settings import BaseServerSettings

TIERS = ("read", "write", "dangerous")

# --- tool selectors -----------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSelector:
    """Comma-separated globs over tool names plus `@<tier>` entries."""

    patterns: tuple[str, ...] = ()
    tiers: frozenset[str] = frozenset()

    @classmethod
    def parse(cls, spec: str | None, *, setting: str) -> ToolSelector:
        patterns: list[str] = []
        tiers: set[str] = set()
        for raw in re.split(r"[,\n]", spec or ""):
            entry = raw.strip()
            if not entry:
                continue
            if entry.startswith("@"):
                tier = entry[1:].lower()
                if tier not in TIERS:
                    raise ValueError(
                        f"{setting}: unknown tier selector {entry!r} (use @read, @write, @dangerous)"
                    )
                tiers.add(tier)
            elif re.fullmatch(r"[A-Za-z0-9_*?\[\]!-]+", entry):
                patterns.append(entry)
            else:
                raise ValueError(f"{setting}: {entry!r} is not a tool name or glob")
        return cls(tuple(patterns), frozenset(tiers))

    def __bool__(self) -> bool:
        return bool(self.patterns or self.tiers)

    def matches(self, tool: str, tier: str | None) -> bool:
        if tier is not None and tier in self.tiers:
            return True
        return any(fnmatch.fnmatchcase(tool, p) for p in self.patterns)

    def describe(self) -> list[str]:
        return [*self.patterns, *sorted("@" + t for t in self.tiers)]


# --- change windows -----------------------------------------------------------------------

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_CRON_DOW = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec"
_CRON_MON = {m: i for i, m in enumerate(_MONTHS.split(), start=1)}  # noqa: SIM905
_MAX_DURATION = timedelta(days=7)
_LOOKAHEAD = timedelta(days=31)


def _zone(name: str | None) -> tzinfo:
    if not name:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {name!r} (use an IANA name such as Australia/Sydney)") from None


def _duration(text: str) -> timedelta:
    m = re.fullmatch(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?", text.lower())
    if not m or not any(m.groups()):
        raise ValueError(f"bad duration {text!r} (e.g. 30m, 4h, 1h30m, 2d)")
    d, h, mi = (int(g) if g else 0 for g in m.groups())
    value = timedelta(days=d, hours=h, minutes=mi)
    if not timedelta(0) < value <= _MAX_DURATION:
        raise ValueError(f"duration {text!r} must be more than 0 and at most 7d")
    return value


def _day_set(text: str) -> frozenset[int]:
    if text.lower() in ("*", "daily", "every"):
        return frozenset(range(7))
    out: set[int] = set()
    for part in text.lower().split(","):
        a, _, b = part.partition("-")
        try:
            start = _DAYS[a[:3]]
            end = _DAYS[b[:3]] if b else start
        except KeyError:
            raise ValueError(f"bad day {part!r} (use Mon..Sun, ranges like Mon-Fri, or daily)") from None
        i = start
        while True:
            out.add(i)
            if i == end:
                break
            i = (i + 1) % 7
    return frozenset(out)


def _hhmm(text: str) -> time:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if (
        not m
        or int(m.group(1)) > 24
        or int(m.group(2)) > 59
        or (int(m.group(1)) == 24 and m.group(2) != "00")
    ):
        raise ValueError(f"bad time {text!r} (HH:MM, 24-hour)")
    hour = int(m.group(1))
    return time(0 if hour == 24 else hour, int(m.group(2)))


def _cron_field(text: str, lo: int, hi: int, names: dict[str, int] | None = None) -> frozenset[int]:
    out: set[int] = set()
    for part in text.lower().split(","):
        rng, _, step_s = part.partition("/")
        step = int(step_s) if step_s.isdigit() and int(step_s) > 0 else None
        if step_s and step is None:
            raise ValueError(f"bad cron step {part!r}")

        def num(s: str) -> int:
            if names and s in names:
                return names[s]
            if not s.isdigit():
                raise ValueError(f"bad cron value {s!r}")
            return int(s)

        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            x, y = rng.split("-", 1)
            a, b = num(x), num(y)
        else:
            a = num(rng)
            b = hi if step else a
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise ValueError(f"cron value {part!r} out of range {lo}-{hi}")
        out.update(range(a, b + 1, step or 1))
    return frozenset(out)


@dataclass(frozen=True)
class _Window:
    text: str
    zone: tzinfo
    duration: timedelta
    # weekly form
    days: frozenset[int] = frozenset()
    start: time | None = None
    # cron form
    minutes: frozenset[int] = frozenset()
    hours: frozenset[int] = frozenset()
    doms: frozenset[int] = frozenset()
    months: frozenset[int] = frozenset()
    dows: frozenset[int] = frozenset()
    dom_any: bool = True
    dow_any: bool = True

    def _cron_match(self, t: datetime) -> bool:
        if t.minute not in self.minutes or t.hour not in self.hours or t.month not in self.months:
            return False
        dom_ok = t.day in self.doms
        dow_ok = (t.weekday() + 1) % 7 in self.dows
        if self.dom_any or self.dow_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok  # cron: both restricted means either

    def _starts(self, now: datetime, back: timedelta, ahead: timedelta) -> Iterable[datetime]:
        """Window start times in [now-back, now+ahead], in ascending order, in the window's zone."""
        local = now.astimezone(self.zone)
        if self.start is not None:
            first = (local - back).date()
            last = (local + ahead).date()
            day = first
            while day <= last:
                if day.weekday() in self.days:
                    yield datetime.combine(day, self.start, tzinfo=self.zone)
                day += timedelta(days=1)
            return
        t = (local - back).replace(second=0, microsecond=0)
        end = local + ahead
        while t <= end:
            if t.hour not in self.hours:
                t = (t + timedelta(hours=1)).replace(minute=0)
                continue
            if self._cron_match(t):
                yield t
            t += timedelta(minutes=1)

    def open_at(self, now: datetime) -> datetime | None:
        """End of the window containing `now`, or None."""
        local = now.astimezone(self.zone)
        for s in self._starts(now, self.duration, timedelta(0)):
            if s <= local < s + self.duration:
                return s + self.duration
        return None

    def next_open(self, now: datetime) -> datetime | None:
        local = now.astimezone(self.zone)
        for s in self._starts(now, timedelta(0), _LOOKAHEAD):
            if s > local:
                return s
        return None


def _parse_window(text: str) -> _Window:
    tokens = text.split()
    if len(tokens) >= 6 and re.fullmatch(r"[0-9*,/-]+", tokens[0]):
        m, h, dom, mon, dow = tokens[:5]
        duration = _duration(tokens[5])
        if len(tokens) > 7:
            raise ValueError("cron windows look like '<m h dom mon dow> <duration> [Area/City]'")
        zone = _zone(tokens[6] if len(tokens) == 7 else None)
        dows = frozenset(d % 7 for d in _cron_field(dow, 0, 7, _CRON_DOW))  # 0 and 7 are Sunday
        return _Window(
            text,
            zone,
            duration,
            minutes=_cron_field(m, 0, 59),
            hours=_cron_field(h, 0, 23),
            doms=_cron_field(dom, 1, 31),
            months=_cron_field(mon, 1, 12, _CRON_MON),
            dows=dows,
            dom_any=dom == "*",
            dow_any=dow == "*",
        )
    if len(tokens) not in (2, 3):
        raise ValueError("windows look like 'Mon-Fri 22:00-02:00 Australia/Sydney' or '0 22 * * 1-5 4h'")
    days = _day_set(tokens[0])
    a, sep, b = tokens[1].partition("-")
    if not sep:
        raise ValueError(f"bad time range {tokens[1]!r} (HH:MM-HH:MM)")
    start, end = _hhmm(a), _hhmm(b)
    minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    if minutes <= 0:
        minutes += 24 * 60
    zone = _zone(tokens[2] if len(tokens) == 3 else None)
    return _Window(text, zone, timedelta(minutes=minutes), days=days, start=start)


@dataclass(frozen=True)
class ChangeWindow:
    windows: tuple[_Window, ...]

    @classmethod
    def parse(cls, spec: str) -> ChangeWindow:
        parts = [p.strip() for p in spec.split(";") if p.strip()]
        if not parts:
            raise ValueError("MCP_CHANGE_WINDOW is empty")
        out = []
        for p in parts:
            try:
                out.append(_parse_window(p))
            except ValueError as e:
                raise ValueError(f"MCP_CHANGE_WINDOW {p!r}: {e}") from None
        return cls(tuple(out))

    @property
    def text(self) -> str:
        return "; ".join(w.text for w in self.windows)

    def state(self, now: datetime | None = None) -> dict[str, Any]:
        """{"window", "open", "closes_at" | "next_open"} (ISO timestamps in the window's zone)."""
        now = now or _now()
        ends = [e for w in self.windows if (e := w.open_at(now)) is not None]
        out: dict[str, Any] = {"window": self.text, "open": bool(ends)}
        if ends:
            out["closes_at"] = max(ends).isoformat(timespec="minutes")
        else:
            nexts = [n for w in self.windows if (n := w.next_open(now)) is not None]
            out["next_open"] = min(nexts).isoformat(timespec="minutes") if nexts else None
        return out


def _now() -> datetime:
    return datetime.now(UTC)  # patched by tests


# --- the policy ---------------------------------------------------------------------------


def _blast_overrides(spec: str | None) -> tuple[tuple[str, int], ...]:
    out = []
    for raw in re.split(r"[,\n]", spec or ""):
        entry = raw.strip()
        if not entry:
            continue
        name, sep, value = entry.partition("=")
        if not sep or not name.strip() or not value.strip().isdigit():
            raise ValueError(f"MCP_BLAST_RADIUS_OVERRIDES: {entry!r} must look like 'tool_or_glob=N'")
        out.append((name.strip(), int(value)))
    return tuple(out)


@dataclass(frozen=True)
class SafetyPolicy:
    dry_run: bool = False
    window: ChangeWindow | None = None
    deny: ToolSelector = field(default_factory=ToolSelector)
    allow: ToolSelector | None = None
    max_blast_radius: int | None = None
    blast_overrides: tuple[tuple[str, int], ...] = ()
    two_person: ToolSelector = field(default_factory=ToolSelector)

    @classmethod
    def from_settings(cls, settings: BaseServerSettings) -> SafetyPolicy:
        return cls(
            dry_run=settings.mcp_dry_run,
            window=ChangeWindow.parse(settings.mcp_change_window) if settings.mcp_change_window else None,
            deny=ToolSelector.parse(settings.mcp_deny_tools, setting="MCP_DENY_TOOLS"),
            allow=(
                ToolSelector.parse(settings.mcp_allow_tools, setting="MCP_ALLOW_TOOLS")
                if settings.mcp_allow_tools
                else None
            ),
            max_blast_radius=settings.mcp_max_blast_radius,
            blast_overrides=_blast_overrides(settings.mcp_blast_radius_overrides),
            two_person=ToolSelector.parse(settings.mcp_two_person_tools, setting="MCP_TWO_PERSON_TOOLS"),
        )

    @property
    def active(self) -> bool:
        return bool(
            self.dry_run
            or self.window
            or self.deny
            or self.allow is not None
            or self.max_blast_radius is not None
            or self.blast_overrides
            or self.two_person
        )

    def registers(self, tool: str, tier: str, *, always: bool = False) -> bool:
        """Whether a tool is registered at all (deny / allow lists)."""
        if always:
            return True
        if self.deny.matches(tool, tier):
            return False
        return self.allow is None or self.allow.matches(tool, tier)

    def blast_limit(self, tool: str) -> int | None:
        """The limit for `tool`: the first matching override (exact names before globs), else the default."""
        for name, limit in self.blast_overrides:
            if name == tool:
                return limit
        for name, limit in self.blast_overrides:
            if fnmatch.fnmatchcase(tool, name):
                return limit
        return self.max_blast_radius

    def needs_two_person(self, tool: str, tier: str | None) -> bool:
        return self.two_person.matches(tool, tier)

    def for_write(
        self, tool: str, tier: str | None, blast_radius: int | None, now: datetime | None = None
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        """(preview section, blockers, apply refusals) for one write call.

        Blockers stop the preview from issuing a token; refusals only stop the confirm (the change can
        still be previewed, e.g. outside the change window)."""
        info: dict[str, Any] = {}
        blockers: list[str] = []
        refusals: list[str] = []
        if self.dry_run:
            info["dry_run"] = True
            refusals.append("the server is in dry-run mode (MCP_DRY_RUN=true): previews only")
        if self.window is not None:
            state = self.window.state(now)
            info["change_window"] = state
            if not state["open"]:
                when = f"; it next opens {state['next_open']}" if state.get("next_open") else ""
                refusals.append(f"outside the change window ({self.window.text}){when}")
        limit = self.blast_limit(tool)
        if blast_radius is not None:
            info["blast_radius"] = blast_radius
        if limit is not None:
            info["max_blast_radius"] = limit
            if blast_radius is not None and blast_radius > limit:
                blockers.append(
                    f"blast radius {blast_radius} exceeds the limit of {limit} for {tool} "
                    "(MCP_MAX_BLAST_RADIUS / MCP_BLAST_RADIUS_OVERRIDES); split the change into smaller ones"
                )
        if self.needs_two_person(tool, tier):
            info["two_person"] = True
        return info, blockers, refusals

    def describe(self) -> dict[str, Any]:
        """Summary for `<prefix>_status`."""
        out: dict[str, Any] = {}
        if self.dry_run:
            out["dry_run"] = True
        if self.window is not None:
            out["change_window"] = self.window.state()
        if self.deny:
            out["deny_tools"] = self.deny.describe()
        if self.allow is not None:
            out["allow_tools"] = self.allow.describe()
        if self.max_blast_radius is not None:
            out["max_blast_radius"] = self.max_blast_radius
        if self.blast_overrides:
            out["blast_radius_overrides"] = dict(self.blast_overrides)
        if self.two_person:
            out["two_person_tools"] = self.two_person.describe()
        return out
