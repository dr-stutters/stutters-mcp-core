"""Lazily load, index and disk-cache a product's OpenAPI specs.

A server describes where its specs come from with SpecSource objects:

    SpecSource(
        name="openapi",
        load=lambda app: app.openapi.get("/api/v3/api-docs"),     # one doc, or a list of docs
        client=lambda app: app.openapi,                            # ApiClient used by api_call
        version=lambda app: app.version(),                         # cache key; optional
    )

`app` is whatever the server's client factory returned (the lifespan "client" object).
Bundled specs (shipped inside a server's wheel) use `load=bundled("catc_mcp", "specs/catc-3.2.json")`.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from stutters_mcp_core.http.client import ApiClient
from stutters_mcp_core.spec.index import INDEX_FORMAT, SpecIndex, build_index

log = logging.getLogger(__name__)

Doc = dict[str, Any]


@dataclass
class SpecSource:
    name: str
    load: Callable[[Any], Awaitable[Doc | list[Doc]]]
    client: Callable[[Any], ApiClient]
    version: Callable[[Any], Awaitable[str]] | None = None
    path_prefix: str = ""
    description: str = ""
    # Defaults for {placeholders} in paths given to api_call, e.g. {"domainUUID": app.domain_uuid}.
    path_params: Callable[[Any], dict[str, str]] | None = None


def bundled(package: str, resource: str) -> Callable[[Any], Awaitable[Doc]]:
    """Loader for a spec file shipped as package data (.json or .json.gz)."""

    async def load(_app: Any) -> Doc:
        data = resources.files(package).joinpath(resource).read_bytes()
        if resource.endswith(".gz"):
            data = gzip.decompress(data)
        doc: Doc = json.loads(data)
        return doc

    return load


class SpecStore:
    def __init__(self, app: Any, sources: list[SpecSource], cache_dir: Path | None, cache_scope: str) -> None:
        self.app = app
        self.sources = {s.name: s for s in sources}
        self._cache_dir = cache_dir
        self._scope = cache_scope  # e.g. base URL, so different appliances don't share a cache
        self._index: SpecIndex | None = None
        self._lock = asyncio.Lock()
        self.errors: dict[str, str] = {}

    @property
    def default_source(self) -> str:
        return next(iter(self.sources))

    async def get(self, *, refresh: bool = False) -> SpecIndex:
        if self._index is not None and not refresh:
            return self._index
        async with self._lock:
            if self._index is None or refresh:
                self._index = await self._load_all(refresh)
            return self._index

    async def _load_all(self, refresh: bool) -> SpecIndex:
        merged = SpecIndex(version="")
        versions = []
        self.errors = {}
        for src in self.sources.values():
            try:
                idx = await self._load_one(src, refresh)
            except Exception as e:  # one broken surface shouldn't hide the others
                log.warning("Could not load %s spec: %s", src.name, e)
                self.errors[src.name] = str(e)[:300]
                continue
            versions.append(f"{src.name}={idx.version}")
            merged.merge(idx)
        merged.version = ", ".join(versions) or "unavailable"
        return merged

    async def _load_one(self, src: SpecSource, refresh: bool) -> SpecIndex:
        version = await src.version(self.app) if src.version else "unversioned"
        cache_file = None
        if self._cache_dir is not None:
            key = f"{self._scope}|{src.name}|{version}|{INDEX_FORMAT}"
            cache_file = (
                self._cache_dir / f"spec-{src.name}-{hashlib.sha256(key.encode()).hexdigest()[:16]}.json"
            )
            if cache_file.exists() and not refresh:
                try:
                    return SpecIndex.from_json(json.loads(cache_file.read_text()))
                except (ValueError, KeyError, TypeError) as e:
                    log.warning("Ignoring unreadable spec cache %s: %s", cache_file, e)
        log.info("Fetching %s spec (%s)", src.name, version)
        docs = await src.load(self.app)
        idx = SpecIndex(version=version)
        for doc in docs if isinstance(docs, list) else [docs]:
            part = build_index(doc, source=src.name, path_prefix=src.path_prefix)
            idx.merge(part)
            if idx.version == "unversioned":
                idx.version = part.version
        log.info(
            "Indexed %s spec: %d operations, %d schemas", src.name, len(idx.operations), len(idx.schemas)
        )
        if cache_file is not None:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(idx.to_json(), separators=(",", ":")))
                tmp.replace(cache_file)
            except OSError as e:
                log.warning("Could not write spec cache %s: %s", cache_file, e)
        return idx
