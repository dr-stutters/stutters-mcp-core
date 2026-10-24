"""The snapshot contract (roadmap C53): compact, versioned health blobs for pre/post change checks.

A server's `<prefix>_snapshot` tool returns a JSON blob describing the health of what it manages (for a
site, or everything); `<prefix>_compare_snapshot` takes the blob from before a change and either a second
blob or a fresh snapshot, and names what changed by severity. The **client holds the blobs**: nothing is
stored on the server (ADR-003). The full contract, for server builders, is docs/SNAPSHOTS.md.

    from stutters_mcp_core.snapshot import Snapshot, register_snapshot_tools

    async def take(ctx: Context, site: str) -> Snapshot:
        snap = Snapshot("catsdwan", "cat-sdwan-mcp-server", scope={"site": site})
        bfd = snap.section("bfd", "BFD sessions", metrics={"loss_pct": Metric("lower", 1.0, limit=5.0)})
        for s in await sessions(ctx, site):
            bfd.item(f"{s.local}|{s.color}|{s.remote}|{s.remote_color}", ok=s.state == "up", state=s.state,
                     loss_pct=s.loss)
        return snap

    def register(reg: ToolRegistrar) -> None:
        register_snapshot_tools(reg, take, product="Catalyst SD-WAN Manager",
                                covers="BFD sessions, OMP peers, app-route SLA, control connections, alarms",
                                site_doc="SD-WAN site id or name; empty = the whole fabric")
"""

import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeGuard

from fastmcp import Context
from fastmcp.tools import ToolResult
from pydantic import Field

from stutters_mcp_core.errors import InvalidRequest
from stutters_mcp_core.formatting import object_result

if TYPE_CHECKING:
    from stutters_mcp_core.writes import ToolRegistrar

SCHEMA_VERSION = 1
# A blob must survive the round trip through the client intact, so it stays well under the default
# MCP_MAX_RESPONSE_CHARS (40,000), which would cut it.
MAX_CHARS = 24_000
DEFAULT_MAX_ITEMS = 100
MAX_CHANGES = 100
RESERVED = ("ok", "state", "label")
SEVERITIES = ("major", "minor", "info")
_ORDER = {s: i for i, s in enumerate(SEVERITIES)}

Scalar = str | int | float | bool | None


@dataclass(frozen=True)
class Metric:
    """How a numeric item field is compared. `better`: which direction is good. `tolerance`: the absolute
    change ignored as noise. `limit`: the threshold of acceptable (e.g. loss_pct 5); crossing it is a
    major `degraded` change, any other change beyond the tolerance is minor `worse` or info `improved`."""

    better: Literal["lower", "higher"]
    tolerance: float = 0.0
    limit: float | None = None

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"better": self.better, "tolerance": _num(self.tolerance)}
        if self.limit is not None:
            out["limit"] = _num(self.limit)
        return out


def _num(v: Any) -> Any:
    """Numbers normalised so a blob re-serialises identically: integral floats become ints, others are
    rounded to 3 decimals."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        if v.is_integer():
            return int(v)
        return round(v, 3)
    return v


class Section:
    """One part of a snapshot (e.g. "bfd"): items keyed by a stable id."""

    def __init__(self, title: str, metrics: Mapping[str, Metric] | None, max_items: int) -> None:
        self.title = title
        self.metrics = dict(metrics or {})
        self.max_items = max_items
        self.items: dict[str, dict[str, Scalar]] = {}
        self.error: str | None = None

    def item(
        self,
        key: str,
        *,
        ok: bool | None = None,
        state: str | None = None,
        label: str | None = None,
        **fields: Scalar,
    ) -> None:
        """Add an item. `key` is stable across snapshots (names, not UUIDs that change on rebuild where a
        name exists). `ok`: is it healthy (compared: true -> false is degraded). `state`: the product's own
        word (compared). Other fields are scalars: those named in `metrics` are compared with their
        tolerance, the rest for equality, so leave out counters, uptimes and timestamps."""
        if not key or len(key) > 200:
            raise ValueError(f"snapshot item key must be 1-200 characters: {key!r}")
        if key in self.items:
            raise ValueError(f"snapshot item {key!r} added twice to section {self.title!r}")
        out: dict[str, Scalar] = {}
        if ok is not None:
            out["ok"] = bool(ok)
        if state is not None:
            out["state"] = str(state)
        if label is not None and label != key:
            out["label"] = str(label)
        for name, value in fields.items():
            if value is not None and not isinstance(value, str | int | float | bool):
                raise ValueError(f"snapshot item field {name!r} must be a scalar, got {type(value).__name__}")
            if value is not None:
                out[name] = _num(value)
        self.items[key] = out

    def fail(self, error: str) -> None:
        """Mark the section unreadable (the product API failed); compare reports it instead of a diff."""
        self.error = error[:300]

    def as_json(self, limit: int) -> dict[str, Any]:
        out: dict[str, Any] = {"title": self.title}
        if self.metrics:
            out["metrics"] = {k: m.as_json() for k, m in sorted(self.metrics.items())}
        if self.error is not None:
            out["error"] = self.error
        keys = sorted(self.items)
        if len(keys) > limit:
            # Keep the unhealthy items, then the rest in key order, so a cut is deterministic.
            bad = [k for k in keys if self.items[k].get("ok") is False]
            rest = [k for k in keys if self.items[k].get("ok") is not False]
            keys = sorted((bad + rest)[:limit])
            out["truncated"] = len(self.items) - len(keys)
        out["items"] = {k: self.items[k] for k in keys}
        return out


class Snapshot:
    """Builds a snapshot blob. Section names are snake_case; keep blobs small (MAX_CHARS)."""

    def __init__(
        self,
        prefix: str,
        server: str,
        *,
        scope: Mapping[str, Scalar] | None = None,
        server_version: str | None = None,
        instance: str | None = None,
    ) -> None:
        self.prefix = prefix
        self.server = server
        self.scope = {k: v for k, v in (scope or {}).items() if v not in (None, "")}
        self.server_version = server_version
        self.instance = instance
        self.taken_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.sections: dict[str, Section] = {}

    def section(
        self,
        name: str,
        title: str,
        *,
        metrics: Mapping[str, Metric] | None = None,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> Section:
        if not name.replace("_", "").isalnum() or not name.islower():
            raise ValueError(f"snapshot section name must be snake_case: {name!r}")
        if name in self.sections:
            raise ValueError(f"snapshot section {name!r} added twice")
        sec = Section(title, metrics, max_items)
        self.sections[name] = sec
        return sec

    def blob(self, max_chars: int = MAX_CHARS) -> dict[str, Any]:
        """The JSON blob, with its digest. Sections over budget are cut (unhealthy items kept) and
        marked `truncated`; the compare then skips added/removed in them."""
        limits = {n: s.max_items for n, s in self.sections.items()}
        while True:
            out = self._render(limits)
            if len(json.dumps(out, separators=(",", ":"))) <= max_chars:
                return out
            biggest = max(limits, key=lambda n: min(limits[n], len(self.sections[n].items)), default=None)
            if biggest is None or min(limits[biggest], len(self.sections[biggest].items)) <= 5:
                raise ValueError(
                    f"snapshot does not fit in {max_chars} characters even with 5 items per section; "
                    "drop fields or sections"
                )
            limits[biggest] = min(limits[biggest], len(self.sections[biggest].items)) // 2

    def _render(self, limits: Mapping[str, int]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "snapshot": SCHEMA_VERSION,
            "prefix": self.prefix,
            "server": self.server,
            "taken_at": self.taken_at,
            "scope": dict(self.scope),
        }
        if self.server_version:
            out["server_version"] = self.server_version
        if self.instance:
            out["instance"] = self.instance
        out["sections"] = {n: s.as_json(limits[n]) for n, s in sorted(self.sections.items())}
        out["digest"] = digest(out)
        return out


def digest(blob: Mapping[str, Any]) -> str:
    """sha256 (first 16 hex digits) of the blob's canonical JSON without its `digest`."""
    body = {k: v for k, v in blob.items() if k != "digest"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:16]


def parse(
    blob: str | Mapping[str, Any], *, prefix: str | None = None, what: str = "snapshot"
) -> dict[str, Any]:
    """A blob from a client (JSON text or an object), checked against the contract. Raises InvalidRequest
    with what is wrong. `prefix`: the server that must have taken it."""
    data: Any = blob
    if isinstance(blob, str):
        try:
            data = json.loads(blob)
        except ValueError as e:
            raise InvalidRequest(
                f"{what}: not JSON ({e}). Pass the blob exactly as the snapshot tool returned it."
            ) from None
    if not isinstance(data, dict) or "snapshot" not in data:
        raise InvalidRequest(f"{what}: not a snapshot blob (no 'snapshot' version field)")
    if data["snapshot"] != SCHEMA_VERSION:
        raise InvalidRequest(
            f"{what}: snapshot schema {data['snapshot']!r} is not supported (this server reads "
            f"{SCHEMA_VERSION}); take a new snapshot"
        )
    if prefix is not None and data.get("prefix") != prefix:
        raise InvalidRequest(
            f"{what}: taken by {data.get('prefix')!r}, not {prefix!r}; pass this server's own blob "
            f"(each server compares only its own snapshots)"
        )
    sections = data.get("sections")
    if not isinstance(sections, dict):
        raise InvalidRequest(f"{what}: 'sections' must be an object")
    for name, sec in sections.items():
        if not isinstance(sec, dict) or not isinstance(sec.get("items", {}), dict):
            raise InvalidRequest(f"{what}: section {name!r} must be an object with an 'items' object")
        for key, item in sec.get("items", {}).items():
            if not isinstance(item, dict):
                raise InvalidRequest(f"{what}: item {name}/{key} must be an object")
    return data


# --- compare -------------------------------------------------------------------------------------


def compare(
    before: str | Mapping[str, Any], after: str | Mapping[str, Any], *, prefix: str | None = None
) -> dict[str, Any]:
    """Name what changed between two blobs of the same server, most severe first.

    Severity: **major** = something got unhealthy (`ok` true -> false, a healthy item gone, a new unhealthy
    item, a metric crossing its limit); **minor** = a change that is not clearly bad (`state` or another
    field changed, a metric worse within its limit, a section unreadable); **info** = recoveries and
    improvements, healthy items added, unhealthy items gone."""
    b = parse(before, prefix=prefix, what="before")
    a = parse(after, prefix=prefix, what="after")
    if b.get("prefix") != a.get("prefix"):
        raise InvalidRequest(f"before is from {b.get('prefix')!r} and after from {a.get('prefix')!r}")
    notes: list[str] = []
    integrity: dict[str, str] = {}
    for label, blob in (("before", b), ("after", a)):
        if blob.get("digest") != digest(blob):
            integrity[label] = "altered"
            notes.append(
                f"The {label} blob differs from what the server returned (digest mismatch): it was edited "
                "or retyped, so some changes below may be wrong. Pass blobs exactly as returned."
            )
    if b.get("scope") != a.get("scope"):
        notes.append(f"Scopes differ: before {b.get('scope')}, after {a.get('scope')}.")
    if b.get("instance") != a.get("instance"):
        notes.append(f"Instances differ: before {b.get('instance')!r}, after {a.get('instance')!r}.")

    changes: list[dict[str, Any]] = []
    unchanged = 0
    bs, as_ = b["sections"], a["sections"]
    for name in sorted(set(bs) | set(as_)):
        sb, sa = bs.get(name), as_.get(name)
        title = str((sa or sb or {}).get("title") or name)
        if sb is None or sa is None:
            notes.append(
                f"{title}: only in the {'after' if sb is None else 'before'} snapshot; not compared."
            )
            continue
        if sb.get("error") or sa.get("error"):
            if sa.get("error"):
                changes.append(
                    _change("minor", "unreadable", name, title, "", [f"could not be read: {sa['error']}"])
                )
            notes.append(f"{title}: unreadable {'after' if sa.get('error') else 'before'}; not compared.")
            continue
        metrics = {**(sb.get("metrics") or {}), **(sa.get("metrics") or {})}
        cut = bool(sb.get("truncated") or sa.get("truncated"))
        if cut:
            notes.append(f"{title}: truncated in a snapshot; items added or removed there are not reported.")
        ib, ia = sb.get("items", {}), sa.get("items", {})
        for key in sorted(set(ib) | set(ia)):
            change = _compare_item(name, title, key, ib.get(key), ia.get(key), metrics, cut)
            if change is None:
                unchanged += key in ib and key in ia
            else:
                changes.append(change)

    changes.sort(key=lambda c: (_ORDER[c["severity"]], c["section"], c["item"]))
    counts = {s: sum(1 for c in changes if c["severity"] == s) for s in SEVERITIES}
    out: dict[str, Any] = {
        "prefix": a.get("prefix"),
        "server": a.get("server"),
        "scope": a.get("scope"),
        "before_taken_at": b.get("taken_at"),
        "after_taken_at": a.get("taken_at"),
        "verdict": _verdict(counts, unchanged),
        "counts": counts,
        "unchanged_items": unchanged,
        "changes": changes[:MAX_CHANGES],
    }
    if len(changes) > MAX_CHANGES:
        out["changes_omitted"] = len(changes) - MAX_CHANGES
        notes.append(f"{len(changes) - MAX_CHANGES} less severe changes omitted.")
    if integrity:
        out["integrity"] = integrity
    if notes:
        out["notes"] = notes
    return out


def _verdict(counts: Mapping[str, int], unchanged: int) -> str:
    if not any(counts.values()):
        return f"No change ({unchanged} items compared)."
    parts = [f"{counts[s]} {s}" for s in SEVERITIES if counts[s]]
    return (
        ", ".join(parts) + f" change{'s' if sum(counts.values()) != 1 else ''}; {unchanged} items unchanged."
    )


def _change(
    severity: str,
    kind: str,
    section: str,
    title: str,
    key: str,
    details: list[str],
    label: str | None = None,
) -> dict[str, Any]:
    name = label or key
    return {
        "severity": severity,
        "kind": kind,
        "section": section,
        "item": key,
        "what": f"{title}{' ' + name if name else ''}: {'; '.join(details)}",
    }


def _compare_item(
    section: str,
    title: str,
    key: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    metrics: Mapping[str, Any],
    cut: bool,
) -> dict[str, Any] | None:
    label = str((after or before or {}).get("label") or key)
    if before is None or after is None:
        if cut:
            return None
        item = after if before is None else before
        assert item is not None
        ok = item.get("ok")
        if before is None:
            sev, kind, what = (
                ("major", "added", "new and unhealthy") if ok is False else ("info", "added", "new")
            )
            if ok is None:
                sev = "minor"
            return _change(sev, kind, section, title, key, [what + _state(item)], label=label)
        if ok is True:
            sev, what = "major", "gone (was healthy)"
        elif ok is False:
            sev, what = "info", "gone (was unhealthy)"
        else:
            sev, what = "minor", "gone"
        return _change(sev, "removed", section, title, key, [what + _state(item)], label=label)

    found: list[tuple[str, str, str]] = []  # (severity, kind, detail)
    ok_b, ok_a = before.get("ok"), after.get("ok")
    if ok_b is True and ok_a is False:
        found.append(("major", "degraded", "healthy -> unhealthy"))
    elif ok_b is False and ok_a is True:
        found.append(("info", "recovered", "unhealthy -> healthy"))
    elif ok_b != ok_a:
        found.append(("minor", "changed", f"ok {ok_b} -> {ok_a}"))
    if before.get("state") != after.get("state"):
        # A state change that comes with an ok change is part of it (up -> down is the degradation).
        sev, kind = found[0][:2] if found else ("minor", "changed")
        found.append((sev, kind, f"state {before.get('state')} -> {after.get('state')}"))
    for field in sorted((set(before) | set(after)) - set(RESERVED)):
        vb, va = before.get(field), after.get(field)
        spec = metrics.get(field)
        if isinstance(spec, Mapping) and _is_number(vb) and _is_number(va):
            m = _metric_change(field, float(vb), float(va), spec)
            if m is not None:
                found.append(m)
        elif vb != va:
            found.append(("minor", "changed", f"{field} {vb} -> {va}"))
    if not found:
        return None
    found.sort(key=lambda f: _ORDER[f[0]])
    sev, kind = found[0][0], found[0][1]
    return _change(sev, kind, section, title, key, [f[2] for f in found], label=label)


def _metric_change(field: str, vb: float, va: float, spec: Mapping[str, Any]) -> tuple[str, str, str] | None:
    lower = spec.get("better") != "higher"
    tol = float(spec.get("tolerance") or 0)
    delta = va - vb
    if abs(delta) <= tol:
        return None
    worse = delta > 0 if lower else delta < 0
    text = f"{field} {_num(vb)} -> {_num(va)}"
    limit = spec.get("limit")
    if _is_number(limit):
        lim = float(limit)

        def beyond(v: float) -> bool:
            return v > lim if lower else v < lim

        if worse and beyond(va) and not beyond(vb):
            return ("major", "degraded", f"{text} (past the limit {_num(lim)})")
        if not worse and beyond(vb) and not beyond(va):
            return ("info", "recovered", f"{text} (back within the limit {_num(lim)})")
    return ("minor", "worse", text) if worse else ("info", "improved", text)


def _is_number(v: Any) -> TypeGuard[int | float]:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _state(item: Mapping[str, Any]) -> str:
    return f" ({item['state']})" if item.get("state") else ""


# --- tools -------------------------------------------------------------------------------------------

TakeFn = Callable[[Context, str], Awaitable["Snapshot | dict[str, Any]"]]
BLOB_DOC = "A snapshot blob from {prefix}_snapshot, exactly as it was returned (JSON object or JSON text)"


def register_snapshot_tools(
    reg: "ToolRegistrar",
    take: TakeFn,
    *,
    product: str,
    covers: str,
    site_doc: str,
) -> None:
    """Register `<prefix>_snapshot(site)` and `<prefix>_compare_snapshot(before, after)` (read tier) from
    one `take(ctx, site)` that returns a Snapshot (or its blob). See docs/SNAPSHOTS.md."""
    prefix = reg.prefix

    async def _take(ctx: Context, site: str) -> dict[str, Any]:
        snap = await take(ctx, site)
        blob = snap.blob() if isinstance(snap, Snapshot) else dict(snap)
        if blob.get("prefix") != prefix:
            raise ValueError(f"take() returned a {blob.get('prefix')!r} snapshot on the {prefix} server")
        return blob

    @reg.read(
        title=f"{product} snapshot",
        name=f"{prefix}_snapshot",
        description=(
            f"Take a compact, versioned snapshot of {product} health for a pre/post change check: {covers}. "
            "Returns a JSON blob to keep in the conversation and pass, unchanged, to "
            f"{prefix}_compare_snapshot after the change; nothing is stored on the server. Volatile "
            "counters are left out, so two snapshots of an unchanged network compare equal."
        ),
    )
    async def snapshot_tool(
        ctx: Context, site: Annotated[str, Field(description=site_doc)] = ""
    ) -> ToolResult:
        return object_result(await _take(ctx, site), shape=False)

    @reg.read(
        title=f"Compare {product} snapshots",
        name=f"{prefix}_compare_snapshot",
        description=(
            f"Compare a {prefix}_snapshot blob from before a change with one from after it (or, when `after` "
            "is empty, a fresh snapshot of the same scope taken now) and name what changed, most severe "
            "first: major = got unhealthy (session down, metric past its limit, healthy item gone), minor = "
            "changed but not clearly bad, info = recovered or improved. Unchanged items are only counted."
        ),
    )
    async def compare_tool(
        ctx: Context,
        before: Annotated[str | dict[str, Any], Field(description=BLOB_DOC.format(prefix=prefix))],
        after: Annotated[
            str | dict[str, Any] | None,
            Field(description=BLOB_DOC.format(prefix=prefix) + "; empty = take one now"),
        ] = None,
    ) -> ToolResult:
        b = parse(before, prefix=prefix, what="before")
        if after in (None, "", {}):
            site = str((b.get("scope") or {}).get("site") or "")
            a: Mapping[str, Any] = await _take(ctx, site)
        else:
            assert after is not None
            a = parse(after, prefix=prefix, what="after")
        return object_result(compare(b, a, prefix=prefix))
