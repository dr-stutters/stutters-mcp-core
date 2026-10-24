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
from typing import Any, ClassVar, Literal, get_args

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
        hide_input_in_errors=True,  # validation errors never echo values (they may be secrets)
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
    reads the password from that file (one trailing newline stripped). `<VAR>_REF=<scheme>:<ref>` resolves it
    through a secret loader (stutters_mcp_core.secrets; built-in `file:`, more via MCP_SECRET_LOADERS).
    Looked up in the environment and the .env file; setting more than one of <VAR>, _FILE, _REF is an error.
    Fields that already have a `<name>_file` setting (MCP_AUTH_TOKENS_FILE) are left to it."""

    def __init__(self, settings_cls: type[BaseSettings], env_file: Any, given: Any = ()) -> None:
        super().__init__(settings_cls)
        self.env_file = env_file
        self.given = set(given)  # passed to the constructor: those win, their _FILE is not read

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
            n
            for n, f in fields.items()
            if _is_secret(f.annotation) and f"{n}_file" not in fields and n not in self.given
        ]
        if not secret_fields:
            return {}
        env = {k.upper(): v for k, v in os.environ.items()}
        dotenv: dict[str, str] | None = None
        prefix = str(self.settings_cls.model_config.get("env_prefix", ""))
        out: dict[str, Any] = {}
        loaders: dict[str, Any] | None = None
        for name in secret_fields:
            var = _env_var(name, fields[name], prefix)
            file_var, ref_var = var + "_FILE", var + "_REF"
            dotenv = self._dotenv() if dotenv is None else dotenv
            path = env.get(file_var) or dotenv.get(file_var)
            ref = env.get(ref_var) or dotenv.get(ref_var)
            if not path and not ref:
                continue
            conflicts = [v for v in (var, file_var, ref_var) if env.get(v) or dotenv.get(v)]
            if len(conflicts) > 1:
                both = "both" if len(conflicts) == 2 else "several"
                raise ValueError(f"set {' or '.join(conflicts)}, not {both}")
            if path:
                try:
                    text = Path(path).expanduser().read_text()
                except OSError as e:
                    raise ValueError(f"{file_var}: cannot read {path}: {e.strerror or e}") from None
                out[name] = text.removesuffix("\n")  # read_text() already turned CRLF into LF
                continue
            from stutters_mcp_core.secrets import load_loaders, resolve_ref

            if loaders is None:
                loaders = load_loaders(env.get("MCP_SECRET_LOADERS") or dotenv.get("MCP_SECRET_LOADERS"))
            out[name] = resolve_ref(var, str(ref), loaders)
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
    # Per-appliance write rules (C15), read with the instance prefix (<P>_READ_ONLY, <P>_PROD_READ_ONLY...);
    # they add to the server-wide MCP_* rails and are never inherited between instances.
    read_only: bool = False
    two_person: bool = False
    change_window: str | None = None
    max_blast_radius: int | None = Field(default=None, ge=0)
    # Fields a named instance never inherits from the default instance (G10), beyond URL, credentials,
    # TLS checking and secrets; products list appliance-specific ones, e.g. {"domain"} for FMC.
    per_instance_fields: ClassVar[frozenset[str]] = frozenset()
    # Several appliances (G10, ADR-004): <P>_INSTANCES=prod,dr adds instances read from <P>_PROD_URL, ...
    instances: str | None = None
    default_instance: str = "default"

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
    # C67: token (ADR-002), elicit (only the user's answer in the client's dialog confirms), auto (elicit
    # when the client supports it, else token). Two-person changes always use tokens.
    mcp_confirm_mode: Literal["token", "elicit", "auto"] = Field(
        default="token", validation_alias=_alias("MCP_CONFIRM_MODE")
    )
    mcp_cache_dir: Path | None = Field(default=None, validation_alias=_alias("MCP_CACHE_DIR"))
    mcp_max_response_chars: int = Field(
        default=40_000, ge=2_000, validation_alias=_alias("MCP_MAX_RESPONSE_CHARS")
    )
    mcp_task_timeout: float = Field(default=600.0, gt=0, validation_alias=_alias("MCP_TASK_TIMEOUT"))
    mcp_audit_log: str = Field(default="stderr", validation_alias=_alias("MCP_AUDIT_LOG"))
    # Rotation for a file audit log (C13); unset = no rotation (as before)
    mcp_audit_log_max_mb: float | None = Field(
        default=None, gt=0, validation_alias=_alias("MCP_AUDIT_LOG_MAX_MB")
    )
    mcp_audit_log_backups: int = Field(
        default=5, ge=1, le=100, validation_alias=_alias("MCP_AUDIT_LOG_BACKUPS")
    )
    log_level: str = Field(default="INFO", validation_alias=_alias("LOG_LEVEL"))

    # --- OAuth 2.1 for HTTP (G1, stutters_mcp_core.oauth); off unless MCP_OAUTH_PROVIDER is set
    mcp_oauth_provider: Literal["github", "oidc"] | None = Field(
        default=None, validation_alias=_alias("MCP_OAUTH_PROVIDER")
    )
    mcp_oauth_client_id: str | None = Field(default=None, validation_alias=_alias("MCP_OAUTH_CLIENT_ID"))
    mcp_oauth_client_secret: SecretStr | None = Field(
        default=None, validation_alias=_alias("MCP_OAUTH_CLIENT_SECRET")
    )
    mcp_oauth_base_url: str | None = Field(default=None, validation_alias=_alias("MCP_OAUTH_BASE_URL"))
    mcp_oauth_config_url: str | None = Field(default=None, validation_alias=_alias("MCP_OAUTH_CONFIG_URL"))
    mcp_oauth_allowed_users: str | None = Field(
        default=None, validation_alias=_alias("MCP_OAUTH_ALLOWED_USERS")
    )
    mcp_oauth_scopes: str | None = Field(default=None, validation_alias=_alias("MCP_OAUTH_SCOPES"))
    mcp_oauth_login_claim: str | None = Field(default=None, validation_alias=_alias("MCP_OAUTH_LOGIN_CLAIM"))
    mcp_oauth_redirect_uris: str | None = Field(
        default=None, validation_alias=_alias("MCP_OAUTH_REDIRECT_URIS")
    )
    mcp_oauth_jwt_key: SecretStr | None = Field(default=None, validation_alias=_alias("MCP_OAUTH_JWT_KEY"))
    mcp_oauth_storage_dir: Path | None = Field(default=None, validation_alias=_alias("MCP_OAUTH_STORAGE_DIR"))

    # --- inbound per-caller rate limit (C16); off by default -----------------------------------
    mcp_caller_rate_limit: float | None = Field(
        default=None, gt=0, validation_alias=_alias("MCP_CALLER_RATE_LIMIT")
    )
    mcp_caller_rate_limit_by_role: str | None = Field(
        default=None, validation_alias=_alias("MCP_CALLER_RATE_LIMIT_BY_ROLE")
    )

    # --- MCP background tasks (C12): `reg.write(..., task=True)` tools become MCP tasks -------
    mcp_tasks: bool = Field(default=False, validation_alias=_alias("MCP_TASKS"))
    mcp_tasks_url: str = Field(default="memory://", validation_alias=_alias("MCP_TASKS_URL"))

    # --- metrics (G5, stutters_mcp_core.metrics); off by default -------------------------
    mcp_metrics: bool = Field(default=False, validation_alias=_alias("MCP_METRICS"))
    mcp_metrics_port: int | None = Field(
        default=None, ge=1, le=65535, validation_alias=_alias("MCP_METRICS_PORT")
    )
    mcp_metrics_host: str = Field(default="127.0.0.1", validation_alias=_alias("MCP_METRICS_HOST"))

    # --- safety rails (G7, stutters_mcp_core.policy); all off by default ----------------------
    mcp_dry_run: bool = Field(default=False, validation_alias=_alias("MCP_DRY_RUN"))
    mcp_change_window: str | None = Field(default=None, validation_alias=_alias("MCP_CHANGE_WINDOW"))
    mcp_deny_tools: str | None = Field(default=None, validation_alias=_alias("MCP_DENY_TOOLS"))
    mcp_allow_tools: str | None = Field(default=None, validation_alias=_alias("MCP_ALLOW_TOOLS"))
    mcp_max_blast_radius: int | None = Field(
        default=None, ge=0, validation_alias=_alias("MCP_MAX_BLAST_RADIUS")
    )
    mcp_blast_radius_overrides: str | None = Field(
        default=None, validation_alias=_alias("MCP_BLAST_RADIUS_OVERRIDES")
    )
    # "scheme=package.module:Class" entries for <VAR>_REF references. SecretFileSource reads the raw variable
    # (secrets are resolved before this model exists); the field documents it and shows in the model.
    mcp_secret_loaders: str | None = Field(default=None, validation_alias=_alias("MCP_SECRET_LOADERS"))
    mcp_two_person_tools: str | None = Field(default=None, validation_alias=_alias("MCP_TWO_PERSON_TOOLS"))

    # --- per-user roles (G2, stutters_mcp_core.roles); off unless MCP_ROLES(_FILE) is set ----
    mcp_roles: str | None = Field(default=None, validation_alias=_alias("MCP_ROLES"))
    mcp_roles_file: Path | None = Field(default=None, validation_alias=_alias("MCP_ROLES_FILE"))
    mcp_default_role: Literal["ro", "rw", "admin"] = Field(
        default="ro", validation_alias=_alias("MCP_DEFAULT_ROLE")
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

    @field_validator("mcp_cache_dir", "ca_bundle", "mcp_roles_file", "mcp_oauth_storage_dir", mode="before")
    @classmethod
    def _empty_path_is_none(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v.strip()).expanduser() if v.strip() else None
        return v

    @field_validator(
        "mcp_change_window",
        "mcp_deny_tools",
        "mcp_allow_tools",
        "mcp_blast_radius_overrides",
        "mcp_two_person_tools",
        "mcp_roles",
        "mcp_caller_rate_limit_by_role",
        "mcp_oauth_provider",
        "mcp_oauth_client_id",
        "mcp_oauth_base_url",
        "mcp_oauth_config_url",
        "mcp_oauth_allowed_users",
        "mcp_oauth_scopes",
        "mcp_oauth_login_claim",
        "mcp_oauth_redirect_uris",
        mode="before",
    )
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator(
        "mcp_max_blast_radius",
        "mcp_metrics_port",
        "mcp_audit_log_max_mb",
        "mcp_caller_rate_limit",
        mode="before",
    )
    @classmethod
    def _blank_int_is_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _dangerous_needs_writes(self) -> BaseServerSettings:
        if self.mcp_enable_dangerous and not self.mcp_enable_writes:
            raise ValueError("MCP_ENABLE_DANGEROUS=true also requires MCP_ENABLE_WRITES=true")
        return self

    @model_validator(mode="after")
    def _oauth_complete(self) -> BaseServerSettings:
        if not self.mcp_oauth_provider:
            return self
        missing = [
            name
            for name, value in (
                ("MCP_OAUTH_CLIENT_ID", self.mcp_oauth_client_id),
                ("MCP_OAUTH_BASE_URL", self.mcp_oauth_base_url),
                ("MCP_OAUTH_ALLOWED_USERS", self.mcp_oauth_allowed_users),
            )
            if not value
        ]
        if self.mcp_oauth_provider == "github" and self.mcp_oauth_client_secret is None:
            missing.append("MCP_OAUTH_CLIENT_SECRET")
        if self.mcp_oauth_provider == "oidc" and not self.mcp_oauth_config_url:
            missing.append("MCP_OAUTH_CONFIG_URL")
        if (
            self.mcp_oauth_provider == "oidc"
            and self.mcp_oauth_client_secret is None
            and not self.mcp_oauth_jwt_key
        ):
            missing.append("MCP_OAUTH_CLIENT_SECRET (or MCP_OAUTH_JWT_KEY for a public client)")
        if missing:
            raise ValueError(f"MCP_OAUTH_PROVIDER={self.mcp_oauth_provider} also needs {', '.join(missing)}")
        if self.mcp_oauth_jwt_key is not None and len(self.mcp_oauth_jwt_key.get_secret_value()) < 32:
            raise ValueError(
                "MCP_OAUTH_JWT_KEY must be at least 32 characters (e.g. openssl rand -base64 32)"
            )
        if self.mcp_oauth_base_url and not self.mcp_oauth_base_url.startswith(
            ("https://", "http://localhost", "http://127.0.0.1")
        ):
            raise ValueError("MCP_OAUTH_BASE_URL must be https:// (http only for localhost)")
        return self

    @model_validator(mode="after")
    def _targets_parse(self) -> BaseServerSettings:
        from stutters_mcp_core.targets import TARGET_NAME_RE

        if not TARGET_NAME_RE.match(self.default_instance):
            raise ValueError("DEFAULT_INSTANCE: use lowercase letters, digits and _ (start with a letter)")
        self.target_names()
        return self

    @field_validator("change_window", "max_blast_radius", mode="before")
    @classmethod
    def _blank_instance_rule(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _instance_rules_parse(self) -> BaseServerSettings:
        from stutters_mcp_core.policy import ChangeWindow

        if self.change_window:
            ChangeWindow.parse(self.change_window)
        return self

    @model_validator(mode="after")
    def _rate_limits_parse(self) -> BaseServerSettings:
        from stutters_mcp_core.inbound_limits import parse_role_limits

        by_role = parse_role_limits(self.mcp_caller_rate_limit_by_role)
        if by_role and self.mcp_roles is None and self.mcp_roles_file is None:
            raise ValueError("MCP_CALLER_RATE_LIMIT_BY_ROLE needs MCP_ROLES (or MCP_ROLES_FILE)")
        return self

    @model_validator(mode="after")
    def _metrics_port_needs_metrics(self) -> BaseServerSettings:
        if self.mcp_metrics_port is not None and not self.mcp_metrics:
            log.warning("MCP_METRICS_PORT is set but MCP_METRICS is not true: no metrics are served")
        return self

    @model_validator(mode="after")
    def _policy_parses(self) -> BaseServerSettings:
        from stutters_mcp_core.policy import SafetyPolicy

        SafetyPolicy.from_settings(self)  # raises ValueError naming the bad setting
        self.role_map()  # also reads and checks MCP_ROLES_FILE
        return self

    def role_map(self) -> Any:
        """The per-user role map (stutters_mcp_core.roles.RoleMap), or None when roles are off."""
        from stutters_mcp_core.roles import RoleMap

        if self.mcp_roles is None and self.mcp_roles_file is None:
            return None
        file_spec = None
        if self.mcp_roles_file is not None:
            try:
                file_spec = self.mcp_roles_file.read_text()
            except OSError as e:
                raise ValueError(
                    f"MCP_ROLES_FILE: cannot read {self.mcp_roles_file}: {e.strerror or e}"
                ) from None
        return RoleMap.parse(self.mcp_roles or "", self.mcp_default_role, file_spec=file_spec)

    def target_names(self) -> list[str]:
        """[default instance, *<P>_INSTANCES]; a single entry means single-instance (the normal case)."""
        from stutters_mcp_core.targets import parse_target_names

        return parse_target_names(self.instances, self.default_instance)

    def safety_policy(self) -> Any:
        """The parsed safety rails (stutters_mcp_core.policy.SafetyPolicy)."""
        from stutters_mcp_core.policy import SafetyPolicy

        return SafetyPolicy.from_settings(self)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # init > env > <VAR>_FILE / <VAR>_REF > .env > secrets_dir
        # (a <VAR> together with its _FILE/_REF is an error)
        given = getattr(init_settings, "init_kwargs", {}) or {}
        secret_files = SecretFileSource(settings_cls, getattr(dotenv_settings, "env_file", None), given)
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
