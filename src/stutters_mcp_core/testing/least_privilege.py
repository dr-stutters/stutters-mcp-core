"""Least-privilege contract tests (roadmap G3): run a server's read tier as a read-only identity.

Two identities are checked at once:
- the **appliance account**: `<P>RO_USERNAME` / `<P>RO_PASSWORD` (a dedicated user with the custom read-only
  role from docs/least-privilege.md), used instead of the admin credentials;
- the **MCP caller**: role `ro` (MCP_ROLES), so only read tools are visible and callable.

    from stutters_mcp_core.testing.least_privilege import readonly_live_settings, assert_read_tier_works

    @pytest.mark.live
    async def test_read_tier_as_readonly_user() -> None:
        settings = readonly_live_settings(Settings, "ISE_")          # skips when ISE_RO_USERNAME is unset
        report = await assert_read_tier_works(create_server(settings), SAMPLES, skip={"ise_api_call"})

`SAMPLES` maps tool names to arguments ({"ise_get_nad": {"name": "mcp-test-nad"}}). Tools without a sample
are called with no arguments when they have no required parameters, and skipped otherwise. A tool fails the
test when the appliance denies it (AuthError, 401/403): either the role lacks a permission (fix the role and
the guide) or the tool needs more than the read tier should. Other errors (404 for a sample that doesn't
exist, timeouts) are reported but don't fail, unless `strict=True`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fastmcp import Client, FastMCP

RO_CALLER = "least-privilege-check"
DENIED_MARKERS = ("denied access", "(401)", "(403)", "Forbidden", "Unauthorized", "not authorized")


@dataclass
class ReadTierReport:
    ok: list[str] = field(default_factory=list)
    denied: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    hidden_writes: list[str] = field(default_factory=list)  # write tools an `ro` caller can't see (expected)

    def summary(self) -> str:
        counts = (len(self.ok), len(self.denied), len(self.errors), len(self.skipped))
        lines = ["ok {}, denied {}, errors {}, skipped {}".format(*counts)]
        lines += [f"  DENIED {k}: {v}" for k, v in sorted(self.denied.items())]
        lines += [f"  error  {k}: {v}" for k, v in sorted(self.errors.items())]
        lines += [f"  skip   {k}: {v}" for k, v in sorted(self.skipped.items())]
        return "\n".join(lines)


def readonly_live_settings(settings_cls: type[Any], env_prefix: str, **overrides: Any) -> Any:
    """Live settings using the read-only appliance account `<P>RO_USERNAME` / `<P>RO_PASSWORD`
    (pytest.skip when unset or the appliance is unreachable). MCP roles are switched on with the check's
    caller as `ro`, and writes enabled so the test also proves write tools are hidden from `ro`."""
    import pytest

    from stutters_mcp_core.testing import live_settings

    user = os.environ.get(f"{env_prefix}RO_USERNAME")
    password = os.environ.get(f"{env_prefix}RO_PASSWORD")
    if not user or not password:
        try:
            from dotenv import dotenv_values

            env = dotenv_values(".env")
            user = user or env.get(f"{env_prefix}RO_USERNAME")
            password = password or env.get(f"{env_prefix}RO_PASSWORD")
        except ImportError:  # pragma: no cover
            pass
    if not user or not password:
        pytest.skip(f"{env_prefix}RO_USERNAME / {env_prefix}RO_PASSWORD not configured (read-only account)")
    return live_settings(
        settings_cls,
        env_prefix,
        username=user,
        password=password,
        mcp_enable_writes=True,
        mcp_roles=f"{RO_CALLER}:ro",
        **overrides,
    )


def _required(schema: Mapping[str, Any]) -> list[str]:
    return list(schema.get("required", []))


async def read_tier_report(
    mcp: FastMCP[Any],
    samples: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    skip: Iterable[str] = (),
    caller: str = RO_CALLER,
) -> ReadTierReport:
    """Call every tool visible to `caller` (role `ro` when MCP_ROLES maps it) and classify the outcome."""
    from stutters_mcp_core.testing import as_caller

    samples = samples or {}
    skip_set = set(skip)
    report = ReadTierReport()
    tiers: dict[str, Any] = getattr(mcp, "_stutters_tiers", {})
    async with Client(mcp) as client:
        with as_caller(caller):
            visible = await client.list_tools()
            names = {t.name for t in visible}
            report.hidden_writes = sorted(n for n, t in tiers.items() if t.value != "read" and n not in names)
            for tool in sorted(visible, key=lambda t: t.name):
                tier = tiers.get(tool.name)
                if tier is not None and tier.value != "read":
                    report.denied[tool.name] = f"{tier.value} tool visible to a read-only caller"
                    continue
                if tool.name in skip_set:
                    report.skipped[tool.name] = "skipped by the test"
                    continue
                args = dict(samples.get(tool.name, {}))
                missing = [p for p in _required(tool.input_schema) if p not in args]
                if missing:
                    report.skipped[tool.name] = f"no sample for {', '.join(missing)}"
                    continue
                result = await client.call_tool(tool.name, args, raise_on_error=False)
                if not result.is_error:
                    report.ok.append(tool.name)
                    continue
                text = " ".join(getattr(c, "text", "") for c in result.content)[:300]
                if any(m.lower() in text.lower() for m in DENIED_MARKERS):
                    report.denied[tool.name] = text
                else:
                    report.errors[tool.name] = text
    return report


async def assert_read_tier_works(
    mcp: FastMCP[Any],
    samples: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    skip: Iterable[str] = (),
    strict: bool = False,
) -> ReadTierReport:
    """read_tier_report, failing on any denial (and on any error with strict=True)."""
    report = await read_tier_report(mcp, samples, skip=skip)
    failed = bool(report.denied) or (strict and bool(report.errors))
    assert not failed, "Read tier as a read-only identity:\n" + report.summary()
    return report
