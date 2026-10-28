"""Shared FastMCP foundation for the network MCP servers.

Most servers need only these names; submodules hold the rest.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("stutters-mcp-core")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"

# Imported after __version__: submodules read it.
from stutters_mcp_core.cli import run_cli  # noqa: E402
from stutters_mcp_core.errors import (  # noqa: E402
    AuthError,
    Conflict,
    InvalidRequest,
    NotFound,
    RateLimited,
    TaskFailed,
    Unreachable,
    UpstreamError,
    UpstreamTimeout,
    ValidationFailed,
    WritesDisabled,
)
from stutters_mcp_core.formatting import (  # noqa: E402
    ResponseFormat,
    compact,
    list_result,
    object_result,
    text_result,
)
from stutters_mcp_core.inbound_auth import current_caller  # noqa: E402
from stutters_mcp_core.prompts import PromptRegistrar, tool_ref  # noqa: E402
from stutters_mcp_core.server import ServerSpec, build_server, get_client, get_settings  # noqa: E402
from stutters_mcp_core.settings import BaseServerSettings, server_config  # noqa: E402
from stutters_mcp_core.tasks import poll_until  # noqa: E402
from stutters_mcp_core.writes import (  # noqa: E402
    Confirm,
    FromResult,
    Plan,
    Tier,
    ToolRegistrar,
    Undo,
    field_changes,
    preview_or_apply,
)

__all__ = [
    "AuthError",
    "BaseServerSettings",
    "Confirm",
    "Conflict",
    "FromResult",
    "InvalidRequest",
    "NotFound",
    "Plan",
    "PromptRegistrar",
    "RateLimited",
    "ResponseFormat",
    "ServerSpec",
    "TaskFailed",
    "Tier",
    "ToolRegistrar",
    "Undo",
    "Unreachable",
    "UpstreamError",
    "UpstreamTimeout",
    "ValidationFailed",
    "WritesDisabled",
    "__version__",
    "build_server",
    "compact",
    "current_caller",
    "field_changes",
    "get_client",
    "get_settings",
    "list_result",
    "object_result",
    "poll_until",
    "preview_or_apply",
    "run_cli",
    "server_config",
    "text_result",
    "tool_ref",
]
