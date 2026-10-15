import base64
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastmcp import Client, FastMCP
from pydantic import ValidationError

from stutters_mcp_core import ServerSpec, build_server
from stutters_mcp_core.events import (
    Event,
    EventOptions,
    EventSource,
    PollingEventSource,
    SyslogReceiver,
    WebhookAuth,
    WebhookReceiver,
    parse_webhook_secrets,
)
from stutters_mcp_core.inbound_auth import NamedTokenVerifier, hash_token
from stutters_mcp_core.testing import assert_tool_contract, call_tool

from .demo import make_settings
from .test_events import wait_for

SPEC = ServerSpec("ev-mcp-server", "ev", "EvProduct", "DEMO_", "Tools for the event test server.")
SECRET = "s3cret-s3cret-s3cret"
MCP_TOKEN = "t" * 32


def server(
    sources: Any,
    *,
    options: EventOptions | None = None,
    auth: Any = None,
    register: list[Any] | None = None,
    **settings_kw: Any,
) -> FastMCP[Any]:
    settings_kw.setdefault("mcp_transport", "http")
    return build_server(
        SPEC,
        make_settings(**settings_kw),
        client_factory=lambda s: object(),
        register=register or [],
        event_sources=sources,
        event_options=options,
        auth=auth,
    )


@asynccontextmanager
async def http_client(mcp: FastMCP[Any], peer: str = "10.1.1.1") -> AsyncIterator[httpx.AsyncClient]:
    app = mcp.http_app(path="/mcp")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=(peer, 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


def hook_sources(settings_auth: WebhookAuth | None = None, **kw: Any) -> Any:
    def make(app: Any) -> list[EventSource]:
        return [
            WebhookReceiver.from_settings(
                make_settings(mcp_webhooks_enabled=True, mcp_webhook_secrets=f"catc:{SECRET}", **kw),
                "catc",
                auth=settings_auth,
            )
        ]

    return make


# --- settings -----------------------------------------------------------------------------


def test_webhook_settings_validation() -> None:
    s = make_settings(
        mcp_webhook_secrets=f"catc:{SECRET},\n# comment\n gh:{SECRET}#notacomment", mcp_webhook_port=""
    )
    assert s.webhook_secrets() == {"catc": SECRET, "gh": SECRET + "#notacomment"}
    assert s.mcp_webhook_port is None
    assert make_settings(mcp_webhook_path="hooks/").mcp_webhook_path == "/hooks"
    assert make_settings(mcp_path="/events").mcp_path == "/events"  # no clash while webhooks are off
    for bad in (
        {"mcp_webhook_path": "/mcp", "mcp_webhooks_enabled": True},
        {"mcp_webhook_path": "/mcp/x", "mcp_webhooks_enabled": True},
        {"mcp_webhook_path": "/health", "mcp_webhooks_enabled": True},
        {"mcp_webhook_path": "/a b"},
        {"mcp_webhook_secrets": "nocolon"},
        {"mcp_webhook_secrets": f"a:{SECRET},a:{SECRET}"},
        {"mcp_webhook_allow_ips": "10.0.0.0/33"},
        {"mcp_syslog_enabled": True, "mcp_syslog_udp_port": "", "mcp_syslog_tcp_port": "off"},
    ):
        with pytest.raises(ValidationError):
            make_settings(**bad)
    with pytest.raises(ValueError) as e:
        parse_webhook_secrets("x:")
    assert "entry #1" in str(e.value)


# --- auth modes ----------------------------------------------------------------------------


def test_webhook_auth_modes() -> None:
    h = WebhookAuth.header("X-Token", secret=SECRET)
    assert h.check_headers({"X-Token": SECRET}) and not h.check_headers({"X-Token": "nope"})
    assert not h.check_headers({})
    bearer = WebhookAuth.header("Authorization", secret=SECRET, scheme="Bearer")
    assert bearer.check_headers({"Authorization": f"Bearer {SECRET}"})
    assert not bearer.check_headers({"Authorization": SECRET})
    b = WebhookAuth.basic("catc", "pw")
    good = "Basic " + base64.b64encode(b"catc:pw").decode()
    assert b.check_headers({"Authorization": good}) and not b.check_headers({"Authorization": "Basic !!!"})
    assert not b.check_headers({"Authorization": "Bearer x"})
    body = b'{"a":1}'
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    m = WebhookAuth.hmac(secret=SECRET)
    assert m.check_headers({}) and m.check_body({"X-Signature-256": f"sha256={sig}"}, body)
    assert m.check_body({"X-Signature-256": sig.upper()}, body)
    assert not m.check_body({"X-Signature-256": f"sha256={sig}"}, body + b" ")
    b64 = WebhookAuth.hmac("X-Sig", secret=SECRET, prefix="", encoding="base64")
    sig64 = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    assert b64.check_body({"X-Sig": sig64}, body)
    assert not WebhookAuth.header().check_headers({"X-Webhook-Token": ""})  # no secret: always refused
    with pytest.raises(ValueError):
        WebhookAuth.hmac(algorithm="nope")
    with pytest.raises(ValueError):
        WebhookAuth.hmac(encoding="b32")


# --- route on the MCP HTTP app ------------------------------------------------------------


async def test_webhook_on_mcp_app_end_to_end_and_mcp_auth_untouched() -> None:
    mcp = server(
        hook_sources(),
        auth=NamedTokenVerifier({"alice": hash_token(MCP_TOKEN)}),
        mcp_webhooks_enabled=True,
    )
    async with http_client(mcp) as c:
        payload = [
            {"eventId": "e1", "type": "NETWORK-DEVICES-3-251", "severity": "warning", "summary": "down"}
        ]
        r = await c.post("/events/catc", json=payload, headers={"X-Webhook-Token": SECRET})
        assert r.status_code == 202 and r.json() == {"accepted": 1, "duplicates": 0}
        r = await c.post("/events/catc", json=payload, headers={"X-Webhook-Token": SECRET})
        assert r.json() == {"accepted": 0, "duplicates": 1}
        assert (await c.post("/events/catc", json=payload)).status_code == 401
        assert (
            await c.post("/events/catc", json=payload, headers={"X-Webhook-Token": "x" * 20})
        ).status_code == 401
        assert (
            await c.post("/events/other", json=payload, headers={"X-Webhook-Token": SECRET})
        ).status_code == 404
        bad = await c.post("/events/catc", content=b"{not json", headers={"X-Webhook-Token": SECRET})
        assert bad.status_code == 400
        # MCP endpoint still demands its bearer token; the webhook secret is no substitute.
        assert (await c.post("/mcp", json={}, headers={"X-Webhook-Token": SECRET})).status_code == 401
        assert (
            await c.post("/mcp", json={}, headers={"Authorization": f"Bearer {SECRET}"})
        ).status_code == 401
        assert (await c.get("/health")).status_code == 200


async def test_webhook_limits_allow_list_and_rate() -> None:
    mcp = server(
        hook_sources(
            mcp_webhook_max_body=1024, mcp_webhook_allow_ips="10.1.1.0/24", mcp_webhook_rate_limit=1
        ),
        mcp_webhooks_enabled=True,
    )
    hdr = {"X-Webhook-Token": SECRET}
    async with http_client(mcp, peer="10.1.1.7") as c:
        big = await c.post("/events/catc", content=b"{" + b" " * 2000 + b"}", headers=hdr)
        assert big.status_code == 413
        assert (await c.post("/events/catc", json={"id": 1}, headers=hdr)).status_code == 202  # burst of 2
        limited = await c.post("/events/catc", json={"id": 3}, headers=hdr)
        assert limited.status_code == 429 and limited.headers["Retry-After"] == "1"
    async with http_client(mcp, peer="192.0.2.1") as c:
        assert (await c.post("/events/catc", json={}, headers=hdr)).status_code == 403


async def test_webhook_streamed_body_over_limit_without_content_length() -> None:
    rcv = WebhookReceiver("x", auth=WebhookAuth.header(secret=SECRET), max_body=1024, rate_limit=None)

    class Req:
        headers = {"X-Webhook-Token": SECRET}
        client = None

        async def stream(self) -> AsyncIterator[bytes]:
            for _ in range(5):
                yield b"x" * 400

    rcv._ready = True
    resp = await rcv.handle(Req())  # type: ignore[arg-type]
    assert resp.status_code == 413 and rcv.drops == {"oversize": 1}


async def test_webhook_hmac_and_basic_and_custom_parser() -> None:
    def parse(payload: Any, headers: Any) -> Event:
        return Event(type="gh.push", summary=payload["ref"], keys={"repo": payload["repo"]})

    def make(app: Any) -> list[EventSource]:
        return [
            WebhookReceiver("gh", auth=WebhookAuth.hmac(secret=SECRET), parse=parse),
            WebhookReceiver("basic", auth=WebhookAuth.basic("catc", "pw-pw-pw-pw-pw-pw")),
            WebhookReceiver("weak", auth=WebhookAuth.basic("catc", "pw")),
            WebhookReceiver("bad", auth=WebhookAuth.header(secret=SECRET), parse=lambda p, h: 1 / 0),
        ]

    mcp = server(make, mcp_webhooks_enabled=True)
    body = json.dumps({"ref": "main", "repo": "core"}).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    async with http_client(mcp) as c:
        ok = await c.post("/events/gh", content=body, headers={"X-Signature-256": sig})
        assert ok.status_code == 202
        tampered = await c.post("/events/gh", content=body + b" ", headers={"X-Signature-256": sig})
        assert tampered.status_code == 401
        r = await c.post("/events/basic", json={"name": "x"}, auth=("catc", "pw-pw-pw-pw-pw-pw"))
        assert r.status_code == 202
        assert (await c.post("/events/weak", json={}, auth=("catc", "pw"))).status_code == 503
        r = await c.post("/events/basic", json={}, auth=("catc", "wrong"))
        assert r.status_code == 401 and r.headers["WWW-Authenticate"].startswith("Basic")
        r = await c.post("/events/bad", json={}, headers={"X-Webhook-Token": SECRET})
        assert r.status_code == 422
    async with Client(mcp) as client:
        data = (await client.call_tool("ev_list_events", {"type": "gh.*", "response_format": "json"})).data
    assert data["results"][0]["summary"] == "main" and data["results"][0]["keys"] == {"repo": "core"}


async def test_webhook_without_secret_fails_closed() -> None:
    def make(app: Any) -> list[EventSource]:
        return [
            WebhookReceiver.from_settings(make_settings(mcp_webhooks_enabled=True), "catc"),
            WebhookReceiver("short", auth=WebhookAuth.header(secret="short")),
        ]

    mcp = server(make, mcp_webhooks_enabled=True)
    async with http_client(mcp) as c:
        r = await c.post("/events/catc", json={}, headers={"X-Webhook-Token": ""})
        assert r.status_code == 503
        r = await c.post("/events/short", json={}, headers={"X-Webhook-Token": "short"})
        assert r.status_code == 503
    async with Client(mcp) as client:
        await wait_for(lambda: True)
        st = (await client.call_tool("ev_event_sources", {})).structured_content
    by_name = {s["name"]: s for s in st["sources"]}  # type: ignore[index]
    assert (
        by_name["catc"]["state"] == "failed" and "MCP_WEBHOOK_SECRETS=catc" in by_name["catc"]["last_error"]
    )
    assert by_name["catc"]["secret_configured"] is False
    assert "shorter than" in by_name["short"]["last_error"]


async def test_disabled_webhooks_not_routed() -> None:
    def make(app: Any) -> list[EventSource]:
        return [WebhookReceiver.from_settings(make_settings(), "catc")]  # MCP_WEBHOOKS_ENABLED unset

    mcp = server(make)  # route not mounted either
    async with http_client(mcp) as c:
        assert (await c.post("/events/catc", json={})).status_code in (401, 404, 405)
    async with Client(mcp) as client:
        st = (await client.call_tool("ev_event_sources", {})).structured_content
    src = st["sources"][0]  # type: ignore[index]
    assert src["state"] == "disabled" and "MCP_WEBHOOKS_ENABLED=true" in src["disabled_reason"]


async def test_dedicated_webhook_listener() -> None:
    settings_kw = {"mcp_webhooks_enabled": True, "mcp_webhook_port": 0, "mcp_transport": "stdio"}

    def make(app: Any) -> list[EventSource]:
        return [WebhookReceiver("catc", auth=WebhookAuth.header(secret=SECRET))]

    mcp = server(make, **settings_kw)
    async with Client(mcp) as client:
        st = (await client.call_tool("ev_event_sources", {})).structured_content
        listen = st["sources"][0]["listen"]  # type: ignore[index]
        assert listen.startswith("POST http://127.0.0.1:") and listen.endswith("/events/catc")
        url = listen.removeprefix("POST ")
        async with httpx.AsyncClient() as c:
            r = await c.post(url, json={"id": "1", "message": "hi"}, headers={"X-Webhook-Token": SECRET})
            assert r.status_code == 202
            assert (await c.post(url, json={})).status_code == 401
            assert (await c.post(url.replace("/catc", "/nope"), json={})).status_code == 404
        data = await call_tool(mcp, "ev_list_events", {"response_format": "json"})
        assert data["results"][0]["summary"] == "hi"
    # stopped with the lifespan: the port is closed
    async with httpx.AsyncClient() as c:
        with pytest.raises(httpx.ConnectError):
            await c.post(url, json={})


async def test_stdio_without_port_reports_not_served() -> None:
    def make(app: Any) -> list[EventSource]:
        return [WebhookReceiver("catc", auth=WebhookAuth.header(secret=SECRET))]

    mcp = server(make, mcp_webhooks_enabled=True, mcp_transport="stdio")
    async with Client(mcp) as client:
        st = (await client.call_tool("ev_event_sources", {})).structured_content
    assert st["sources"][0]["listen"].startswith("not served")  # type: ignore[index]


# --- generic tools, resource and options -------------------------------------------------


def poller(app: Any) -> list[EventSource]:
    t = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)

    async def poll(since: datetime | None) -> list[Event]:
        return [
            Event(
                "auth.fail",
                "bob failed",
                time=t,
                keys={"user": "bob", "mac": "aa:bb"},
                id="1",
                severity="error",
            ),
            Event("auth.pass", "amy ok", time=t, keys={"user": "amy"}, id="2", data={"raw": 1}),
        ]

    return [PollingEventSource("mnt", poll, interval=3600, description="MnT poller")]


async def test_list_events_tool_sources_tool_resource_and_status() -> None:
    mcp = server(poller)
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
        assert {"ev_list_events", "ev_event_sources", "ev_status"} <= names
        await wait_for(lambda: True)
        data = (await c.call_tool("ev_list_events", {"response_format": "json"})).structured_content
        for _ in range(100):
            if data and data.get("returned"):
                break
            data = (await c.call_tool("ev_list_events", {"response_format": "json"})).structured_content
        assert data is not None and [r["summary"] for r in data["results"]] == ["amy ok", "bob failed"]
        assert data["latest_seq"] == 2 and "data" not in data["results"][0]
        only = (await c.call_tool("ev_list_events", {"key": "user=bob", "response_format": "json"})).data
        assert [r["type"] for r in only["results"]] == ["auth.fail"]
        new = (await c.call_tool("ev_list_events", {"after_seq": 2, "response_format": "json"})).data
        assert new["results"] == [] and new["latest_seq"] == 2
        withdata = (
            await c.call_tool("ev_list_events", {"include_data": True, "response_format": "json"})
        ).data
        assert withdata["results"][0]["data"] == {"raw": 1}
        md = (await c.call_tool("ev_list_events", {"severity": "error"})).content[0].text  # type: ignore[union-attr]
        assert "| seq | time | source | type | severity | summary | keys |" in md and "bob failed" in md
        bad = await c.call_tool("ev_list_events", {"since": "last tuesday"}, raise_on_error=False)
        assert bad.is_error
        st = (await c.call_tool("ev_event_sources", {})).structured_content
        src = st["sources"][0]  # type: ignore[index]
        assert src["kind"] == "poll" and src["events"] == 2 and src["interval_seconds"] == 3600
        assert st["buffer"]["stored"] == 2  # type: ignore[index]
        res = await c.read_resource("events://ev/recent")
        recent = json.loads(res[0].text)  # type: ignore[union-attr]
        assert recent["latest_seq"] == 2 and len(recent["events"]) == 2
        status = (await c.call_tool("ev_status", {})).structured_content
        assert status["server"]["events"]["sources"] == {"mnt": "connected"}  # type: ignore[index]
    assert "ev_list_events reads recent" in (mcp.instructions or "")


async def test_event_tool_names_configurable_and_opt_out() -> None:
    mcp = server(
        poller,
        options=EventOptions(list_tool="{prefix}_list_buffered_events", sources_tool=None, resource=False),
    )
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
        assert "ev_list_buffered_events" in names and "ev_list_events" not in names
        assert "ev_event_sources" not in names
        assert await c.list_resources() == []
    with pytest.raises(ValueError, match="must start with ev_"):
        server(poller, options=EventOptions(list_tool="other_list_events"))


async def test_servers_without_events_unchanged() -> None:
    mcp = build_server(SPEC, make_settings(), client_factory=lambda s: object(), register=[])
    async with Client(mcp) as c:
        assert {t.name for t in await c.list_tools()} == {"ev_status"}
        assert await c.list_resources() == []
        status = (await c.call_tool("ev_status", {})).structured_content
        assert "events" not in status["server"]  # type: ignore[index]


async def test_contract_with_event_tools_and_no_sources() -> None:
    mcp = server(lambda app: [])
    await assert_tool_contract(mcp, "ev")
    data = await call_tool(mcp, "ev_list_events", {"response_format": "json"})
    assert data["note"] == "No event sources are configured on this server."


async def test_duplicate_source_names_and_factory_failure_do_not_crash() -> None:
    def dup(app: Any) -> list[EventSource]:
        return [PollingEventSource("x", None), PollingEventSource("x", None)]

    def boom(app: Any) -> list[EventSource]:
        raise RuntimeError("factory broke")

    for make in (dup, boom):
        data = await call_tool(server(make), "ev_status")
        assert data["server"]["name"] == "ev-mcp-server"


async def test_syslog_from_settings_through_server() -> None:
    import socket

    def make(app: Any) -> list[EventSource]:
        s = make_settings(mcp_syslog_enabled=True, mcp_syslog_udp_port=0, mcp_syslog_tcp_port=None)
        return [SyslogReceiver.from_settings(s)]

    mcp = server(make)
    async with Client(mcp) as c:
        st = (await c.call_tool("ev_event_sources", {})).structured_content
        listen = st["sources"][0]["listen"][0]  # type: ignore[index]
        port = int(listen.rsplit(":", 1)[1])
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(b"<166>Oct 15 2026 10:00:00 ftd1 : %FTD-6-430002: SrcIP: 10.1.1.10", ("127.0.0.1", port))
        sock.close()
        for _ in range(200):
            data = (await c.call_tool("ev_list_events", {"response_format": "json"})).data
            if data["returned"]:
                break
            await wait_for(lambda: True)
        assert data["results"][0]["type"] == "syslog.430002"
    disabled = SyslogReceiver.from_settings(make_settings())
    assert not disabled.enabled and "MCP_SYSLOG_ENABLED" in disabled.disabled_reason


async def test_unauthenticated_flood_does_not_starve_the_real_sender() -> None:
    mcp = server(hook_sources(mcp_webhook_rate_limit=1), mcp_webhooks_enabled=True)
    async with http_client(mcp, peer="203.0.113.9") as attacker:
        codes = [(await attacker.post("/events/catc", json={})).status_code for _ in range(5)]
        assert codes[:2] == [401, 401] and codes[-1] == 429
    async with http_client(mcp, peer="198.18.128.11") as product:
        r = await product.post("/events/catc", json={"id": "x"}, headers={"X-Webhook-Token": SECRET})
        assert r.status_code == 202
