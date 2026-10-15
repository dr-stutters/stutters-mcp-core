"""Tool/prompt manifests: a static JSON description of a server's MCP surface (roadmap C9).

A server exports its manifest with the shared CLI (all tiers on, no appliance needed):

    ise-mcp-server tools --json --out tests/manifests/ise.json

Other servers commit a copy and check their cross-product prompts against it in CI, with no running
servers:

    from stutters_mcp_core.testing import assert_prompt_contract, load_tool_manifests
    await assert_prompt_contract(server, "ise", external_tools=load_tool_manifests("tests/manifests"))

Format (`"manifest": 1`; keys are sorted so the file diffs cleanly):

    {"manifest": 1, "server": "ise-mcp-server", "prefix": "ise", "version": "0.3.0", "core_version": "0.4.0",
     "tools": {"ise_status": {"tier": "read", "title": "..."}, ...},
     "prompts": {"ise_quarantine_host": {"uses": ["ise", "fmc"], "tier": "write", "arguments": ["ip", ...],
                                         "references": ["fmc_contain_host", "ise_apply_anc", ...],
                                         "rendered": true}}}

`references` are the declared `tools` plus the backticked tool names found by rendering the prompt with
the prompt's registered `example` (else placeholder) arguments. That is best effort: a prompt that fails
to render (it needs its lifespan, or the placeholders are invalid) has `"rendered": false` and lists only
its declared `tools`; register it with `example={...}` to fix that.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.server import server as fastmcp_server

from stutters_mcp_core import __version__
from stutters_mcp_core.prompts import find_tool_refs, render_args, rendered_text

MANIFEST_VERSION = 1
_TIERS = ("read", "write", "dangerous")


@dataclass(frozen=True)
class ToolManifest:
    server: str
    prefix: str
    version: str | None = None
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    prompts: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def names(self) -> set[str]:
        """Every tool and prompt name the server registers (with the tiers the manifest was built with)."""
        return set(self.tools) | set(self.prompts)

    def references_to(self, prefix: str) -> dict[str, list[str]]:
        """{prompt name: [referenced names with `prefix`]} for this server's prompts."""
        out: dict[str, list[str]] = {}
        for name, meta in self.prompts.items():
            refs = [r for r in meta.get("references", []) if r.split("_", 1)[0] == prefix]
            if refs:
                out[name] = refs
        return out


async def build_manifest(
    mcp: FastMCP[Any], *, server: str, prefix: str, version: str | None = None
) -> dict[str, Any]:
    """The manifest dict for `mcp` (built without running the server's lifespan, so no appliance
    connection is made). Build the server with all tiers on to list every tool."""
    tools: dict[str, dict[str, Any]] = {}
    for t in await mcp.list_tools(run_middleware=False):
        tier = next((x for x in _TIERS if x in (t.tags or set())), None)
        entry: dict[str, Any] = {"tier": tier}
        title = t.title or (t.annotations.title if t.annotations else None)
        if title:
            entry["title"] = title
        tools[t.name] = entry
    prompts: dict[str, dict[str, Any]] = {}
    for p in await mcp.list_prompts(run_middleware=False):
        meta = dict(p.meta or {})
        uses = list(meta.get("uses") or [prefix])
        args = p.to_mcp_prompt().arguments or []
        refs: list[str] = list(meta.get("tools") or [])
        try:
            with _quiet_fastmcp():
                result = await mcp.render_prompt(
                    p.name, render_args(args, None, meta.get("example")), run_middleware=False
                )
        except Exception:  # noqa: BLE001 - best effort; needs a lifespan or real arguments
            rendered = False
        else:
            rendered = True
            refs += find_tool_refs(rendered_text(result), {prefix, *uses})
        prompts[p.name] = {
            "uses": uses,
            "tier": meta.get("tier", "read"),
            "arguments": [a.name for a in args],
            "references": list(dict.fromkeys(refs)),
            "rendered": rendered,
        }
    return {
        "manifest": MANIFEST_VERSION,
        "server": server,
        "prefix": prefix,
        "version": version,
        "core_version": __version__,
        "tools": dict(sorted(tools.items())),
        "prompts": dict(sorted(prompts.items())),
    }


@contextmanager
def _quiet_fastmcp() -> Iterator[None]:
    """FastMCP logs a traceback for a prompt that fails to render; the manifest just notes it."""
    lg = logging.getLogger(getattr(getattr(fastmcp_server, "logger", None), "name", "fastmcp"))
    old = lg.disabled
    lg.disabled = True
    try:
        yield
    finally:
        lg.disabled = old


def manifest_json(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def parse_tool_manifest(data: dict[str, Any], source: str = "manifest") -> ToolManifest:
    if not isinstance(data, dict) or data.get("manifest") != MANIFEST_VERSION:
        raise ValueError(f'{source}: not a stutters tool manifest (expected "manifest": {MANIFEST_VERSION})')
    prefix, server = data.get("prefix"), data.get("server")
    tools, prompts = data.get("tools"), data.get("prompts", {})
    if not isinstance(prefix, str) or not prefix or not isinstance(server, str):
        raise ValueError(f"{source}: manifest needs string 'server' and 'prefix'")
    if not isinstance(tools, dict) or not isinstance(prompts, dict):
        raise ValueError(f"{source}: 'tools' and 'prompts' must be objects")
    return ToolManifest(
        server=server, prefix=prefix, version=data.get("version"), tools=tools, prompts=prompts
    )


def load_tool_manifest(path: str | Path) -> ToolManifest:
    """Read one manifest file written by `<server> tools --json`."""
    p = Path(path)
    try:
        data = json.loads(p.read_text())
    except ValueError as e:
        raise ValueError(f"{p}: invalid JSON ({e})") from e
    return parse_tool_manifest(data, str(p))


def load_tool_manifests(*paths: str | Path) -> dict[str, set[str]]:
    """{prefix: tool and prompt names} from manifest files and/or directories of `*.json` manifests,
    ready for `prompt_contract_problems(external_tools=...)`. Two manifests for one prefix are an error."""
    out: dict[str, set[str]] = {}
    for m in read_tool_manifests(*paths):
        if m.prefix in out:
            raise ValueError(f"two manifests for prefix {m.prefix!r}")
        out[m.prefix] = m.names
    return out


def read_tool_manifests(*paths: str | Path) -> list[ToolManifest]:
    """Every manifest in the given files and/or directories (`*.json`, sorted by file name)."""
    out: list[ToolManifest] = []
    for path in paths:
        p = Path(path)
        files = sorted(p.glob("*.json")) if p.is_dir() else [p]
        out += [load_tool_manifest(f) for f in files]
    return out
