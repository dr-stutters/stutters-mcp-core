"""Shape API payloads for LLM consumption: trim noise, mask secrets, cap size, render Markdown.

Every list tool should return `list_result(page, fmt=...)`, every get tool `object_result(obj, ...)`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any, Literal

from fastmcp.tools import ToolResult

from stutters_mcp_core.http.paging import Page

ResponseFormat = Literal["json", "markdown"]

_max_chars = 40_000

# Keys whose values are secrets on one product or another (RADIUS/TACACS shared secrets, SNMP
# communities, PSKs, passwords, private keys). Values are masked unless a tool opts out.
SECRET_KEY_RE = re.compile(
    r"(password|passphrase|passwd|secret|psk|preshared|pre_shared|private_?key|enable_?pass|"
    r"auth_?key|authentication_?key|priv_?key|api_?key|shared_?key|encryption_?key|"
    r"message_?authenticator|^token$|auth_?token|access_?token|refresh_?token|x-auth|"
    r"snmp\w*community|community_?(string|name)|r[ow]_?community|(read|write)_?community)",
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


def is_secret_key(key: str) -> bool:
    return bool(SECRET_KEY_RE.search(key))


def mask_all(v: Any) -> Any:
    """Mask every scalar leaf (keeps structure so the model sees what kind of value is there)."""
    if isinstance(v, dict):
        return {k: mask_all(x) for k, x in v.items()}
    if isinstance(v, list):
        return [mask_all(x) for x in v]
    return v if isinstance(v, bool) or v is None else MASK


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
            if mask_secrets and is_secret_key(str(k)) and not isinstance(val, bool):
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
    if not rows:
        return "_No results._"
    cols = list(columns or [])
    if not cols:
        for r in rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
        cols = cols[:max_cols]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(_cell(r.get(c, "")) for c in cols) + " |" for r in rows]
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
    meta = Page(items, page.offset, page.total).meta()
    if page.total is None and truncated:
        meta["next_offset"] = page.offset + len(items)
    if truncated:
        meta["note"] = f"Output truncated to fit the size limit; {hint}."
    if extra:
        meta.update(extra)
    if fmt == "markdown":
        header = ", ".join(f"{k}: {v}" for k, v in meta.items())
        rows = [i if isinstance(i, dict) else {"value": i} for i in items]
        return ToolResult(content=f"{header}\n\n{to_markdown(rows, columns=columns)}")
    payload = {**meta, "results": items}
    return ToolResult(content=dumps(payload), structured_content=payload)


def object_result(obj: Any, *, fmt: ResponseFormat = "json", shape: bool = True) -> ToolResult:
    data = compact(obj) if shape else obj
    text = dumps(data)
    if len(text) > _max_chars:
        cut = text[:_max_chars] + '..."[truncated: request fewer fields or a narrower object]"'
        return ToolResult(content=cut)
    if fmt == "markdown" and isinstance(data, dict):
        return ToolResult(content=kv_markdown(data))
    if isinstance(data, dict):
        return ToolResult(content=text, structured_content=data)
    return ToolResult(content=text)


def text_result(text: str) -> ToolResult:
    if len(text) > _max_chars:
        text = text[:_max_chars] + "\n...[truncated]"
    return ToolResult(content=text)
