"""Elicitation-based confirm (C67): MCP_CONFIRM_MODE=token|elicit|auto."""

import json
import logging
from datetime import datetime
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from fastmcp.exceptions import ToolError
from mcp.types import ClientCapabilities, ElicitationCapability, UrlElicitationCapability

from stutters_mcp_core import policy as policy_mod
from stutters_mcp_core.confirm import client_can_elicit, elicit_channel, elicit_message
from stutters_mcp_core.formatting import MASK
from stutters_mcp_core.testing import as_caller

from .demo import BASE, create_server, make_settings

RWD = {"mcp_enable_writes": True, "mcp_enable_dangerous": True}
# "legacy": a handshake-era session (server-initiated elicitation, the push channel);
# "auto": the in-memory client negotiates 2026-07-28 (InputRequiredResult, the input channel).
PROTOCOLS = pytest.mark.parametrize("proto", ["legacy", "auto"])


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/api/login").mock(return_value=httpx.Response(200, json={"token": "T"}))
        mock.get("/api/version").mock(return_value=httpx.Response(200, json={"version": "1.2.3"}))
        mock.post("/api/items").mock(return_value=httpx.Response(201, json={"id": "1"}))
        mock.delete("/api/items").mock(return_value=httpx.Response(204))
        yield mock


def audit_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "stutters_mcp_core.audit"]


class Dialog:
    """A client elicitation handler that records what it was asked and answers as told."""

    def __init__(self, answer: Any = True) -> None:
        self.answer = answer
        self.asked: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, message: str, response_type: Any, params: Any, context: Any) -> Any:
        self.asked.append((message, params.requested_schema))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


async def call(c: Client, name: str, args: dict[str, Any] | None = None) -> Any:
    return (await c.call_tool(name, args or {})).structured_content


def posted(api: Any) -> int:
    return sum(1 for r in api.calls if r.request.method == "POST" and r.request.url.path == "/api/items")


async def test_token_mode_is_unchanged_with_an_eliciting_client(api: Any) -> None:
    dialog = Dialog()
    async with Client(create_server(make_settings(**RWD)), elicitation_handler=dialog) as c:
        prev = await call(c, "demo_create_item", {"name": "x"})
    assert prev["status"] == "preview" and prev["confirm"].startswith("pv_")
    assert dialog.asked == [] and posted(api) == 0


@pytest.mark.parametrize("mode", ["auto", "elicit"])
async def test_accept_applies_in_one_call(api: Any, caplog: pytest.LogCaptureFixture, mode: str) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    dialog = Dialog(True)
    server = create_server(make_settings(mcp_confirm_mode=mode, **RWD))
    async with Client(server, elicitation_handler=dialog) as c:
        out = await call(c, "demo_create_item", {"name": "x"})
    assert out["status"] == "applied" and out["confirmed_via"] == "elicit"
    assert out["result"] == {"id": "1"} and "confirm" not in out
    assert posted(api) == 1
    message, schema = dialog.asked[0]
    assert "Create item x" in message and "Apply this change?" in message
    # never elicit secrets: the only field is one boolean
    assert schema["properties"].keys() == {"value"} and schema["properties"]["value"]["type"] == "boolean"
    events = [(e["event"], e.get("confirm_mode"), e.get("confirmed_via")) for e in audit_events(caplog)]
    assert ("preview", "elicit", None) in events and ("applied", None, "elicit") in events


@PROTOCOLS
@pytest.mark.parametrize(
    "answer, status, phrase",
    [
        (False, "declined", "declined"),
        (ElicitResult(action="decline"), "declined", "declined"),
        (ElicitResult(action="cancel"), "declined", "dismissed"),
    ],
)
async def test_decline_and_cancel_change_nothing(
    api: Any, caplog: pytest.LogCaptureFixture, answer: Any, status: str, phrase: str, proto: Any
) -> None:
    caplog.set_level(logging.INFO, logger="stutters_mcp_core.audit")
    server = create_server(make_settings(mcp_confirm_mode="auto", **RWD))
    async with Client(server, elicitation_handler=Dialog(answer), mode=proto) as c:
        out = await call(c, "demo_create_item", {"name": "x"})
    assert out["status"] == status and phrase in out["note"] and "confirm" not in out
    assert posted(api) == 0
    assert any(e["event"] == "declined" for e in audit_events(caplog))


async def test_auto_without_elicitation_uses_tokens(api: Any) -> None:
    async with Client(create_server(make_settings(mcp_confirm_mode="auto", **RWD))) as c:
        prev = await call(c, "demo_create_item", {"name": "x"})
        assert prev["status"] == "preview" and prev["confirm"].startswith("pv_")
        done = await call(c, "demo_create_item", {"name": "x", "confirm": prev["confirm"]})
    assert done["status"] == "applied" and "confirmed_via" not in done


async def test_auto_falls_back_to_a_token_when_the_push_dialog_fails(api: Any) -> None:
    server = create_server(make_settings(mcp_confirm_mode="auto", **RWD))
    async with Client(server, elicitation_handler=Dialog(RuntimeError("no UI")), mode="legacy") as c:
        prev = await call(c, "demo_create_item", {"name": "x"})
    assert prev["status"] == "preview" and prev["confirm"].startswith("pv_") and posted(api) == 0


async def test_elicit_mode_blocks_clients_without_elicitation_and_refuses_tokens(api: Any) -> None:
    server = create_server(make_settings(mcp_confirm_mode="elicit", **RWD))
    async with Client(server) as c:
        prev = await call(c, "demo_create_item", {"name": "x"})
        assert prev["status"] == "blocked" and "confirm" not in prev
        assert "MCP_CONFIRM_MODE=elicit" in prev["blockers"][0]
        with pytest.raises(ToolError, match="Confirm tokens are not accepted"):
            await call(c, "demo_create_item", {"name": "x", "confirm": "pv_forged"})
    assert posted(api) == 0


async def test_elicit_mode_push_dialog_failure_is_blocked(api: Any) -> None:
    server = create_server(make_settings(mcp_confirm_mode="elicit", **RWD))
    async with Client(server, elicitation_handler=Dialog(RuntimeError("no UI")), mode="legacy") as c:
        out = await call(c, "demo_create_item", {"name": "x"})
    assert out["status"] == "blocked" and "confirm" not in out and posted(api) == 0


async def test_two_person_still_uses_tokens(api: Any) -> None:
    dialog = Dialog(True)
    server = create_server(make_settings(mcp_confirm_mode="elicit", mcp_two_person_tools="@dangerous", **RWD))
    async with Client(server, elicitation_handler=dialog) as c:
        with as_caller("alice"):
            prev = await call(c, "demo_wipe")
            assert prev["status"] == "preview" and prev["confirm"].startswith("pv_")
        with as_caller("bob"):
            done = await call(c, "demo_wipe", {"confirm": prev["confirm"]})
    assert done["status"] == "applied" and done["approved_by"] == "bob"
    assert dialog.asked == []


async def test_dry_run_and_blockers_never_elicit(api: Any) -> None:
    dialog = Dialog(True)
    async with Client(
        create_server(make_settings(mcp_confirm_mode="auto", mcp_dry_run=True, **RWD)),
        elicitation_handler=dialog,
    ) as c:
        assert "confirm" not in await call(c, "demo_create_item", {"name": "x"})
    async with Client(
        create_server(make_settings(mcp_confirm_mode="auto", mcp_max_blast_radius=1, **RWD)),
        elicitation_handler=dialog,
    ) as c:
        assert (await call(c, "demo_wipe"))["status"] == "blocked"
    assert dialog.asked == [] and posted(api) == 0


async def test_closed_window_never_elicits(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    dialog = Dialog(True)
    monkeypatch.setattr(policy_mod, "_now", lambda: datetime.fromisoformat("2026-10-17T12:00:00+00:00"))
    for mode in ("auto", "elicit"):
        server = create_server(
            make_settings(mcp_confirm_mode=mode, mcp_change_window="Mon-Fri 22:00-02:00 UTC", **RWD)
        )
        async with Client(server, elicitation_handler=dialog) as c:
            out = await call(c, "demo_create_item", {"name": "x"})
        assert "warning" in out and out["status"] == "preview"
    assert dialog.asked == [] and posted(api) == 0


async def test_status_and_instructions_show_the_mode(api: Any) -> None:
    server = create_server(make_settings(mcp_confirm_mode="elicit", **RWD))
    assert "MCP_CONFIRM_MODE=elicit" in (server.instructions or "")
    async with Client(server) as c:
        status = await call(c, "demo_status")
    assert status["server"]["confirm_mode"] == "elicit"
    plain = create_server(make_settings(**RWD))
    assert "MCP_CONFIRM_MODE" not in (plain.instructions or "")


def test_message_masks_secrets() -> None:
    from stutters_mcp_core.writes import _shown_args

    text = elicit_message(
        "demo_set_secret",
        {"action": "Set the RADIUS key", "preview": {"shared_secret": "s3cr3t-value"}, "blast_radius": 2},
        _shown_args({"name": "nad1", "password": "hunter2"}),
    )
    assert "hunter2" not in text and "s3cr3t-value" not in text and MASK in text
    assert "Blast radius: 2" in text and text.endswith("Nothing changes unless you accept.")
    long = elicit_message("demo_x", {"action": "x", "preview": {"blob": "y" * 10_000}}, {})
    assert len(long) < 4_200 and "truncated" in long


class _Session:
    def __init__(self, caps: ClientCapabilities | None) -> None:
        self.client_capabilities = caps


class _Ctx:
    def __init__(self, caps: ClientCapabilities | None, *, task: bool = False, modern: bool = False) -> None:
        self.session = _Session(caps)
        self.is_background_task = task
        self.modern = modern

    def _is_modern_protocol(self) -> bool:
        return self.modern


def test_client_support_check() -> None:
    form = ClientCapabilities(elicitation=ElicitationCapability())
    url_only = ClientCapabilities(elicitation=ElicitationCapability(url=UrlElicitationCapability()))
    assert client_can_elicit(_Ctx(form))  # type: ignore[arg-type]
    assert not client_can_elicit(_Ctx(url_only))  # type: ignore[arg-type]
    assert not client_can_elicit(_Ctx(ClientCapabilities()))  # type: ignore[arg-type]
    assert not client_can_elicit(_Ctx(None))  # type: ignore[arg-type]
    assert not client_can_elicit(_Ctx(form, task=True))  # type: ignore[arg-type]
    assert not client_can_elicit(object())  # type: ignore[arg-type]
    assert elicit_channel(_Ctx(form)) == "push"  # type: ignore[arg-type]
    # 2026-07-28: only through ConfirmInputMiddleware's slot
    assert elicit_channel(_Ctx(form, modern=True)) is None  # type: ignore[arg-type]


def _server_with_state(mode: str, state: dict[str, Any]) -> Any:
    """The demo server plus a tool whose plan depends on `state` (fingerprint) and whose replan
    reads it again, to check that a target changed while the dialog was open is refused."""
    from fastmcp import Context

    from stutters_mcp_core import Confirm, Plan, ToolRegistrar, preview_or_apply

    settings = make_settings(mcp_confirm_mode=mode, **RWD)
    server = create_server(settings)
    reg = ToolRegistrar(server, settings, "demo")

    @reg.write(title="Rename")
    async def demo_rename(ctx: Context, name: str, confirm: Confirm = None) -> dict[str, Any]:
        """Rename the thing (test tool)."""

        def plan() -> Plan:
            return Plan(f"Rename {state['name']} to {name}", fingerprint=state["name"])

        async def replan() -> Plan:
            return plan()

        async def apply() -> dict[str, Any]:
            state["applied"] = True
            return {"name": name}

        first = plan()
        state["planned"] = state.get("planned", 0) + 1
        return await preview_or_apply(
            ctx,
            tool="demo_rename",
            args={"name": name},
            confirm=confirm,
            plan=first,
            apply=apply,
            replan=replan,
        )

    return server


@PROTOCOLS
async def test_target_changed_while_the_dialog_was_open_is_refused(api: Any, proto: Any) -> None:
    state: dict[str, Any] = {"name": "old"}

    class Meddler(Dialog):
        async def __call__(self, message: str, response_type: Any, params: Any, context: Any) -> Any:
            state["name"] = "changed-meanwhile"
            return True

    async with Client(_server_with_state("elicit", state), elicitation_handler=Meddler(), mode=proto) as c:
        with pytest.raises(ToolError, match="changed"):
            await call(c, "demo_rename", {"name": "new"})
    assert "applied" not in state
    if proto == "auto":
        assert state["planned"] == 2  # the retry recomputed the plan before redeeming the token


@PROTOCOLS
async def test_unchanged_target_applies(api: Any, proto: Any) -> None:
    state: dict[str, Any] = {"name": "old"}
    async with Client(_server_with_state("elicit", state), elicitation_handler=Dialog(True), mode=proto) as c:
        out = await call(c, "demo_rename", {"name": "new"})
    assert out["status"] == "applied" and state["applied"] is True


async def test_input_channel_without_middleware_falls_back(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from stutters_mcp_core import confirm

    class NoSlot(confirm.ConfirmInputMiddleware):
        async def on_call_tool(self, context: Any, call_next: Any) -> Any:
            return await call_next(context)

    monkeypatch.setattr(confirm, "ConfirmInputMiddleware", NoSlot)
    dialog = Dialog(True)
    async with Client(
        create_server(make_settings(mcp_confirm_mode="auto", **RWD)), elicitation_handler=dialog
    ) as c:
        prev = await call(c, "demo_create_item", {"name": "x"})
    assert prev["confirm"].startswith("pv_") and dialog.asked == []
