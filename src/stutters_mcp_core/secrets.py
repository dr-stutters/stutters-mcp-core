"""Secret hygiene (roadmap G4): scrub secrets out of log lines and messages, and pluggable secret loaders.

Two layers keep credentials out of logs, audit events and `<prefix>_status`:

1. `scrub_text(text)` masks credentials by shape: `user:pass@` in URLs, secret-looking query parameters
   (`?token=...`, `password=`), and `Authorization`-style `Bearer` / `Basic` values. Used for upstream error
   messages, the status `url`, and audit `error` fields.
2. `SecretValueFilter` masks the actual configured secret values (every `SecretStr` setting, raw
   `MCP_AUTH_TOKENS` entries) in every record that reaches the log handlers, whatever the message shape.
   The CLI installs it on the root and audit handlers at startup.

Secret loaders: every `SecretStr` setting can also be given as a reference, `<VAR>_REF=<scheme>:<ref>`,
resolved at startup by a loader registered for the scheme. Core ships `file:` (a JSON or KEY=VALUE file,
`file:/run/secrets/creds.json#ise_password`); Vault, 1Password and the like plug in as a class with
`load(ref) -> str`, named in `MCP_SECRET_LOADERS="vault=mypkg.loaders:VaultLoader"` (see
docs/PRODUCTION.md for worked examples). Precedence: `<VAR>` > `<VAR>_FILE` > `<VAR>_REF`; setting more than
one of them is an error.
"""

from __future__ import annotations

import importlib
import json
import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import SecretStr

MASK = "********"  # same as formatting.MASK (not imported: http.client imports this module)

MIN_SECRET_LEN = 4  # shorter values would mask unrelated text

_URL_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<userinfo>[^/\s@]+)@")
_SECRET_PARAM = re.compile(
    r"(?P<key>[?&;](?:[\w.-]*(?:token|password|passwd|secret|api_?key|apikey|sig|signature|auth)[\w.-]*)=)"
    r"(?P<value>[^&\s#;'\"]+)",
    re.IGNORECASE,
)
_AUTH_VALUE = re.compile(
    r"\b(?P<scheme>Bearer|Basic|Token)\s+(?P<value>[A-Za-z0-9._~+/=-]{8,})", re.IGNORECASE
)


def scrub_text(text: str) -> str:
    """Mask credentials recognisable by shape (URL userinfo, secret query parameters, auth headers)."""
    text = _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}{MASK}@", text)
    text = _SECRET_PARAM.sub(lambda m: f"{m.group('key')}{MASK}", text)
    return _AUTH_VALUE.sub(lambda m: f"{m.group('scheme')} {MASK}", text)


def scrub_url(url: str) -> str:
    return _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}{MASK}@", url)


def mask_values(text: str, values: Iterable[str]) -> str:
    for v in sorted(values, key=len, reverse=True):
        if v and v in text:
            text = text.replace(v, MASK)
    return text


def secret_values(settings: Any) -> list[str]:
    """Every configured secret value worth masking: all SecretStr settings, plus raw (non-hashed) tokens
    inside MCP_AUTH_TOKENS."""
    out: set[str] = set()
    for name in type(settings).model_fields:
        value = getattr(settings, name, None)
        if isinstance(value, SecretStr):
            raw = value.get_secret_value()
            if len(raw) >= MIN_SECRET_LEN:
                out.add(raw)
    tokens = getattr(settings, "mcp_auth_tokens", None)
    if isinstance(tokens, SecretStr):
        for entry in re.split(r"[,\n]", tokens.get_secret_value()):
            _, _, value = entry.split("#", 1)[0].partition(":")
            value = value.strip()
            if value and not value.startswith("sha256:") and len(value) >= MIN_SECRET_LEN:
                out.add(value)
    return sorted(out)


class SecretValueFilter(logging.Filter):
    """Replace known secret values (and credential shapes) in log records before any handler writes them."""

    def __init__(self, values: Iterable[str]) -> None:
        super().__init__()
        self.values = [v for v in values if len(v) >= MIN_SECRET_LEN]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken format string is the handler's problem, not ours
            return True
        clean = scrub_text(mask_values(message, self.values))
        if record.exc_info and record.exc_info[1] is not None:
            # Tracebacks are formatted later; render and scrub them now.
            formatted = logging.Formatter().formatException(record.exc_info)
            record.exc_text = scrub_text(mask_values(formatted, self.values))
            record.exc_info = None
        if clean != message:
            record.msg, record.args = clean, None
        return True


def install_secret_filter(settings: Any, loggers: Iterable[logging.Logger] = ()) -> SecretValueFilter:
    """Attach a SecretValueFilter to every handler of the root logger and of `loggers`."""
    filt = SecretValueFilter(secret_values(settings))
    for logger in (logging.getLogger(), *loggers):
        for handler in logger.handlers:
            if not any(isinstance(f, SecretValueFilter) for f in handler.filters):
                handler.addFilter(filt)
    return filt


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
        except (ImportError, AttributeError) as e:
            raise ValueError(f"MCP_SECRET_LOADERS: cannot import {target.strip()!r}: {e}") from None
        loader = cls()
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
        raise ValueError(f"{var}_REF: {e}") from None
    if not isinstance(secret, str) or not secret:
        raise ValueError(f"{var}_REF: the {scheme} loader returned no value")
    return secret
