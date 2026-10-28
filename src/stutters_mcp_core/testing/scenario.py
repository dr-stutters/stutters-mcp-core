"""Scenario manifests (roadmap C23): check a cross-product scenario against the servers' tool manifests.

A scenario strings prompts and tools from several suite servers together (docs/scenarios/README.md). Its
manifest names the servers, the ordered steps and their hand-offs, and what the lab must provide
(`requires:`). The checker compares it with the committed `<server> tools --json` manifests, so a renamed
or removed tool or prompt fails CI instead of the demo:

    from stutters_mcp_core.testing.scenario import assert_scenarios

    async def test_scenarios() -> None:          # in the repo that owns the opening prompt
        await assert_scenarios("tests/scenarios", manifests="tests/manifests",
                               server=create_server(settings(writes=True, dangerous=True)), prefix="ise")

`server`/`prefix` add this server's live surface (built with all tiers on), so it needs no manifest of
itself. Manifest files are YAML (`*.yaml`, `*.yml`; needs PyYAML, which FastMCP already installs) or JSON:

    scenario: 1
    name: incident                       # lowercase, '-' or '_'; the file and docs/scenarios/<name>.md
    title: Incident, find and contain a host
    epic: 73                             # the stutters-mcp-core epic issue (optional)
    servers: [ise, catc, fmc]            # tool prefixes taking part
    no_manifest: [win]                   # servers that publish no manifest yet: their steps aren't checked
    pending: [ise_incident_response]     # names known to be missing (not shipped yet)
    steps:
      - prompt: ise_incident_response    # or `tool: <name>`; exactly one of the two
        calls: [ise_endpoint_360, catc_find_host, fmc_find_address]   # what the prompt is expected to drive
        leaves: [endpoint_mac]           # hand-off: what this step leaves for later steps
      - tool: fmc_contain_host
        needs: [endpoint_mac]            # must be left by an earlier step
    requires:                            # what the lab must provide (read by the lab repo's preflight)
      recipes: [ise-fmc-identity]
      labs: []
      ram_gb: 32
      cml_licences: 12
      fmc_device_slots: 1
      fmc_eval_days: 7                   # evaluation days the run needs left
      ise_eval_days: 7
      seed_objects: [mcp-test-endpoint]

Checks: every step's server is in `servers` and has a manifest (or is in `no_manifest`); a prompt step
names a prompt and a tool step a tool in that manifest; `calls` exist, and a prompt whose manifest says
it rendered must still reference them; a prompt's `uses` are all in `servers`; `needs` are left by an
earlier step; `pending` entries that now exist are reported (drop them), as is a `no_manifest` server
that has one now. Structural errors (unknown keys, wrong types, a malformed `requires:`) are reported
the same way, so one CI run lists everything.
"""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from stutters_mcp_core.manifest import ToolManifest, build_manifest, read_tool_manifests

SCENARIO_VERSION = 1
SCENARIO_SUFFIXES = (".yaml", ".yml", ".json")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
TOOL_RE = re.compile(r"^[a-z][a-z0-9]*_[a-z0-9_]+$")
PREFIX_RE = re.compile(r"^[a-z][a-z0-9]*$")

_TOP_KEYS = {"scenario", "name", "title", "epic", "servers", "no_manifest", "pending", "steps", "requires"}
_STEP_KEYS = {"prompt", "tool", "calls", "leaves", "needs", "note"}
REQUIRES_LISTS = ("recipes", "labs", "seed_objects")
REQUIRES_COUNTS = ("ram_gb", "cml_licences", "fmc_device_slots", "fmc_eval_days", "ise_eval_days")


@dataclass(frozen=True)
class ScenarioStep:
    name: str
    kind: str  # "prompt" or "tool"
    calls: tuple[str, ...] = ()
    leaves: tuple[str, ...] = ()
    needs: tuple[str, ...] = ()

    @property
    def prefix(self) -> str:
        return self.name.split("_", 1)[0]


@dataclass(frozen=True)
class ScenarioManifest:
    name: str
    servers: tuple[str, ...]
    steps: tuple[ScenarioStep, ...]
    title: str = ""
    epic: int | None = None
    no_manifest: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    requires: dict[str, Any] = field(default_factory=dict)
    source: str = "scenario"

    @property
    def names(self) -> list[str]:
        """Every tool and prompt the scenario names, in step order (steps, then their calls)."""
        out: list[str] = []
        for s in self.steps:
            out += [s.name, *s.calls]
        return list(dict.fromkeys(out))


def _str_list(
    value: Any, where: str, problems: list[str], pattern: re.Pattern[str] | None = None
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        problems.append(f"{where} must be a list of strings")
        return ()
    if pattern is not None:
        for v in value:
            if not pattern.match(v):
                problems.append(f"{where}: {v!r} is not a valid name")
    return tuple(value)


def _requires(value: Any, where: str, problems: list[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.append(f"{where} must be a mapping")
        return {}
    out: dict[str, Any] = {}
    for key, v in value.items():
        if key in REQUIRES_LISTS:
            out[key] = list(_str_list(v, f"{where}.{key}", problems))
        elif key in REQUIRES_COUNTS:
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                problems.append(f"{where}.{key} must be a whole number >= 0 (got {v!r})")
            else:
                out[key] = v
        else:
            known = ", ".join((*REQUIRES_LISTS, *REQUIRES_COUNTS))
            problems.append(f"{where}: unknown key {key!r} (known: {known})")
    return out


def parse_scenario(data: Any, source: str = "scenario") -> ScenarioManifest:
    """A `ScenarioManifest` from parsed YAML/JSON; ValueError listing every structural problem."""
    problems: list[str] = []
    if not isinstance(data, dict):
        raise ValueError(f"{source}: a scenario manifest is a mapping")
    if data.get("scenario") != SCENARIO_VERSION:
        problems.append(f'expected "scenario: {SCENARIO_VERSION}"')
    for key in sorted(set(data) - _TOP_KEYS):
        problems.append(f"unknown key {key!r}")
    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        problems.append("`name` must be lowercase letters, digits, '-' or '_'")
        name = ""
    title = data.get("title", "")
    if not isinstance(title, str):
        problems.append("`title` must be a string")
        title = ""
    epic = data.get("epic")
    if epic is not None and (isinstance(epic, bool) or not isinstance(epic, int)):
        problems.append("`epic` must be an issue number")
        epic = None
    servers = _str_list(data.get("servers"), "`servers`", problems, PREFIX_RE)
    if not servers:
        problems.append("`servers` must name at least one server prefix")
    no_manifest = _str_list(data.get("no_manifest"), "`no_manifest`", problems, PREFIX_RE)
    pending = _str_list(data.get("pending"), "`pending`", problems, TOOL_RE)
    steps: list[ScenarioStep] = []
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        problems.append("`steps` must be a non-empty list")
        raw_steps = []
    for i, raw in enumerate(raw_steps, 1):
        where = f"step {i}"
        if not isinstance(raw, dict):
            problems.append(f"{where} must be a mapping")
            continue
        for key in sorted(set(raw) - _STEP_KEYS):
            problems.append(f"{where}: unknown key {key!r}")
        kinds = [k for k in ("prompt", "tool") if k in raw]
        if len(kinds) != 1:
            problems.append(f"{where}: give exactly one of `prompt` or `tool`")
            continue
        step_name = raw[kinds[0]]
        if not isinstance(step_name, str) or not TOOL_RE.match(step_name):
            problems.append(f"{where}: {kinds[0]} {step_name!r} is not a <prefix>_<name>")
            continue
        steps.append(
            ScenarioStep(
                name=step_name,
                kind=kinds[0],
                calls=_str_list(raw.get("calls"), f"{where}.calls", problems, TOOL_RE),
                leaves=_str_list(raw.get("leaves"), f"{where}.leaves", problems),
                needs=_str_list(raw.get("needs"), f"{where}.needs", problems),
            )
        )
    requires = _requires(data.get("requires"), "`requires`", problems)
    if problems:
        raise ValueError(f"{source}: " + "; ".join(problems))
    return ScenarioManifest(
        name=name,
        title=title,
        epic=epic,
        servers=servers,
        no_manifest=no_manifest,
        pending=pending,
        steps=tuple(steps),
        requires=requires,
        source=source,
    )


def _load_yaml(text: str, source: str) -> Any:
    try:
        yaml = importlib.import_module("yaml")
    except ImportError as e:  # pragma: no cover - FastMCP installs PyYAML
        raise ValueError(f"{source}: YAML scenario manifests need PyYAML (or write it as .json)") from e
    try:
        return yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001 - yaml.YAMLError without importing yaml for typing
        raise ValueError(f"{source}: invalid YAML ({e})") from e


def load_scenario(path: str | Path) -> ScenarioManifest:
    """Read and validate one scenario manifest (`.yaml`, `.yml` or `.json`)."""
    p = Path(path)
    text = p.read_text()
    if p.suffix == ".json":
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ValueError(f"{p}: invalid JSON ({e})") from e
    else:
        data = _load_yaml(text, str(p))
    return parse_scenario(data, str(p))


def scenario_files(*paths: str | Path) -> list[Path]:
    """The scenario files in the given files and/or directories (sorted by name within a directory)."""
    out: list[Path] = []
    for path in paths:
        p = Path(path)
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.suffix in SCENARIO_SUFFIXES and f.is_file())
        else:
            out.append(p)
    return out


def _manifest_map(manifests: Iterable[ToolManifest] | Mapping[str, ToolManifest]) -> dict[str, ToolManifest]:
    if isinstance(manifests, Mapping):
        return dict(manifests)
    out: dict[str, ToolManifest] = {}
    for m in manifests:
        if m.prefix in out:
            raise ValueError(f"two manifests for prefix {m.prefix!r}")
        out[m.prefix] = m
    return out


def scenario_problems(
    scenario: ScenarioManifest, manifests: Iterable[ToolManifest] | Mapping[str, ToolManifest]
) -> list[str]:
    """Everything in `scenario` that the tool manifests contradict, as human-readable strings."""
    by_prefix = _manifest_map(manifests)
    where = f"scenario {scenario.name}"
    servers, unchecked, waived = set(scenario.servers), set(scenario.no_manifest), set(scenario.pending)
    problems: list[str] = []
    for prefix in scenario.servers:
        if prefix in unchecked:
            if prefix in by_prefix:
                problems.append(f"{where}: '{prefix}' is in `no_manifest` but has a manifest now (drop it)")
        elif prefix not in by_prefix:
            problems.append(
                f"{where}: no manifest for server '{prefix}' (commit tests/manifests/{prefix}.json "
                "or list it in `no_manifest`)"
            )
    for prefix in sorted(unchecked - servers):
        problems.append(f"{where}: `no_manifest` names '{prefix}', which is not in `servers`")

    def exists(name: str) -> bool | None:
        """True/False against the manifest; None when the server isn't checked."""
        m = by_prefix.get(name.split("_", 1)[0])
        if m is None or name.split("_", 1)[0] in unchecked:
            return None
        return name in m.names

    left: set[str] = set()
    for i, step in enumerate(scenario.steps, 1):
        at = f"{where} step {i} ({step.name})"
        if step.prefix not in servers:
            problems.append(f"{at}: server '{step.prefix}' is not in `servers`")
        for need in step.needs:
            if need not in left:
                problems.append(f"{at}: needs '{need}', which no earlier step leaves")
        left |= set(step.leaves)
        m = by_prefix.get(step.prefix)
        found = exists(step.name)
        if found is False and step.name not in waived:
            problems.append(f"{at}: the {step.prefix} manifest has no {step.kind} '{step.name}'")
        elif found and m is not None and step.name not in (m.prompts if step.kind == "prompt" else m.tools):
            other = "tool" if step.kind == "prompt" else "prompt"
            problems.append(f"{at}: is a {other} in the {step.prefix} manifest, not a {step.kind}")
        meta = m.prompts.get(step.name) if (m is not None and step.kind == "prompt") else None
        if meta is not None:
            for u in meta.get("uses") or ():
                if u not in servers:
                    problems.append(f"{at}: the prompt uses '{u}', which is not in `servers`")
        for call in step.calls:
            call_prefix = call.split("_", 1)[0]
            if call_prefix not in servers:
                problems.append(f"{at}: calls '{call}' but '{call_prefix}' is not in `servers`")
            if call in waived:
                continue
            if exists(call) is False:
                problems.append(f"{at}: calls '{call}', which the {call_prefix} manifest does not list")
            elif meta is not None and meta.get("rendered") and call not in (meta.get("references") or ()):
                problems.append(f"{at}: calls '{call}', which the prompt no longer references")
    for name in scenario.pending:
        if name not in scenario.names:
            problems.append(f"{where}: `pending` names '{name}', which no step uses")
        elif exists(name):
            problems.append(f"{where}: '{name}' is in `pending` but exists now (drop it)")
    return list(dict.fromkeys(problems))


def scenario_file_problems(
    paths: Iterable[str | Path], manifests: Iterable[ToolManifest] | Mapping[str, ToolManifest]
) -> list[str]:
    """`scenario_problems` for every scenario file in `paths` (files or directories); files that don't
    parse are reported, not raised. Two scenarios with one name are reported too."""
    by_prefix = _manifest_map(manifests)
    files = scenario_files(*paths)
    if not files:
        return [f"no scenario manifests in {', '.join(str(p) for p in paths)}"]
    problems: list[str] = []
    seen: dict[str, Path] = {}
    for f in files:
        try:
            sc = load_scenario(f)
        except (OSError, ValueError) as e:
            problems.append(str(e))
            continue
        if sc.name in seen:
            problems.append(f"{f}: scenario name '{sc.name}' is also used by {seen[sc.name]}")
        seen[sc.name] = f
        problems += scenario_problems(sc, by_prefix)
    return problems


async def assert_scenarios(
    *paths: str | Path,
    manifests: str | Path | Iterable[str | Path] = (),
    server: FastMCP[Any] | None = None,
    prefix: str | None = None,
) -> None:
    """Fail with every problem in the scenario manifests under `paths`, checked against the tool manifests
    in `manifests` (files or directories) plus, when given, `server`'s own surface as `prefix` (build it
    with all tiers on; it replaces a committed manifest of the same prefix)."""
    manifest_paths = [manifests] if isinstance(manifests, (str, Path)) else list(manifests)
    by_prefix = _manifest_map(read_tool_manifests(*manifest_paths))
    if server is not None:
        if not prefix:
            raise ValueError("assert_scenarios: pass prefix= with server=")
        data = await build_manifest(server, server=f"{prefix}-mcp-server", prefix=prefix)
        by_prefix[prefix] = ToolManifest(
            server=data["server"], prefix=prefix, tools=data["tools"], prompts=data["prompts"]
        )
    problems = scenario_file_problems(paths, by_prefix)
    assert not problems, "Scenario manifest problems:\n  " + "\n  ".join(problems)
