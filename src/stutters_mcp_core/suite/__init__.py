"""The suite prompt pack (roadmap C48): `suite_*` workflow prompts shipped by core.

Some workflows span several products and have no natural owner (change check, maintenance window,
certificate expiry, decommission, ...). If one server owned them they would be missing wherever that server
is not deployed, so core ships them and **every server registers the whole pack**: add `suite.register` to
`build_server(register=[...])`:

    from stutters_mcp_core import suite

    def create_server(settings, *, auth=None):
        return build_server(SPEC, settings, ..., register=[system.register, ..., suite.register])

A client connected to several servers then sees the same `suite_*` prompt from each of them; any copy
works. A server does not know which other servers the client has, so each suite prompt:
- declares its **legs**: one per peer server (`SuiteLeg(peer="ise", ...)`), with the tools that leg calls;
  the prompt's `requires` (and `uses`, for clients and the prompt test kit) are those peers;
- takes a `servers` argument (comma-separated prefixes or server names the client has connected). Given,
  the body holds only the connected legs and its first lines name each skipped leg and what is not
  checked; left empty, the body tells the model to check which `<prefix>_*` tools it has, skip the other
  legs and say so in the first lines of its answer.

Define a suite prompt in a module under `stutters_mcp_core/suite/` (imported at the bottom of this file):

    LEGS = (SuiteLeg("catsdwan", "BFD and OMP state", ("catsdwan_bfd_sessions",)),
            SuiteLeg("ise", "authentication failures", ("ise_recent_auth_failures",)))

    @suite_prompt(title="Site health across products", legs=LEGS, example={"site": "100"})
    def suite_site_health(site: Annotated[str, Field(description="...")],
                          servers: SERVERS_ARG = "") -> str:
        '''One site's health across SD-WAN and ISE ... (30-1500 characters).'''
        plan = plan_legs(LEGS, servers)
        steps = [f"Call {tool_ref('catsdwan_bfd_sessions')} for site {site}."] if plan.has("catsdwan") else []
        ...
        return plan.header + "\\n\\n" + numbered(steps)

Tests: `pack_problems()` checks the pack itself (core's own tests); servers check that the pack is
registered and that every leg's tools exist on them and in their committed peer manifests with
`testing.assert_suite_pack(server, "ise", "tests/manifests")`.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, TypeVar

from pydantic import Field

from stutters_mcp_core.prompts import PROMPT_TAG, find_tool_refs, registered_prompts, tool_ref
from stutters_mcp_core.writes import Tier, tier_enabled

if TYPE_CHECKING:
    from stutters_mcp_core.writes import ToolRegistrar

F = TypeVar("F", bound=Callable[..., Any])

SUITE_PREFIX = "suite"
SUITE_TAG = "suite"

# Server prefixes a suite prompt may name as a peer, with the product each one fronts.
PEERS: dict[str, str] = {
    "catsdwan": "Catalyst SD-WAN Manager",
    "ise": "Cisco ISE",
    "catc": "Catalyst Center",
    "fmc": "Secure Firewall Management Center",
    "cml": "Cisco Modeling Labs",
    "netbox": "NetBox",
    "splunk": "Splunk",
    "nd": "Nexus Dashboard",
    "sna": "Secure Network Analytics",
    "nso": "Network Services Orchestrator",
    "cnc": "Crosswork Network Controller",
    "win": "Windows Server (Active Directory)",
}

# Server names that don't reduce to their prefix ('Windows_Server' -> 'win'), after lower-casing and
# dropping '-mcp-server', '-mcp', '-' and '_'.
PEER_ALIASES: dict[str, str] = {"windowsserver": "win", "windows": "win"}

SERVERS_DOC = (
    "Servers connected in this client, comma-separated prefixes or server names (e.g. 'catsdwan,ise' or "
    "'cat-sdwan-mcp-server, ise-mcp-server'). Legs for servers not listed are skipped and named as "
    "skipped. Leave empty to let the assistant check which tools it has."
)
SERVERS_ARG = Annotated[str, Field(description=SERVERS_DOC)]

_NAME_RE = re.compile(r"^suite_[a-z0-9_]+$")


@dataclass(frozen=True)
class SuiteLeg:
    """One peer server's part of a suite prompt."""

    peer: str  # server prefix, a key of PEERS
    purpose: str  # what the leg contributes, e.g. "BFD sessions, OMP peers and app-route SLA"
    tools: tuple[str, ...] = ()  # every tool of `peer` the leg calls

    @property
    def product(self) -> str:
        return PEERS.get(self.peer, self.peer)


@dataclass(frozen=True)
class SuitePrompt:
    name: str
    title: str
    fn: Callable[..., Any]
    legs: tuple[SuiteLeg, ...]
    tier: Tier = Tier.READ
    example: dict[str, str] = field(default_factory=dict)

    @property
    def requires(self) -> list[str]:
        """Peer prefixes in leg order."""
        return list(dict.fromkeys(leg.peer for leg in self.legs))

    @property
    def tools(self) -> list[str]:
        return list(dict.fromkeys(t for leg in self.legs for t in leg.tools))

    @property
    def description(self) -> str:
        return inspect.cleandoc(self.fn.__doc__ or "")

    def meta(self) -> dict[str, Any]:
        """The prompt's MCP `meta`: the usual `uses`/`tools`/`tier` plus `suite` and `requires`."""
        meta: dict[str, Any] = {
            "uses": self.requires,
            "requires": self.requires,
            "tools": self.tools,
            "tier": self.tier.value,
            "suite": True,
        }
        if self.example:
            meta["example"] = dict(self.example)
        return meta


# The pack: every suite prompt core ships, by name.
PACK: dict[str, SuitePrompt] = {}


def suite_prompt(
    *,
    title: str,
    legs: Iterable[SuiteLeg],
    tier: Tier = Tier.READ,
    example: dict[str, str] | None = None,
    pack: dict[str, SuitePrompt] | None = None,
) -> Callable[[F], F]:
    """Decorator: add a `suite_<workflow>` prompt to the pack (`pack`, default PACK). The function takes
    a `servers: SERVERS_ARG = ""` argument and builds its body with `plan_legs(legs, servers)`."""
    leg_list = tuple(legs)
    if not leg_list:
        raise ValueError("a suite prompt needs at least one leg")
    for leg in leg_list:
        if leg.peer not in PEERS:
            raise ValueError(f"leg peer {leg.peer!r} is not a suite server prefix ({', '.join(PEERS)})")
        for t in leg.tools:
            tool_ref(t)
            if not t.startswith(leg.peer + "_"):
                raise ValueError(f"leg {leg.peer!r} lists {t!r}, which is not a {leg.peer} tool")
    if example is not None and not all(isinstance(v, str) for v in example.values()):
        raise ValueError("example: argument values must be strings, as a client sends them")
    target = PACK if pack is None else pack

    def deco(fn: F) -> F:
        name = fn.__name__
        if not _NAME_RE.match(name):
            raise ValueError(f"suite prompt {name!r} must be named suite_<workflow>, snake_case")
        if name in target:
            raise ValueError(f"suite prompt {name!r} is defined twice")
        if "servers" not in inspect.signature(fn).parameters:
            raise ValueError(f"suite prompt {name!r} must take a `servers: SERVERS_ARG = ''` argument")
        target[name] = SuitePrompt(name, title, fn, leg_list, tier, dict(example or {}))
        return fn

    return deco


def parse_servers(servers: str | None) -> set[str] | None:
    """Prefixes from a `servers` argument ('catsdwan, ise-mcp-server, cat-sdwan-mcp-server'), or None
    when it is empty (unknown: the model checks)."""
    out: set[str] = set()
    for raw in re.split(r"[,\s]+", (servers or "").strip().lower()):
        if not raw:
            continue
        name = raw.removesuffix("-mcp-server").removesuffix("-mcp").replace("-", "").replace("_", "")
        out.add(PEER_ALIASES.get(name, name))
    return out or None


@dataclass(frozen=True)
class LegPlan:
    """Which legs of a suite prompt run, from the `servers` argument."""

    legs: tuple[SuiteLeg, ...]  # the legs to run (every leg when the servers are unknown)
    skipped: tuple[SuiteLeg, ...]  # legs whose server is not connected
    known: bool  # False when `servers` was empty: the model checks which tools it has

    def has(self, peer: str) -> bool:
        return any(leg.peer == peer for leg in self.legs)

    @property
    def header(self) -> str:
        """The body's first lines: which servers the workflow needs, and which legs are skipped."""
        need = ", ".join(f"{leg.peer} ({leg.product})" for leg in _unique(self.legs + self.skipped))
        lines = [f"This suite workflow uses these servers: {need}."]
        if not self.known:
            prefixes = ", ".join(f"`{leg.peer}_*`" for leg in _unique(self.legs))
            lines.append(
                f"First check which of these tools you have: {prefixes}. Skip the leg of every server whose "
                "tools you don't have, and start your answer with one line per skipped leg: "
                "'<server> leg skipped: server not connected; not checked: <what the leg checks>'."
            )
            return "\n".join(lines)
        if self.skipped:
            lines.append("Skipped legs (server not connected); say so in the first lines of your answer:")
            lines += [
                f"- {leg.peer} leg skipped: server not connected; not checked: {leg.purpose}."
                for leg in self.skipped
            ]
        if not self.legs:
            lines.append("None of these servers is connected, so there is nothing to run: say so and stop.")
        return "\n".join(lines)


def _unique(legs: Iterable[SuiteLeg]) -> list[SuiteLeg]:
    seen: dict[str, SuiteLeg] = {}
    for leg in legs:
        seen.setdefault(leg.peer, leg)
    return list(seen.values())


def plan_legs(legs: Iterable[SuiteLeg], servers: str | None) -> LegPlan:
    leg_list = tuple(legs)
    connected = parse_servers(servers)
    if connected is None:
        return LegPlan(leg_list, (), known=False)
    run = tuple(leg for leg in leg_list if leg.peer in connected)
    skip = tuple(leg for leg in leg_list if leg.peer not in connected)
    return LegPlan(run, skip, known=True)


def numbered(steps: Iterable[str], start: int = 1) -> str:
    """Steps as a numbered list, one per line."""
    return "\n".join(f"{i}. {s}" for i, s in enumerate(steps, start))


def register(reg: ToolRegistrar, pack: Mapping[str, SuitePrompt] | None = None) -> None:
    """Register the suite prompt pack on this server. Every server adds this to `build_server(register=
    [...])`. A prompt whose tier is off is skipped, like other prompts."""
    for sp in (PACK if pack is None else pack).values():
        registered = registered_prompts(reg.mcp)
        if sp.name in registered:
            raise ValueError(f"Prompt {sp.name!r} is registered twice")
        if reg.settings is not None and not tier_enabled(reg.settings, sp.tier):
            continue
        meta = sp.meta()
        reg.mcp.prompt(
            sp.fn,
            name=sp.name,
            title=sp.title,
            tags={PROMPT_TAG, SUITE_TAG},
            meta=meta,
        )
        registered[sp.name] = meta


def is_suite_prompt(name: str, meta: Mapping[str, Any] | None) -> bool:
    """True for a pack prompt (name `suite_*` with `suite: true` in its meta or manifest entry)."""
    return name.startswith(SUITE_PREFIX + "_") and bool((meta or {}).get("suite"))


def pack_problems(pack: Mapping[str, SuitePrompt] | None = None) -> list[str]:
    """Everything wrong with the pack itself, without a server: descriptions, argument docs, the
    `servers` argument, and that every backticked tool in each rendering (all servers, none, and each
    server alone) belongs to a declared leg."""
    from stutters_mcp_core.prompts import AUTO_ARG_DOC

    problems: list[str] = []
    for sp in (PACK if pack is None else pack).values():
        where = f"suite prompt {sp.name}"
        if not 30 <= len(sp.description) <= 1500:
            problems.append(f"{where}: description length {len(sp.description)} not in 30..1500")
        sig = inspect.signature(sp.fn, eval_str=True)
        for pname, param in sig.parameters.items():
            doc = _field_description(param.annotation)
            if not doc or doc.startswith(AUTO_ARG_DOC):
                problems.append(f"{where}: argument '{pname}' has no Field(description=...)")
        declared = set(sp.tools)
        variants = ["", ",".join(sp.requires), *sp.requires]
        for servers in variants:
            try:
                text = str(sp.fn(**{**_defaults(sig), **sp.example, "servers": servers}))
            except Exception as e:  # noqa: BLE001 - report, don't stop the linter
                problems.append(f"{where}: does not render with servers={servers!r} ({e})")
                continue
            for ref in find_tool_refs(text, PEERS):
                if ref not in declared:
                    problems.append(f"{where}: references '{ref}', which no leg declares")
            if servers in sp.requires:
                others = [p for p in sp.requires if p != servers]
                leaked = [r for r in find_tool_refs(text, others) if r in declared]
                if leaked:
                    problems.append(
                        f"{where}: with servers={servers!r} still calls {', '.join(leaked)} (skipped legs)"
                    )
                if not all(f"{p} leg skipped" in text for p in others):
                    problems.append(f"{where}: with servers={servers!r} does not name every skipped leg")
    return list(dict.fromkeys(problems))


def _defaults(sig: inspect.Signature) -> dict[str, Any]:
    return {
        n: ("example" if p.default is inspect.Parameter.empty else p.default)
        for n, p in sig.parameters.items()
    }


def _field_description(annotation: Any) -> str:
    for extra in getattr(annotation, "__metadata__", ()):
        desc = getattr(extra, "description", None)
        if desc:
            return str(desc)
    return ""


# The pack's prompts register themselves into PACK on import.
from stutters_mcp_core.suite import (  # noqa: E402, F401
    access_review,
    asbuilt_drift,
    change_check,
    decommission_site,
)
