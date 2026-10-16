"""Command line entry point shared by every server.

    def main() -> None:
        run_cli(SPEC, Settings, create_server)

Subcommands:
    serve (default)   run over stdio, or HTTP with `serve --http` / MCP_TRANSPORT=http / --transport http
    token <name>      generate a bearer token + the MCP_AUTH_TOKENS entry for it
    check             preflight: reachability per surface, TLS trust, credentials, the account's role vs the
                      enabled tiers, product version vs the tested matrix (stutters_mcp_core.preflight);
                      --json, --strict, --instance; exit 1 on failure. A server without a preflight
                      function prints <prefix>_status as JSON instead.
    config [--json]   show the effective configuration and where each value came from (secrets masked)
    tools [--json]    list the tools and prompts (all tiers on unless --tier); --json writes the
                      manifest other servers' prompt tests load (no appliance connection needed)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import FastMCP

from stutters_mcp_core.audit import configure_audit_log
from stutters_mcp_core.inbound_auth import generate_token, hash_token, valid_token_name
from stutters_mcp_core.secrets import install_secret_filter, scrub_text
from stutters_mcp_core.server import ServerSpec
from stutters_mcp_core.settings import BaseServerSettings

CreateServer = Callable[..., FastMCP[Any]]
PreflightFn = Callable[[Any, Any], Awaitable[None]]  # (settings, preflight.Preflight) -> None


def _token_command(prog: str, name: str) -> None:
    if not valid_token_name(name):
        sys.exit(f"{prog}: token name may only contain letters, digits and _ . @ - ('local' is reserved)")
    token = generate_token()
    print(f"Bearer token for {name!r} (give this to the user; it is not stored anywhere):\n")
    print(f"  {token}\n")
    print("Add this entry to MCP_AUTH_TOKENS on the server (comma-separate multiple entries):\n")
    print(f"  {name}:sha256:{hash_token(token)}")


def _configure_logging(settings: BaseServerSettings, package: str) -> None:
    # Logs must go to stderr: stdout carries the stdio protocol.
    logging.basicConfig(
        level=settings.log_level.upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger(package).setLevel(settings.log_level.upper())
    configure_audit_log(
        settings.mcp_audit_log, max_mb=settings.mcp_audit_log_max_mb, backups=settings.mcp_audit_log_backups
    )
    # Last line of defence: mask every configured secret value in anything that reaches a handler.
    install_secret_filter(settings, [logging.getLogger("stutters_mcp_core.audit")])


async def _check(spec: ServerSpec, server: FastMCP[Any], targets: list[str] | None = None) -> int:
    from fastmcp import Client

    failed = 0
    async with Client(server) as client:
        for target in targets or [None]:  # type: ignore[list-item]
            args = {"instance": target} if target else {}
            result = await client.call_tool(f"{spec.prefix}_status", args, raise_on_error=False)
            data: Any = result.structured_content or {
                "text": [getattr(c, "text", "") for c in result.content]
            }
            if target:
                print(f"== instance {target}")
            print(json.dumps(data, indent=2, default=str))
            product = data.get(spec.prefix, {}) if isinstance(data, dict) else {}
            failed |= result.is_error or (isinstance(product, dict) and "error" in product)
    return 1 if failed else 0


def _preflight_command(
    spec: ServerSpec, settings: BaseServerSettings, preflight: PreflightFn, args: Any
) -> int:
    from stutters_mcp_core import preflight as pre

    try:
        report = asyncio.run(
            pre.run_preflight(
                settings,
                preflight,
                server=spec.name,
                product=spec.product,
                env_prefix=spec.env_prefix,
                version=spec.version or _server_version(spec.name),
                instances=args.instance,
            )
        )
    except ValueError as e:  # an unknown --instance, or a named instance with invalid settings
        print(f"{spec.name}: {scrub_text(str(e))}", file=sys.stderr)
        return 2
    sys.stdout.write(pre.dumps(report) if args.json else pre.render_text(report))
    return pre.exit_code(report, strict=args.strict)


_PLACEHOLDER_URL = "https://manifest.invalid"


def _manifest_settings(settings_cls: type[BaseServerSettings], tier: str) -> BaseServerSettings:
    """Settings for listing the surface: env/.env values when they validate, else a placeholder URL.
    Writes/dangerous follow `tier`, overriding the environment."""
    flags = {"mcp_enable_writes": tier != "read", "mcp_enable_dangerous": tier == "dangerous"}
    try:
        return settings_cls(**flags)  # type: ignore[arg-type]
    except ValueError:
        return settings_cls(url=_PLACEHOLDER_URL, **flags)  # type: ignore[arg-type]


def _server_version(name: str) -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _tools_command(
    spec: ServerSpec, settings_cls: type[BaseServerSettings], create_server: CreateServer, args: Any
) -> None:
    from stutters_mcp_core.manifest import build_manifest, manifest_json

    try:
        settings = _manifest_settings(settings_cls, args.tier)
    except ValueError as e:
        sys.exit(f"{spec.name}: cannot build settings to list tools:\n{e}")
    manifest = asyncio.run(
        build_manifest(
            create_server(settings), server=spec.name, prefix=spec.prefix, version=_server_version(spec.name)
        )
    )
    if args.json:
        text = manifest_json(manifest)
    else:
        lines = [f"{spec.name} {manifest['version'] or ''} (core {manifest['core_version']})".rstrip()]
        lines += [f"  {meta['tier']:<9} {name}" for name, meta in manifest["tools"].items()]
        if manifest["prompts"]:
            lines.append("prompts:")
            lines += [
                f"  {meta['tier']:<9} {name}  uses={','.join(meta['uses'])}"
                + ("" if meta["rendered"] else "  (did not render: register example=...)")
                for name, meta in manifest["prompts"].items()
            ]
        text = "\n".join(lines) + "\n"
    if args.out:
        from pathlib import Path

        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        print(f"wrote {args.out} ({len(manifest['tools'])} tools, {len(manifest['prompts'])} prompts)")
    else:
        sys.stdout.write(text)


def run_cli(
    spec: ServerSpec,
    settings_cls: type[BaseServerSettings],
    create_server: CreateServer,
    *,
    argv: list[str] | None = None,
    preflight: PreflightFn | None = None,
) -> None:
    """`preflight`: the server's `async (settings, Preflight) -> None` for `check` (core preflight module)."""
    prog = spec.name
    parser = argparse.ArgumentParser(prog=prog, description=f"MCP server for {spec.product}")
    parser.add_argument("--transport", choices=["stdio", "http"], help="overrides MCP_TRANSPORT")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the server (default: stdio)")
    mode = serve.add_mutually_exclusive_group()
    mode.add_argument(
        "--http", dest="serve_transport", action="store_const", const="http", help="serve over HTTP"
    )
    mode.add_argument(
        "--stdio", dest="serve_transport", action="store_const", const="stdio", help="serve over stdio"
    )
    tok = sub.add_parser("token", help="generate a bearer token for the HTTP transport")
    tok.add_argument("name", help="who the token is for, e.g. alice")
    chk = sub.add_parser("check", help="preflight: connect to the appliance and check access (no MCP client)")
    chk.add_argument("--json", action="store_true", help="print the report as JSON")
    chk.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    chk.add_argument(
        "--instance", action="append", metavar="NAME", help="only this instance (repeatable; default: all)"
    )
    cfg = sub.add_parser(
        "config", help="show the effective configuration (secrets masked; no appliance needed)"
    )
    cfg.add_argument("--json", action="store_true", help="print JSON (includes defaults)")
    tools = sub.add_parser(
        "tools", help="list tools and prompts; --json for the manifest other servers test against"
    )
    tools.add_argument("--json", action="store_true", help="print the JSON manifest")
    tools.add_argument("--out", help="write to this file instead of stdout")
    tools.add_argument(
        "--tier",
        choices=["read", "write", "dangerous"],
        default="dangerous",
        help="highest tier on (default: all)",
    )
    args = parser.parse_args(argv)

    if args.command == "token":
        _token_command(prog, args.name)
        return
    if args.command == "tools":
        _tools_command(spec, settings_cls, create_server, args)
        return

    if args.command == "config":
        try:
            config_settings = settings_cls()  # type: ignore[call-arg]
        except ValueError as e:
            sys.exit(f"{prog}: invalid configuration:\n{scrub_text(str(e))}")
        from stutters_mcp_core.config_report import config_report, render_text

        report = config_report(config_settings)
        sys.stdout.write(
            json.dumps(report, indent=2, default=str) + "\n" if args.json else render_text(report)
        )
        return

    try:
        settings = settings_cls()  # type: ignore[call-arg]  # populated from env / .env
    except ValueError as e:  # pydantic's ValidationError, or a <VAR>_FILE problem
        problem = scrub_text(str(e))
        sys.exit(f"{prog}: invalid configuration (variables use the {spec.env_prefix} prefix):\n{problem}")

    _configure_logging(settings, spec.name.replace("-", "_"))
    log = logging.getLogger(spec.name)

    if args.command == "check":
        if preflight is None:
            names = settings.target_names()
            sys.exit(asyncio.run(_check(spec, create_server(settings), names if len(names) > 1 else None)))
        sys.exit(_preflight_command(spec, settings, preflight, args))

    transport = getattr(args, "serve_transport", None) or args.transport or settings.mcp_transport
    if transport == "stdio":
        create_server(settings).run(transport="stdio", show_banner=False)
        return

    # HTTP: fail closed. Never expose the appliance over the network without authentication.
    try:
        tokens = settings.auth_token_hashes()
    except (ValueError, OSError) as e:
        sys.exit(f"{prog}: invalid MCP_AUTH_TOKENS / MCP_AUTH_TOKENS_FILE: {e}")
    if not tokens and not settings.mcp_oauth_provider:
        sys.exit(
            f"{prog}: HTTP transport requires MCP_AUTH_TOKENS (or MCP_AUTH_TOKENS_FILE) "
            "and/or MCP_OAUTH_PROVIDER. "
            f"Generate a token with: {prog} token <name>"
        )
    from stutters_mcp_core.oauth import build_auth

    try:
        auth = build_auth(settings, tokens)
    except Exception as e:  # noqa: BLE001 - e.g. OIDC discovery failed
        sys.exit(f"{prog}: OAuth setup failed: {scrub_text(str(e))}")
    if auth is None:  # unreachable: tokens or OAuth were checked above
        sys.exit(f"{prog}: no HTTP authentication configured")
    if settings.mcp_oauth_provider:
        log.info(
            "OAuth (%s) on: base URL %s, allowed users: %s",
            settings.mcp_oauth_provider,
            settings.mcp_oauth_base_url,
            settings.mcp_oauth_allowed_users,
        )
    if settings.mcp_two_person_tools and len(tokens) < 2 and not settings.mcp_oauth_provider:
        log.warning(
            "MCP_TWO_PERSON_TOOLS is set but only one token is configured: nobody can approve those changes. "
            "Give each person their own token (the identity is the token name)."
        )
    roles = settings.role_map()
    if roles is not None:
        log.info(
            "Per-user roles on: %s",
            ", ".join(f"{name}={roles.role_for(name).value}" for name in sorted(tokens)),
        )
    log.info(
        "HTTP transport on %s:%s%s; %d token(s): %s; writes %s; dangerous %s",
        settings.mcp_host,
        settings.mcp_port,
        settings.mcp_path,
        len(tokens),
        ", ".join(sorted(tokens)),
        "ENABLED" if settings.mcp_enable_writes else "disabled",
        "ENABLED" if settings.mcp_enable_dangerous else "disabled",
    )
    create_server(settings, auth=auth).run(
        transport="http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        path=settings.mcp_path,
        show_banner=False,
    )
