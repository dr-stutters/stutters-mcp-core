"""Pending two-person changes from a chat client (C66): list them and cancel one.

Registered next to `<prefix>_review_change` whenever two-person confirm is on (MCP_TWO_PERSON_TOOLS or an
instance's `<PREFIX>_<NAME>_TWO_PERSON`). The list lives in the PreviewStore (process memory, the confirm
TTL, ADR-002 §4): a restart empties it, like every outstanding preview. There is deliberately no durable
store.

- `<prefix>_list_pending_changes`: what is waiting for a second person, by preview id (`pc_...`). The
  listing never shows confirm tokens; `<prefix>_review_change(<preview id>)` shows one change in full,
  including the token the approver confirms with.
- `<prefix>_cancel_pending_change(preview_id, reason)`: the requester withdraws, or an approver rejects.
  The token stops working at once and a `cancelled` audit event records who cancelled it and why.

Both need the `rw` role (like the change they act on) when per-user roles are configured.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context
from fastmcp.tools import ToolResult
from pydantic import Field

from stutters_mcp_core.errors import InvalidRequest
from stutters_mcp_core.formatting import ResponseFormat, list_result
from stutters_mcp_core.http.paging import Page
from stutters_mcp_core.inbound_auth import current_caller
from stutters_mcp_core.writes import _emit, get_previews

if TYPE_CHECKING:
    from stutters_mcp_core.writes import PreviewStore, ToolRegistrar, _Pending

MAX_REASON = 200


def _row(store: PreviewStore, token: str, pending: _Pending, caller: str) -> dict[str, Any]:
    tool, _, on_target = pending.tool.partition("@")
    preview = pending.preview or {}
    row: dict[str, Any] = {
        "preview_id": store.preview_id(token),
        "tool": tool,
        **({"instance": on_target} if on_target else {}),
        "action": preview.get("action"),
        "requested_by": pending.caller,
        "expires_in_seconds": store.remaining(pending),
    }
    if preview.get("blast_radius") is not None:
        row["blast_radius"] = preview["blast_radius"]
    elif preview.get("blast_unknown"):
        row["blast_unknown"] = preview["blast_unknown"]
    row["you_can_approve"] = pending.caller != caller
    return row


def register_pending_tools(reg: ToolRegistrar, prefix: str) -> None:
    list_name = f"{prefix}_list_pending_changes"
    cancel_name = f"{prefix}_cancel_pending_change"
    overrides: dict[str, str] = getattr(reg.mcp, "_stutters_role_tiers", {})
    # With per-user roles: only callers who could request or approve a change see the queue.
    overrides[list_name] = "write"
    overrides[cancel_name] = "write"
    reg.mcp._stutters_role_tiers = overrides  # type: ignore[attr-defined]

    @reg.read(
        title="List changes awaiting approval",
        name=list_name,
        description=(
            "List the changes waiting for a second person's approval (two-person confirm): preview id, "
            "tool, action, requester and seconds left before the preview expires. Confirm tokens are not "
            f"shown: call {prefix}_review_change with a preview id to see one change in full and the token "
            f"to approve it with, or {prefix}_cancel_pending_change to withdraw or reject it. Pending "
            "changes live in memory for the confirm TTL only; a server restart clears them. Changes "
            "nothing."
        ),
    )
    async def list_pending_changes(
        ctx: Context,
        mine: Annotated[
            bool, Field(description="Only changes you requested (default: every pending change)")
        ] = False,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
        response_format: ResponseFormat = "json",
    ) -> ToolResult:
        store = get_previews(ctx)
        caller = current_caller()
        rows = [
            _row(store, token, pending, caller)
            for token, pending in store.awaiting_approval()
            if not mine or pending.caller == caller
        ]
        page = Page(rows[offset : offset + limit], offset, total=len(rows))
        extra: dict[str, Any] = {}
        if not rows:
            extra["note"] = (
                "Nothing is waiting for approval. Previews expire after "
                f"{int(store.ttl)} seconds and a restart clears them."
            )
        return list_result(page, fmt=response_format, shape=False, extra=extra)

    # A read tool: it changes nothing upstream (only drops an in-memory preview), so it stays available
    # under MCP_DRY_RUN and for read+write role holders who need to reject a change.
    @reg.read(
        title="Cancel a change awaiting approval",
        name=cancel_name,
        idempotent=False,
        description=(
            "Withdraw (requester) or reject (approver) a change waiting for two-person approval. The "
            "confirm token stops working at once; nothing is changed on the appliance. Takes the preview "
            f"id from {prefix}_list_pending_changes (or the confirm token). The cancel is written to the "
            "audit log with your name and reason."
        ),
    )
    async def cancel_pending_change(
        ctx: Context,
        preview_id: Annotated[
            str,
            Field(
                description="The pending change's preview id (pc_... from list_pending_changes) or its token"
            ),
        ],
        reason: Annotated[
            str | None,
            Field(description="Why it is withdrawn or rejected (recorded in the audit log)", max_length=200),
        ] = None,
    ) -> dict[str, Any]:
        store = get_previews(ctx)
        caller = current_caller()
        found = store.find(preview_id.strip())
        if found is None or not found[1].two_person:
            raise InvalidRequest(
                f"No pending two-person change {preview_id!r} (it was confirmed, cancelled or expired). "
                f"{list_name} shows what is still waiting."
            )
        token, pending = found
        store.cancel(token)
        tool, _, on_target = pending.tool.partition("@")
        action = (pending.preview or {}).get("action") or tool
        as_who = "requester" if pending.caller == caller else "approver"
        extra: dict[str, Any] = {
            "preview_id": store.preview_id(token),
            "requested_by": pending.caller,
            "cancelled_by": caller,
            "cancelled_as": as_who,
        }
        if on_target:
            extra["instance"] = on_target
        if reason:
            extra["reason"] = reason.strip()[:MAX_REASON]
        _emit(ctx, "cancelled", tool, caller, action, **extra)
        return {
            "status": "cancelled",
            "tool": tool,
            **({"instance": on_target} if on_target else {}),
            "action": action,
            **{k: v for k, v in extra.items() if k != "instance"},
            "note": "The confirm token no longer works. Nothing was changed on the appliance.",
        }
