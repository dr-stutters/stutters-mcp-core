"""Secret hygiene (roadmap G4): scrub secrets out of log lines and messages, and pluggable secret loaders.

Two layers keep credentials out of logs, audit events and `<prefix>_status`:

1. `scrub_text(text)` masks credentials by shape: `user:pass@` in URLs, secret-looking query parameters
   (`?token=...`, `password=`), `Authorization`-style `Bearer` / `Basic` values, and the secret on IOS /
   NX-OS config lines (`enable secret`, `username ... secret`, `snmp-server community`, `key 7 ...`,
   `crypto isakmp key`; stutters_mcp_core.config_mask). Used for upstream error messages, the status
   `url`, audit `error` fields and every string in audited arguments and previews.
2. `install_secret_filter(settings)` masks the actual configured secret values (every `SecretStr` setting,
   the Basic-auth base64, raw `MCP_AUTH_TOKENS` entries, and values passed to `register_secret`) in every
   log record the process creates, via the log record factory, so non-propagating loggers (fastmcp,
   uvicorn) and handlers added later are covered. The CLI installs it at startup.
3. `SecretErrorMiddleware` applies the same to tool errors returned to the MCP client.

Secret loaders: every `SecretStr` setting can also be given as a reference, `<VAR>_REF=<scheme>:<ref>`,
resolved at startup by a loader registered for the scheme. Core ships `file:` (a JSON or KEY=VALUE file,
`file:/run/secrets/creds.json#ise_password`); Vault, 1Password and the like plug in as a class with
`load(ref) -> str`, named in `MCP_SECRET_LOADERS="vault=mypkg.loaders:VaultLoader"` (see
docs/PRODUCTION.md for worked examples). `<VAR>`, `<VAR>_FILE` and `<VAR>_REF` are mutually exclusive.
MCP_SECRET_LOADERS is trusted configuration (it imports code): only name modules installed in the image.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from pydantic import SecretStr

from stutters_mcp_core.config_mask import mask_command_lines

MASK = "********"  # same as formatting.MASK (not imported: http.client imports this module)

MIN_SECRET_LEN = 4  # shorter values would mask unrelated text

_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<userinfo>[^/\s@?#]+)@")
# key=value in query strings and log text; the key is checked by _secret_key (whole words only).
_KEY_VALUE = re.compile(r"(?P<lead>(?:^|(?<=[?&;\s,(])))(?P<key>[A-Za-z][\w.-]*)=(?P<value>[^&\s#;'\",)]+)")
# "key": "value" (JSON) and Header-Name: value
_JSON_PAIR = re.compile(
    r"(?P<key>(?P<q>[\"'])[A-Za-z][\w.-]*(?P=q))\s*:\s*(?P<q2>[\"'])(?P<value>(?:(?!(?P=q2)).)*)(?P=q2)"
)
_AUTH_HEADER = re.compile(
    r"(?P<key>\b(?:Proxy-)?Authorization\s*[:=]\s*(?:Basic|Bearer|Digest|Negotiate|Token|NTLM)\s+)(?P<value>\S+)",
    re.IGNORECASE,
)
# Cookie / Set-Cookie / X-...-Token headers: the whole rest of the line.
_LINE_HEADER = re.compile(
    r"(?P<key>\b(?:(?:Set-)?Cookie|X-[\w-]*(?:Token|Key|Secret|Auth)[\w-]*)\s*:\s*)(?P<value>[^\r\n]+)",
    re.IGNORECASE,
)
# A bare "Bearer <credential>": the value must look like a credential (16+ chars with a digit).
_BEARER = re.compile(r"\bBearer\s+(?P<value>(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{16,})")

_SECRET_WORDS = {"token", "password", "passwd", "pwd", "secret", "apikey", "passphrase", "credential"}
_SECRET_KEYS = {"sig", "signature", "api_key", "api-key", "private_key", "psk", "community", "credentials"}


def _secret_key(key: str) -> bool:
    k = key.strip("\"'").lower()
    if k in _SECRET_KEYS or k in _SECRET_WORDS:
        return True
    if any(part in _SECRET_WORDS for part in re.split(r"[_.-]", k)):
        return True
    return k.endswith(("token", "password", "passwd", "secret", "apikey", "api_key", "privatekey"))


def scrub_text(text: str) -> str:
    """Mask credentials recognisable by shape: URL userinfo, secret-named `key=value` and JSON pairs
    (password, token, secret, api_key, sig...; whole words, so `author=` is left alone), Authorization /
    Cookie / X-*-Token headers, bare `Bearer <credential>` values, and lines that start like an IOS / NX-OS
    command carrying a secret ('enable secret 0 x', 'snmp-server community x RO', 'key 7 x')."""
    if not text:
        return text
    text = _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}{MASK}@", text)
    text = _AUTH_HEADER.sub(lambda m: f"{m.group('key')}{MASK}", text)
    text = _LINE_HEADER.sub(lambda m: f"{m.group('key')}{MASK}", text)
    text = _BEARER.sub(f"Bearer {MASK}", text)
    text = _KEY_VALUE.sub(
        lambda m: f"{m.group('lead')}{m.group('key')}={MASK}" if _secret_key(m.group("key")) else m.group(0),
        text,
    )

    def json_pair(m: re.Match[str]) -> str:
        if _secret_key(m.group("key")) and m.group("value"):
            return f"{m.group('key')}: {m.group('q2')}{MASK}{m.group('q2')}"
        return m.group(0)

    return mask_command_lines(_JSON_PAIR.sub(json_pair, text))


def scrub_url(url: str) -> str:
    return _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}{MASK}@", url)


def mask_values(text: str, values: Iterable[str]) -> str:
    """Mask each value where it stands alone (not inside a longer word), so a weak lab password such as
    "admin" doesn't turn "administrator" into "********istrator"."""
    for v in sorted(values, key=len, reverse=True):
        if v and v in text:
            text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(v)}(?![A-Za-z0-9])", MASK, text)
    return text


def secret_values(settings: Any) -> list[str]:
    """Every configured secret value worth masking: all SecretStr settings, the base64 `user:password`
    an HTTP Basic header carries, and raw (non-hashed) tokens inside MCP_AUTH_TOKENS."""
    import base64

    out: set[str] = set()
    for name in type(settings).model_fields:
        value = getattr(settings, name, None)
        if isinstance(value, SecretStr):
            raw = value.get_secret_value()
            if len(raw) >= MIN_SECRET_LEN:
                out.add(raw)
    username, password = getattr(settings, "username", None), getattr(settings, "password", None)
    if username and isinstance(password, SecretStr):
        out.add(base64.b64encode(f"{username}:{password.get_secret_value()}".encode()).decode())
    tokens = getattr(settings, "mcp_auth_tokens", None)
    if isinstance(tokens, SecretStr):
        for entry in re.split(r"[,\n]", tokens.get_secret_value()):
            _, _, value = entry.split("#", 1)[0].partition(":")
            value = value.strip()
            if value and not value.startswith("sha256:") and len(value) >= MIN_SECRET_LEN:
                out.add(value)
    return sorted(out)


class SecretScrubber:
    """Masks the configured secret values and credential shapes in text and in log records."""

    RUNTIME_KEEP = 64  # session tokens rotate; remember the most recent ones only

    def __init__(self, values: Iterable[str] = ()) -> None:
        from collections import deque

        self._lock = threading.Lock()  # the log-record factory runs on any thread
        self.configured: set[str] = set()
        self.runtime: deque[str] = deque(maxlen=self.RUNTIME_KEEP)
        self.add(values)

    @property
    def values(self) -> frozenset[str]:
        """A snapshot of every value masked (read-only: use add / add_runtime to register more)."""
        with self._lock:
            return frozenset(self.configured) | frozenset(self.runtime)

    def add(self, values: Iterable[str]) -> None:
        """Register configured secret values to mask (kept for the life of the process)."""
        new = [v for v in values if v and len(v) >= MIN_SECRET_LEN]
        with self._lock:
            self.configured.update(new)

    def add_runtime(self, value: str) -> None:
        """Register a value obtained at runtime (session token, cookie); the newest RUNTIME_KEEP are kept."""
        if not value or len(value) < MIN_SECRET_LEN:
            return
        with self._lock:
            if value not in self.runtime and value not in self.configured:
                self.runtime.append(value)

    def clear(self) -> None:
        with self._lock:
            self.configured.clear()
            self.runtime.clear()

    def text(self, text: str) -> str:
        return scrub_text(mask_values(text, self.values))

    def _arg(self, arg: Any) -> Any:
        if isinstance(arg, str):
            return self.text(arg)
        if isinstance(arg, int | float | bool) or arg is None:
            return arg
        rendered = str(arg)
        clean = self.text(rendered)
        return arg if clean == rendered else clean

    def _scrubbed_exc(self, exc: BaseException) -> BaseException:
        message = self.text(str(exc))
        try:
            new = type(exc)(message)
            if str(new) != message:
                raise TypeError
        except Exception:  # noqa: BLE001 - exception types with other constructors
            new = RuntimeError(f"{type(exc).__name__}: {message}")
        new.__traceback__ = exc.__traceback__
        new.__cause__ = None
        new.__suppress_context__ = True
        return new

    def record(self, record: logging.LogRecord) -> None:
        """Scrub a record in place: msg, each argument (kept separate, since some formatters such as
        uvicorn's access log unpack `record.args`), the formatted traceback (exc_text; exc_info is kept)
        and stack_info."""
        if isinstance(record.msg, str):
            record.msg = self.text(record.msg)
        elif record.msg is not None and not isinstance(record.msg, int | float):
            record.msg = self._arg(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._arg(v) for k, v in record.args.items()}
        if record.exc_info and record.exc_info[1] is not None and not record.exc_text:
            formatted = logging.Formatter().formatException(record.exc_info)
            clean = self.text(formatted)
            record.exc_text = clean
            if clean != formatted:
                # Some handlers (Rich, used by FastMCP) render exc_info themselves and ignore exc_text:
                # swap in an exception carrying the scrubbed text (chained exceptions dropped).
                record.exc_info = (
                    record.exc_info[0],
                    self._scrubbed_exc(record.exc_info[1]),
                    record.exc_info[2],
                )
        if record.stack_info:
            record.stack_info = self.text(record.stack_info)


class SecretValueFilter(logging.Filter):
    """A handler filter applying a SecretScrubber (for handlers you attach yourself)."""

    def __init__(self, values: Iterable[str] | SecretScrubber) -> None:
        super().__init__()
        self.scrubber = values if isinstance(values, SecretScrubber) else SecretScrubber(values)

    def filter(self, record: logging.LogRecord) -> bool:
        self.scrubber.record(record)
        return True


_installed: SecretScrubber | None = None


def install_secret_filter(settings: Any, loggers: Iterable[logging.Logger] = ()) -> SecretScrubber:
    """Scrub every log record the process creates, whichever logger or handler it goes to (FastMCP's and
    uvicorn's non-propagating loggers included, and handlers added later): wraps the log record factory
    once, and adds `settings`' secret values to it. `loggers` is accepted for compatibility (unused)."""
    global _installed
    if _installed is None:
        scrubber = SecretScrubber()
        previous = logging.getLogRecordFactory()

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = previous(*args, **kwargs)
            try:
                scrubber.record(record)
            except Exception:  # noqa: BLE001 - never break logging; fall back to the bare message
                record.msg, record.args = scrubber.text(str(record.msg)), None
            return record

        logging.setLogRecordFactory(factory)
        _installed = scrubber
    _installed.add(secret_values(settings))
    return _installed


def reset_secret_filter() -> None:
    """Forget the values registered with the installed scrubber (tests); the factory stays installed."""
    if _installed is not None:
        _installed.clear()


def register_secret(value: str) -> None:
    """Mask a value obtained at runtime (a session token, a cookie) in later log records and tool errors.
    Core's LoginTokenAuth calls this for every token it obtains; servers only need it for credentials they
    handle outside the core auth strategies."""
    if _installed is not None:
        _installed.add_runtime(value)


CREDENTIAL_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "set-cookie"})


def register_header_values(headers: dict[str, str]) -> None:
    """register_secret for each credential header value (Authorization, Cookie, and names that look like
    secrets: X-Auth-Token, X-Auth-Access-Token...), plus the credential after a `Bearer `-style scheme.
    Other headers (Accept, a domain id) are left alone so they don't get masked everywhere."""
    from stutters_mcp_core.formatting import is_secret_key

    for name, value in headers.items():
        if not isinstance(value, str):
            continue
        lowered = name.lower()
        if lowered not in CREDENTIAL_HEADERS and not is_secret_key(name) and "token" not in lowered:
            continue
        register_secret(value)
        scheme, _, rest = value.partition(" ")
        if rest and scheme.isalpha():
            register_secret(rest.strip())


def scrub_error(text: str, settings: Any = None) -> str:
    """Credential shapes masked, plus the configured (and registered) secret values."""
    values: set[str] = set(_installed.values) if _installed is not None else set()
    if settings is not None:
        values.update(secret_values(settings))
    return scrub_text(mask_values(text, values))


# --- loaders ------------------------------------------------------------------------------


@runtime_checkable
class SecretLoader(Protocol):
    """Resolves `<VAR>_REF=<scheme>:<ref>` to the secret. Called once per setting at startup; raise
    ValueError with a message naming what is missing (never the secret itself)."""

    def load(self, ref: str) -> str: ...


class FileLoader:
    """`file:<path>#<key>`: <key> from a JSON object or a KEY=VALUE (.env style) file; without `#<key>`
    the whole file (one trailing newline stripped), like `<VAR>_FILE`."""

    def load(self, ref: str) -> str:
        path_s, _, key = ref.partition("#")
        path = Path(path_s).expanduser()
        try:
            text = path.read_text()
        except OSError as e:
            raise ValueError(f"cannot read {path}: {e.strerror or e}") from None
        if not key:
            return text.removesuffix("\n")
        data: dict[str, Any]
        try:
            loaded = json.loads(text)
            data = loaded if isinstance(loaded, dict) else {}
        except ValueError:
            from dotenv import dotenv_values

            data = {k: v for k, v in dotenv_values(stream=_StringIO(text)).items() if v is not None}
        if key not in data or not isinstance(data[key], str | int):
            raise ValueError(f"{path} has no string value for key {key!r}")
        return str(data[key])


def _StringIO(text: str) -> Any:  # noqa: N802 - tiny shim so dotenv parses text
    import io

    return io.StringIO(text)


BUILTIN_LOADERS: dict[str, type] = {"file": FileLoader}


def load_loaders(spec: str | None) -> dict[str, SecretLoader]:
    """Built-in loaders plus `MCP_SECRET_LOADERS="scheme=package.module:Class, ..."`."""
    loaders: dict[str, SecretLoader] = {k: v() for k, v in BUILTIN_LOADERS.items()}
    for raw in re.split(r"[,\n]", spec or ""):
        entry = raw.strip()
        if not entry:
            continue
        scheme, sep, target = entry.partition("=")
        module_name, colon, attr = target.strip().partition(":")
        scheme = scheme.strip().lower()
        if not sep or not colon or not re.fullmatch(r"[a-z][a-z0-9+.-]*", scheme):
            raise ValueError(f"MCP_SECRET_LOADERS: {entry!r} must look like 'scheme=package.module:Class'")
        try:
            cls = getattr(importlib.import_module(module_name), attr)
        except Exception as e:  # noqa: BLE001 - any import-time failure is a configuration problem
            raise ValueError(f"MCP_SECRET_LOADERS: cannot import {target.strip()!r}: {e}") from None
        if not isinstance(cls, type):
            raise ValueError(f"MCP_SECRET_LOADERS: {target.strip()!r} is not a class")
        try:
            loader = cls()
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"MCP_SECRET_LOADERS: {target.strip()!r} could not be created: {e}") from None
        if not isinstance(loader, SecretLoader):
            raise ValueError(f"MCP_SECRET_LOADERS: {target.strip()!r} has no load(ref) method")
        loaders[scheme] = loader
    return loaders


def resolve_ref(var: str, value: str, loaders: dict[str, SecretLoader]) -> str:
    scheme, sep, ref = value.partition(":")
    if not sep or scheme.lower() not in loaders:
        known = ", ".join(sorted(loaders))
        raise ValueError(
            f"{var}_REF: unknown scheme {scheme!r} (known: {known}; add one with MCP_SECRET_LOADERS)"
        )
    try:
        secret = loaders[scheme.lower()].load(ref)
    except ValueError as e:
        raise ValueError(f"{var}_REF: {scrub_text(str(e))}") from None
    if not isinstance(secret, str) or not secret:
        raise ValueError(f"{var}_REF: the {scheme} loader returned no value")
    return secret


class SecretErrorMiddleware(Middleware):
    """FastMCP middleware: errors returned to the MCP client (tool errors, upstream messages that echo
    input) go through `scrub_error`, so configured secrets and credential shapes never reach the client
    or FastMCP's own error log. Unchanged messages re-raise the original exception."""

    def __init__(self, settings: Any) -> None:
        self.values = secret_values(settings)

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        try:
            return await call_next(context)
        except Exception as e:
            message = str(e)
            clean = self.scrub(message)
            if clean == message:
                raise
            raise ToolError(clean) from None

    def scrub(self, message: str) -> str:
        return scrub_text(mask_values(message, self._all_values()))

    def _all_values(self) -> set[str]:
        values = set(self.values)
        if _installed is not None:
            values.update(_installed.values)
        return values
