"""Runtime, time-boxed rail overrides (roadmap C70): widen a server's own safety rail for a window.

A server rail set in the environment (CML's L20 `CML_MANAGED_LABS` / `CML_PROTECTED_LABS`, say) can only be
changed by editing settings and restarting. An override lets an operator widen it at runtime **for a bounded
time, with a reason, through a previewed and (by default) two-person-approved tool**, and it lapses on its
own. It weakens the rail by design, so it is OFF unless the operator turns it on.

Overrides are **add only** (owner decision 2026-10-24, "Add labs only"): an override can only add entries to
the lists the server names in `addable` (CML: managed lab globs). It can never remove or change anything
else, and never lift a protected entry: a request naming another list, or adding an entry that overlaps a
`protected` pattern, is refused in the preview. Lifting a protection means editing settings and restarting.


    MCP_RUNTIME_OVERRIDES=true           allow override tools to set anything (default false)
    MCP_OVERRIDE_MAX_MINUTES=240         longest window one override may ask for (1..1440)
    MCP_OVERRIDE_TWO_PERSON=true         a different caller must confirm the override (default true; set
                                         false for a single-user stdio server, where nobody else can)

Core owns the primitive; a server owns the meaning of the values. In a server's write tool:

    from stutters_mcp_core.overrides import active_override, set_override

    @reg.write(title="Widen the lab scope for a while")
    async def cml_set_lab_scope(ctx, managed: list[str], until: str, reason: str, confirm: Confirm = None):
        return await set_override(ctx, tool="cml_set_lab_scope", scope="lab_scope",
                                  values={"managed": managed}, until=until, reason=reason, confirm=confirm,
                                  addable=["managed"], protected=protected_patterns,
                                  current={"managed": managed_patterns, "protected": protected_patterns})

    # wherever the rail is checked:
    ov = active_override(ctx, "lab_scope")
    managed = base_managed + (ov.values["managed"] if ov else [])

Overrides live in the server process only (a restart drops them: the rail snaps back), one per scope and
instance. Setting, clearing and lapsing are audit events (`override_set`, `override_cleared`,
`override_expired`); `<prefix>_status` lists the active ones under `server.overrides`.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from stutters_mcp_core.errors import InvalidRequest
from stutters_mcp_core.inbound_auth import current_caller
from stutters_mcp_core.policy import _duration
from stutters_mcp_core.targets import target_name

if TYPE_CHECKING:
    from fastmcp import Context

__all__ = [
    "Override",
    "OverrideStore",
    "active_override",
    "add_only_problems",
    "clear_override",
    "get_overrides",
    "parse_until",
    "set_override",
]

audit = logging.getLogger("stutters_mcp_core.audit")

MIN_REASON = 8


def _now() -> datetime:
    return datetime.now(UTC)  # patched by tests


@dataclass(frozen=True)
class Override:
    scope: str  # "<scope>" or "<scope>@<instance>"
    values: dict[str, Any]
    reason: str
    requested_by: str
    approved_by: str | None
    set_at: datetime
    until: datetime

    def remaining(self, now: datetime) -> timedelta:
        return max(timedelta(0), self.until - now)

    def describe(self, now: datetime) -> dict[str, Any]:
        out: dict[str, Any] = {
            "scope": self.scope,
            "values": self.values,
            "reason": self.reason,
            "requested_by": self.requested_by,
            "set_at": self.set_at.isoformat(timespec="seconds"),
            "until": self.until.isoformat(timespec="seconds"),
            "remaining_minutes": round(self.remaining(now).total_seconds() / 60, 1),
        }
        if self.approved_by is not None:
            out["approved_by"] = self.approved_by
        return out


def _event(event: str, override: Override, now: datetime, **extra: Any) -> None:
    audit.info(json.dumps({"event": event, **override.describe(now), **extra}, default=str))


@dataclass
class OverrideStore:
    """In-memory overrides, one per key (scope plus instance). Expired entries are dropped (and audited)
    the next time anything looks at them; nothing runs in the background."""

    enabled: bool = False
    max_duration: timedelta = timedelta(hours=4)
    two_person: bool = True
    clock: Callable[[], datetime] = field(default=lambda: _now())  # late-bound: tests patch _now
    _items: dict[str, Override] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Any) -> OverrideStore:
        return cls(
            enabled=bool(getattr(settings, "mcp_runtime_overrides", False)),
            max_duration=timedelta(minutes=int(getattr(settings, "mcp_override_max_minutes", 240))),
            two_person=bool(getattr(settings, "mcp_override_two_person", True)),
        )

    def _expire(self) -> None:
        now = self.clock()
        for key in [k for k, o in self._items.items() if o.until <= now]:
            _event("override_expired", self._items.pop(key), now)

    def get(self, key: str) -> Override | None:
        self._expire()
        return self._items.get(key)

    def put(self, override: Override) -> Override:
        self._expire()
        self._items[override.scope] = override
        _event("override_set", override, self.clock())
        return override

    def pop(self, key: str, *, by: str) -> Override | None:
        self._expire()
        override = self._items.pop(key, None)
        if override is not None:
            _event("override_cleared", override, self.clock(), cleared_by=by)
        return override

    def active(self) -> list[Override]:
        self._expire()
        return sorted(self._items.values(), key=lambda o: o.scope)

    def describe(self) -> dict[str, Any]:
        """The `server.overrides` block of `<prefix>_status`."""
        now = self.clock()
        return {
            "enabled": self.enabled,
            "max_minutes": int(self.max_duration.total_seconds() // 60),
            "two_person": self.two_person,
            "active": [o.describe(now) for o in self.active()],
        }


def get_overrides(ctx: Context) -> OverrideStore:
    """The server's override store (created with the server's settings on first use)."""
    lc = ctx.lifespan_context
    store = lc.get("overrides")
    if store is None:
        holder = ctx.fastmcp
        store = getattr(holder, "_stutters_overrides", None)
        if store is None:
            store = OverrideStore.from_settings(lc.get("settings"))
            holder._stutters_overrides = store  # type: ignore[attr-defined]
    if not isinstance(store, OverrideStore):
        raise TypeError("lifespan_context['overrides'] must be an OverrideStore")
    return store


def _key(scope: str) -> str:
    target = target_name()
    return scope if target is None else f"{scope}@{target}"


def active_override(ctx: Context, scope: str) -> Override | None:
    """The live override for `scope` on the current instance, or None (none set, expired, or disabled)."""
    store = get_overrides(ctx)
    if not store.enabled:
        return None
    return store.get(_key(scope))


def add_only_problems(
    values: Mapping[str, Any], addable: Iterable[str], protected: Iterable[str]
) -> list[str]:
    """Why `values` is not a pure addition (owner decision C70: add only), or [] when it is.

    Every key must be one of `addable`, every value a list of non-empty strings, and no added entry may
    overlap a `protected` pattern (either matches the other as a case-insensitive glob), so an override
    can never lift a protection."""
    allowed = list(addable)
    shielded = [str(q) for q in protected if str(q).strip()]
    problems: list[str] = []
    for key, entries in values.items():
        if key not in allowed:
            problems.append(
                f"{key!r} can't be changed at runtime: overrides only add {', '.join(map(repr, allowed))} "
                "entries and can never lift a protected entry or narrow a rail (edit the settings and "
                "restart instead)."
            )
            continue
        if not isinstance(entries, list) or not all(isinstance(e, str) and e.strip() for e in entries):
            problems.append(f"{key!r} must be a list of non-empty strings to add")
            continue
        for entry in entries:
            for q in shielded:
                a, b = entry.strip().lower(), q.lower()
                if fnmatch.fnmatchcase(b, a) or fnmatch.fnmatchcase(a, b):
                    problems.append(
                        f"{entry!r} overlaps the protected entry {q!r}: an override can never lift a "
                        "protection (edit the settings and restart instead)."
                    )
    return problems


def parse_until(text: str, now: datetime, max_duration: timedelta) -> datetime:
    """`until` as a duration from now ("30m", "2h", "1h30m") or an ISO 8601 time (no zone = UTC).
    Raises ValueError when it is unreadable, not in the future, or longer than `max_duration`."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("until is required: a duration such as 30m or 2h, or an ISO 8601 time")
    try:
        when = now + _duration(raw)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(
                f"until {raw!r} is neither a duration (30m, 2h, 1h30m) nor an ISO 8601 time"
            ) from None
        when = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    if when <= now:
        raise ValueError(f"until {raw!r} is not in the future")
    if when - now > max_duration:
        limit = int(max_duration.total_seconds() // 60)
        raise ValueError(f"until {raw!r} is more than {limit} minutes away (MCP_OVERRIDE_MAX_MINUTES)")
    return when


async def set_override(
    ctx: Context,
    *,
    tool: str,
    scope: str,
    values: Mapping[str, Any],
    until: str,
    reason: str,
    confirm: str | None,
    addable: Iterable[str],
    protected: Iterable[str] = (),
    current: Mapping[str, Any] | None = None,
    blockers: Iterable[str] = (),
    args: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Preview, then (after confirm, by a second caller when MCP_OVERRIDE_TWO_PERSON) set an override.

    Add only: `values` maps each list in `addable` to the entries to add to it; any other key, or an entry
    overlapping a `protected` pattern, blocks the preview (an override never lifts a protection).
    `current` is the rail as configured (shown next to the override in the preview); `blockers` are the
    server's own checks on `values`; `args` are the tool's arguments except `confirm` (defaults to
    values/until/reason). A duration `until` counts from the confirm, not the preview."""
    from stutters_mcp_core.writes import Plan, get_previews, preview_or_apply

    store = get_overrides(ctx)
    key = _key(scope)
    now = store.clock()
    problems = list(blockers)
    when: datetime | None = None
    if not store.enabled:
        problems.append(
            "Runtime overrides are off on this server (MCP_RUNTIME_OVERRIDES=false): change the setting "
            "and restart to allow them."
        )
    try:
        when = parse_until(until, now, store.max_duration)
    except ValueError as e:
        problems.append(str(e))
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON:
        problems.append(f"reason is required (at least {MIN_REASON} characters): say why and for what work")
    if not values or not any(values.values()):
        problems.append("nothing to override: values is empty")
    problems.extend(add_only_problems(values, addable, protected))
    existing = store.get(key)
    details: dict[str, Any] = {
        "scope": key,
        "values": dict(values),
        "until": when.isoformat(timespec="seconds") if when else until,
        "reason": reason,
    }
    if when is not None:
        details["minutes"] = round((when - now).total_seconds() / 60, 1)
    if current is not None:
        details["configured"] = dict(current)
    if existing is not None:
        details["replaces"] = existing.describe(now)
    plan = Plan(
        f"Override {key} until {details['until']}",
        details=details,
        # The token is refused if another override for this scope was set or cleared meanwhile.
        fingerprint={"existing": existing.set_at.isoformat() if existing else None},
        blockers=problems,
        side_effects=[
            "Weakens a safety rail for this window: the server's checks add the override's entries until "
            "it lapses, is cleared, or the server restarts (protected entries stay protected)."
        ],
        two_person=store.two_person,
        blast_unknown="widens a safety rail for a while; it touches no appliance object itself",
    )
    call_args = dict(args) if args is not None else {"values": dict(values), "until": until, "reason": reason}

    # Who asked: the previewing caller behind a two-person token, else whoever confirms.
    pending = get_previews(ctx).peek(confirm) if confirm else None
    caller = current_caller()
    two_person = pending is not None and pending.two_person
    requested_by = pending.caller if two_person and pending is not None else caller
    approved_by = caller if two_person else None

    async def apply() -> dict[str, Any]:
        assert when is not None  # blockers stop the apply otherwise
        override = Override(key, dict(values), reason, requested_by, approved_by, store.clock(), when)
        return store.put(override).describe(store.clock())

    return await preview_or_apply(ctx, tool=tool, args=call_args, confirm=confirm, plan=plan, apply=apply)


async def clear_override(ctx: Context, *, tool: str, scope: str, confirm: str | None) -> dict[str, Any]:
    """Preview, then remove the override for `scope` now (the rail returns to its configured values).
    Narrowing a rail needs no second person."""
    from stutters_mcp_core.writes import Plan, preview_or_apply

    store = get_overrides(ctx)
    key = _key(scope)
    existing = store.get(key)
    now = store.clock()
    if existing is None:
        raise InvalidRequest(f"No active override for {key}; nothing to clear.")
    plan = Plan(
        f"Clear the override on {key}",
        details={"override": existing.describe(now)},
        fingerprint={"existing": existing.describe(now)["set_at"]},
    )

    async def apply() -> dict[str, Any]:
        removed = store.pop(key, by=current_caller())
        return {"cleared": removed.describe(store.clock()) if removed else None}

    return await preview_or_apply(
        ctx, tool=tool, args={"scope": scope}, confirm=confirm, plan=plan, apply=apply
    )
