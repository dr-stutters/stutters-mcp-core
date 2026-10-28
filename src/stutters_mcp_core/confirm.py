"""Elicitation-based confirm (C67, ADR-002 amendment): `MCP_CONFIRM_MODE=token|elicit|auto`.

- `token` (default): the preview returns a single-use confirm token and the model repeats the call with it
  (ADR-002). Unchanged.
- `auto`: when the client advertises form elicitation, the preview call asks the user directly ("Apply
  this change?") through the client's confirmation dialog and applies on accept, in the same call; clients
  without elicitation get the token flow.
- `elicit`: only the user's answer in the dialog confirms a change; the server issues no tokens for
  one-person changes and refuses `confirm=<token>` for them, so a model can't confirm on its own. Clients
  without elicitation get a blocked preview saying so.

Two channels, picked per request:
- `push` (handshake-era protocol versions, e.g. 2025-06-18 / 2025-11-25): a server-initiated
  `elicitation/create` while the tool call waits (`ctx.elicit`), then apply in the same call.
- `input` (the 2026-07-28 stateless protocol, SEP-2322): the preview call returns an
  `InputRequiredResult` carrying the elicitation, with the confirm token in the sealed `request_state`; the
  client asks the user and retries the identical call with the answer, and the token is redeemed against
  the freshly computed plan (so a target that changed meanwhile is refused, as with tokens).
  `ConfirmInputMiddleware` turns the tool's ordinary preview dict into that result, so tools that
  post-process `preview_or_apply`'s dict keep working.

Two-person changes (MCP_TWO_PERSON_TOOLS) always use tokens: the second person confirms from their own
session. Dry-run, blocked previews and closed change windows never elicit.

Never elicit secrets: the dialog's schema is a single boolean ("Apply this change"), never a text field,
and the message shows the preview with secret-looking keys masked, the arguments redacted as in the audit
log, and credential shapes scrubbed. Passwords and keys come from settings, never from an elicitation
(MCP spec: form mode must not request sensitive information).
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Literal

import mcp.types as mt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from stutters_mcp_core.formatting import mask_secrets
from stutters_mcp_core.secrets import scrub_text

if TYPE_CHECKING:
    from fastmcp import Context

log = logging.getLogger(__name__)

ConfirmMode = Literal["token", "elicit", "auto"]
Channel = Literal["push", "input"]
Answer = Literal["accept", "decline", "cancel", "timeout", "error"]

INPUT_KEY = "confirm"  # the key of our elicitation in input_requests / input_responses
RESPONSE_TITLE = "Apply this change"
# The dialog's whole schema: one boolean. Never a text field, so a confirm can't collect a secret.
CONFIRM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"value": {"type": "boolean", "title": RESPONSE_TITLE}},
    "required": ["value"],
}

# Set by ConfirmInputMiddleware around each tool call; preview_or_apply puts an InputRequiredResult here.
_input_slot: ContextVar[list[mt.InputRequiredResult] | None] = ContextVar(
    "stutters_confirm_input", default=None
)

MAX_MESSAGE = 4_000  # keep the dialog readable; the full preview is in the tool result


def confirm_mode(ctx: Context) -> ConfirmMode:
    lc = ctx.lifespan_context if hasattr(ctx, "lifespan_context") else {}
    mode = lc.get("confirm_mode") or getattr(lc.get("settings"), "mcp_confirm_mode", "token")
    if mode == "elicit":
        return "elicit"
    return "auto" if mode == "auto" else "token"


def elicit_channel(ctx: Context) -> Channel | None:
    """How this request's client can be asked to confirm: `push` (server-initiated elicitation on a
    handshake-era session), `input` (an InputRequiredResult on the 2026-07-28 protocol, when
    ConfirmInputMiddleware is installed), or None (the client didn't advertise form elicitation, or this
    is a background task). URL-only elicitation doesn't count: a confirm needs an accept/decline answer."""
    try:
        if ctx.is_background_task:
            return None
        modern = ctx._is_modern_protocol()
        session = ctx.session
    except Exception:  # noqa: BLE001 - no live session (unit tests of the helper, workers)
        return None
    caps = getattr(session, "client_capabilities", None)
    if caps is None:
        params = getattr(session, "client_params", None)
        caps = getattr(params, "capabilities", None)
    elicitation = getattr(caps, "elicitation", None)
    if elicitation is None:
        return None
    if getattr(elicitation, "form", None) is None and getattr(elicitation, "url", None) is not None:
        return None  # an empty `elicitation: {}` (older clients) means form mode
    if not modern:
        return "push"
    return "input" if _input_slot.get() is not None else None


def client_can_elicit(ctx: Context) -> bool:
    return elicit_channel(ctx) is not None


def elicit_message(tool: str, preview: dict[str, Any], shown_args: dict[str, Any]) -> str:
    """The text of the confirmation dialog: what will change, with secrets masked and scrubbed."""
    lines = [f"{tool} wants to make this change:", "", str(preview.get("action", tool))]
    if preview.get("instance"):
        lines.append(f"Instance: {preview['instance']}")
    if preview.get("blast_radius") is not None:
        lines.append(f"Blast radius: {preview['blast_radius']}")
    elif preview.get("blast_unknown"):
        lines.append(f"Blast radius: {preview['blast_unknown']}")
    for effect in preview.get("side_effects") or []:
        lines.append(f"Side effect: {effect}")
    if preview.get("warning"):
        lines.append(f"Warning: {preview['warning']}")
    body = {k: v for k, v in (("preview", preview.get("preview")), ("arguments", shown_args)) if v}
    if body:
        lines += ["", json.dumps(mask_secrets(body), indent=1, default=str, sort_keys=True)]
    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE:
        text = text[: MAX_MESSAGE - 60].rstrip() + "\n... (truncated; the full preview is in the tool result)"
    return scrub_text(text) + "\n\nApply this change? Nothing changes unless you accept."


async def ask_user(ctx: Context, message: str, *, wait_seconds: float) -> Answer:
    """Ask the user to confirm through the client's elicitation dialog. Only `accept` with the box ticked
    counts as approval; a declined, cancelled, unanswered (timeout) or failed request does not."""
    try:
        result = await asyncio.wait_for(
            ctx.elicit(message, bool, response_title=RESPONSE_TITLE), timeout=wait_seconds
        )
    except TimeoutError:
        return "timeout"
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - client error, method not found: never treat as approval
        log.warning("Elicitation confirm failed: %s", scrub_text(str(e))[:200])
        return "error"
    action = getattr(result, "action", None)
    if action == "accept":
        return "accept" if getattr(result, "data", None) is True else "decline"
    if action == "decline":
        return "decline"
    return "cancel"


def request_input(message: str, token: str) -> bool:
    """Ask for the user's confirm through an InputRequiredResult (the `input` channel): the result
    replaces this tool call's output, and the client retries the call with the answer. The token rides in
    `request_state`, which the framework seals against tampering. False if the middleware isn't there."""
    slot = _input_slot.get()
    if slot is None:
        return False
    slot.append(
        mt.InputRequiredResult(
            input_requests={
                INPUT_KEY: mt.ElicitRequest(
                    params=mt.ElicitRequestFormParams(message=message, requested_schema=CONFIRM_SCHEMA)
                )
            },
            request_state=token,
        )
    )
    return True


def input_answer(ctx: Context) -> tuple[Answer, str] | None:
    """On the retry of an `input` round: (the user's answer, the token from request_state); None when
    this call is not answering one of our confirm dialogs."""
    try:
        responses = ctx.input_responses
        state = ctx.request_state
    except Exception:  # noqa: BLE001 - no request (unit tests of the helper)
        return None
    if not responses or not state or INPUT_KEY not in responses:
        return None
    response = responses[INPUT_KEY]
    action = getattr(response, "action", None)
    content = getattr(response, "content", None) or {}
    if action == "accept":
        return ("accept" if content.get("value") is True else "decline"), state
    return ("decline" if action == "decline" else "cancel"), state


class ConfirmInputMiddleware(Middleware):
    """Installed when MCP_CONFIRM_MODE is elicit or auto: gives each tool call a slot for an
    InputRequiredResult and, when preview_or_apply filled it, returns that instead of the preview dict."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        from fastmcp.tools.base import InputRequiredToolResult

        slot: list[mt.InputRequiredResult] = []
        reset = _input_slot.set(slot)
        try:
            result = await call_next(context)
        finally:
            _input_slot.reset(reset)
        if slot and not isinstance(result, InputRequiredToolResult):
            return InputRequiredToolResult(slot[-1])
        return result
