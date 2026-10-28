"""Generic spec-driven tools every server gets: <prefix>_search_spec, <prefix>_get_definition and
<prefix>_api_call (GET-only unless writes are enabled; non-GET calls are preview-first)."""

# No `from __future__ import annotations`: FastMCP inspects these signatures at registration time.

import re
from typing import Annotated, Any, Literal
from urllib.parse import quote, unquote

from fastmcp import Context
from fastmcp.tools import ToolResult
from pydantic import Field

from stutters_mcp_core.audit import redact_value
from stutters_mcp_core.errors import InvalidRequest
from stutters_mcp_core.formatting import compact, dumps, object_result, text_result
from stutters_mcp_core.spec.store import SpecStore
from stutters_mcp_core.writes import Confirm, Plan, ToolRegistrar, preview_or_apply

# Relative API path: no scheme/host, no query string, no '..' segments.
_PATH_RE = re.compile(r"^/?[A-Za-z0-9_\-/.{}:~%=,@+]+$")


def get_specs(ctx: Context) -> SpecStore:
    store: SpecStore = ctx.lifespan_context["specs"]
    return store


def fill_path(path: str, values: dict[str, str]) -> str:
    """Substitute known {placeholders} (URL-quoted); unknown ones are left for validate_path to reject."""

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        return quote(str(values[name]), safe="") if name in values else m.group(0)

    return re.sub(r"\{([A-Za-z0-9_]+)\}", sub, path)


def validate_path(path: str) -> str:
    path = path.strip()
    decoded = unquote(path)
    if (
        not _PATH_RE.match(path)
        or ".." in decoded.split("/")
        or "//" in decoded
        or "\\" in decoded
        or re.search(r"%(2e|2f|5c)", path, re.IGNORECASE)
    ):
        raise InvalidRequest(
            f"Invalid path {path!r}: give a path relative to the API base (as shown by the search tool), "
            "without scheme, host or query string (use `params` for the query)."
        )
    if "{" in path:
        raise InvalidRequest(f"Path {path!r} still has a {{placeholder}}; substitute the real id.")
    return "/" + path.lstrip("/")


def register_spec_tools(
    reg: ToolRegistrar,
    *,
    product: str,
    api_call: bool = True,
    search_example: str | None = None,
    path_example: str | None = None,
) -> None:
    """`search_example` (e.g. "network device group") and `path_example` (e.g. "/api/v1/endpoint/{id}")
    make the descriptions product-specific; without them the text stays neutral."""
    query_desc = (
        f"Keywords, e.g. {search_example!r}"
        if search_example
        else "Keywords naming the resource or action you need"
    )
    path_desc = (
        f"('{path_example}')" if path_example else "(as listed by the search tool, e.g. '/<resource>/{id}')"
    )

    prefix = reg.prefix
    settings = reg.settings

    _DESC_SEARCH_SPEC = (
        f"Search the {product} OpenAPI spec for operations (method + path) and schemas. Use it to find "
        f"endpoints for {prefix}_api_call or exact field names and enums."
    )

    @reg.read(
        title=f"Search the {product} API spec", name=f"{prefix}_search_spec", description=_DESC_SEARCH_SPEC
    )
    async def search_spec(
        ctx: Context,
        query: Annotated[str, Field(description=query_desc)],
        source: Annotated[str | None, Field(description="Limit to one API surface/spec (see status)")] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        store = get_specs(ctx)
        idx = await store.get()
        hits = idx.search(query, limit=limit, source=source)
        out: dict[str, Any] = {"query": query, "results": hits}
        if not hits:
            out["hint"] = "No match: try fewer or different keywords (singular nouns work best)."
        if store.errors:
            out["unavailable_specs"] = store.errors
        return out

    _DESC_GET_DEFINITION = (
        f"Describe a {product} API operation (parameters, request fields, required fields) or a schema "
        f"(fields with types and enums). Names come from {prefix}_search_spec."
    )

    @reg.read(
        title=f"Describe a {product} API path or schema",
        name=f"{prefix}_get_definition",
        description=_DESC_GET_DEFINITION,
    )
    async def get_definition(
        ctx: Context,
        name: Annotated[
            str,
            Field(description=f"A schema name, a path {path_desc}, or 'METHOD /path'"),
        ],
        source: Annotated[str | None, Field(description="API surface/spec to look in")] = None,
        examples: Annotated[
            bool,
            Field(
                description="Also return the spec's request/response examples and full parameter and "
                "operation descriptions (larger; use when required fields or filter syntax are unclear)"
            ),
        ] = False,
    ) -> ToolResult:
        idx = await get_specs(ctx).get()
        return object_result(idx.definition(name, source=source, examples=examples), shape=False)

    if not api_call:
        return

    async def _call(
        ctx: Context, method: str, path: str, params: dict[str, Any] | None, body: Any, source: str | None
    ) -> Any:
        store = get_specs(ctx)
        src_name = source or store.default_source
        if src_name not in store.sources:
            raise InvalidRequest(f"Unknown source {src_name!r}; choose from {', '.join(store.sources)}")
        src = store.sources[src_name]
        path = fill_path(path, src.path_params(store.app) if src.path_params else {})
        return await src.client(store.app).request(method, validate_path(path), params=params, json=body)

    surfaces_note = "API surface to call (default: the main one; see status for the list)"

    if settings.mcp_enable_writes and settings.mcp_enable_dangerous:
        _DESC_API_CALL_RW = (
            f"Call any {product} REST endpoint (escape hatch for things no other tool covers). GET runs "
            f"immediately; other methods are preview-first: the first call returns a preview and a confirm "
            f"token, nothing changes until you repeat the call with confirm. Find paths with "
            f"{prefix}_search_spec and bodies with {prefix}_get_definition. Secret values (passwords, "
            f"pre-shared keys, communities) are masked in the preview and the result."
        )

        @reg.dangerous(
            title=f"Raw {product} API call",
            name=f"{prefix}_api_call",
            description=_DESC_API_CALL_RW,
        )
        async def api_call_rw(
            ctx: Context,
            method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"],
            path: Annotated[str, Field(description="Path relative to the API base, ids substituted")],
            params: Annotated[dict[str, Any] | None, Field(description="Query parameters")] = None,
            body: Annotated[Any, Field(description="JSON request body (POST/PUT/PATCH)")] = None,
            source: Annotated[str | None, Field(description=surfaces_note)] = None,
            confirm: Confirm = None,
        ) -> ToolResult:
            if method == "GET":
                return text_result(
                    dumps(compact(await _call(ctx, method, path, params, body, source), keep_empty=True))
                )
            store = get_specs(ctx)
            src = store.sources.get(source or store.default_source)
            filled = fill_path(path, src.path_params(store.app) if src and src.path_params else {})
            plan = Plan(
                f"{method} {validate_path(filled)}",
                # Masked: a body can carry a pre-shared key, password or community (the preview is shown to
                # the user and logged by the client); the confirm call still sends the real values.
                details={
                    k: redact_value(v)
                    for k, v in {"params": params, "body": body, "source": source}.items()
                    if v
                },
                blast_unknown="a raw API call: the server can't tell what it touches",
            )
            result = await preview_or_apply(
                ctx,
                tool=f"{prefix}_api_call",
                args={"method": method, "path": path, "params": params, "body": body, "source": source},
                confirm=confirm,
                plan=plan,
                apply=lambda: _call(ctx, method, path, params, body, source),
            )
            if "result" in result:  # upstream answers often echo the body, secrets included
                result["result"] = redact_value(result["result"])
            return text_result(dumps(result))

    else:
        _DESC_API_CALL_RO = (
            f"GET any {product} REST endpoint (escape hatch for things no other tool covers; this server "
            f"allows raw GETs only; raw writes need MCP_ENABLE_DANGEROUS). Secret values are masked. "
            f"Find paths with {prefix}_search_spec."
        )

        @reg.read(title=f"Raw {product} API GET", name=f"{prefix}_api_call", description=_DESC_API_CALL_RO)
        async def api_call_ro(
            ctx: Context,
            path: Annotated[str, Field(description="Path relative to the API base, ids substituted")],
            params: Annotated[dict[str, Any] | None, Field(description="Query parameters")] = None,
            source: Annotated[str | None, Field(description=surfaces_note)] = None,
        ) -> ToolResult:
            return text_result(
                dumps(compact(await _call(ctx, "GET", path, params, None, source), keep_empty=True))
            )
