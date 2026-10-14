"""Command line entry point shared by every server.

    def main() -> None:
        run_cli(SPEC, Settings, create_server)

Subcommands:
    serve (default)   run over stdio, or HTTP when MCP_TRANSPORT=http / --transport http
    token <name>      generate a bearer token + the MCP_AUTH_TOKENS entry for it
    check             validate config, connect, print <prefix>_status as JSON; exit 1 on failure
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from pydantic import ValidationError

from stutters_mcp_core.audit import configure_audit_log
from stutters_mcp_core.inbound_auth import NamedTokenVerifier, generate_token, hash_token, valid_token_name
from stutters_mcp_core.server import ServerSpec
from stutters_mcp_core.settings import BaseServerSettings

CreateServer = Callable[..., FastMCP[Any]]


def _token_command(prog: str, name: str) -> None:
    if not valid_token_name(name):
        sys.exit(f"{prog}: token name may only contain letters, digits and _ . @ -")
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
    configure_audit_log(settings.mcp_audit_log)


async def _check(spec: ServerSpec, server: FastMCP[Any]) -> int:
    from fastmcp import Client

    async with Client(server) as client:
        result = await client.call_tool(f"{spec.prefix}_status", raise_on_error=False)
    data: Any = result.structured_content or {"text": [getattr(c, "text", "") for c in result.content]}
    print(json.dumps(data, indent=2, default=str))
    product = data.get(spec.prefix, {}) if isinstance(data, dict) else {}
    return 1 if result.is_error or (isinstance(product, dict) and "error" in product) else 0


def run_cli(
    spec: ServerSpec,
    settings_cls: type[BaseServerSettings],
    create_server: CreateServer,
    *,
    argv: list[str] | None = None,
) -> None:
    prog = spec.name
    parser = argparse.ArgumentParser(prog=prog, description=f"MCP server for {spec.product}")
    parser.add_argument("--transport", choices=["stdio", "http"], help="overrides MCP_TRANSPORT")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the server (default)")
    tok = sub.add_parser("token", help="generate a bearer token for the HTTP transport")
    tok.add_argument("name", help="who the token is for, e.g. alice")
    sub.add_parser("check", help="connect to the appliance and print status")
    args = parser.parse_args(argv)

    if args.command == "token":
        _token_command(prog, args.name)
        return

    try:
        settings = settings_cls()  # type: ignore[call-arg]  # populated from env / .env
    except ValidationError as e:
        sys.exit(f"{prog}: invalid configuration (variables use the {spec.env_prefix} prefix):\n{e}")

    _configure_logging(settings, spec.name.replace("-", "_"))
    log = logging.getLogger(spec.name)

    if args.command == "check":
        sys.exit(asyncio.run(_check(spec, create_server(settings))))

    transport = args.transport or settings.mcp_transport
    if transport == "stdio":
        create_server(settings).run(transport="stdio", show_banner=False)
        return

    # HTTP: fail closed. Never expose the appliance over the network without authentication.
    try:
        tokens = settings.auth_token_hashes()
    except (ValueError, OSError) as e:
        sys.exit(f"{prog}: invalid MCP_AUTH_TOKENS / MCP_AUTH_TOKENS_FILE: {e}")
    if not tokens:
        sys.exit(
            f"{prog}: HTTP transport requires MCP_AUTH_TOKENS (or MCP_AUTH_TOKENS_FILE). "
            f"Generate one with: {prog} token <name>"
        )
    auth: AuthProvider = NamedTokenVerifier(tokens)
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
