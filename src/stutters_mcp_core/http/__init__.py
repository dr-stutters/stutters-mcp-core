"""Outbound HTTP: client, auth strategies, throttling, pagination."""

from stutters_mcp_core.http.auth import (
    AuthStrategy,
    BasicAuth,
    LoginTokenAuth,
    NoAuth,
    StaticHeaderAuth,
    Token,
    basic_header,
)
from stutters_mcp_core.http.client import ApiClient, default_error_detail
from stutters_mcp_core.http.paging import (
    NoPaging,
    OffsetLimit,
    Page,
    PageNumber,
    Pager,
    dig,
    fetch_all,
    page_of,
)
from stutters_mcp_core.http.ratelimit import RateLimiter

__all__ = [
    "ApiClient",
    "AuthStrategy",
    "BasicAuth",
    "LoginTokenAuth",
    "NoAuth",
    "NoPaging",
    "OffsetLimit",
    "Page",
    "PageNumber",
    "Pager",
    "RateLimiter",
    "StaticHeaderAuth",
    "Token",
    "basic_header",
    "default_error_detail",
    "dig",
    "fetch_all",
    "page_of",
]
