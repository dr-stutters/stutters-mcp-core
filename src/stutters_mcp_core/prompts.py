"""MCP prompts: cross-product workflows shipped in each server (roadmap C2).

A prompt is a parameterised set of instructions the user picks in the client ("/ise_troubleshoot_
endpoint_access"); the model then carries the steps out with the tools of the servers named in
`uses`. Register prompts from the same `register(reg)` functions as tools:

    def register(reg: ToolRegistrar) -> None:
        @reg.prompt(title="Troubleshoot endpoint access", uses=["ise", "catc"])
        def ise_troubleshoot_endpoint_access(
            mac: Annotated[str, Field(description="Endpoint MAC address, any notation")],
        ) -> str:
            '''Why can't this endpoint get on the network? Sessions, failure reason, policy hit
            and the switch port, across ISE and Catalyst Center.'''
            return f'''
            1. Call {tool_ref("ise_why_auth_failed")} with mac={mac}.
            2. If a switch is named, run {tool_ref("catc_run_show_commands")} for its port.
            '''

Rules (checked by `testing.prompt_contract_problems`):
- Names are `<prefix>_<workflow>`, snake_case.
- Descriptions are 30-1500 characters (the docstring or `description=`).
- Every argument has a `Field(description=...)`.
- `uses` lists every server prefix the prompt expects, including this server's own.
- Tool names in the body are written with `tool_ref(...)` (or in backticks); every local reference must
  name a tool or prompt this server registers (with all tiers on), and every other prefix must be in `uses`.
- A prompt that needs write tools passes `tier=Tier.WRITE` so it is hidden when writes are off.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, TypeVar

from stutters_mcp_core.writes import Tier, tier_enabled

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from stutters_mcp_core.settings import BaseServerSettings

F = TypeVar("F", bound=Callable[..., Any])

PROMPT_TAG = "workflow"
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]*$")
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9]*_[a-z0-9_]+$")


def tool_ref(name: str) -> str:
    """A tool name formatted for a prompt body (backticks), so the prompt contract test finds it.

    Raises ValueError for something that can't be a tool name, which catches typos such as a
    missing prefix at import time."""
    if not _TOOL_NAME_RE.match(name):
        raise ValueError(f"{name!r} is not a tool name (<prefix>_<name>, snake_case)")
    return f"`{name}`"


def find_tool_refs(text: str, prefixes: Iterable[str]) -> list[str]:
    """Backticked `<prefix>_<name>` identifiers in `text` whose prefix is one of `prefixes`, in order
    of first appearance."""
    wanted = {p for p in prefixes if p}
    if not wanted:
        return []
    pattern = re.compile(r"`((?:" + "|".join(sorted(map(re.escape, wanted))) + r")_[a-z0-9_]+)`")
    seen: dict[str, None] = {}
    for m in pattern.finditer(text):
        seen.setdefault(m.group(1), None)
    return list(seen)


def _check_uses(prefix: str, uses: Iterable[str] | None) -> list[str]:
    out = list(dict.fromkeys(uses or [prefix]))
    for p in out:
        if not _PREFIX_RE.match(p):
            raise ValueError(f"uses: {p!r} is not a server prefix (e.g. 'ise', 'fmc')")
    if prefix not in out:
        out.insert(0, prefix)
    return out


def registered_prompts(mcp: FastMCP[Any]) -> dict[str, dict[str, Any]]:
    """{prompt name: meta} for every prompt registered through a PromptRegistrar on `mcp`."""
    registry: dict[str, dict[str, Any]] | None = getattr(mcp, "_stutters_prompts", None)
    if registry is None:
        registry = {}
        mcp._stutters_prompts = registry  # type: ignore[attr-defined]
    return registry


class PromptRegistrar:
    """Registers workflow prompts named `<prefix>_<workflow>` with metadata the contract linter and
    clients can read: `meta={"uses": [...prefixes], "tools": [...declared tool names], "tier": ...}`."""

    def __init__(self, mcp: FastMCP[Any], settings: BaseServerSettings | None, prefix: str) -> None:
        self.mcp = mcp
        self.settings = settings
        self.prefix = prefix
        self.registered = registered_prompts(mcp)

    def prompt(
        self,
        *,
        title: str | None = None,
        name: str | None = None,
        description: str | None = None,
        uses: Iterable[str] | None = None,
        tools: Iterable[str] = (),
        tier: Tier = Tier.READ,
        tags: set[str] | None = None,
    ) -> Callable[[F], F]:
        """Decorator. `uses`: server prefixes the workflow expects (this server's is added if missing).
        `tools`: tool names the body references (optional; the linter also scans the rendered body).
        `tier`: the highest tool tier the workflow needs; the prompt is hidden when it's disabled."""
        uses_list = _check_uses(self.prefix, uses)
        tool_list = list(dict.fromkeys(tools))
        for t in tool_list:
            tool_ref(t)  # validates the shape

        def deco(fn: F) -> F:
            prompt_name = name or fn.__name__
            if not prompt_name.startswith(self.prefix + "_"):
                raise ValueError(f"Prompt {prompt_name!r} must start with {self.prefix}_")
            if prompt_name in self.registered:
                raise ValueError(f"Prompt {prompt_name!r} is registered twice")
            if self.settings is not None and not tier_enabled(self.settings, tier):
                return fn
            meta = {"uses": uses_list, "tools": tool_list, "tier": tier.value}
            self.mcp.prompt(
                fn,
                name=prompt_name,
                title=title,
                description=description,
                tags={PROMPT_TAG, *(tags or set())},
                meta=meta,
            )
            self.registered[prompt_name] = meta
            return fn

        return deco
