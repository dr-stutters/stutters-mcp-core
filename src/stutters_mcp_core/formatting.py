"""Shape API payloads for LLM consumption: trim noise, mask secrets, cap size, render Markdown.

Every list tool should return `list_result(page, fmt=...)`, every get tool `object_result(obj, ...)`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any, Literal

from fastmcp.tools import ToolResult

from stutters_mcp_core.http.paging import PARTIAL_NOTE, Page

ResponseFormat = Literal["json", "markdown"]

_max_chars = 40_000

# Keys whose values are secrets on one product or another (RADIUS/TACACS shared secrets, SNMP
# communities, PSKs, passwords, private keys, FTD registration keys and NAT ids). Values are masked unless
# a tool opts out.
SECRET_KEY_RE = re.compile(
    r"(password|passphrase|passwd|secret|psk|preshared|pre_shared|private_?key|enable_?pass|"
    r"auth_?key|authentication_?key|md5_?key|key_?string|priv_?key|api_?key|shared_?key|encryption_?key|"
    r"message_?authenticator|^token$|auth_?token|access_?token|refresh_?token|x-auth|"
    r"snmp\w*community|community_?(string|name)|r[ow]_?community|(read|write)_?community|"
    r"reg_?key|nat_?id)",
    re.IGNORECASE,
)
MASK = "********"
DEFAULT_DROP_KEYS = frozenset({"links", "link", "_links"})


def set_max_chars(n: int) -> None:
    """Called once by the server factory from MCP_MAX_RESPONSE_CHARS."""
    global _max_chars
    _max_chars = n


def max_chars() -> int:
    return _max_chars


# Keys that match SECRET_KEY_RE but hold flags or names, not secrets. Extend per product with
# `NOT_SECRET_KEYS.update({...})` at import time if needed.
NOT_SECRET_KEYS: set[str] = {"passwordIDStore", "enableMultiSecret", "passwordPolicy", "secretStorage"}
# Exact key names that hold secrets on one product but don't match SECRET_KEY_RE (a WLC's `key` or
# `wepKey`). Extend per product with `EXTRA_SECRET_KEYS.update({...})` at import time.
EXTRA_SECRET_KEYS: set[str] = set()
_FLAG_VALUES = {"true", "false", "yes", "no", "enabled", "disabled", "on", "off"}


def is_secret_key(key: str) -> bool:
    if key in EXTRA_SECRET_KEYS:
        return True
    return key not in NOT_SECRET_KEYS and bool(SECRET_KEY_RE.search(key))


def _is_flag(v: Any) -> bool:
    return isinstance(v, bool) or (isinstance(v, str) and v.strip().lower() in _FLAG_VALUES)


def mask_secrets(data: Any) -> Any:
    """Mask secret values anywhere in `data` without dropping or reshaping anything else."""
    return compact(data, drop=(), keep_empty=True)


def mask_all(v: Any) -> Any:
    """Mask every scalar leaf (keeps structure so the model sees what kind of value is there)."""
    if isinstance(v, dict):
        return {k: mask_all(x) for k, x in v.items()}
    if isinstance(v, list):
        return [mask_all(x) for x in v]
    return v if _is_flag(v) or v is None else MASK


def compact(
    data: Any,
    *,
    drop: Iterable[str] = DEFAULT_DROP_KEYS,
    mask_secrets: bool = True,
    keep_empty: bool = False,
) -> Any:
    """Recursively drop noise keys and empty values (None, "", [], {}), and mask secret values."""
    drop_set = frozenset(drop)

    def walk(v: Any) -> Any:
        if isinstance(v, list):
            return [walk(x) for x in v]
        if not isinstance(v, dict):
            return v
        out: dict[str, Any] = {}
        for k, val in v.items():
            if k in drop_set:
                continue
            if mask_secrets and is_secret_key(str(k)) and not _is_flag(val):
                if val in (None, "", [], {}):
                    if keep_empty:
                        out[k] = val
                    continue
                out[k] = mask_all(val)
                continue
            val = walk(val)
            if not keep_empty and val in (None, "", [], {}):
                continue
            out[k] = val
        return out

    return walk(data)


def pick(obj: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    """Keep only `fields` (dotted paths allowed: "paging.count" -> {"paging.count": ...})."""
    out: dict[str, Any] = {}
    for f in fields:
        if f in obj:
            out[f] = obj[f]
            continue
        cur: Any = obj
        for part in f.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is not None:
            out[f] = cur
    return out


def _cell(v: Any) -> str:
    if isinstance(v, dict):
        v = (
            v.get("name")
            or v.get("label")
            or v.get("display")
            or v.get("id")
            or json.dumps(v, separators=(",", ":"), default=str)
        )
    elif isinstance(v, list):
        v = ", ".join(_cell(x) for x in v)
    return str(v).replace("|", "\\|").replace("\n", " ")


def to_markdown(rows: list[dict[str, Any]], *, columns: list[str] | None = None, max_cols: int = 10) -> str:
    """A Markdown table. Without `columns`, the first `max_cols` keys become columns (`id` is always
    kept when the rows have one) and a closing line names the columns left out, so the model knows
    to ask for `response_format=json` instead of assuming the table is the whole record."""
    if not rows:
        return "_No results._"
    cols = list(columns or [])
    hidden: list[str] = []
    if not cols:
        every: list[str] = []
        for r in rows:
            for k in r:
                if k not in every:
                    every.append(k)
        cols = every[:max_cols]
        if "id" in every and "id" not in cols and max_cols > 0:
            cols = [*cols[: max_cols - 1], "id"]
        hidden = [c for c in every if c not in cols]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(_cell(r.get(c, "")) for c in cols) + " |" for r in rows]
    if hidden:
        lines += ["", f"(+{len(hidden)} columns not shown: {', '.join(hidden)}; use response_format=json)"]
    return "\n".join(lines)


def kv_markdown(obj: dict[str, Any]) -> str:
    return "\n".join(f"- **{k}**: {_cell(v)}" for k, v in obj.items())


def dumps(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), default=str)


def list_result(
    page: Page,
    *,
    fmt: ResponseFormat = "json",
    shape: bool = True,
    columns: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    hint: str = "use filters, `fields`, or a smaller limit",
) -> ToolResult:
    """Package a page of results with paging info, enforcing the response size budget."""
    items = [compact(r) for r in page.items] if shape else list(page.items)
    truncated = False
    while len(items) > 1 and len(dumps(items)) > _max_chars:
        items = items[: max(1, len(items) * 3 // 4)]
        truncated = True
    # Keep the source page's "more" signal (APIs without a total), and add our own truncation.
    meta = Page(items, page.offset, page.total, more=page.more or truncated, partial=page.partial).meta()
    # The page's own note (a `fetch_all` cap, a `page_of(note=)`) and our truncation note both show.
    notes = [
        n for n in (f"Output truncated to fit the size limit; {hint}." if truncated else "", page.note) if n
    ]
    if notes:
        meta["note"] = " ".join(notes)
    if extra:
        extra = dict(extra)
        # A tool's own note must not hide the truncation warning: keep both.
        if "note" in meta and "note" in extra and extra["note"] != meta["note"]:
            extra["note"] = f"{meta['note']} {extra['note']}"
        meta.update(extra)
    if fmt == "markdown":
        header = ", ".join(f"{k}: {v}" for k, v in meta.items())
        rows = [i if isinstance(i, dict) else {"value": i} for i in items]
        return ToolResult(content=f"{header}\n\n{to_markdown(rows, columns=columns)}")
    payload = {**meta, "results": items}
    return ToolResult(content=dumps(payload), structured_content=payload)


# Fields structural truncation never drops: they say what the object is.
_KEEP_FIELDS = frozenset({"id", "uuid", "name", "type", "kind", "partial", "note"})


def _fields(data: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    """Every field reachable through dict keys alone (not through list elements), deepest last."""
    out: list[tuple[tuple[str, ...], Any]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            p = (*path, str(k))
            if str(k) not in _KEEP_FIELDS:
                out.append((p, v))
            out += _fields(v, p)
    return out


def _drop(data: dict[str, Any], path: tuple[str, ...]) -> None:
    cur: Any = data
    for part in path[:-1]:
        cur = cur[part]
    del cur[path[-1]]


def _fits(data: Any) -> bool:
    return len(dumps(data)) <= _max_chars


def shrink_object(
    data: dict[str, Any], *, fetch_hints: dict[str, str] | None = None, note: str | None = None
) -> dict[str, Any]:
    """Drop whole fields, largest list first, until `data` fits the size budget.

    Returns the smaller object (still valid JSON, every kept field intact) plus `truncated_fields`:
    per dropped field its dotted path, size, and (from `fetch_hints`, keyed by dotted path or
    top-level name) the tool or argument that fetches it; and a `note` (`note` first, if given).
    The result can still be over budget when one protected field alone is too big."""
    hints = fetch_hints or {}
    out = json.loads(dumps(data))  # a deep copy we can cut, with exotic values already stringified
    # The object's own note (a tool warning, a partial-collection note) stays in front of ours.
    own = out.get("note") if isinstance(out.get("note"), str) else None
    parts = [own, note if note and not (own and note in own) else None, _SHRINK_NOTE]
    final_note = " ".join(p for p in parts if p)
    dropped: list[dict[str, Any]] = []

    def framed() -> dict[str, Any]:
        return {**out, "truncated_fields": dropped, "note": final_note}

    while not _fits(framed()):
        fields = _fields(out)
        if not fields:
            break
        # Lists first (the usual culprit: rules, members, interfaces), then anything else; biggest first.
        path, value = max(fields, key=lambda f: (isinstance(f[1], list), len(dumps(f[1]))))
        entry: dict[str, Any] = {"field": ".".join(path), "chars": len(dumps(value))}
        if isinstance(value, list):
            entry["items"] = len(value)
        hint = hints.get(entry["field"]) or hints.get(path[0])
        if hint:
            entry["fetch"] = hint
        _drop(out, path)
        dropped.append(entry)
    return framed()


_SHRINK_NOTE = (
    "Object too large for one response: the fields named in truncated_fields were left out whole; every "
    "other field is complete. Fetch them with the tool or argument named per field, or narrow the request."
)


def object_result(
    obj: Any,
    *,
    fmt: ResponseFormat = "json",
    shape: bool = True,
    note: str | None = None,
    partial: bool = False,
    source: Page | None = None,
    fetch_hints: dict[str, str] | None = None,
) -> ToolResult:
    """Package one object, enforcing the response size budget.

    An object over the budget is cut structurally (`shrink_object`): the largest list fields are
    dropped whole, the result stays valid JSON with `structured_content`, and `truncated_fields`
    names what is missing (with `fetch_hints={"rules": "fmc_list_access_rules"}` saying where to get
    it). `note=` / `partial=` / `source=` (a `fetch_all` page the object was built from) add a
    `note` and `partial: true` so an aggregate built on a cut collection says so."""
    data = compact(obj) if shape else obj
    if source is not None:
        partial = partial or source.partial or not source.complete
        note = note or source.note
    if partial and not note:
        note = PARTIAL_NOTE
    if isinstance(data, dict) and (note or partial):
        data = dict(data)
        if partial:
            data["partial"] = True
        if note:
            data["note"] = f"{data['note']} {note}" if data.get("note") and data["note"] != note else note
    text = dumps(data)
    if len(text) > _max_chars:
        if isinstance(data, dict):
            data = shrink_object(data, fetch_hints=fetch_hints, note=note)
            text = dumps(data)
            if len(text) <= _max_chars:
                if fmt == "markdown":
                    return ToolResult(content=kv_markdown(data))
                return ToolResult(content=text, structured_content=data)
        elif isinstance(data, list):
            items = list(data)
            while len(items) > 1 and len(dumps(items)) > _max_chars - 400:
                items = items[: max(1, len(items) * 3 // 4)]
            if _fits(items):
                payload: dict[str, Any] = {
                    "results": items,
                    "returned": len(items),
                    "total": len(data),
                    "note": (
                        f"Output truncated to the first {len(items)} of {len(data)} items "
                        "to fit the size limit."
                    ),
                }
                return ToolResult(content=dumps(payload), structured_content=payload)
        # One indivisible value (a huge string or protected field): keep the reply valid JSON.
        wrap_note = "Object too large for one response and not divisible by field; narrow the request."
        cut = text[: max(0, _max_chars - 300)]
        payload = {"truncated": True, "chars": len(text), "text": cut}
        payload["note"] = f"{note} {wrap_note}" if note else wrap_note
        if partial:
            payload["partial"] = True
        # JSON-escaping the embedded text (quotes, backslashes) can grow it past the budget: trim more.
        while len(dumps(payload)) > _max_chars and cut:
            cut = cut[: max(0, len(cut) - (len(dumps(payload)) - _max_chars) - 1)]
            payload["text"] = cut
        return ToolResult(content=dumps(payload), structured_content=payload)
    if fmt == "markdown" and isinstance(data, dict):
        return ToolResult(content=kv_markdown(data))
    if isinstance(data, dict):
        return ToolResult(content=text, structured_content=data)
    return ToolResult(content=text)


def text_result(text: str) -> ToolResult:
    if len(text) > _max_chars:
        text = text[:_max_chars] + "\n...[truncated]"
    return ToolResult(content=text)
