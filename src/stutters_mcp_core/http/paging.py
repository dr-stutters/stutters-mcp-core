"""One `fetch_page(client, path, limit=, offset=)` interface over each product's paging style.

    OffsetLimit(items="items", total="paging.count", max_page=1000)          # FMC
    OffsetLimit(items="response", one_based=True, max_page=500)             # Catalyst Center
    PageNumber(items="SearchResult.resources", total="SearchResult.total")  # ISE ERS
    NoPaging(items=None)                                                    # CML (full collections)

Tools always speak 0-based `offset` + `limit`; pagers translate. One pager can serve endpoints with
different page-size caps: pass `max_page=` per call (e.g. Catalyst Center issues cap at 25, events at 20):

    page = await app.pager.fetch_page(app.api, "/dna/data/api/v1/assuranceIssues", limit=50, max_page=25)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from stutters_mcp_core.errors import UpstreamError

if TYPE_CHECKING:
    from stutters_mcp_core.http.client import ApiClient

log = logging.getLogger(__name__)

Extractor = str | Callable[[Any], Any] | None


def dig(data: Any, path: Extractor) -> Any:
    """Follow a dotted path ("paging.count") or call an extractor; None path returns data itself."""
    if path is None:
        return data
    if callable(path):
        return path(data)
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


@dataclass
class Page:
    items: list[Any]
    offset: int
    total: int | None = None  # None when the API doesn't say
    more: bool = False  # with total None: the fetch stopped at `limit`, so more items may exist
    # partial: the items were derived from a collection that was cut (a `fetch_all` cap), so totals,
    # counts and "not found" answers built from them may be wrong. `note` says so in the tool output.
    partial: bool = False
    note: str | None = None

    @property
    def next_offset(self) -> int | None:
        end = self.offset + len(self.items)
        if self.total is not None:
            return end if end < self.total else None
        return end if self.more else None

    @property
    def complete(self) -> bool:
        """True when nothing exists beyond this page."""
        return self.next_offset is None

    def meta(self) -> dict[str, Any]:
        m: dict[str, Any] = {"offset": self.offset, "returned": len(self.items)}
        if self.total is not None:
            m["total"] = self.total
        if self.next_offset is not None:
            m["next_offset"] = self.next_offset
        if self.partial:
            m["partial"] = True
        if self.note:
            m["note"] = self.note
        return m


class Pager(Protocol):
    async def fetch_page(
        self,
        client: ApiClient,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int,
        offset: int = 0,
        max_page: int | None = None,
    ) -> Page: ...


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _page_cap(override: int | None, default: int) -> int:
    """The per-request page size: `max_page=` from the call (an endpoint's own cap) or the pager's."""
    if override is None:
        return default
    if override < 1:
        raise ValueError("max_page must be at least 1")
    return override


def _as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@dataclass
class OffsetLimit:
    items: Extractor = "items"
    total: Extractor = None
    offset_param: str = "offset"
    limit_param: str = "limit"
    max_page: int = 1000
    one_based: bool = False

    async def fetch_page(
        self,
        client: ApiClient,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int,
        offset: int = 0,
        max_page: int | None = None,
    ) -> Page:
        out: list[Any] = []
        total: int | None = None
        query = dict(params or {})
        cap = _page_cap(max_page, self.max_page)
        while len(out) < limit:
            want = min(cap, limit - len(out))
            query[self.offset_param] = offset + len(out) + (1 if self.one_based else 0)
            query[self.limit_param] = want
            data = await client.get(path, query)
            batch = _as_list(dig(data, self.items))
            total = _as_int(dig(data, self.total)) if self.total is not None else None
            out.extend(batch)
            if len(batch) < want or (total is not None and offset + len(out) >= total):
                if total is None and len(out) < limit:
                    total = offset + len(out)  # short page: we've seen the end
                break
        return Page(out[:limit], offset, total, more=total is None and len(out) >= limit)


@dataclass
class PageNumber:
    items: Extractor = "items"
    total: Extractor = None
    page_param: str = "page"
    size_param: str = "size"
    max_size: int = 100
    first_page: int = 1

    async def fetch_page(
        self,
        client: ApiClient,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int,
        offset: int = 0,
        max_page: int | None = None,
    ) -> Page:
        size = _page_cap(max_page, self.max_size)
        page = offset // size
        skip = offset % size
        out: list[Any] = []
        total: int | None = None
        query = dict(params or {})
        while len(out) < limit:
            query[self.page_param] = page + self.first_page
            query[self.size_param] = size
            data = await client.get(path, query)
            batch = _as_list(dig(data, self.items))
            if self.total is not None:
                total = _as_int(dig(data, self.total))
            out.extend(batch[skip:])
            skip = 0
            page += 1
            if len(batch) < size or (total is not None and page * size >= total):
                if total is None:
                    total = offset + len(out)
                break
        return Page(out[:limit], offset, total, more=total is None and len(out) >= limit)


@dataclass
class NoPaging:
    """The API returns the whole collection; slice it client-side."""

    items: Extractor = None

    async def fetch_page(
        self,
        client: ApiClient,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int,
        offset: int = 0,
        max_page: int | None = None,
    ) -> Page:
        _page_cap(max_page, 1)  # validated like the other pagers; the API returns everything anyway
        data = await client.get(path, params)
        everything = _as_list(dig(data, self.items))
        return Page(everything[offset : offset + limit], offset, len(everything))


PARTIAL_NOTE = (
    "Partial results: the source collection was larger than the read cap, so this answer may be incomplete."
)


class PartialCollection(UpstreamError):
    """`fetch_all(on_partial="raise")` hit its cap: an answer built from the items would be wrong."""


def page_of(
    items: list[Any],
    *,
    limit: int,
    offset: int = 0,
    note: str | None = None,
    partial: bool = False,
    source: Page | None = None,
) -> Page:
    """Slice an in-memory list (for derived/filtered results).

    Pass `source=` (the `fetch_all` page the items came from) to carry its partial flag and note, or
    `partial=` / `note=` directly; `list_result` renders both."""
    if source is not None:
        partial = partial or source.partial or not source.complete
        if note is None:
            note = source.note
    if partial and note is None:
        note = PARTIAL_NOTE
    return Page(items[offset : offset + limit], offset, len(items), partial=partial, note=note)


async def fetch_all(
    pager: Pager,
    client: ApiClient,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    cap: int = 10_000,
    max_page: int | None = None,
    on_partial: Literal["note", "raise"] = "note",
) -> Page:
    """Every item (up to `cap`), for tools that aggregate or search a whole collection.

    When the collection is larger than `cap` the page comes back with `complete=False`,
    `partial=True` and a `note` that `list_result` / `object_result(source=page)` put in the output.
    Pass `on_partial="raise"` where the items feed a safety check (a delete's "is it still
    assigned / in use?" count): a cut collection then raises `PartialCollection` instead of
    letting the check pass on half the data."""
    if max_page is None:
        page = await pager.fetch_page(client, path, params, limit=cap, offset=0)
    else:
        page = await pager.fetch_page(client, path, params, limit=cap, offset=0, max_page=max_page)
    if not page.complete:
        log.warning("fetch_all(%s) hit the %d-item cap; results are partial", path, cap)
        size = f"{page.total} items" if page.total is not None else f"more than {cap} items"
        if on_partial == "raise":
            raise PartialCollection(
                f"{path} has {size}, more than the {cap}-item read cap, so the check this tool makes "
                "would run on part of the data. Narrow the request (a filter or a smaller scope) and "
                "try again."
            )
        page.partial = True
        page.note = (
            f"Partial results: {path} has {size}; only the first {len(page.items)} were read "
            f"(cap {cap}), so totals and 'not found' answers may be incomplete."
        )
    return page
