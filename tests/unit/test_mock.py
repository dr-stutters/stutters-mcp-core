"""C64: offline demo mode (record a storyline, replay it from a query-aware mock, status shows mode=mock)."""

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from stutters_mcp_core import mock
from stutters_mcp_core.mock import (
    MASKED,
    WELL_KNOWN,
    MockAppliance,
    Recorder,
    Route,
    Routes,
    findings,
    parse_query,
)
from stutters_mcp_core.testing import call_tool

from .demo import create_server, make_settings

STORY: dict[str, Any] = {
    "mock": 2,
    "storyline": "branch triage",
    "captured_at": "2026-10-24T09:00:00Z",
    "ignore_query": ["startTime"],
    "routes": {
        "POST /api/login": [{"json": {"token": "mock-masked"}}],
        "GET /api/version": [{"json": {"version": "20.18.1"}}],
        "GET /api/items": [
            {"query": {"name": "br1"}, "json": {"items": [{"name": "br1"}]}},
            {
                "query": {"name": "br1", "kind": "router"},
                "json": {"items": [{"name": "br1", "kind": "router"}]},
            },
            {"query": {"tag": ["a", "b"]}, "json": {"items": [{"name": "tagged"}]}},
            {"json": {"items": [{"name": "br1"}, {"name": "hub1"}]}},
        ],
        "GET /api/config": [{"text": "hostname __NAME__\n", "content_type": "text/plain"}],
    },
}


def routes() -> Routes:
    return Routes.from_dict(json.loads(json.dumps(STORY)))


def test_match_prefers_the_most_specific_query() -> None:
    r = routes()

    def names(path: str, qs: str = "") -> Any:
        route = r.match("get", path, parse_query(qs))
        return route.json_body if route else None

    assert names("/api/items") == {"items": [{"name": "br1"}, {"name": "hub1"}]}
    assert names("/api/items", "name=br1") == {"items": [{"name": "br1"}]}
    assert names("/api/items", "name=br1&kind=router")["items"][0]["kind"] == "router"
    assert names("/api/items", "kind=router&name=br1&offset=0")["items"][0]["kind"] == "router"
    assert names("/api/items", "name=hub1") == {"items": [{"name": "br1"}, {"name": "hub1"}]}  # fallback
    assert names("/api/items", "tag=b&tag=a") == {"items": [{"name": "tagged"}]}
    assert names("/api/items/", "name=br1&startTime=1729760000") == {"items": [{"name": "br1"}]}  # ignored
    assert r.match("DELETE", "/api/items") is None
    assert r.match("GET", "/api/nothing") is None


def test_version_1_routes_still_load_and_ignore_the_query() -> None:
    v1 = {"GET /api/version": {"json": {"version": "1"}}, "POST /api/login?x=1": {"status": 201, "text": ""}}
    r = Routes.from_dict(v1)
    assert r.match("GET", "/api/version", parse_query("a=1")).json_body == {"version": "1"}  # type: ignore[union-attr]
    assert r.match("POST", "/api/login").status == 201  # type: ignore[union-attr]
    assert r.storyline is None and r.ignore_query == ()


def test_newer_routes_file_is_refused() -> None:
    with pytest.raises(ValueError, match="newer"):
        Routes.from_dict({"mock": 99, "routes": {}})


def test_round_trip_save_and_load(tmp_path: Path) -> None:
    r = routes()
    r.meta["server"] = "demo"
    path = tmp_path / "routes.json"
    r.save(path)
    again = Routes.load(path)
    assert again.to_dict() == r.to_dict()
    assert again.meta == {"server": "demo"}
    assert again.to_dict()["routes"]["GET /api/items"][2]["query"] == {"tag": ["a", "b"]}


def test_appliance_answers_well_known_unmatched_and_name() -> None:
    app = MockAppliance(routes(), name="hub-a")
    info = app.answer("GET", WELL_KNOWN)
    assert info.status == 200
    assert json.loads(info.body) == {
        "mode": "mock",
        "storyline": "branch triage",
        "captured_at": "2026-10-24T09:00:00Z",
        "routes": 7,
        "name": "hub-a",
    }
    miss = app.answer("GET", "/api/devices?site=200")
    assert miss.status == 404
    assert "not in this mock's storyline 'branch triage'" in json.loads(miss.body)["detail"]
    assert miss.headers[mock.MOCK_HEADER] == "captured=2026-10-24T09:00:00Z"
    text = app.answer("GET", "/api/config")
    assert text.body == b"hostname hub-a\n" and text.headers["Content-Type"] == "text/plain"


async def test_transport_drives_an_httpx_client() -> None:
    async with httpx.AsyncClient(transport=MockAppliance(routes()).transport(), base_url="http://m") as c:
        resp = await c.get("/api/items", params={"name": "br1", "kind": "router"})
    assert resp.json()["items"][0]["kind"] == "router"


@pytest.fixture
def served() -> Iterator[str]:
    srv = MockAppliance(routes(), name="demo").server("127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


async def test_status_shows_mock_mode_end_to_end(served: str) -> None:
    server = create_server(make_settings(url=served))
    data = await call_tool(server, "demo_status")
    assert data["demo"] == {"version": "20.18.1"}
    assert data["server"]["mode"] == "mock"
    assert data["server"]["captured_at"] == "2026-10-24T09:00:00Z"
    assert data["server"]["storyline"] == "branch triage"


async def test_detect_ignores_https_and_non_mock_urls(served: str) -> None:
    assert await mock.detect("https://demo.test") is None
    assert await mock.detect(None) is None
    assert await mock.detect("http://127.0.0.1:9") is None  # nothing listening
    assert (await mock.detect(f"{served}/some/base"))["mode"] == "mock"  # type: ignore[index]


def upstream() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/login":
            return httpx.Response(
                200,
                json={"token": "s3cr3t-session-value"},
                headers={"Set-Cookie": "JSESSIONID=abc123; Path=/; Secure; HttpOnly; Domain=lab.example"},
            )
        if path == "/api/items":
            name = request.url.params.get("name", "all")
            return httpx.Response(
                200,
                json={"items": [{"name": name, "ip": "10.0.0.5", "password": "hunter22"}]},
                headers={"X-Total-Count": "1", "X-Auth-Token": "tok", "Server": "nginx"},
            )
        if path == "/api/config":
            return httpx.Response(200, text="user=admin password=public123\n")
        return httpx.Response(404, json={"error": "no"})

    return httpx.MockTransport(handle)


def test_recorder_records_scrubbed_gets_and_refuses_writes() -> None:
    rec = Recorder(
        "https://manager.lab.example:8443",
        storyline="triage",
        replace={"10.0.0.": "198.51.100."},
        transport=upstream(),
    )
    login = rec.handle("POST", "/api/login", {"Host": "x"}, b'{"u": "admin"}')
    assert login.status == 200
    cookie = login.headers["set-cookie"]
    assert "Secure" not in cookie and "Domain" not in cookie and "JSESSIONID=abc123" in cookie
    rec.handle("GET", "/api/items?name=br1&startTime=5", {}, b"")
    rec.handle("GET", "/api/items?name=br1", {}, b"")  # same query again: replaces, no duplicate
    rec.handle("GET", "/api/items", {}, b"")
    rec.handle("GET", "/api/config", {}, b"")
    refused = rec.handle("DELETE", "/api/items/1", {}, b"")
    assert refused.status == 403 and rec.refused == ["DELETE /api/items/1"]

    data = rec.routes.to_dict()
    assert data["storyline"] == "triage" and data["captured_at"].endswith("Z")
    assert data["routes"]["POST /api/login"][0]["json"] == {"token": "********"}
    assert data["routes"]["POST /api/login"][0]["headers"] == {"Set-Cookie": f"JSESSIONID={MASKED}; Path=/"}
    items = data["routes"]["GET /api/items"]
    assert [i.get("query") for i in items] == [{"name": "br1"}, {}]
    first = items[0]
    assert first["json"]["items"][0] == {"name": "br1", "ip": "198.51.100.5", "password": "********"}
    assert first["headers"] == {"x-total-count": "1", "x-auth-token": MASKED}
    assert "DELETE /api/items/1" not in data["routes"]
    config = data["routes"]["GET /api/config"][0]
    assert "public123" not in config["text"] and config["content_type"] == "text/plain"
    assert "manager.lab.example" not in json.dumps(data)

    # what was recorded replays
    replay = Routes.from_dict(data)
    assert replay.match("GET", "/api/items", parse_query("name=br1&startTime=9")) is not None


def test_findings_flag_lab_data_left_in_a_capture() -> None:
    r = Routes(storyline="x")
    r.add("GET", "/api/a", {}, Route(json_body={"mgmt": "198.18.1.10", "ok": "192.0.2.1"}, has_json=True))
    problems = findings(r)
    assert len(problems) == 1 and "lab-address" in problems[0]
    assert findings(routes()) == []


def test_cli_check_and_serve_arguments(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "routes.json"
    routes().save(path)
    assert mock.main(["check", str(path)]) == 0
    assert '"storyline": "branch triage"' in capsys.readouterr().out
    with pytest.raises(SystemExit, match="OLD=NEW"):
        mock._pairs(["nope"])


def test_recorder_masks_a_bare_token_body_from_a_login_path() -> None:
    token = "4F3A9C2B7D1E0F6A5B8C9D2E3F4A1B0C7D6E5F4A"

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dataservice/client/token":
            return httpx.Response(200, text=token, headers={"content-type": "text/html"})
        return httpx.Response(200, text="ABCDEFGHIJKLMNOPQRSTUVWXYZ", headers={"content-type": "text/plain"})

    rec = Recorder("https://upstream.example", storyline="t", transport=httpx.MockTransport(upstream))
    # the caller still gets the real token; only the capture is masked
    assert rec.handle("GET", "/dataservice/client/token", {}, b"").body == token.encode()
    rec.handle("GET", "/api/serial", {}, b"")
    assert rec.routes.match("GET", "/dataservice/client/token").text == MASKED  # type: ignore[union-attr]
    assert rec.routes.match("GET", "/api/serial").text == "ABCDEFGHIJKLMNOPQRSTUVWXYZ"  # type: ignore[union-attr]
