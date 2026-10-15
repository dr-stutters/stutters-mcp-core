"""Runtime configuration shared by every server.

Each server subclasses `BaseServerSettings` and sets its own `env_prefix` (e.g. "ISE_") in
`model_config`. Upstream connection fields (`url`, `username`, ...) pick up that prefix, so they are
read from ISE_URL, ISE_USERNAME, ...  Server-level fields carry explicit aliases (MCP_TRANSPORT,
LOG_LEVEL, ...) and are therefore the same for every product.

    class Settings(BaseServerSettings):
        model_config = server_config("ISE_")
        ers_port: int = 443           # -> ISE_ERS_PORT
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


def server_config(env_prefix: str) -> SettingsConfigDict:
    """`model_config` for a product's Settings class: env prefix, .env file, unknown vars ignored."""
    return SettingsConfigDict(
        env_prefix=env_prefix,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )


def _alias(name: str) -> Any:
    # validation_alias bypasses env_prefix, so these names are product-independent.
    return AliasChoices(name, name.lower())


class BaseServerSettings(BaseSettings):
    model_config = server_config("")

    # --- upstream appliance (prefixed: <P>_URL, <P>_USERNAME, ...) -------------------------
    url: str
    username: str | None = None
    password: SecretStr | None = None
    token: SecretStr | None = None
    verify_ssl: bool = True
    ca_bundle: Path | None = None
    timeout: float = Field(default=30.0, gt=0)
    retries: int = Field(default=2, ge=0, le=10)
    rate_limit: float | None = Field(default=None, gt=0, description="max requests per minute")
    max_concurrency: int | None = Field(default=None, ge=1)

    # --- MCP server (unprefixed) ----------------------------------------------------------
    mcp_transport: Literal["stdio", "http"] = Field(default="stdio", validation_alias=_alias("MCP_TRANSPORT"))
    mcp_host: str = Field(default="0.0.0.0", validation_alias=_alias("MCP_HOST"))
    mcp_port: int = Field(default=8000, validation_alias=_alias("MCP_PORT"))
    mcp_path: str = Field(default="/mcp", validation_alias=_alias("MCP_PATH"))
    # "name:token" or "name:sha256:<hex>" entries, comma/newline separated.
    mcp_auth_tokens: SecretStr | None = Field(default=None, validation_alias=_alias("MCP_AUTH_TOKENS"))
    mcp_auth_tokens_file: Path | None = Field(default=None, validation_alias=_alias("MCP_AUTH_TOKENS_FILE"))
    mcp_enable_writes: bool = Field(default=False, validation_alias=_alias("MCP_ENABLE_WRITES"))
    mcp_enable_dangerous: bool = Field(default=False, validation_alias=_alias("MCP_ENABLE_DANGEROUS"))
    mcp_confirm_ttl: int = Field(default=600, ge=30, validation_alias=_alias("MCP_CONFIRM_TTL"))
    mcp_cache_dir: Path | None = Field(default=None, validation_alias=_alias("MCP_CACHE_DIR"))
    mcp_max_response_chars: int = Field(
        default=40_000, ge=2_000, validation_alias=_alias("MCP_MAX_RESPONSE_CHARS")
    )
    mcp_task_timeout: float = Field(default=600.0, gt=0, validation_alias=_alias("MCP_TASK_TIMEOUT"))
    mcp_audit_log: str = Field(default="stderr", validation_alias=_alias("MCP_AUDIT_LOG"))
    log_level: str = Field(default="INFO", validation_alias=_alias("LOG_LEVEL"))

    # --- events (ADR-003); receivers are off by default --------------------------------------
    mcp_event_buffer_size: int = Field(
        default=5000, ge=10, le=1_000_000, validation_alias=_alias("MCP_EVENT_BUFFER_SIZE")
    )
    mcp_webhooks_enabled: bool = Field(default=False, validation_alias=_alias("MCP_WEBHOOKS_ENABLED"))
    mcp_webhook_path: str = Field(default="/events", validation_alias=_alias("MCP_WEBHOOK_PATH"))
    # Unset: the routes live on the MCP HTTP app. Set: a dedicated listener (also with stdio).
    mcp_webhook_host: str = Field(default="127.0.0.1", validation_alias=_alias("MCP_WEBHOOK_HOST"))
    mcp_webhook_port: int | None = Field(
        default=None, ge=0, le=65535, validation_alias=_alias("MCP_WEBHOOK_PORT")
    )
    # "source:secret" entries (basic auth: "source:user:password"), comma/newline separated.
    mcp_webhook_secrets: SecretStr | None = Field(
        default=None, validation_alias=_alias("MCP_WEBHOOK_SECRETS")
    )
    mcp_webhook_allow_ips: str = Field(default="", validation_alias=_alias("MCP_WEBHOOK_ALLOW_IPS"))
    mcp_webhook_max_body: int = Field(
        default=262_144, ge=1024, le=16 * 1024 * 1024, validation_alias=_alias("MCP_WEBHOOK_MAX_BODY")
    )
    mcp_webhook_rate_limit: float | None = Field(
        default=20.0, gt=0, validation_alias=_alias("MCP_WEBHOOK_RATE_LIMIT")
    )  # requests/second per source
    mcp_syslog_enabled: bool = Field(default=False, validation_alias=_alias("MCP_SYSLOG_ENABLED"))
    mcp_syslog_host: str = Field(default="127.0.0.1", validation_alias=_alias("MCP_SYSLOG_HOST"))
    mcp_syslog_udp_port: int | None = Field(
        default=5514, ge=0, le=65535, validation_alias=_alias("MCP_SYSLOG_UDP_PORT")
    )
    mcp_syslog_tcp_port: int | None = Field(
        default=5514, ge=0, le=65535, validation_alias=_alias("MCP_SYSLOG_TCP_PORT")
    )
    mcp_syslog_allow_ips: str = Field(default="", validation_alias=_alias("MCP_SYSLOG_ALLOW_IPS"))
    mcp_syslog_rate_limit: float | None = Field(
        default=500.0, gt=0, validation_alias=_alias("MCP_SYSLOG_RATE_LIMIT")
    )  # messages/second per sender
    mcp_syslog_max_message: int = Field(
        default=16_384, ge=480, le=1024 * 1024, validation_alias=_alias("MCP_SYSLOG_MAX_MESSAGE")
    )
    mcp_syslog_max_connections: int = Field(
        default=64, ge=1, le=10_000, validation_alias=_alias("MCP_SYSLOG_MAX_CONNECTIONS")
    )

    @field_validator("url")
    @classmethod
    def _normalise_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("token")
    @classmethod
    def _strip_auth_scheme(cls, v: SecretStr | None) -> SecretStr | None:
        # Tolerate tokens pasted with their header scheme, e.g. "Bearer abc...".
        if v is None:
            return None
        raw = v.get_secret_value().strip()
        for prefix in ("Bearer ", "Token "):
            if raw.startswith(prefix):
                raw = raw[len(prefix) :].strip()
        return SecretStr(raw) if raw else None

    @field_validator(
        "mcp_webhook_port",
        "mcp_syslog_udp_port",
        "mcp_syslog_tcp_port",
        "mcp_webhook_rate_limit",
        "mcp_syslog_rate_limit",
        mode="before",
    )
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        # An empty value (or "off") disables a port or a rate limit.
        if isinstance(v, str) and v.strip().lower() in ("", "off", "none"):
            return None
        return v

    @field_validator("mcp_webhook_allow_ips", "mcp_syslog_allow_ips")
    @classmethod
    def _valid_networks(cls, v: str) -> str:
        from stutters_mcp_core.events.limits import parse_networks

        parse_networks(v)
        return v

    @field_validator("mcp_webhook_path")
    @classmethod
    def _webhook_path(cls, v: str) -> str:
        v = "/" + v.strip().strip("/")
        if v == "/" or not all(part.replace("-", "").replace("_", "").isalnum() for part in v[1:].split("/")):
            raise ValueError("MCP_WEBHOOK_PATH must be a path such as /events")
        return v

    @field_validator("mcp_cache_dir", "ca_bundle", mode="before")
    @classmethod
    def _empty_path_is_none(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v.strip()).expanduser() if v.strip() else None
        return v

    @model_validator(mode="after")
    def _dangerous_needs_writes(self) -> BaseServerSettings:
        if self.mcp_enable_dangerous and not self.mcp_enable_writes:
            raise ValueError("MCP_ENABLE_DANGEROUS=true also requires MCP_ENABLE_WRITES=true")
        return self

    @model_validator(mode="after")
    def _receivers_consistent(self) -> BaseServerSettings:
        hook, mcp = self.mcp_webhook_path.rstrip("/"), "/" + self.mcp_path.strip("/")
        if hook == "/health" or hook == mcp or hook.startswith(mcp + "/") or mcp.startswith(hook + "/"):
            raise ValueError("MCP_WEBHOOK_PATH must not overlap MCP_PATH or /health")
        if self.mcp_syslog_enabled and self.mcp_syslog_udp_port is None and self.mcp_syslog_tcp_port is None:
            raise ValueError("MCP_SYSLOG_ENABLED=true needs MCP_SYSLOG_UDP_PORT and/or MCP_SYSLOG_TCP_PORT")
        if self.mcp_webhook_secrets is not None:
            self.webhook_secrets()  # validates the format
        return self

    # --- helpers -------------------------------------------------------------------------

    @property
    def verify(self) -> bool | str:
        """Value for httpx's `verify` argument."""
        if self.ca_bundle is not None:
            return str(self.ca_bundle)
        return self.verify_ssl

    def password_value(self) -> str:
        return self.password.get_secret_value() if self.password else ""

    def token_value(self) -> str:
        return self.token.get_secret_value() if self.token else ""

    def require_credentials(self, env_prefix: str) -> tuple[str, str]:
        """(username, password), or an AuthError naming the variables to set."""
        if not self.username or not self.password:
            from stutters_mcp_core.errors import AuthError

            raise AuthError(f"No credentials configured: set {env_prefix}USERNAME and {env_prefix}PASSWORD")
        return self.username, self.password_value()

    def auth_token_hashes(self) -> dict[str, str]:
        """All configured HTTP bearer tokens as {name: sha256_hex}; raises ValueError if malformed."""
        from stutters_mcp_core.inbound_auth import parse_tokens

        spec = self.mcp_auth_tokens.get_secret_value() if self.mcp_auth_tokens else ""
        if self.mcp_auth_tokens_file is not None:
            spec += "\n" + self.mcp_auth_tokens_file.read_text()
        return parse_tokens(spec)

    def webhook_secrets(self) -> dict[str, str]:
        """MCP_WEBHOOK_SECRETS as {source: secret}; raises ValueError if malformed."""
        from stutters_mcp_core.events.webhook import parse_webhook_secrets

        spec = self.mcp_webhook_secrets.get_secret_value() if self.mcp_webhook_secrets else ""
        return parse_webhook_secrets(spec)

    def cache_dir_for(self, server_name: str) -> Path | None:
        """MCP_CACHE_DIR if set, else ~/.cache/<server_name>. Set MCP_CACHE_DIR="" to disable."""
        if "mcp_cache_dir" in self.model_fields_set:
            return self.mcp_cache_dir
        return Path.home() / ".cache" / server_name
