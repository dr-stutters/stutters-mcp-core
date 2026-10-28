"""Backup-age rail (roadmap C73): dangerous previews show the appliance's newest backup age, and are blocked
when it is older than the limit or unknown.

Every Cisco upgrade and migration guide says to take a backup first. With the rail on, `preview_or_apply`
asks the server's backup-age hook how old the newest backup is, shows it in the preview under `backup`, and
adds a blocker when it is over the limit or can't be told (no hook, the hook failed, no backup at all):

    MCP_REQUIRE_BACKUP_HOURS=24          server-wide limit, in hours (unset = off)
    <P>_REQUIRE_BACKUP_HOURS=72          per instance (<P>_<NAME>_REQUIRE_BACKUP_HOURS for a named one);
                                         wins over the server-wide value for that appliance, 0 = off there
    MCP_REQUIRE_BACKUP_TOOLS=fmc_deploy,@dangerous
                                         which tools the rail covers (tool globs and @tier selectors;
                                         default @dangerous). Read tools are never covered.

A server registers its hook once (like the snapshot hook), from reads it already has:

    from stutters_mcp_core.backup_rail import BackupAge, register_backup_age

    async def backup_age(ctx: Context) -> BackupAge:
        newest = await get_client(ctx).newest_backup()        # e.g. what fmc_list_backups reads
        if newest is None:
            return BackupAge(None, "FMC backups", "no backup found")
        return BackupAge.from_time(newest["date"], "FMC backups", newest["name"])

    def register(reg: ToolRegistrar) -> None:
        register_backup_age(reg, backup_age)

The C70 runtime override lifts the blocker for a while, with a reason: `<prefix>_waive_backup_check(tools,
until, reason)` (registered when MCP_RUNTIME_OVERRIDES=true and the rail is configured; a second person
confirms it unless MCP_OVERRIDE_TWO_PERSON=false). The preview still shows the age and the waiver.
`<prefix>_status` reports the setting under `server.rails.backup`. Tool tiers are unchanged.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context
from pydantic import Field

from stutters_mcp_core.policy import ToolSelector
from stutters_mcp_core.writes import Confirm

if TYPE_CHECKING:
    from stutters_mcp_core.settings import BaseServerSettings
    from stutters_mcp_core.writes import ToolRegistrar

__all__ = [
    "BackupAge",
    "BackupAgeFn",
    "BackupRail",
    "OVERRIDE_SCOPE",
    "audit_brief",
    "backup_check",
    "format_age",
    "register_backup_age",
    "rail_configured",
    "register_waiver_tool",
]

OVERRIDE_SCOPE = "backup"
DEFAULT_TOOLS = "@dangerous"
WAIVE_SUFFIX = "waive_backup_check"  # <prefix>_waive_backup_check
_GLOB = re.compile(r"[A-Za-z0-9_*?\[\]!-]+")


def _now() -> datetime:
    return datetime.now(UTC)  # patched by tests


@dataclass(frozen=True)
class BackupAge:
    """What the server's hook found: the newest backup's age in seconds (None = unknown or no backup),
    where it looked (`source`, e.g. "FMC backups"), and a short `detail` (the backup's name, or why the age
    is unknown). `taken_at` is the backup's time as ISO 8601, when known."""

    seconds: float | None
    source: str
    detail: str | None = None
    taken_at: str | None = None

    @classmethod
    def from_time(
        cls,
        when: datetime | str | float | None,
        source: str,
        detail: str | None = None,
        *,
        now: datetime | None = None,
    ) -> BackupAge:
        """From the backup's time: a datetime (naive = UTC), ISO 8601 text, or epoch seconds or
        milliseconds (as the Cisco APIs return them). None or an unreadable value = unknown."""
        current = now or _now()
        stamp: datetime | None = None
        if isinstance(when, datetime):
            stamp = when
        elif isinstance(when, int | float) and not isinstance(when, bool):
            value = float(when)
            stamp = datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
        elif isinstance(when, str) and when.strip():
            raw = when.strip()
            if re.fullmatch(r"\d+(\.\d+)?", raw):
                return cls.from_time(float(raw), source, detail, now=current)
            try:
                stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return cls(None, source, detail or f"unreadable backup time {raw!r}")
        if stamp is None:
            return cls(None, source, detail or "no backup found")
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        seconds = max(0.0, (current - stamp).total_seconds())
        return cls(seconds, source, detail, stamp.astimezone(UTC).isoformat(timespec="seconds"))


BackupAgeFn = Callable[["Context"], Awaitable[BackupAge]]


def register_backup_age(reg: ToolRegistrar, fn: BackupAgeFn) -> None:
    """Register the server's backup-age hook (one per server; it runs per instance through `ctx`)."""
    reg.mcp._stutters_backup_age = fn  # type: ignore[attr-defined]


def format_age(seconds: float) -> str:
    """'3d 4h', '5h 12m', '40m', '<1m'."""
    minutes = int(seconds // 60)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m" if mins else "<1m"


def _hours(value: float) -> float | int:
    return int(value) if float(value).is_integer() else round(value, 2)


@dataclass(frozen=True)
class BackupRail:
    """The server-wide part of the rail (MCP_REQUIRE_BACKUP_HOURS / MCP_REQUIRE_BACKUP_TOOLS); the
    per-instance limit is read from each instance's settings."""

    hours: float | None = None
    tools: ToolSelector = ToolSelector((), frozenset({"dangerous"}))

    @classmethod
    def from_settings(cls, settings: BaseServerSettings) -> BackupRail:
        spec = getattr(settings, "mcp_require_backup_tools", None)
        tools = ToolSelector.parse(spec or DEFAULT_TOOLS, setting="MCP_REQUIRE_BACKUP_TOOLS")
        return cls(getattr(settings, "mcp_require_backup_hours", None), tools)

    def limit_for(self, instance: BaseServerSettings | None) -> float | None:
        """The limit in hours for one appliance: its own <P>_REQUIRE_BACKUP_HOURS wins (0 = off)."""
        own = getattr(instance, "require_backup_hours", None) if instance is not None else None
        if own is not None:
            return float(own) or None
        return self.hours

    def covers(self, tool: str, tier: str | None) -> bool:
        if tier == "read" or tool.endswith(f"_{WAIVE_SUFFIX}"):
            return False
        if tier is None and self.tools.tiers:
            return True  # an unknown tier counts as matching a tier selector (fail closed)
        return self.tools.matches(tool, tier)

    def describe(self, instance: BaseServerSettings | None, *, hook: bool) -> dict[str, Any]:
        """The `server.rails.backup` block of `<prefix>_status`."""
        limit = self.limit_for(instance)
        own = getattr(instance, "require_backup_hours", None) if instance is not None else None
        out: dict[str, Any] = {
            "required": limit is not None,
            "max_age_hours": _hours(limit) if limit is not None else None,
            "tools": self.tools.describe(),
            "hook": hook,
        }
        if self.hours is not None:
            out["server_hours"] = _hours(self.hours)
        if own is not None:
            out["instance_hours"] = _hours(own)
        if limit is not None and not hook:
            out["note"] = "this server has no backup-age check: every covered change is blocked (age unknown)"
        return out


def _hook(ctx: Context) -> BackupAgeFn | None:
    try:
        return getattr(ctx.fastmcp, "_stutters_backup_age", None)
    except Exception:  # noqa: BLE001 - no server attached (unit tests of the helper)
        return None


def _waiver(ctx: Context, tool: str) -> dict[str, Any] | None:
    from stutters_mcp_core.overrides import active_override, get_overrides

    try:
        override = active_override(ctx, OVERRIDE_SCOPE)
    except Exception:  # noqa: BLE001 - no override store (unit tests of the helper)
        return None
    if override is None:
        return None
    globs = [str(g) for g in override.values.get("tools", [])]
    if not any(fnmatch.fnmatchcase(tool, g) for g in globs):
        return None
    return override.describe(get_overrides(ctx).clock())


async def backup_check(ctx: Context, tool: str, tier: str | None) -> tuple[dict[str, Any] | None, list[str]]:
    """(the preview's `backup` block, blockers) for a write; (None, []) when the rail doesn't apply."""
    from stutters_mcp_core.secrets import scrub_text

    lc = ctx.lifespan_context
    rail = lc.get("backup_rail")
    if not isinstance(rail, BackupRail):
        return None, []
    limit = rail.limit_for(lc.get("settings"))
    if limit is None or not rail.covers(tool, tier):
        return None, []
    prefix = tool.split("_", 1)[0]
    info: dict[str, Any] = {"max_age_hours": _hours(limit)}
    hook = _hook(ctx)
    age: BackupAge | None = None
    unknown: str | None = None
    if hook is None:
        unknown = "this server has no backup-age check"
    else:
        try:
            age = await hook(ctx)
        except Exception as e:  # noqa: BLE001 - any failure means the age is unknown, which blocks
            unknown = f"the backup-age check failed: {scrub_text(str(e))[:200]}"
    if age is not None:
        info["source"] = age.source
        if age.detail:
            info["detail"] = scrub_text(age.detail)[:300]
        if age.taken_at:
            info["taken_at"] = age.taken_at
        if age.seconds is None:
            unknown = f"{age.source}: {age.detail or 'no backup found'}"
        else:
            info["age_hours"] = round(age.seconds / 3600, 1)
            info["age"] = format_age(age.seconds)
    problem: str | None = None
    if unknown is not None:
        info["age"] = "unknown"
        problem = (
            f"the age of the newest backup is unknown ({unknown}) and MCP_REQUIRE_BACKUP_HOURS needs one "
            f"taken in the last {_hours(limit)} h"
        )
    elif age is not None and age.seconds is not None and age.seconds > limit * 3600:
        problem = (
            f"the newest backup ({age.source}) is {info['age']} old, more than the "
            f"{_hours(limit)} h MCP_REQUIRE_BACKUP_HOURS allows"
        )
    info["ok"] = problem is None
    if problem is None:
        return info, []
    waiver = _waiver(ctx, tool)
    if waiver is not None:
        info["waived"] = waiver
        info["note"] = f"Not blocked: {problem}, but a backup-check waiver is active ({waiver['reason']})."
        return info, []
    return info, [
        f"{problem}: take a backup first, or have the requirement waived for a while with "
        f"{prefix}_{WAIVE_SUFFIX} (MCP_RUNTIME_OVERRIDES)"
    ]


def audit_brief(info: dict[str, Any]) -> dict[str, Any]:
    """The `backup` field of the audit event: age, limit, outcome."""
    keys = ("age_hours", "age", "max_age_hours", "source", "ok")
    out = {k: info[k] for k in keys if k in info}
    if "waived" in info:
        out["waived"] = info["waived"].get("reason")
    return out


def rail_configured(settings: list[BaseServerSettings]) -> bool:
    """MCP_REQUIRE_BACKUP_HOURS or any instance's own limit is set."""
    rail = BackupRail.from_settings(settings[0])
    return any(rail.limit_for(cfg) is not None for cfg in settings)


def register_waiver_tool(reg: ToolRegistrar) -> None:
    """`<prefix>_waive_backup_check`: a C70 override (add only: tool globs) that lifts the rail's blocker
    for a while, with a reason. build_server registers it when overrides are on and the rail is set."""
    from stutters_mcp_core.overrides import set_override

    prefix = reg.prefix
    name = f"{prefix}_{WAIVE_SUFFIX}"
    rail = BackupRail.from_settings(reg.settings)

    @reg.write(
        title="Waive the backup-age check for a while",
        name=name,
        description=(
            "Lift the MCP_REQUIRE_BACKUP_HOURS blocker for some tools until a time, with a reason (a C70 "
            "runtime override: it lapses on its own, a restart drops it, and by default a second person "
            "confirms it). Previews keep showing the backup age and the waiver. Prefer taking a backup. "
            "Preview-first: returns a confirm token before changing anything."
        ),
    )
    async def waive_backup_check(
        ctx: Context,
        tools: Annotated[
            list[str],
            Field(description="Tool names or globs the waiver covers (a deploy tool, or ['*'] for all)"),
        ],
        until: Annotated[str, Field(description="When it lapses: a duration (30m, 2h) or an ISO 8601 time")],
        reason: Annotated[str, Field(description="Why the backup can't be taken first (8+ characters)")],
        confirm: Confirm = None,
    ) -> dict[str, Any]:
        from stutters_mcp_core.writes import Tier

        problems: list[str] = []
        clean = [t.strip() for t in tools if isinstance(t, str) and t.strip()]
        bad = [t for t in clean if not _GLOB.fullmatch(t)]
        if bad:
            problems.append(f"not a tool name or glob: {', '.join(map(repr, bad))}")
        covered = sorted(
            t for t, tier in reg.tiers.items() if tier is not Tier.READ and rail.covers(t, tier.value)
        )
        matched = sorted({t for t in covered for g in clean if fnmatch.fnmatchcase(t, g)})
        if clean and not bad and not matched:
            problems.append(
                f"{', '.join(map(repr, clean))} matches none of the tools the backup rail covers "
                f"({', '.join(covered) or 'none registered'})"
            )
        limit = rail.limit_for(ctx.lifespan_context.get("settings"))
        result = await set_override(
            ctx,
            tool=name,
            scope=OVERRIDE_SCOPE,
            values={"tools": clean},
            until=until,
            reason=reason,
            confirm=confirm,
            addable=["tools"],
            current={
                "max_age_hours": _hours(limit) if limit is not None else None,
                "covered_tools": covered,
                "waived_tools": matched,
            },
            blockers=problems,
            args={"tools": tools, "until": until, "reason": reason},
        )
        return result
