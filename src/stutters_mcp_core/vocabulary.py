"""Argument vocabulary lint (roadmap C68): one name per idea across every server's tools.

A model that learned `since_hours` on one server should not have to guess `hours`, `lookback_hours` or
`window_hours` on the next. The suite vocabulary is in docs/VOCABULARY.md; this module checks a server's
tool parameters against it and reports **warnings**, never failures, so CI stays green while servers
rename on their own schedule (each rename keeps the old name as a deprecated alias).

    from stutters_mcp_core.testing import warn_vocabulary

    async def test_vocabulary():                 # warnings in the pytest summary, the test still passes
        await warn_vocabulary(create_server(settings(writes=True, dangerous=True)), "fmc",
                              allow={"fmc_export_device_config.format"})   # a file format, not output shape

Two kinds of finding:
- **alias**: a parameter uses a known synonym of a vocabulary name (`hours` -> `since_hours`,
  `timeout` -> `timeout_seconds`, `format` -> `response_format`, `device_id` -> `device`, ...).
- **type**: a parameter uses a vocabulary name with the wrong shape (`wait` that is not a boolean,
  `since_hours` that is not a number, `response_format` without markdown/json).

A parameter whose description starts with "Deprecated" is a kept alias and is not reported. `allow`
silences a finding that is a deliberate different meaning: "tool.param", a bare "param" (every tool), or a
glob over either ("cml_*.timeout").
"""

from __future__ import annotations

import fnmatch
import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = [
    "ALIASES",
    "TERMS",
    "Term",
    "VocabularyFinding",
    "VocabularyWarning",
    "check_tool",
    "vocabulary_findings",
    "vocabulary_problems",
    "warn_vocabulary",
]


class VocabularyWarning(UserWarning):
    """A tool parameter off the suite vocabulary (docs/VOCABULARY.md). Reported, never fatal."""


@dataclass(frozen=True)
class Term:
    """One vocabulary name: what it means and the JSON Schema types it may have."""

    name: str
    meaning: str
    types: frozenset[str]
    enum_must_include: frozenset[str] = frozenset()


def _term(name: str, meaning: str, *types: str, enum: Iterable[str] = ()) -> Term:
    return Term(name, meaning, frozenset(types), frozenset(enum))


_NUMBER = ("integer", "number")

# The suite vocabulary. Order is the order of docs/VOCABULARY.md.
TERMS: dict[str, Term] = {
    t.name: t
    for t in (
        _term("since_hours", "look back this many hours from now", *_NUMBER),
        _term("since_minutes", "look back this many minutes (when hours are too coarse)", *_NUMBER),
        _term("since_days", "look back this many days (when hours are too fine)", *_NUMBER),
        _term("wait", "block until the job/task finishes (true) or return its id at once (false)", "boolean"),
        _term("timeout_seconds", "how long a wait may take before giving up, in seconds", *_NUMBER),
        _term(
            "response_format",
            "output shape: markdown for people, json for the complete data",
            "string",
            enum=("markdown", "json"),
        ),
        _term("limit", "page size: at most this many items", "integer"),
        _term("offset", "items to skip (offset counts items, not pages)", "integer"),
        _term("detail", "how much to return per item (summary/full, or a section name)", "string", "boolean"),
        _term("instance", "which configured appliance (multi-instance servers, ADR-004)", "string"),
        _term("device", "a device by host name, IP, serial or id (the tool resolves it)", "string"),
        _term("confirm", "the preview's single-use confirm token (ADR-002)", "string"),
    )
}

# Known synonyms -> the vocabulary name to use instead.
ALIASES: dict[str, str] = {
    "hours": "since_hours",
    "last_hours": "since_hours",
    "lookback_hours": "since_hours",
    "window_hours": "since_hours",
    "minutes": "since_minutes",
    "last_minutes": "since_minutes",
    "lookback_minutes": "since_minutes",
    "days": "since_days",
    "last_days": "since_days",
    "lookback_days": "since_days",
    "timeout": "timeout_seconds",
    "wait_timeout": "timeout_seconds",
    "wait_seconds": "timeout_seconds",
    "max_wait": "timeout_seconds",
    "max_wait_seconds": "timeout_seconds",
    "timeout_minutes": "timeout_seconds",
    "wait_for": "wait",
    "wait_for_completion": "wait",
    "blocking": "wait",
    "format": "response_format",
    "output_format": "response_format",
    "fmt": "response_format",
    "verbose": "detail",
    "max_results": "limit",
    "max_items": "limit",
    "page_size": "limit",
    "page": "offset",
    "skip": "offset",
    "device_id": "device",
    "confirm_token": "confirm",
    "confirmation": "confirm",
}


@dataclass(frozen=True)
class VocabularyFinding:
    tool: str
    param: str
    kind: str  # "alias" | "type"
    use: str  # the vocabulary name to use
    message: str

    def __str__(self) -> str:
        return f"{self.tool}: {self.message}"


def _types(schema: Mapping[str, Any]) -> set[str]:
    """JSON Schema types of a parameter, null dropped (pydantic writes Optional as anyOf [..., null])."""
    out: set[str] = set()
    t = schema.get("type")
    if isinstance(t, str):
        out.add(t)
    elif isinstance(t, list):
        out.update(x for x in t if isinstance(x, str))
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(key) or []:
            if isinstance(sub, Mapping):
                out |= _types(sub)
    if "enum" in schema and not out:
        out.add("string")
    out.discard("null")
    return out


def _enum(schema: Mapping[str, Any]) -> set[str]:
    out = {str(v) for v in schema.get("enum") or []}
    if "const" in schema:
        out.add(str(schema["const"]))
    for key in ("anyOf", "oneOf"):
        for sub in schema.get(key) or []:
            if isinstance(sub, Mapping):
                out |= _enum(sub)
    return out


def _allowed(tool: str, param: str, allow: Iterable[str]) -> bool:
    qualified = f"{tool}.{param}"
    for pattern in allow:
        target = qualified if "." in pattern else param
        if fnmatch.fnmatchcase(target, pattern):
            return True
    return False


def check_tool(
    tool: str, input_schema: Mapping[str, Any] | None, *, allow: Iterable[str] = ()
) -> list[VocabularyFinding]:
    """The vocabulary findings for one tool's input schema."""
    findings: list[VocabularyFinding] = []
    allow = tuple(allow)
    props: Mapping[str, Any] = (input_schema or {}).get("properties") or {}
    for param, schema in props.items():
        if not isinstance(schema, Mapping):
            continue
        if str(schema.get("description") or "").lstrip().lower().startswith("deprecated"):
            continue
        if _allowed(tool, param, allow):
            continue
        if param in ALIASES:
            use = ALIASES[param]
            term = TERMS[use]
            findings.append(
                VocabularyFinding(
                    tool, param, "alias", use, f"parameter '{param}': use '{use}' ({term.meaning})"
                )
            )
            continue
        term_or_none = TERMS.get(param)
        if term_or_none is None:
            continue
        term = term_or_none
        types = _types(schema)
        if types and not types & term.types:
            want = " or ".join(sorted(term.types))
            hint = " (for a duration use 'timeout_seconds')" if param == "wait" else ""
            findings.append(
                VocabularyFinding(
                    tool,
                    param,
                    "type",
                    param,
                    f"parameter '{param}' is {'/'.join(sorted(types))}, the vocabulary says {want}{hint}",
                )
            )
        elif term.enum_must_include:
            values = _enum(schema)
            missing = term.enum_must_include - values
            if values and missing:
                findings.append(
                    VocabularyFinding(
                        tool,
                        param,
                        "type",
                        param,
                        f"parameter '{param}' choices {sorted(values)} lack {sorted(missing)}",
                    )
                )
    return findings


def vocabulary_findings(
    tools: Iterable[tuple[str, Mapping[str, Any] | None]], *, allow: Iterable[str] = ()
) -> list[VocabularyFinding]:
    """Findings for (tool name, input schema) pairs, sorted by tool then parameter."""
    allow = tuple(allow)
    out = [f for name, schema in tools for f in check_tool(name, schema, allow=allow)]
    return sorted(out, key=lambda f: (f.tool, f.param))


async def vocabulary_problems(mcp: FastMCP[Any], prefix: str, *, allow: Iterable[str] = ()) -> list[str]:
    """Vocabulary findings for the server's tools (build it with all tiers on), as strings. Tools without
    `prefix` (none, normally) are skipped."""
    from fastmcp import Client

    async with Client(mcp) as client:
        tools = await client.list_tools()
    pairs = [(t.name, t.input_schema) for t in tools if t.name.startswith(prefix + "_")]
    return [str(f) for f in vocabulary_findings(pairs, allow=allow)]


async def warn_vocabulary(mcp: FastMCP[Any], prefix: str, *, allow: Iterable[str] = ()) -> list[str]:
    """`vocabulary_problems` emitted as VocabularyWarning (shown in pytest's warnings summary; the test
    passes). Returns the findings so a server can also assert a budget (`len(...) <= N`) if it wants."""
    problems = await vocabulary_problems(mcp, prefix, allow=allow)
    for p in problems:
        warnings.warn(p + " (docs/VOCABULARY.md)", VocabularyWarning, stacklevel=2)
    return problems
