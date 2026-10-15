"""Outbound auth strategies (this server -> the managed product).

    BasicAuth          ISE (every surface)
    StaticHeaderAuth   API keys / pre-issued tokens
    LoginTokenAuth     CML, Catalyst Center, FMC: log in for a token, re-login on expiry or 401,
                       optional refresh hook (FMC refresh tokens)

Strategies only set headers; the ApiClient owns retries and error mapping. Login calls go through
`ApiClient.send_raw`, which skips auth (so a login can't recurse into itself).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from stutters_mcp_core.http.client import ApiClient

log = logging.getLogger(__name__)


class AuthStrategy(Protocol):
    async def headers(self, client: ApiClient) -> dict[str, str]:
        """Headers to add to an outgoing request (may log in first)."""
        ...

    async def on_unauthorized(self, client: ApiClient, sent: dict[str, str]) -> bool:
        """Called after a 401. Return True if credentials were renewed and the request should be
        retried once. `sent` are the headers the failed request carried."""
        ...


class NoAuth:
    async def headers(self, client: ApiClient) -> dict[str, str]:
        return {}

    async def on_unauthorized(self, client: ApiClient, sent: dict[str, str]) -> bool:
        return False


def basic_header(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


class BasicAuth:
    def __init__(self, username: str, password: str) -> None:
        self._header = {"Authorization": basic_header(username, password)}

    async def headers(self, client: ApiClient) -> dict[str, str]:
        return dict(self._header)

    async def on_unauthorized(self, client: ApiClient, sent: dict[str, str]) -> bool:
        return False


class StaticHeaderAuth:
    def __init__(self, name: str, value: str) -> None:
        self._header = {name: value}

    async def headers(self, client: ApiClient) -> dict[str, str]:
        return dict(self._header)

    async def on_unauthorized(self, client: ApiClient, sent: dict[str, str]) -> bool:
        return False


@dataclass
class Token:
    """A session credential. `headers` are added to each request; `expires_at` is time.monotonic()
    based (None = unknown, renewed only on 401); `data` holds anything else the login returned
    (refresh tokens, domain ids, ...)."""

    headers: dict[str, str]
    expires_at: float | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def expiring_in(seconds: float | None) -> float | None:
        return None if seconds is None else time.monotonic() + seconds


LoginFn = Callable[["ApiClient"], Awaitable[Token]]
RefreshFn = Callable[["ApiClient", Token], Awaitable[Token | None]]


def _register_token(token: Token) -> None:
    """Session credentials never reach logs or tool errors (C14): mask the header values and secret-named
    data (refresh tokens...) the login returned."""
    from stutters_mcp_core.formatting import is_secret_key
    from stutters_mcp_core.secrets import register_header_values, register_secret

    register_header_values(token.headers)
    for key, value in token.data.items():
        if isinstance(value, str) and is_secret_key(key):
            register_secret(value)


class LoginTokenAuth:
    """Log in lazily, share one token across concurrent requests (single-flight), renew it shortly
    before it expires or after a 401. If `refresh` is given it is tried before a full login and may
    return None to force one."""

    def __init__(self, login: LoginFn, *, refresh: RefreshFn | None = None, margin: float = 60.0) -> None:
        self._login = login
        self._refresh = refresh
        self._margin = margin
        self._token: Token | None = None
        self._lock = asyncio.Lock()

    @property
    def token(self) -> Token | None:
        return self._token

    def _fresh(self) -> bool:
        t = self._token
        return t is not None and (t.expires_at is None or time.monotonic() < t.expires_at - self._margin)

    async def _renew(self, client: ApiClient) -> Token:
        token = await self._obtain(client)
        _register_token(token)
        return token

    async def _obtain(self, client: ApiClient) -> Token:
        old = self._token
        if old is not None and self._refresh is not None:
            try:
                new = await self._refresh(client, old)
            except (httpx.HTTPError, ValueError, KeyError) as e:
                log.info("Token refresh failed (%s); logging in again", e)
                new = None
            if new is not None:
                return new
        return await self._login(client)

    async def token_for(self, client: ApiClient) -> Token:
        if self._fresh():
            assert self._token is not None
            return self._token
        async with self._lock:
            if not self._fresh():
                self._token = await self._renew(client)
            assert self._token is not None
            return self._token

    async def headers(self, client: ApiClient) -> dict[str, str]:
        return dict((await self.token_for(client)).headers)

    async def on_unauthorized(self, client: ApiClient, sent: dict[str, str]) -> bool:
        async with self._lock:
            # Another request may already have renewed the token since this one was sent.
            if self._token is not None and self._token.headers.items() <= sent.items():
                self._token = await self._renew(client)
            elif self._token is None:
                self._token = await self._login(client)
                _register_token(self._token)
        return True

    def invalidate(self) -> None:
        self._token = None
