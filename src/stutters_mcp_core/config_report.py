"""`<server> config`: the effective configuration, secrets never shown (roadmap C17).

Builds the settings exactly as `serve` would (environment, .env, <VAR>_FILE, <VAR>_REF) without contacting
the appliance, and reports every setting with where its value came from, plus the parsed safety rails,
roles, instances and HTTP auth. Secret settings show only whether they are set and from which source.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from stutters_mcp_core.secrets import scrub_url
from stutters_mcp_core.settings import BaseServerSettings, _env_var, _is_secret


def _dotenv(settings_cls: type[BaseServerSettings]) -> dict[str, str]:
    env_file = settings_cls.model_config.get("env_file")
    files = env_file if isinstance(env_file, list | tuple) else [env_file]
    out: dict[str, str] = {}
    for f in files:
        if f and Path(str(f)).expanduser().is_file():
            from dotenv import dotenv_values

            out.update(
                {k.upper(): v for k, v in dotenv_values(Path(str(f)).expanduser()).items() if v is not None}
            )
    return out


def _source(var: str, env: dict[str, str], dotenv: dict[str, str], secret: bool) -> str:
    candidates = [var, var + "_FILE", var + "_REF"] if secret else [var]
    for name in candidates:
        if env.get(name):
            return "env" if name == var else f"env {name}"
        if dotenv.get(name):
            return ".env" if name == var else f".env {name}"
    return "default"


def _plain(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, frozenset | set):
        return sorted(value)
    return value


def config_report(settings: BaseServerSettings) -> dict[str, Any]:
    cls = type(settings)
    prefix = str(cls.model_config.get("env_prefix", ""))
    env = {k.upper(): v for k, v in os.environ.items()}
    dotenv = _dotenv(cls)
    fields: dict[str, Any] = {}
    for name, info in cls.model_fields.items():
        var = _env_var(name, info, prefix)
        secret = _is_secret(info.annotation)
        value = getattr(settings, name)
        entry: dict[str, Any] = {"source": _source(var, env, dotenv, secret)}
        if secret or isinstance(value, SecretStr):
            entry["set"] = value is not None and bool(value.get_secret_value())
        elif name == "url" or (isinstance(value, str) and value.startswith(("http://", "https://"))):
            entry["value"] = scrub_url(value)
        elif name in ("mcp_roles",) and value:
            entry["value"] = (
                f"{len([e for e in value.replace(chr(10), ',').split(',') if e.strip()])} entries"
            )
        else:
            entry["value"] = _plain(value)
        fields[var] = entry

    report: dict[str, Any] = {"settings": fields}
    policy = settings.safety_policy()
    report["safety"] = policy.describe() if policy.active else "off"
    roles = settings.role_map()
    report["roles"] = (
        {"entries": len(roles.entries), "default_role": roles.default.value} if roles is not None else "off"
    )
    from stutters_mcp_core.policy import InstanceRules
    from stutters_mcp_core.targets import target_settings

    names = settings.target_names()
    instances: dict[str, Any] = {}
    for i, name in enumerate(names):
        try:
            cfg = settings if i == 0 else target_settings(settings, name)
        except ValueError as e:
            instances[name] = {"error": str(e)}
            continue
        rules = InstanceRules.from_settings(cfg)
        instances[name] = {
            "url": scrub_url(cfg.url),
            "username": cfg.username,
            "credentials": "set" if (cfg.password or cfg.token) else "missing",
            "verify_ssl": cfg.verify_ssl,
            **({"rules": rules.describe()} if rules.active else {}),
        }
    report["instances"] = instances
    http: dict[str, Any] = {"transport": settings.mcp_transport}
    try:
        tokens = settings.auth_token_hashes()
        http["bearer_tokens"] = sorted(tokens)
    except (ValueError, OSError) as e:
        http["bearer_tokens_error"] = str(e)
    if settings.mcp_oauth_provider:
        http["oauth"] = {
            "provider": settings.mcp_oauth_provider,
            "base_url": settings.mcp_oauth_base_url,
            "allowed_users": settings.mcp_oauth_allowed_users,
        }
    report["http"] = http
    report["features"] = {
        "writes": settings.mcp_enable_writes,
        "dangerous": settings.mcp_enable_dangerous,
        "metrics": settings.mcp_metrics,
        "tasks": settings.mcp_tasks,
        "caller_rate_limit": getattr(settings, "mcp_caller_rate_limit", None),
    }
    return report


def render_text(report: dict[str, Any]) -> str:
    lines = ["Settings (source: env / .env / default; secrets show set/unset only):"]
    for var, entry in report["settings"].items():
        shown = ("set" if entry["set"] else "unset") if "set" in entry else repr(entry["value"])
        if entry["source"] == "default" and "set" not in entry:
            continue  # defaults are noise; --json shows everything
        lines.append(f"  {var} = {shown}  ({entry['source']})")
    for key in ("features", "safety", "roles", "instances", "http"):
        lines.append(f"{key}: {report[key]}")
    return "\n".join(lines) + "\n"
