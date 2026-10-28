"""Async REST client used by every server: auth strategy, retries, throttling, error mapping.

One ApiClient talks to one base URL. Products with several API surfaces (ISE: OpenAPI, ERS, MnT)
create one client per surface and may share a RateLimiter between them.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import re
import time
from collections.abc import Callable, Iterable
from typing import Any, Literal

import httpx

from stutters_mcp_core import __version__, clock, metrics
from stutters_mcp_core.errors import (
    AuthError,
    Conflict,
    NotFound,
    RateLimited,
    Unreachable,
    UpstreamError,
    UpstreamTimeout,
    ValidationFailed,
)
from stutters_mcp_core.http.auth import AuthStrategy, NoAuth
from stutters_mcp_core.http.ratelimit import RateLimiter
from stutters_mcp_core.secrets import scrub_text

log = logging.getLogger(__name__)

IDEMPOTENT = {"GET", "HEAD", "OPTIONS"}
RETRY_ANY_METHOD = {429}  # the request was not processed, so retrying is always safe
RETRY_IDEMPOTENT = {502, 503, 504}
MAX_RETRY_AFTER = 60.0

Expect = Literal["json", "text", "bytes", "response"]
# Turns an error response body into a short human-readable detail string.
ErrorExtractor = Callable[[Any], str | None]


def default_error_detail(body: Any) -> str | None:
    """Best-effort message from common error shapes."""
    if isinstance(body, str):
        return body.strip()[:500] or None
    if isinstance(body, list):
        parts = [default_error_detail(b) for b in body]
        return " | ".join(p for p in parts if p) or None
    if not isinstance(body, dict):
        return None
    for key in ("message", "detail", "description", "error_description", "errorMessage", "title"):
        v = body.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:500]
    for key in ("error", "errors", "response", "messages"):
        if key in body and (d := default_error_detail(body[key])):
            return d
    return None


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(MAX_RETRY_AFTER, max(0.0, float(value)))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None
    return min(MAX_RETRY_AFTER, max(0.0, when - time.time()))


def _backoff(attempt: int) -> float:
    return float(min(10.0, 0.5 * 2**attempt) + random.uniform(0, 0.25))


# A 403 detail that names the reason as a feature (flag) or a licence rather than the login (C72).
_FEATURE_REFUSAL_RE = re.compile(r"\bfeature|\blicen[cs]", re.IGNORECASE)


class ApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        product: str = "API",
        env_prefix: str = "",
        auth: AuthStrategy | None = None,
        verify: bool | str = True,
        timeout: float = 30.0,
        retries: int = 2,
        limiter: RateLimiter | None = None,
        headers: dict[str, str] | None = None,
        error_detail: ErrorExtractor | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`product` names the system in error messages ("ISE", "FMC"); `env_prefix` ("ISE_") lets
        error hints name the right settings."""
        self.base_url = base_url.rstrip("/")
        self.product = product
        self.env_prefix = env_prefix
        self.auth: AuthStrategy = auth or NoAuth()
        self.retries = retries
        self.limiter = limiter or RateLimiter()
        self._error_detail = error_detail or default_error_detail
        self._http = httpx.AsyncClient(
            base_url=self.base_url + "/",
            headers={
                "Accept": "application/json",
                "User-Agent": f"stutters-mcp-core/{__version__}",
                **(headers or {}),
            },
            verify=verify,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )

    # --- lifecycle -----------------------------------------------------------------------

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> ApiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # --- low level -----------------------------------------------------------------------

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            if not path.startswith(self.base_url + "/") and path != self.base_url:
                raise ValueError(f"Refusing to call {path}: outside {self.base_url}")
            return path
        return path.lstrip("/")

    async def send_raw(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One request with no auth, retries or error mapping (used by login flows). Connection
        problems still become Unreachable/UpstreamTimeout."""
        try:
            return await self._send(method.upper(), path, **kwargs)
        except httpx.TimeoutException as e:
            raise UpstreamTimeout(f"{self.product} request timed out: {method.upper()} {path}") from e
        except httpx.TransportError as e:
            raise self._unreachable(e) from e

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One HTTP exchange through the rate limiter, recording metrics (G5)."""
        waited = time.monotonic()
        async with self.limiter.slot():
            started = time.monotonic()
            metrics.record_rate_wait(self.product, started - waited)
            sent = time.time()
            try:
                resp = await self._http.request(method, self._url(path), **kwargs)
            except httpx.TimeoutException:
                metrics.record_upstream(self.product, method, "timeout", time.monotonic() - started)
                raise
            except httpx.TransportError:
                metrics.record_upstream(self.product, method, "error", time.monotonic() - started)
                raise
            metrics.record_upstream(self.product, method, str(resp.status_code), time.monotonic() - started)
            clock.observe_response(self.base_url, resp.headers, sent=sent)  # appliance clock offset (C69)
            return resp

    def _unreachable(self, e: Exception) -> Unreachable:
        text = str(e)
        hint = ""
        if "CERTIFICATE" in text.upper() or "SSL" in text.upper():
            hint = (
                f" TLS verification failed: set {self.env_prefix}CA_BUNDLE to the appliance's CA, "
                f"or {self.env_prefix}VERIFY_SSL=false for labs."
            )
        return Unreachable(
            scrub_text(
                f"Could not connect to {self.product} at {self.base_url}: {text or type(e).__name__}.{hint}"
            )
        )

    def _credentials_hint(self) -> str:
        return (
            f"Check {self.env_prefix}USERNAME/{self.env_prefix}PASSWORD and that the account's role "
            "allows this operation."
        )

    def forbidden_hint(self, detail: str) -> str | None:
        """The sentence after a 403's detail (C72); None for none. Override per product for refusals the
        default doesn't recognise. When the detail already says the refusal is about a feature (flag) or a
        licence, e.g. FMC's "Feature Flag : bulk_device_command_execution is disabled for the request",
        the credentials sentence is left out: the login worked."""
        if _FEATURE_REFUSAL_RE.search(detail):
            return (
                f"{self.product} refused it on a feature or licence setting, not the login: check that the "
                "feature is enabled and licensed, and that the account's role allows this operation."
            )
        return self._credentials_hint()

    def error_from_response(self, resp: httpx.Response) -> UpstreamError:
        body: Any
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        detail = scrub_text(self._error_detail(body) or resp.reason_phrase or "no detail")
        code = resp.status_code
        where = f"{resp.request.method} {resp.request.url.path}"
        if code == 401:
            return AuthError(
                f"{self.product} denied access ({code}) on {where}: {detail}. {self._credentials_hint()}",
                code,
            )
        if code == 403:
            hint = self.forbidden_hint(detail)
            return AuthError(
                f"{self.product} denied access ({code}) on {where}: {detail}." + (f" {hint}" if hint else ""),
                code,
            )
        if code == 404:
            return NotFound(
                f"Not found (404): {where}. Check the id/name, or list/search for it first.", code
            )
        if code == 409:
            return Conflict(f"{self.product} reported a conflict (409) on {where}: {detail}", code)
        if code in (400, 422):
            return ValidationFailed(
                f"{self.product} rejected the request ({code}) on {where}: {detail}", code
            )
        if code == 429:
            return RateLimited(
                f"{self.product} rate limit hit (429) on {where}. Wait a minute or narrow the query.", code
            )
        if 300 <= code < 400:
            loc = scrub_text(resp.headers.get("Location", "?"))
            return UpstreamError(
                f"{self.product} redirected {where} to {loc} ({code}); this API/service may be disabled.",
                code,
            )
        return UpstreamError(f"{self.product} returned HTTP {code} on {where}: {detail}", code)

    # --- main entry point ----------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        content: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        expect: Expect = "json",
        retry: bool | None = None,
        ok_status: Iterable[int] = (),
        timeout: float | None = None,  # noqa: ASYNC109 - per-request HTTP timeout, not a cancel scope
    ) -> Any:
        """Send a request relative to base_url. Returns parsed JSON (None for empty bodies), text,
        bytes, or the httpx.Response depending on `expect`. Statuses in `ok_status` (e.g. {500} for an
        API that reports "not found" as 500 with a body) are returned instead of raised; use
        expect="response" to inspect the status. `retry` overrides whether 5xx/connection
        errors are retried (default: only for idempotent methods); 429 is always retried."""
        method = method.upper()
        ok_codes = frozenset(ok_status)
        retry_5xx = method in IDEMPOTENT if retry is None else retry
        reauthed = False
        attempt = 0
        while True:
            auth_headers = await self.auth.headers(self)
            try:
                resp = await self._send(
                    method,
                    path,
                    params=params,
                    json=json,
                    content=content,
                    headers={**auth_headers, **(headers or {})},
                    timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if retry_5xx and attempt < self.retries:
                    attempt += 1
                    metrics.record_retry(
                        self.product, "timeout" if isinstance(e, httpx.TimeoutException) else "error"
                    )
                    await asyncio.sleep(_backoff(attempt))
                    continue
                if isinstance(e, httpx.TimeoutException):
                    raise UpstreamTimeout(
                        f"{self.product} request timed out: {method} /{path.lstrip('/')}"
                    ) from e
                raise self._unreachable(e) from e

            code = resp.status_code
            if code == 401 and not reauthed:
                reauthed = True
                if await self.auth.on_unauthorized(self, auth_headers):
                    continue
            if attempt < self.retries and (
                code in RETRY_ANY_METHOD or (retry_5xx and code in RETRY_IDEMPOTENT)
            ):
                attempt += 1
                metrics.record_retry(self.product, str(code))
                delay = _retry_after(resp)
                log.warning("%s %s on %s %s; retry %d", self.product, code, method, path, attempt)
                await asyncio.sleep(delay if delay is not None else _backoff(attempt))
                continue
            if (resp.is_error or resp.is_redirect) and code not in ok_codes:
                raise self.error_from_response(resp)
            return self._decode(resp, expect)

    @staticmethod
    def _decode(resp: httpx.Response, expect: Expect) -> Any:
        if expect == "response":
            return resp
        if expect == "bytes":
            return resp.content
        if expect == "text":
            return resp.text
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    async def get(self, path: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        return await self.request("GET", path, params=params, **kw)

    async def post(self, path: str, json: Any = None, **kw: Any) -> Any:
        return await self.request("POST", path, json=json, **kw)

    async def put(self, path: str, json: Any = None, **kw: Any) -> Any:
        return await self.request("PUT", path, json=json, **kw)

    async def patch(self, path: str, json: Any = None, **kw: Any) -> Any:
        return await self.request("PATCH", path, json=json, **kw)

    async def delete(self, path: str, **kw: Any) -> Any:
        return await self.request("DELETE", path, **kw)
