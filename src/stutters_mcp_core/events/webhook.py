"""Webhook receiver: `POST <MCP_WEBHOOK_PATH>/<source>` (default /events/<source>) feeding the buffer.

    WebhookReceiver.from_settings(settings, "catc", parse=catc_webhook_events,
                                  auth=WebhookAuth.header("Authorization"))

Security model (docs/adr/003-events.md):
- The route sits outside MCP bearer auth (the product can't send our MCP token) and has its own,
  per-source authentication: a shared-secret header, an HMAC signature of the body, or HTTP Basic.
  There is no unauthenticated mode: a receiver without a secret refuses every request (503).
- `/mcp` keeps its bearer auth; webhook paths may not overlap MCP_PATH or /health.
- Checks run cheapest first: known source -> sender allow-list (403) -> rate limit (429) ->
  header/basic auth (401) -> body size (413) -> HMAC over the raw body (401) -> JSON (400).
- Behind a proxy or the SSH-tunnelled CML relay the peer address is the tunnel end, so allow-list
  that and rely on the secret. X-Forwarded-For is never trusted.

The route is mounted on the MCP HTTP app (MCP_TRANSPORT=http) unless MCP_WEBHOOK_PORT is set; then
a dedicated listener on MCP_WEBHOOK_HOST:MCP_WEBHOOK_PORT serves it (also works with stdio).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import logging
import socket
from collections.abc import Callable, Generator, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from stutters_mcp_core.events.limits import AllowList, PerKeyLimiter, TokenBucket
from stutters_mcp_core.events.model import Event
from stutters_mcp_core.events.source import EventSource, SourceConfigError

if TYPE_CHECKING:
    from stutters_mcp_core.settings import BaseServerSettings

log = logging.getLogger(__name__)

MIN_SECRET_LENGTH = 16


def parse_webhook_secrets(spec: str) -> dict[str, str]:
    """'source:secret' entries (comma/newline separated) -> {source: secret}. For basic auth the
    secret is 'username:password'. Secrets may not contain commas; an entry starting with '#' is a
    comment. Raises ValueError without echoing secrets."""
    out: dict[str, str] = {}
    for position, raw in enumerate(spec.replace("\n", ",").split(","), start=1):
        entry = raw.strip()
        if not entry or entry.startswith("#"):
            continue
        name, sep, secret = entry.partition(":")
        name, secret = name.strip(), secret.strip()
        if not sep or not name or not secret:
            raise ValueError(f"MCP_WEBHOOK_SECRETS entry #{position} must look like 'source:secret'")
        if name in out:
            raise ValueError(f"MCP_WEBHOOK_SECRETS names source {name!r} twice")
        out[name] = secret
    return out


@dataclass(frozen=True)
class WebhookAuth:
    """How one webhook source proves itself. Build with `header()`, `hmac()` or `basic()`. A `secret`
    of None is filled from MCP_WEBHOOK_SECRETS (`<source>:<secret>`) when the receiver starts."""

    mode: str  # "header" | "hmac" | "basic"
    header_name: str = ""
    secret: str | None = None
    scheme: str | None = None  # header mode: expected "<scheme> <secret>", e.g. "Bearer"
    algorithm: str = "sha256"  # hmac mode
    signature_prefix: str = ""  # hmac mode: e.g. "sha256=" (GitHub style); stripped if present
    encoding: str = "hex"  # hmac mode: "hex" or "base64"

    @classmethod
    def header(
        cls, header_name: str = "X-Webhook-Token", *, secret: str | None = None, scheme: str | None = None
    ) -> WebhookAuth:
        """The product sends a fixed secret in a header (Catalyst Center: a custom header or
        'Authorization')."""
        return cls("header", header_name=header_name, secret=secret, scheme=scheme)

    @classmethod
    def hmac(
        cls,
        header_name: str = "X-Signature-256",
        *,
        secret: str | None = None,
        algorithm: str = "sha256",
        prefix: str = "sha256=",
        encoding: str = "hex",
    ) -> WebhookAuth:
        """The product signs the raw body with a shared key and sends the digest in a header."""
        hashlib.new(algorithm)  # validates the name
        if encoding not in ("hex", "base64"):
            raise ValueError("encoding must be 'hex' or 'base64'")
        return cls(
            "hmac",
            header_name=header_name,
            secret=secret,
            algorithm=algorithm,
            signature_prefix=prefix,
            encoding=encoding,
        )

    @classmethod
    def basic(cls, username: str | None = None, password: str | None = None) -> WebhookAuth:
        """HTTP Basic. Credentials from the arguments or MCP_WEBHOOK_SECRETS 'source:user:password'."""
        secret = f"{username}:{password}" if username is not None and password is not None else None
        return cls("basic", header_name="Authorization", secret=secret)

    def with_secret(self, secret: str) -> WebhookAuth:
        return WebhookAuth(
            self.mode,
            self.header_name,
            secret,
            self.scheme,
            self.algorithm,
            self.signature_prefix,
            self.encoding,
        )

    def check_headers(self, headers: Mapping[str, str]) -> bool:
        """Header and basic modes (before the body is read). HMAC returns True here."""
        if self.secret is None:
            return False
        if self.mode == "header":
            got = headers.get(self.header_name, "")
            want = f"{self.scheme} {self.secret}" if self.scheme else self.secret
            return hmac.compare_digest(got.strip().encode(), want.encode())
        if self.mode == "basic":
            got = headers.get("Authorization", "")
            if not got.lower().startswith("basic "):
                return False
            try:
                decoded = base64.b64decode(got[6:].strip(), validate=True).decode()
            except (binascii.Error, UnicodeDecodeError):
                return False
            return hmac.compare_digest(decoded.encode(), self.secret.encode())
        return True

    def check_body(self, headers: Mapping[str, str], body: bytes) -> bool:
        if self.mode != "hmac":
            return True
        if self.secret is None:
            return False
        got = headers.get(self.header_name, "").strip()
        if self.signature_prefix and got.lower().startswith(self.signature_prefix.lower()):
            got = got[len(self.signature_prefix) :]
        digest = hmac.new(self.secret.encode(), body, self.algorithm).digest()
        want = digest.hex() if self.encoding == "hex" else base64.b64encode(digest).decode()
        if self.encoding == "hex":
            got = got.lower()
        return hmac.compare_digest(got.encode(), want.encode())


ParseFn = Callable[[Any, Mapping[str, str]], Event | Iterable[Event] | None]


def default_webhook_events(payload: Any, headers: Mapping[str, str]) -> Iterable[Event]:
    """Generic mapping: one event per JSON object (a top-level list gives several); type "webhook" or
    the payload's 'type'/'eventType'/'category'; id from 'id'/'eventId'/'instanceId'."""
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if not isinstance(item, dict):
            yield Event(type="webhook", summary=str(item)[:300], data={"payload": item})
            continue
        kind = item.get("type") or item.get("eventType") or item.get("category") or "webhook"
        ident = item.get("id") or item.get("eventId") or item.get("instanceId")
        summary = item.get("summary") or item.get("message") or item.get("description") or item.get("name")
        yield Event(
            type=str(kind),
            summary=str(summary or "")[:300],
            severity=str(item["severity"]) if isinstance(item.get("severity"), str) else None,
            data=item,
            id=str(ident) if ident is not None else None,
        )


class WebhookReceiver(EventSource):
    """One webhook source at `<path>/<name>` (kind "push"). See the module docstring."""

    kind = "push"

    def __init__(
        self,
        name: str,
        *,
        auth: WebhookAuth,
        parse: ParseFn | None = None,
        allow: str | list[str] | None = None,
        max_body: int = 262_144,
        rate_limit: float | None = 20.0,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("description", f"Webhook receiver for {name}")
        super().__init__(name, **kwargs)
        self.auth = auth
        self.parse = parse or default_webhook_events
        self.allow = AllowList(allow)
        self.max_body = max_body
        # Unauthenticated traffic is limited per sender before auth, so junk from one host can't use
        # up the source's budget; the per-source bucket is charged only for authenticated requests.
        self.peer_limiter = PerKeyLimiter(rate_limit)
        self.bucket = TokenBucket(rate_limit) if rate_limit else None
        self.requests = 0
        self.listen = ""  # set by the hub
        self.served = True  # set by the hub: False when no HTTP listener serves the route
        self._ready = False

    @classmethod
    def from_settings(
        cls,
        settings: BaseServerSettings,
        name: str,
        *,
        auth: WebhookAuth | None = None,
        parse: ParseFn | None = None,
        **kwargs: Any,
    ) -> WebhookReceiver:
        """A receiver configured by MCP_WEBHOOK_*; disabled (but listed) unless MCP_WEBHOOKS_ENABLED=true.
        The secret comes from MCP_WEBHOOK_SECRETS unless `auth` carries one."""
        auth = auth or WebhookAuth.header()
        if auth.secret is None:
            secrets = settings.webhook_secrets()
            if name in secrets:
                auth = auth.with_secret(secrets[name])
        kwargs.setdefault("enabled", settings.mcp_webhooks_enabled)
        kwargs.setdefault(
            "disabled_reason", f"set MCP_WEBHOOKS_ENABLED=true and MCP_WEBHOOK_SECRETS={name}:<secret>"
        )
        kwargs.setdefault("allow", settings.mcp_webhook_allow_ips or None)
        kwargs.setdefault("max_body", settings.mcp_webhook_max_body)
        kwargs.setdefault("rate_limit", settings.mcp_webhook_rate_limit)
        return cls(name, auth=auth, parse=parse, **kwargs)

    async def run(self) -> None:
        if self.auth.secret is None:
            raise SourceConfigError(
                f"no secret configured for webhook source {self.name!r}: "
                f"set MCP_WEBHOOK_SECRETS={self.name}:<secret>"
            )
        if not self.served:
            raise SourceConfigError(f"webhook route not served: {self.listen}")
        secret = self.auth.secret.partition(":")[2] if self.auth.mode == "basic" else self.auth.secret
        if len(secret) < MIN_SECRET_LENGTH:
            raise SourceConfigError(
                f"webhook secret for {self.name!r} is shorter than {MIN_SECRET_LENGTH} characters"
            )
        if self.allow.open:
            log.info("webhook source %s accepts any sender address (auth still required)", self.name)
        self._ready = True
        self.set_connected()
        try:
            await asyncio.Event().wait()
        finally:
            self._ready = False

    async def handle(self, request: Request) -> Response:
        self.requests += 1
        if not self._ready:
            return JSONResponse({"error": "receiver not ready"}, status_code=503)
        peer = request.client.host if request.client else None
        if not self.allow.allows(peer):
            self.drop("sender_not_allowed")
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if not self.peer_limiter.allow(peer or ""):
            self.drop("rate_limited")
            return JSONResponse({"error": "rate limited"}, status_code=429, headers={"Retry-After": "1"})
        if not self.auth.check_headers(request.headers):
            self.drop("auth_failed")
            headers = {"WWW-Authenticate": 'Basic realm="webhook"'} if self.auth.mode == "basic" else None
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers=headers)
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_body):
            self.drop("oversize")
            return JSONResponse({"error": "body too large"}, status_code=413)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > self.max_body:
                self.drop("oversize")
                return JSONResponse({"error": "body too large"}, status_code=413)
        if not self.auth.check_body(request.headers, bytes(body)):
            self.drop("auth_failed")
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if self.bucket is not None and not self.bucket.take():
            self.drop("rate_limited")
            return JSONResponse({"error": "rate limited"}, status_code=429, headers={"Retry-After": "1"})
        try:
            payload: Any = json.loads(body) if body.strip() else {}
        except ValueError:
            self.drop("invalid_json")
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        try:
            result = self.parse(payload, request.headers)
            events = [result] if isinstance(result, Event) else list(result or [])
        except Exception:  # noqa: BLE001 - never 500 on a product payload we don't understand
            log.warning("webhook %s: parse failed", self.name, exc_info=True)
            self.drop("parse_error")
            return JSONResponse({"error": "payload not understood"}, status_code=422)
        accepted = sum(1 for ev in events if self.emit(ev) is not None)
        return JSONResponse({"accepted": accepted, "duplicates": len(events) - accepted}, status_code=202)

    def extra_status(self) -> dict[str, Any]:
        return {
            "listen": self.listen or None,
            "auth": self.auth.mode,
            "secret_configured": self.auth.secret is not None,
            "allowed_senders": str(self.allow),
            "requests": self.requests,
        }


# --- dedicated listener (MCP_WEBHOOK_PORT) ------------------------------------------------


class WebhookListener:
    """A small uvicorn server for the webhook routes, separate from the MCP app."""

    def __init__(self, app: Any, host: str, port: int) -> None:
        import uvicorn

        class _Server(uvicorn.Server):
            @contextlib.contextmanager
            def capture_signals(self) -> Generator[None]:
                yield  # leave signal handling to the MCP server

        config = uvicorn.Config(
            app, host=host, port=port, lifespan="off", log_level="warning", access_log=False
        )
        self.server = _Server(config)
        self.host = host
        self.port = port
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        # Bind here so a busy port raises OSError (uvicorn would call sys.exit on a bind failure).
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
        except OSError:
            sock.close()
            raise
        sock.set_inheritable(True)
        self.port = int(sock.getsockname()[1])
        self._task = asyncio.create_task(self.server.serve(sockets=[sock]), name="webhook-listener")
        for _ in range(500):
            if self.server.started or self._task.done():
                break
            await asyncio.sleep(0.01)
        if self._task.done():
            sock.close()
            raise OSError(f"webhook listener failed to start on {self.host}:{self.port}")

    async def stop(self) -> None:
        self.server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(self._task, 5)
            self._task = None
