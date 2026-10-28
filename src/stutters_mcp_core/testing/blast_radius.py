"""C29: every dangerous Plan counts its blast radius or says why it can't.

`Plan(blast_radius=None)` is never limited by MCP_MAX_BLAST_RADIUS, so a dangerous tool that doesn't
count leaves an operator who set the limit unprotected where a wrong confirm hurts most. This helper
previews every dangerous tool on a mocked appliance (no confirm: nothing is applied) and fails when a
Plan has neither `blast_radius` nor `blast_unknown`:

    from stutters_mcp_core.testing.blast_radius import assert_dangerous_plans_counted

    async def test_dangerous_plans_counted(api):          # api: the respx mock of the appliance
        server = create_server(settings(writes=True, dangerous=True))
        await assert_dangerous_plans_counted(
            server,
            {"ise_delete_nad": {"name": "nad1"},
             "ise_push_trustsec_matrix": [{}, {"nad": "sw1"}]},   # a list: several previews of one tool
            skip={"ise_create_backup": "returns before planning when no repository is set"},
        )

Every tool of the checked tiers must be previewed (a sample for its required arguments) or skipped with
a reason; with strict=False tools that couldn't be driven (no sample, an error, no Plan) are reported
but don't fail the test. A tool that calls `preview_or_apply` several times is checked on every Plan.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Client, FastMCP

from stutters_mcp_core.writes import Plan, _plan_recorders


@dataclass
class BlastRadiusReport:
    counted: dict[str, list[int]] = field(default_factory=dict)  # tool -> blast_radius per Plan
    declared: dict[str, list[str]] = field(default_factory=dict)  # tool -> blast_unknown reason per Plan
    noop: dict[str, list[str]] = field(default_factory=dict)  # tool -> summaries of no-op Plans (C71)
    missing: dict[str, list[str]] = field(default_factory=dict)  # tool -> summaries of Plans with neither
    not_driven: dict[str, str] = field(default_factory=dict)  # tool -> why no Plan was seen
    skipped: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)  # about the server as a whole

    def failures(self, *, strict: bool = True) -> list[str]:
        out = list(self.problems)
        out += [
            f"NOT COUNTED {tool}: Plan {summary!r} has neither blast_radius nor blast_unknown"
            for tool, summaries in sorted(self.missing.items())
            for summary in summaries
        ]
        if strict:
            out += [f"NOT DRIVEN {tool}: {why}" for tool, why in sorted(self.not_driven.items())]
        return out

    def summary(self) -> str:
        lines = self.failures(strict=True)
        lines += [f"counted {t}: {', '.join(map(str, n))}" for t, n in sorted(self.counted.items())]
        lines += [f"declared {t}: {'; '.join(r)}" for t, r in sorted(self.declared.items())]
        lines += [f"no change {t}: {'; '.join(s)}" for t, s in sorted(self.noop.items())]
        lines += [f"skipped {t}: {why}" for t, why in sorted(self.skipped.items())]
        return "\n".join(lines)


def _required(schema: Mapping[str, Any]) -> list[str]:
    return [p for p in schema.get("required", []) if p != "confirm"]


def _cases(sample: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    if sample is None:
        return [{}]
    if isinstance(sample, Mapping):
        return [dict(sample)]
    return [dict(s) for s in sample]


def _skip_reasons(skip: Mapping[str, str] | Iterable[str]) -> dict[str, str]:
    if isinstance(skip, Mapping):
        return {k: str(v) for k, v in skip.items()}
    return {k: "skipped by the test" for k in skip}


def _record(report: BlastRadiusReport, tool: str, plan: Plan) -> None:
    if plan.noop and not plan.blockers:  # nothing is applied, so there is nothing to count (C71)
        report.noop.setdefault(tool, []).append(plan.summary)
    elif plan.blast_radius is not None:
        report.counted.setdefault(tool, []).append(plan.blast_radius)
    elif plan.blast_unknown is not None:
        report.declared.setdefault(tool, []).append(plan.blast_unknown)
    else:
        report.missing.setdefault(tool, []).append(plan.summary)


async def blast_radius_report(
    mcp: FastMCP[Any],
    samples: Mapping[str, Mapping[str, Any] | Sequence[Mapping[str, Any]]] | None = None,
    *,
    skip: Mapping[str, str] | Iterable[str] = (),
    tiers: Iterable[str] = ("dangerous",),
) -> BlastRadiusReport:
    """Preview every tool of `tiers` (no confirm) and classify the Plans each one built."""
    samples = samples or {}
    skipped = _skip_reasons(skip)
    wanted = set(tiers)
    report = BlastRadiusReport()
    registry: dict[str, Any] = getattr(mcp, "_stutters_tiers", {})
    names = sorted(n for n, t in registry.items() if t.value in wanted)
    report.problems += [
        f"sample for {n}, which isn't a registered {'/'.join(sorted(wanted))} tool"
        for n in sorted(set(samples) - set(names))
    ]
    if not names:
        report.problems.append(
            f"no {'/'.join(sorted(wanted))} tools registered: build the server with MCP_ENABLE_WRITES and "
            "MCP_ENABLE_DANGEROUS (and no MCP_DENY_TOOLS / MCP_ALLOW_TOOLS)"
        )
        return report
    async with Client(mcp) as client:
        schemas = {t.name: t.input_schema for t in await client.list_tools()}
        for name in names:
            if name in skipped:
                report.skipped[name] = skipped[name]
                continue
            if name not in schemas:
                report.not_driven[name] = "registered but not listed (hidden from this caller?)"
                continue
            for args in _cases(samples.get(name)):
                missing = [p for p in _required(schemas[name]) if p not in args]
                if missing:
                    report.not_driven[name] = f"no sample for {', '.join(missing)}"
                    break
                if "confirm" in args:
                    report.problems.append(f"sample for {name} carries confirm: previews only")
                    break
                recorder: list[tuple[str, Plan]] = []
                _plan_recorders.append(recorder)
                try:
                    result = await client.call_tool(name, args, raise_on_error=False)
                finally:
                    _plan_recorders.remove(recorder)
                for _tool, plan in recorder:
                    _record(report, name, plan)
                if not recorder:
                    text = " ".join(getattr(c, "text", "") for c in result.content)[:300]
                    report.not_driven[name] = (
                        f"error before a Plan: {text}"
                        if result.is_error
                        else "returned without building a Plan"
                    )
                    break
    return report


async def assert_dangerous_plans_counted(
    mcp: FastMCP[Any],
    samples: Mapping[str, Mapping[str, Any] | Sequence[Mapping[str, Any]]] | None = None,
    *,
    skip: Mapping[str, str] | Iterable[str] = (),
    tiers: Iterable[str] = ("dangerous",),
    strict: bool = True,
) -> BlastRadiusReport:
    """blast_radius_report, failing on any Plan with neither blast_radius nor blast_unknown (and, with
    strict=True, on any tool that couldn't be previewed: give it a sample or skip it with a reason)."""
    report = await blast_radius_report(mcp, samples, skip=skip, tiers=tiers)
    failures = report.failures(strict=strict)
    assert not failures, "Dangerous Plans without a blast radius (C29):\n  " + "\n  ".join(failures)
    return report
