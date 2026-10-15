"""OAuth 2.1 for the HTTP transport (roadmap G1), alongside the named bearer tokens.

Off unless MCP_OAUTH_PROVIDER is set. Interactive MCP clients (Claude Code, Claude Desktop, the MCP
inspector) then discover the server's OAuth metadata, register themselves (dynamic client registration),
and send the user through the identity provider's login; FastMCP's OAuth proxy issues the client its own
short-lived tokens and checks the upstream token on every request. Automation keeps using the named
bearer tokens from MCP_AUTH_TOKENS: both are accepted (FastMCP MultiAuth).

    MCP_OAUTH_PROVIDER=github            github | oidc (any OIDC-compliant IdP: Entra ID, Okta, Keycloak...)
    MCP_OAUTH_CLIENT_ID=Ov23li...        the OAuth app registered with the IdP
    MCP_OAUTH_CLIENT_SECRET(_FILE)=...
    MCP_OAUTH_BASE_URL=https://mcp.example.com:8036   public URL (callback: <base>/auth/callback)
    MCP_OAUTH_ALLOWED_USERS=octocat,alice,*@example.com   required: who may log in (globs over the login)
    MCP_OAUTH_CONFIG_URL=https://idp/.well-known/openid-configuration   (oidc only)
    MCP_OAUTH_SCOPES=read:user           IdP scopes to require (github default: user; oidc default: openid)
    MCP_OAUTH_REDIRECT_URIS=http://localhost:*,http://127.0.0.1:*      allowed MCP-client redirect URIs
    MCP_OAUTH_JWT_KEY(_FILE)=...         key for the tokens this server issues (default: from the secret)
    MCP_OAUTH_STORAGE_DIR=/cache/oauth   encrypted client registrations and upstream tokens

The caller identity is `<provider>:<login>` (e.g. `github:octocat`, `oidc:alice@example.com`), which is
what MCP_ROLES, the audit log, previews and two-person confirm see. Bearer-token callers keep their
token name.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastmcp.server.auth import AuthProvider, MultiAuth
from fastmcp.server.auth.auth import AccessToken

from stutters_mcp_core.inbound_auth import NamedTokenVerifier

if TYPE_CHECKING:
    from stutters_mcp_core.settings import BaseServerSettings

log = logging.getLogger(__name__)

IDENTITY_CLAIM = "stutters_identity"
TOKENS_SOURCE = "tokens"
_LOGIN_CLAIMS = ("login", "preferred_username", "email", "upn", "unique_name", "sub")


def _split(spec: str | None) -> list[str]:
    return [p.strip() for p in re.split(r"[,\n]", spec or "") if p.strip()]


def login_of(token: AccessToken) -> str | None:
    claims = token.claims or {}
    for key in _LOGIN_CLAIMS:
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class StuttersMultiAuth(MultiAuth):
    """MultiAuth that (1) names every caller (`<token name>` or `<provider>:<login>`) in the access
    token's claims, and (2) admits OAuth users only when their login matches MCP_OAUTH_ALLOWED_USERS."""

    def __init__(self, *, provider: str, allowed_users: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.provider = provider
        self.allowed_users = allowed_users

    def allowed(self, login: str) -> bool:
        return any(fnmatch.fnmatchcase(login.lower(), p.lower()) for p in self.allowed_users)

    async def verify_token(self, token: str) -> AccessToken | None:
        result = await super().verify_token(token)
        if result is None:
            return None
        try:
            source = json.loads(result.client_id)[0]
        except (ValueError, TypeError, IndexError, KeyError):
            source = None
        update: dict[str, Any] = {}
        if source == TOKENS_SOURCE:
            identity = str(result.original_client_id or "")
            # Named bearer tokens carry no OAuth scopes; they are trusted like before, so they satisfy
            # the IdP scopes the server requires of OAuth tokens.
            update["scopes"] = sorted({*result.scopes, *(self.required_scopes or [])})
        else:
            login = login_of(result)
            if login is None or not self.allowed(login):
                log.warning(
                    "OAuth login %r refused: not in MCP_OAUTH_ALLOWED_USERS", login or "(no login claim)"
                )
                return None
            identity = f"{self.provider}:{login}"
        if not identity:
            return None
        update["claims"] = {**(result.claims or {}), IDENTITY_CLAIM: identity}
        return result.model_copy(update=update)


def oauth_server(settings: BaseServerSettings) -> AuthProvider:
    """The FastMCP OAuth proxy for MCP_OAUTH_PROVIDER (constructed, not started)."""
    import fastmcp

    provider = settings.mcp_oauth_provider
    secret = settings.mcp_oauth_client_secret.get_secret_value() if settings.mcp_oauth_client_secret else None
    jwt_key = settings.mcp_oauth_jwt_key.get_secret_value() if settings.mcp_oauth_jwt_key else None
    redirects = _split(settings.mcp_oauth_redirect_uris) or None
    scopes = _split(settings.mcp_oauth_scopes) or None
    storage = settings.mcp_oauth_storage_dir
    if storage is not None:
        storage.mkdir(parents=True, exist_ok=True, mode=0o700)
        # FastMCP keeps its encrypted OAuth state under settings.home/oauth-proxy/<key fingerprint>.
        fastmcp.settings.home = Path(storage)
    common: dict[str, Any] = {
        "client_id": settings.mcp_oauth_client_id,
        "base_url": settings.mcp_oauth_base_url,
        "allowed_client_redirect_uris": redirects,
        "jwt_signing_key": jwt_key,
    }
    if provider == "github":
        from fastmcp.server.auth.providers.github import GitHubProvider

        return GitHubProvider(client_secret=secret or "", required_scopes=scopes, **common)
    from fastmcp.server.auth.oidc_proxy import OIDCProxy

    return OIDCProxy(
        config_url=str(settings.mcp_oauth_config_url),
        client_secret=secret,
        required_scopes=scopes or ["openid"],
        **common,
    )


def build_auth(settings: BaseServerSettings, tokens: dict[str, str]) -> AuthProvider | None:
    """The HTTP auth provider: named bearer tokens alone (unchanged behaviour), or OAuth plus tokens."""
    named = NamedTokenVerifier(tokens) if tokens else None
    if not settings.mcp_oauth_provider:
        return named
    verifiers = {TOKENS_SOURCE: named} if named is not None else {}
    return StuttersMultiAuth(
        provider=settings.mcp_oauth_provider,
        allowed_users=_split(settings.mcp_oauth_allowed_users),
        server=oauth_server(settings),
        server_source_id=settings.mcp_oauth_provider,
        verifiers=verifiers or None,
    )
