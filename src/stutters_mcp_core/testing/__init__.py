"""Test helpers for servers built on stutters-mcp-core.

    from stutters_mcp_core.testing import assert_tool_contract, call_tool, live_settings

    async def test_contract():
        await assert_tool_contract(create_server(settings(writes=True, dangerous=True)), prefix="ise")

    async def test_prompts():                    # every prompt renders; local tool references exist
        server = create_server(settings(writes=True, dangerous=True))
        await assert_prompt_contract(server, prefix="ise", known_prefixes={"cml", "ise", "fmc", "catc"},
                                     samples={"ise_troubleshoot_endpoint_access": {"mac": "0011.2233.4455"}})

    async def test_vocabulary():                 # argument names vs docs/VOCABULARY.md, as warnings
        await warn_vocabulary(server, "ise", allow={"ise_export_config.format"})

    async def test_prompt_snapshots():           # rendered prompts match tests/snapshots/prompts/*.md
        await assert_prompt_snapshots(server, "tests/snapshots/prompts", samples=SAMPLES)

    async def test_cross_server_refs():          # other servers' tools, from committed manifests
        await assert_prompt_contract(server, prefix="ise", samples=SAMPLES,
                                     external_tools=load_tool_manifests("tests/manifests"))
        await assert_inbound_references(server, "ise", "tests/manifests")   # the reverse direction

    @pytest.mark.live
    async def test_live_status():
        s = live_settings(Settings, "ISE_")      # skips unless ISE_URL is set and reachable
        ...

pytest is only imported when a helper that needs it is called, so this module is safe to import from
a server's runtime code (it isn't, normally).
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import re
import socket
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastmcp import Client, FastMCP

from stutters_mcp_core.manifest import (
    ToolManifest,
    build_manifest,
    load_tool_manifest,
    load_tool_manifests,
    read_tool_manifests,
)
from stutters_mcp_core.prompts import AUTO_ARG_DOC, render_args, rendered_text, sample_value
from stutters_mcp_core.settings import BaseServerSettings
from stutters_mcp_core.suite import PACK, SuitePrompt, is_suite_prompt
from stutters_mcp_core.suite import PEERS as SUITE_PEERS
from stutters_mcp_core.testing.scenario import assert_scenarios, load_scenario, scenario_problems
from stutters_mcp_core.vocabulary import vocabulary_problems, warn_vocabulary

__all__ = [
    "as_caller",
    "SNAPSHOT_UPDATE_ENV",
    "ToolManifest",
    "assert_inbound_references",
    "assert_prompt_contract",
    "assert_prompt_snapshots",
    "assert_scenarios",
    "assert_suite_pack",
    "assert_tool_contract",
    "build_manifest",
    "call_tool",
    "compare_snapshots",
    "inbound_reference_problems",
    "live_settings",
    "load_scenario",
    "load_tool_manifest",
    "load_tool_manifests",
    "prompt_contract_problems",
    "prompt_snapshot_problems",
    "reachable",
    "read_tool_manifests",
    "render_prompts",
    "scenario_problems",
    "suite_pack_problems",
    "tool_contract_problems",
    "vocabulary_problems",
    "warn_vocabulary",
]

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PAGING_PARAMS = ("limit", "offset", "response_format")
UNDOCUMENTED_OK = {"limit", "offset", "response_format", "confirm", "fields"}
MIN_DESCRIPTION, MAX_DESCRIPTION = 30, 1500


async def tool_contract_problems(mcp: FastMCP[Any], prefix: str) -> list[str]:
    """Everything wrong with the server's tool surface, as human-readable strings."""
    problems: list[str] = []
    async with Client(mcp) as client:
        tools = await client.list_tools()
    if not tools:
        return ["server exposes no tools"]
    for t in tools:
        where = t.name
        if not t.name.startswith(prefix + "_") or not NAME_RE.match(t.name):
            problems.append(f"{where}: name must be snake_case and start with '{prefix}_'")
        desc = (t.description or "").strip()
        if not MIN_DESCRIPTION <= len(desc) <= MAX_DESCRIPTION:
            problems.append(
                f"{where}: description length {len(desc)} not in {MIN_DESCRIPTION}..{MAX_DESCRIPTION}"
            )
        ann = t.annotations
        if ann is None or ann.read_only_hint is None:
            problems.append(f"{where}: missing readOnlyHint annotation (register via ToolRegistrar)")
            continue
        meta = t.meta or {}
        tags = set(meta.get("fastmcp", {}).get("tags", []) or [])
        if not tags & {"read", "write", "dangerous"}:
            problems.append(f"{where}: no tier tag (register via ToolRegistrar)")
        if "read" in tags and not ann.read_only_hint:
            problems.append(f"{where}: read-tier tool must have readOnlyHint=True")
        if tags & {"write", "dangerous"} and ann.read_only_hint:
            problems.append(f"{where}: write-tier tool must have readOnlyHint=False")
        if "dangerous" in tags and not ann.destructive_hint:
            problems.append(f"{where}: dangerous tool must have destructiveHint=True")
        props: dict[str, Any] = (t.input_schema or {}).get("properties", {})
        if tags & {"write", "dangerous"} and "confirm" not in props:
            problems.append(f"{where}: write tools must take a `confirm` parameter (preview-first)")
        if "_list_" in t.name or t.name.startswith(f"{prefix}_list"):
            missing = [p for p in PAGING_PARAMS if p not in props]
            if missing:
                problems.append(f"{where}: list tools need {', '.join(missing)}")
        for pname, schema in props.items():
            if pname in UNDOCUMENTED_OK:
                continue
            if not (isinstance(schema, dict) and (schema.get("description") or schema.get("enum"))):
                problems.append(f"{where}: parameter '{pname}' has no description")
    return problems


_AUTO_ARG_DOC = AUTO_ARG_DOC
_sample_value = sample_value
_rendered_text = rendered_text


async def prompt_contract_problems(
    mcp: FastMCP[Any],
    prefix: str,
    *,
    samples: dict[str, dict[str, str]] | None = None,
    known_prefixes: set[str] | None = None,
    external_tools: Mapping[str, Iterable[str]] | None = None,
) -> list[str]:
    """Everything wrong with the server's prompts: naming, descriptions, argument docs, `uses`
    metadata, and tool references. Each prompt is rendered with `samples[name]` (string arguments, as
    a client sends them) or placeholder values; tool names in backticks (`tool_ref`) are collected
    from the rendered body plus the declared `tools`, then:
    - references with this server's prefix must be a registered tool or prompt (run with all tiers on);
    - references with another prefix must be listed in `uses`.
    `known_prefixes` (e.g. {"cml", "ise", "fmc", "catc"}), when given, also flags `uses` entries outside
    that set (typos) and finds references to those servers that are missing from `uses`.
    `external_tools` ({prefix: names}, e.g. from `load_tool_manifests("tests/manifests")`) checks
    references to other servers against their exported manifests; its prefixes count as known.
    Suite pack prompts (`suite_*`, C48) are checked for naming, descriptions, arguments, rendering and
    `uses` (against the suite peers) here; whether their leg tools exist is `suite_pack_problems`' job."""
    from stutters_mcp_core.prompts import find_tool_refs

    problems: list[str] = []
    samples = samples or {}
    external = {k: set(v) for k, v in (external_tools or {}).items()}
    if known_prefixes is not None or external:
        known_prefixes = {prefix, *(known_prefixes or ()), *external}
    async with Client(mcp) as client:
        prompts = await client.list_prompts()
        local = {t.name for t in await client.list_tools()} | {p.name for p in prompts}
        for p in prompts:
            where = f"prompt {p.name}"
            suite = is_suite_prompt(p.name, p.meta)
            if (not suite and not p.name.startswith(prefix + "_")) or not NAME_RE.match(p.name):
                problems.append(f"{where}: name must be snake_case and start with '{prefix}_'")
            desc = (p.description or "").strip()
            if not MIN_DESCRIPTION <= len(desc) <= MAX_DESCRIPTION:
                problems.append(
                    f"{where}: description length {len(desc)} not in {MIN_DESCRIPTION}..{MAX_DESCRIPTION}"
                )
            args = p.arguments or []
            for a in args:
                if not (a.description or "").strip() or (a.description or "").startswith(_AUTO_ARG_DOC):
                    problems.append(f"{where}: argument '{a.name}' has no description")
            meta = p.meta or {}
            uses = meta.get("uses")
            if not isinstance(uses, list) or not uses:
                problems.append(f"{where}: no `uses` metadata (register via ToolRegistrar.prompt)")
                uses = [prefix]
            if prefix not in uses and not suite:
                problems.append(f"{where}: `uses` must include this server's prefix '{prefix}'")
            if known_prefixes is not None or suite:
                allowed = set(SUITE_PEERS) if suite else (known_prefixes or set())
                for u in uses:
                    if u not in allowed:
                        problems.append(f"{where}: `uses` names unknown server prefix '{u}'")
            refs: list[str] = list(meta.get("tools") or [])
            try:
                rendered = await client.get_prompt(
                    p.name, render_args(args, samples.get(p.name), meta.get("example"))
                )
            except Exception as e:  # noqa: BLE001 - report, don't stop the linter
                problems.append(f"{where}: does not render ({e}); pass samples={{'{p.name}': {{...}}}}")
            else:
                text = _rendered_text(rendered)
                if not text.strip():
                    problems.append(f"{where}: renders an empty body")
                all_prefixes = {prefix, *uses, *(known_prefixes or ())}
                refs += find_tool_refs(text, all_prefixes)
            for ref in dict.fromkeys(refs):
                ref_prefix = ref.split("_", 1)[0]
                if suite:  # leg tools are checked by suite_pack_problems (with `pending`)
                    if ref_prefix not in uses:
                        problems.append(f"{where}: references '{ref}' but '{ref_prefix}' is not in `uses`")
                    continue
                if ref_prefix == prefix:
                    if ref not in local:
                        problems.append(f"{where}: references unknown local tool '{ref}'")
                elif ref_prefix not in uses:
                    problems.append(f"{where}: references '{ref}' but '{ref_prefix}' is not in `uses`")
                elif ref_prefix in external and ref not in external[ref_prefix]:
                    problems.append(
                        f"{where}: references '{ref}', which the {ref_prefix} manifest does not list"
                    )
    return problems


async def assert_prompt_contract(
    mcp: FastMCP[Any],
    prefix: str,
    *,
    samples: dict[str, dict[str, str]] | None = None,
    known_prefixes: set[str] | None = None,
    external_tools: Mapping[str, Iterable[str]] | None = None,
) -> None:
    """Fail with every prompt contract violation (see `prompt_contract_problems`). Pass
    `known_prefixes={"cml", "ise", "fmc", "catc"}` so references to servers missing from `uses` are found,
    and `external_tools=load_tool_manifests(...)` to check references against the other servers."""
    problems = await prompt_contract_problems(
        mcp, prefix, samples=samples, known_prefixes=known_prefixes, external_tools=external_tools
    )
    assert not problems, "Prompt contract violations:\n  " + "\n  ".join(problems)


SNAPSHOT_UPDATE_ENV = "UPDATE_PROMPT_SNAPSHOTS"


async def render_prompts(
    mcp: FastMCP[Any], *, samples: dict[str, dict[str, str]] | None = None
) -> dict[str, str]:
    """{prompt name: snapshot text} for every prompt, rendered through an in-memory client with
    `samples[name]` plus placeholders for the other arguments (as `prompt_contract_problems` does).
    The text holds the title, description, arguments used and each message, so a change to any of
    them shows in the snapshot diff."""
    samples = samples or {}
    out: dict[str, str] = {}
    async with Client(mcp) as client:
        for p in sorted(await client.list_prompts(), key=lambda p: p.name):
            args = render_args(p.arguments or [], samples.get(p.name), (p.meta or {}).get("example"))
            result = await client.get_prompt(p.name, args)
            lines = [
                f"# {p.name}",
                "",
                f"title: {p.title or ''}",
                f"arguments: {json.dumps(args, sort_keys=True)}",
                "",
                (p.description or "").strip(),
            ]
            for msg in result.messages:
                text = getattr(msg.content, "text", None)
                body = (
                    text
                    if text is not None
                    else json.dumps(msg.content.model_dump(mode="json"), sort_keys=True)
                )
                lines += ["", f"## {msg.role}", "", body.strip()]
            out[p.name] = "\n".join(line.rstrip() for line in "\n".join(lines).splitlines()) + "\n"
    return out


async def prompt_snapshot_problems(
    mcp: FastMCP[Any],
    snapshot_dir: str | Path,
    *,
    samples: dict[str, dict[str, str]] | None = None,
    update: bool | None = None,
) -> list[str]:
    """Compare every rendered prompt with `<snapshot_dir>/<prompt>.md`. Missing snapshots are written
    (and reported, so CI fails until they are committed); changed or stale ones are reported with a
    diff. With `update=True` (default: env UPDATE_PROMPT_SNAPSHOTS=1) changed snapshots are rewritten
    and stale ones deleted instead, and nothing is reported."""
    if update is None:
        update = os.environ.get(SNAPSHOT_UPDATE_ENV, "").lower() in {"1", "true", "yes"}
    rendered = await render_prompts(mcp, samples=samples)
    return compare_snapshots(rendered, Path(snapshot_dir), update=update)


def compare_snapshots(rendered: dict[str, str], directory: Path, *, update: bool) -> list[str]:
    """Sync half of `prompt_snapshot_problems`: compare/write `<directory>/<name>.md` files."""
    directory.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    for name, text in rendered.items():
        path = directory / f"{name}.md"
        if not path.exists():
            path.write_text(text)
            if not update:
                problems.append(f"prompt {name}: new snapshot written to {path}; review and commit it")
            continue
        old = path.read_text()
        if old == text:
            continue
        if update:
            path.write_text(text)
            continue
        diff = "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True), text.splitlines(keepends=True), str(path), "rendered"
            )
        )
        problems.append(
            f"prompt {name}: rendering differs from {path} ({SNAPSHOT_UPDATE_ENV}=1 to accept)\n{diff}"
        )
    for path in sorted(directory.glob("*.md")):
        if path.stem not in rendered:
            if update:
                path.unlink()
            else:
                problems.append(f"{path}: no such prompt any more ({SNAPSHOT_UPDATE_ENV}=1 deletes it)")
    return problems


async def assert_prompt_snapshots(
    mcp: FastMCP[Any],
    snapshot_dir: str | Path,
    *,
    samples: dict[str, dict[str, str]] | None = None,
    update: bool | None = None,
) -> None:
    """Fail if any prompt renders differently from its committed snapshot (see `prompt_snapshot_problems`).
    Use one directory per server configuration, e.g. tests/snapshots/prompts-rw for all tiers on."""
    problems = await prompt_snapshot_problems(mcp, snapshot_dir, samples=samples, update=update)
    assert not problems, "Prompt snapshot mismatches:\n" + "\n".join(problems)


async def inbound_reference_problems(
    mcp: FastMCP[Any], prefix: str, manifests: Iterable[ToolManifest]
) -> list[str]:
    """The reverse check: prompts in *other* servers' manifests that reference a `<prefix>_*` tool or
    prompt this server does not register (build it with all tiers on). Catches renaming or removing a
    tool another server's workflow depends on."""
    local = {t.name for t in await mcp.list_tools(run_middleware=False)}
    local |= {p.name for p in await mcp.list_prompts(run_middleware=False)}
    problems: list[str] = []
    for m in manifests:
        if m.prefix == prefix:
            continue
        for prompt, refs in sorted(m.references_to(prefix).items()):
            if is_suite_prompt(prompt, m.prompts.get(prompt)):
                continue  # core's pack, checked by suite_pack_problems
            for ref in refs:
                if ref not in local:
                    problems.append(
                        f"{m.server} prompt {prompt}: references '{ref}', which this server does not register"
                    )
    return problems


async def assert_inbound_references(mcp: FastMCP[Any], prefix: str, *paths: str | Path) -> None:
    """Fail if another server's prompt (from the manifests in `paths`) references a missing local tool."""
    problems = await inbound_reference_problems(mcp, prefix, read_tool_manifests(*paths))
    assert not problems, "Other servers' prompts reference missing tools:\n  " + "\n  ".join(problems)


async def suite_pack_problems(
    mcp: FastMCP[Any],
    prefix: str,
    *manifest_paths: str | Path,
    pending: Iterable[str] = (),
    pack: Mapping[str, SuitePrompt] | None = None,
) -> list[str]:
    """Check the suite prompt pack (C48) on a server built with all tiers on:
    - every pack prompt is registered (the server calls `suite.register`);
    - every leg tool of this server (`prefix`) is registered here;
    - every leg tool of a peer whose manifest is in `manifest_paths` is listed in that manifest.
    `pending` names leg tools that are known to be missing (a peer has not shipped them yet); they are
    not reported, but listing one that now exists is (drop it from `pending`). Peers without a committed
    manifest are not checked."""
    pack = PACK if pack is None else pack
    local = {t.name for t in await mcp.list_tools(run_middleware=False)}
    registered = {p.name for p in await mcp.list_prompts(run_middleware=False)}
    local |= registered
    peers = {m.prefix: m.names for m in read_tool_manifests(*manifest_paths)}
    peers[prefix] = local
    waived = set(pending)
    problems: list[str] = []
    for sp in pack.values():
        where = f"suite prompt {sp.name}"
        if sp.name not in registered:
            problems.append(f"{where}: not registered (add suite.register to build_server's register list)")
        for leg in sp.legs:
            names = peers.get(leg.peer)
            if names is None:
                continue
            for tool in leg.tools:
                if tool in names and tool in waived:
                    problems.append(f"{where}: '{tool}' is in `pending` but {leg.peer} has it now")
                elif tool not in names and tool not in waived:
                    source = "this server" if leg.peer == prefix else f"the {leg.peer} manifest"
                    problems.append(f"{where}: {leg.peer} leg calls '{tool}', which {source} does not list")
    for tool in sorted(waived - {t for sp in pack.values() for t in sp.tools}):
        problems.append(f"`pending` names '{tool}', which no suite prompt leg calls")
    return list(dict.fromkeys(problems))


async def assert_suite_pack(
    mcp: FastMCP[Any],
    prefix: str,
    *manifest_paths: str | Path,
    pending: Iterable[str] = (),
) -> None:
    """Fail if the suite pack is not registered or a leg's tool is missing (see `suite_pack_problems`)."""
    problems = await suite_pack_problems(mcp, prefix, *manifest_paths, pending=pending)
    assert not problems, "Suite prompt pack problems:\n  " + "\n  ".join(problems)


async def assert_tool_contract(mcp: FastMCP[Any], prefix: str) -> None:
    problems = await tool_contract_problems(mcp, prefix)
    assert not problems, "Tool contract violations:\n  " + "\n  ".join(problems)


async def call_tool(mcp: FastMCP[Any], name: str, args: dict[str, Any] | None = None) -> Any:
    """Call a tool in-memory and return structured content (or the text content)."""
    async with Client(mcp) as client:
        result = await client.call_tool(name, args or {})
    if result.structured_content is not None:
        return result.structured_content
    return "\n".join(getattr(c, "text", "") for c in result.content)


def reachable(url: str, timeout: float = 3.0) -> bool:
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname or "", port), timeout=timeout):
            return True
    except OSError:
        return False


def live_settings[S: BaseServerSettings](settings_cls: type[S], env_prefix: str, **overrides: Any) -> S:
    """Settings from env/.env for live tests; pytest.skip() if not configured or unreachable."""
    import pytest
    from pydantic import ValidationError

    try:
        settings = settings_cls(**overrides)
    except ValidationError:
        pytest.skip(f"{env_prefix}URL / credentials not configured")
    if not reachable(settings.url):
        pytest.skip(f"{settings.url} is unreachable (lab down or VPN off)")
    return settings


@contextlib.contextmanager
def as_caller(name: str) -> Iterator[None]:
    """Make `current_caller()` return `name` inside the block (in-memory tests of roles and
    two-person confirms, where there is no HTTP bearer token)."""
    from stutters_mcp_core.inbound_auth import _caller_override

    _caller_override.append(name)
    try:
        yield
    finally:
        _caller_override.pop()
