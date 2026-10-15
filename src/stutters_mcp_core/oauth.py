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
    MCP_OAUTH_REDIRECT_URIS=...          allowed MCP-client redirect URIs (default: loopback only)
    MCP_OAUTH_LOGIN_CLAIM=email          oidc: the claim used as login (email requires email_verified)
    MCP_OAUTH_JWT_KEY(_FILE)=...         key for the tokens this server issues (default: from the secret)
    MCP_OAUTH_STORAGE_DIR=/cache/oauth   encrypted client registrations and upstream tokens
                                         (default <cache dir>/oauth)

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
DEFAULT_REDIRECT_URIS = ["http://localhost:*", "http://127.0.0.1:*"]
MIN_JWT_KEY = 32


def _split(spec: str | None) -> list[str]:
    return [p.strip() for p in re.split(r"[,\n]", spec or "") if p.strip()]


def login_of(token: AccessToken, provider: str, claim: str | None = None) -> str | None:
    """The login the allow-list and identity use, from a claim that can't be chosen by the user:
    GitHub `login` (from api.github.com/user); OIDC `MCP_OAUTH_LOGIN_CLAIM` (default `email`, which then
    requires `email_verified: true`). Lowercased."""
    claims = token.claims or {}
    key = "login" if provider == "github" else (claim or "email")
    value = claims.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    if key == "email" and claims.get("email_verified") is not True:
        return None
    return value.strip().lower()


def github_id_of(token: AccessToken) -> str | None:
    claims = token.claims or {}
    sub = claims.get("sub")
    return str(sub) if sub is not None else None


class StuttersMultiAuth(MultiAuth):
    """MultiAuth that (1) names every caller (`<token name>` or `<provider>:<login>`) in the access
    token's claims, and (2) admits OAuth users only when their login matches MCP_OAUTH_ALLOWED_USERS."""

    def __init__(
        self, *, provider: str, allowed_users: list[str], login_claim: str | None = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.provider = provider
        self.allowed_users = allowed_users
        self.login_claim = login_claim

    def allowed(self, login: str, user_id: str | None = None) -> bool:
        """Glob match on the login, or `id:<n>` entries on the IdP's stable user id (GitHub: numeric id,
        which survives a login rename and can't be re-registered by someone else)."""
        for pattern in self.allowed_users:
            p = pattern.lower()
            if p.startswith("id:"):
                if user_id is not None and p[3:] == user_id.lower():
                    return True
            elif fnmatch.fnmatchcase(login.lower(), p):
                return True
        return False

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
            login = login_of(result, self.provider, self.login_claim)
            if login is None or not self.allowed(login, github_id_of(result)):
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
    provider = settings.mcp_oauth_provider
    secret = settings.mcp_oauth_client_secret.get_secret_value() if settings.mcp_oauth_client_secret else None
    jwt_key = settings.mcp_oauth_jwt_key.get_secret_value() if settings.mcp_oauth_jwt_key else None
    redirects = _split(settings.mcp_oauth_redirect_uris) or DEFAULT_REDIRECT_URIS
    if any(r.strip() == "*" for r in redirects):
        log.warning("MCP_OAUTH_REDIRECT_URIS allows any redirect URI: the consent page is the only defence")
    scopes = _split(settings.mcp_oauth_scopes) or None
    common: dict[str, Any] = {
        "client_id": settings.mcp_oauth_client_id,
        "base_url": settings.mcp_oauth_base_url,
        "allowed_client_redirect_uris": redirects,
        "jwt_signing_key": jwt_key,
        "client_storage": _storage(settings, jwt_key or secret or ""),
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


def _storage(settings: BaseServerSettings, key_material: str) -> Any:
    """Encrypted file store for client registrations and upstream tokens, in MCP_OAUTH_STORAGE_DIR (else
    <cache dir>/oauth). Directory mode 0700; the encryption key derives from the JWT key or client secret."""
    from cryptography.fernet import Fernet
    from fastmcp.server.auth.jwt_issuer import derive_jwt_key
    from key_value.aio.stores.filetree import (
        FileTreeStore,
        FileTreeV1CollectionSanitizationStrategy,
        FileTreeV1KeySanitizationStrategy,
    )
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    directory = settings.mcp_oauth_storage_dir
    if directory is None:
        base = settings.cache_dir_for("stutters-mcp")
        if base is None:
            raise ValueError("MCP_OAUTH_STORAGE_DIR is required when MCP_CACHE_DIR is disabled")
        directory = base / "oauth"
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    key = derive_jwt_key(high_entropy_material=key_material, salt="stutters-oauth-storage")
    store = FileTreeStore(
        data_directory=directory,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(directory),
    )
    return FernetEncryptionWrapper(key_value=store, fernet=Fernet(key=key), raise_on_decryption_error=False)


def build_auth(settings: BaseServerSettings, tokens: dict[str, str]) -> AuthProvider | None:
    """The HTTP auth provider: named bearer tokens alone (unchanged behaviour), or OAuth plus tokens."""
    named = NamedTokenVerifier(tokens) if tokens else None
    if not settings.mcp_oauth_provider:
        return named
    verifiers = {TOKENS_SOURCE: named} if named is not None else {}
    allowed = _split(settings.mcp_oauth_allowed_users)
    if any(p in ("*", "*@*") for p in allowed):
        log.warning("MCP_OAUTH_ALLOWED_USERS admits every user the identity provider authenticates")
    return StuttersMultiAuth(
        provider=settings.mcp_oauth_provider,
        allowed_users=allowed,
        login_claim=settings.mcp_oauth_login_claim,
        server=oauth_server(settings),
        server_source_id=settings.mcp_oauth_provider,
        verifiers=verifiers or None,
    )
