"""A tiny server built on the core, used by the tests: login-token auth, offset/limit paging, read,
write and dangerous tools, and a spec source."""

from typing import Annotated, Any

import httpx
from fastmcp import Context, FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.tools import ToolResult
from pydantic import Field

from stutters_mcp_core import (
    BaseServerSettings,
    Confirm,
    Plan,
    ResponseFormat,
    ServerSpec,
    Tier,
    ToolRegistrar,
    build_server,
    get_client,
    list_result,
    object_result,
    preview_or_apply,
    server_config,
)
from stutters_mcp_core.http import ApiClient, LoginTokenAuth, OffsetLimit, Token
from stutters_mcp_core.prompts import tool_ref
from stutters_mcp_core.spec import SpecSource

BASE = "https://demo.test"

SPEC_DOC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"version": "9.9"},
    "paths": {
        "/api/items": {
            "get": {
                "operationId": "listItems",
                "summary": "List inventory items",
                "tags": ["inventory"],
                "parameters": [{"name": "name", "in": "query", "schema": {"type": "string"}}],
                "responses": {
                    "200": {
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ItemList"}}}
                    }
                },
            },
            "post": {
                "operationId": "createItem",
                "summary": "Create an item",
                "requestBody": {
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}}
                },
                "responses": {"201": {"description": "created"}},
            },
        },
        "/api/items/{id}": {
            "get": {"operationId": "getItem", "summary": "Get one item", "responses": {"200": {}}}
        },
    },
    "components": {
        "schemas": {
            "Item": {
                "type": "object",
                "description": "An inventory item",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "maxLength": 32},
                    "kind": {"type": "string", "enum": ["router", "switch"]},
                    "secret": {"type": "string"},
                },
            },
            "ItemList": {
                "type": "object",
                "properties": {"items": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}}},
            },
        }
    },
}


class DemoSettings(BaseServerSettings):
    model_config = server_config("DEMO_")


SPEC = ServerSpec(
    name="demo-mcp-server",
    prefix="demo",
    product="Demo",
    env_prefix="DEMO_",
    instructions="Tools for the demo inventory.",
)


class DemoApp:
    def __init__(self, settings: DemoSettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        async def login(client: ApiClient) -> Token:
            resp = await client.send_raw("POST", "/api/login", json={"u": settings.username})
            resp.raise_for_status()
            return Token({"X-Token": resp.json()["token"]}, Token.expiring_in(3600))

        self.api = ApiClient(
            settings.url,
            product="Demo",
            env_prefix="DEMO_",
            auth=LoginTokenAuth(login),
            verify=settings.verify,
            retries=settings.retries,
            transport=transport,
        )
        self.pager = OffsetLimit(items="items", total="total", max_page=2)

    async def aclose(self) -> None:
        await self.api.aclose()

    async def status(self) -> dict[str, Any]:
        info: dict[str, Any] = await self.api.get("/api/version")
        return {"version": info["version"]}


def register(reg: ToolRegistrar) -> None:
    @reg.read(title="List items")
    async def demo_list_items(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
        response_format: ResponseFormat = "json",
    ) -> ToolResult:
        """List inventory items with their kind. Use offset/limit to page through results."""
        app: DemoApp = get_client(ctx)
        page = await app.pager.fetch_page(app.api, "/api/items", limit=limit, offset=offset)
        return list_result(page, fmt=response_format)

    @reg.read(title="Get item")
    async def demo_get_item(
        ctx: Context, item_id: Annotated[str, Field(description="Item id from demo_list_items")]
    ) -> ToolResult:
        """Get one inventory item by id, including all of its fields."""
        app: DemoApp = get_client(ctx)
        return object_result(await app.api.get(f"/api/items/{item_id}"))

    @reg.write(title="Create item")
    async def demo_create_item(
        ctx: Context,
        name: Annotated[str, Field(description="Item name")],
        confirm: Confirm = None,
    ) -> dict[str, Any]:
        """Create an inventory item. Preview-first: returns a confirm token before changing anything."""
        app: DemoApp = get_client(ctx)
        return await preview_or_apply(
            ctx,
            tool="demo_create_item",
            args={"name": name},
            confirm=confirm,
            plan=Plan(f"Create item {name}", {"name": name}),
            apply=lambda: app.api.post("/api/items", {"name": name}),
        )

    @reg.dangerous(title="Wipe items")
    async def demo_wipe(ctx: Context, confirm: Confirm = None) -> dict[str, Any]:
        """Delete every inventory item. Preview-first; irreversible once confirmed."""
        app: DemoApp = get_client(ctx)
        return await preview_or_apply(
            ctx,
            tool="demo_wipe",
            args={},
            confirm=confirm,
            plan=Plan("Delete ALL items", side_effects=["irreversible"]),
            apply=lambda: app.api.delete("/api/items"),
        )

    @reg.prompt(title="Audit items", uses=["demo", "other"], tools=["demo_get_item"])
    def demo_audit_items(
        kind: Annotated[str, Field(description="Item kind to audit: router or switch")],
        max_items: Annotated[int, Field(description="Stop after this many items")] = 20,
    ) -> str:
        """Audit the inventory: list items of one kind, inspect each, and cross-check another server."""
        return (
            f"1. Call {tool_ref('demo_list_items')} and keep the {kind} items (at most {max_items}).\n"
            f"2. For each, call {tool_ref('demo_get_item')}.\n"
            f"3. Compare with {tool_ref('other_list_things')} on the other server, if it is connected."
        )

    @reg.prompt(title="Clean up items", tier=Tier.WRITE)
    def demo_cleanup_items() -> str:
        """Remove test items: find them, then create replacements, previewing every change first."""
        return (
            f"List with {tool_ref('demo_list_items')}, then use {tool_ref('demo_create_item')} "
            "(preview first)."
        )


def make_settings(**kw: Any) -> DemoSettings:
    base: dict[str, Any] = {"url": BASE, "username": "u", "password": "p", "retries": 1}
    base.update(kw)
    return DemoSettings(_env_file=None, **base)  # type: ignore[call-arg]


def create_server(settings: DemoSettings, *, auth: AuthProvider | None = None) -> FastMCP[Any]:
    async def load_spec(app: DemoApp) -> dict[str, Any]:
        return SPEC_DOC

    return build_server(
        SPEC,
        settings,
        client_factory=DemoApp,
        register=[register],
        status=DemoApp.status,
        spec_sources=lambda app: [
            SpecSource("main", load_spec, client=lambda a: a.api, path_params=lambda a: {"tenant": "t1"})
        ],
        auth=auth,
    )
