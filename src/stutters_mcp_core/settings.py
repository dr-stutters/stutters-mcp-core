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
import os
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

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


def _is_secret(annotation: Any) -> bool:
    return annotation is SecretStr or SecretStr in get_args(annotation)


def _env_var(field_name: str, info: FieldInfo, env_prefix: str) -> str:
    alias = info.validation_alias
    if isinstance(alias, AliasChoices) and isinstance(alias.choices[0], str):
        return alias.choices[0].upper()
    if isinstance(alias, str):
        return alias.upper()
    return f"{env_prefix}{field_name}".upper()


class SecretFileSource(PydanticBaseSettingsSource):
    """`<VAR>_FILE` for every SecretStr setting (Docker/Kubernetes secrets): ISE_PASSWORD_FILE=/run/secrets/x
    reads the password from that file (one trailing newline stripped). Looked up in the environment and the
    .env file; setting both <VAR> and <VAR>_FILE is an error. Fields that already have a `<name>_file`
    setting (MCP_AUTH_TOKENS_FILE) are left to it."""

    def __init__(self, settings_cls: type[BaseSettings], env_file: Any) -> None:
        super().__init__(settings_cls)
        self.env_file = env_file

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:  # pragma: no cover
        return None, field_name, False  # unused: __call__ does the work

    def _dotenv(self) -> dict[str, str]:
        files = self.env_file if isinstance(self.env_file, list | tuple) else [self.env_file]
        out: dict[str, str] = {}
        for f in files:
            if f is None or not Path(f).expanduser().is_file():
                continue
            from dotenv import dotenv_values

            out.update(
                {k.upper(): v for k, v in dotenv_values(Path(f).expanduser()).items() if v is not None}
            )
        return out

    def __call__(self) -> dict[str, Any]:
        fields = self.settings_cls.model_fields
        secret_fields = [
            n for n, f in fields.items() if _is_secret(f.annotation) and f"{n}_file" not in fields
        ]
        if not secret_fields:
            return {}
        env = {k.upper(): v for k, v in os.environ.items()}
        dotenv: dict[str, str] | None = None
        prefix = str(self.settings_cls.model_config.get("env_prefix", ""))
        out: dict[str, Any] = {}
        for name in secret_fields:
            var = _env_var(name, fields[name], prefix)
            file_var = var + "_FILE"
            path = env.get(file_var)
            if path is None:
                dotenv = self._dotenv() if dotenv is None else dotenv
                path = dotenv.get(file_var)
            if not path:
                continue
            dotenv = self._dotenv() if dotenv is None else dotenv
            if env.get(var) or dotenv.get(var):
                raise ValueError(f"set {var} or {file_var}, not both")
            try:
                text = Path(path).expanduser().read_text()
            except OSError as e:
                raise ValueError(f"{file_var}: cannot read {path}: {e.strerror or e}") from None
            out[name] = text.removesuffix("\n").removesuffix("\r")
        return out


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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # init > env > <VAR>_FILE > .env > secrets_dir
        secret_files = SecretFileSource(settings_cls, getattr(dotenv_settings, "env_file", None))
        return init_settings, env_settings, secret_files, dotenv_settings, file_secret_settings

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

    def cache_dir_for(self, server_name: str) -> Path | None:
        """MCP_CACHE_DIR if set, else ~/.cache/<server_name>. Set MCP_CACHE_DIR="" to disable."""
        if "mcp_cache_dir" in self.model_fields_set:
            return self.mcp_cache_dir
        return Path.home() / ".cache" / server_name
