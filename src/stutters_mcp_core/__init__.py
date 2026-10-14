"""Shared FastMCP foundation for the network MCP servers."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("stutters-mcp-core")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0"

__all__ = ["__version__"]
