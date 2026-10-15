"""G1: OAuth 2.1 (FastMCP OAuth proxy, GitHub as the reference IdP) alongside named bearer tokens.

The whole authorization-code flow runs in-process against a mocked GitHub (httpx2 MockTransport):
dynamic client registration -> /authorize -> consent -> GitHub -> /auth/callback -> /token (PKCE)
-> MCP request with the issued token."""

import base64
import hashlib
import re
import secrets
import urllib.parse as up
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest

from stutters_mcp_core.inbound_auth import NamedTokenVerifier, hash_token
from stutters_mcp_core.oauth import IDENTITY_CLAIM, StuttersMultiAuth, build_auth

from .demo import create_server, make_settings

RAW = "automation-token-0123456789abcdefghij"
BASE_URL = "http://localhost:8000"
CLIENT_REDIRECT = "http://localhost:9999/cb"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def oauth_settings(tmp_path: Path, **kw: Any) -> Any:
    base: dict[str, Any] = {
        "mcp_oauth_provider": "github",
        "mcp_oauth_client_id": "Ov23test",
        "mcp_oauth_client_secret": "gh-client-secret-123",
        "mcp_oauth_base_url": BASE_URL,
        "mcp_oauth_allowed_users": "octocat,ops-*",
        "mcp_oauth_storage_dir": tmp_path / "oauth",
        "mcp_oauth_redirect_uris": "http://localhost:*",
        "mcp_oauth_jwt_key": "test-jwt-signing-key-0123456789",
    }
    base.update(kw)
    return make_settings(**base)


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A fake GitHub: token endpoint and /user. `state["login"]` is who logs in."""
    state: dict[str, Any] = {"login": "octocat", "calls": []}

    def handler(req: httpx2.Request) -> httpx2.Response:
        url = str(req.url)
        state["calls"].append(url)
        if url.startswith("https://github.com/login/oauth/access_token"):
            return httpx2.Response(
                200, json={"access_token": "gho_upstream", "token_type": "bearer", "scope": "user"}
            )
        if url.startswith("https://api.github.com/user"):
            if not req.headers.get("authorization", "").endswith("gho_upstream"):
                return httpx2.Response(401)
            return httpx2.Response(
                200, json={"id": 583231, "login": state["login"]}, headers={"x-oauth-scopes": "user"}
            )
        return httpx2.Response(404)

    real = httpx2.AsyncClient

    class Mocked(real):  # type: ignore[misc,valid-type]
        def __init__(self, *a: Any, **kw: Any) -> None:
            kw["transport"] = httpx2.MockTransport(handler)
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx2, "AsyncClient", Mocked)
    return state


async def login(c: httpx.AsyncClient) -> str:
    """Run the browser + client side of the OAuth flow; returns the MCP access token."""
    reg = await c.post(
        "/register",
        json={
            "redirect_uris": [CLIENT_REDIRECT],
            "client_name": "test-client",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier = secrets.token_urlsafe(40)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = await c.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLIENT_REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "client-state",
        },
    )
    assert r.status_code == 302 and "/consent" in r.headers["location"]
    page = await c.get(r.headers["location"])
    form = {
        name: re.search(rf'name="{name}" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
        for name in ("txn_id", "csrf_token")
    }
    r = await c.post("/consent", data={**form, "action": "approve", "submit": "true"})
    assert r.headers["location"].startswith("https://github.com/login/oauth/authorize")
    upstream_state = up.parse_qs(up.urlparse(r.headers["location"]).query)["state"][0]
    r = await c.get("/auth/callback", params={"code": "gh-code", "state": upstream_state})
    assert r.headers["location"].startswith(CLIENT_REDIRECT), r.text
    code = up.parse_qs(up.urlparse(r.headers["location"]).query)["code"][0]
    r = await c.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLIENT_REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    return str(r.json()["access_token"])


async def test_github_flow_tokens_and_allow_list(tmp_path: Path, github: dict[str, Any]) -> None:
    settings = oauth_settings(tmp_path)
    auth = build_auth(settings, {"ci": hash_token(RAW)})
    assert isinstance(auth, StuttersMultiAuth)
    app = create_server(settings, auth=auth).http_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as c,
    ):
        meta = (await c.get("/.well-known/oauth-authorization-server")).json()
        assert meta["authorization_endpoint"] == f"{BASE_URL}/authorize"
        assert (await c.post("/mcp", json=INIT, headers=MCP_HEADERS)).status_code == 401

        token = await login(c)
        verified = await auth.verify_token(token)
        assert verified is not None and verified.claims[IDENTITY_CLAIM] == "github:octocat"
        ok = await c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"})
        assert ok.status_code == 200

        # the named bearer token still works, with its own identity
        bearer = await auth.verify_token(RAW)
        assert bearer is not None and bearer.claims[IDENTITY_CLAIM] == "ci"
        ok = await c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {RAW}"})
        assert ok.status_code == 200

        # someone GitHub authenticates but who is not allowed
        github["login"] = "mallory"
        token2 = await login(c)
        assert await auth.verify_token(token2) is None
        denied = await c.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {token2}"})
        assert denied.status_code == 401
    assert any(u.startswith("https://api.github.com/user") for u in github["calls"])


def test_tokens_only_is_unchanged() -> None:
    auth = build_auth(make_settings(), {"ci": hash_token(RAW)})
    assert type(auth) is NamedTokenVerifier


def test_oauth_only_without_tokens(tmp_path: Path) -> None:
    auth = build_auth(oauth_settings(tmp_path), {})
    assert isinstance(auth, StuttersMultiAuth) and auth.verifiers == []


def test_allow_list_globs(tmp_path: Path) -> None:
    auth = build_auth(oauth_settings(tmp_path), {})
    assert isinstance(auth, StuttersMultiAuth)
    assert auth.allowed("octocat") and auth.allowed("OPS-Amy") and not auth.allowed("octocat2")


@pytest.mark.parametrize(
    "kw, error",
    [
        ({"mcp_oauth_client_id": None}, "MCP_OAUTH_CLIENT_ID"),
        ({"mcp_oauth_allowed_users": None}, "MCP_OAUTH_ALLOWED_USERS"),
        ({"mcp_oauth_client_secret": None}, "MCP_OAUTH_CLIENT_SECRET"),
        ({"mcp_oauth_base_url": "http://mcp.example.com"}, "must be https"),
        ({"mcp_oauth_provider": "oidc"}, "MCP_OAUTH_CONFIG_URL"),
    ],
)
def test_settings_validation(tmp_path: Path, kw: dict[str, Any], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        oauth_settings(tmp_path, **kw)


async def test_current_caller_uses_identity_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastmcp.server.auth.auth import AccessToken

    from stutters_mcp_core import inbound_auth

    tok = AccessToken(
        token="x", client_id='["github","583231"]', scopes=[], claims={IDENTITY_CLAIM: "github:octocat"}
    )
    monkeypatch.setattr("fastmcp.server.dependencies.get_access_token", lambda: tok)
    assert inbound_auth.current_caller() == "github:octocat"
    plain = AccessToken(token="x", client_id="alice", scopes=[])
    monkeypatch.setattr("fastmcp.server.dependencies.get_access_token", lambda: plain)
    assert inbound_auth.current_caller() == "alice"
