"""One `fetch_page(client, path, limit=, offset=)` interface over each product's paging style.

    OffsetLimit(items="items", total="paging.count", max_page=1000)          # FMC
    OffsetLimit(items="response", one_based=True, max_page=500)             # Catalyst Center
    PageNumber(items="SearchResult.resources", total="SearchResult.total")  # ISE ERS
    NoPaging(items=None)                                                    # CML (full collections)

Tools always speak 0-based `offset` + `limit`; pagers translate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from stutters_mcp_core.http.client import ApiClient

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

    @property
    def next_offset(self) -> int | None:
        end = self.offset + len(self.items)
        if self.total is not None:
            return end if end < self.total else None
        return None

    def meta(self) -> dict[str, Any]:
        m: dict[str, Any] = {"offset": self.offset, "returned": len(self.items)}
        if self.total is not None:
            m["total"] = self.total
        if self.next_offset is not None:
            m["next_offset"] = self.next_offset
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
    ) -> Page: ...


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


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
    ) -> Page:
        out: list[Any] = []
        total: int | None = None
        query = dict(params or {})
        while len(out) < limit:
            want = min(self.max_page, limit - len(out))
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
        return Page(out[:limit], offset, total)


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
    ) -> Page:
        size = self.max_size
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
        return Page(out[:limit], offset, total)


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
    ) -> Page:
        data = await client.get(path, params)
        everything = _as_list(dig(data, self.items))
        return Page(everything[offset : offset + limit], offset, len(everything))


def page_of(items: list[Any], *, limit: int, offset: int = 0) -> Page:
    """Slice an in-memory list (for derived/filtered results)."""
    return Page(items[offset : offset + limit], offset, len(items))


async def fetch_all(
    pager: Pager, client: ApiClient, path: str, params: dict[str, Any] | None = None, *, cap: int = 10_000
) -> list[Any]:
    """Every item (up to `cap`), for tools that need to aggregate or search a whole collection."""
    page = await pager.fetch_page(client, path, params, limit=cap, offset=0)
    return page.items
