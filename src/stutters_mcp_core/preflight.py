"""`<server> check`: a preflight that runs without an MCP client (C21).

Each server supplies one coroutine that probes its appliance and records what it found; the core runs it for
every instance, adds the configuration checks, classifies connection errors and prints the report:

    async def preflight(settings: Settings, pf: Preflight) -> None:
        async with IseClient.from_settings(settings) as app:
            info = await pf.reach("openapi", app.openapi_version())   # None when it failed
            if info is None:
                return                                             # later checks are reported as skipped
            pf.access(read=True, write=can_write, role=role_name)  # the account's role vs the enabled tiers
            pf.version(info["version"], tested=["3.5"], minimum="3.1")

    def main() -> None:
        run_cli(SPEC, Settings, create_server, preflight=preflight)

Checks, in order: config, reachability (one row per surface), tls, auth, role, version, plus anything the
server adds with ok/warn/fail. A failed check exits 1 (`--strict`: warnings too); `--json` prints the report
as JSON. Credentials never appear: every detail goes through the secret scrubber.
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, TypeVar

from stutters_mcp_core.errors import AuthError, Unreachable, UpstreamError, UpstreamTimeout
from stutters_mcp_core.secrets import scrub_text, scrub_url
from stutters_mcp_core.settings import BaseServerSettings

T = TypeVar("T")

# The checks every server reports, in report order; a server may add its own after these.
STANDARD_CHECKS = ("config", "reachability", "tls", "auth", "role", "version")
_SKIPPED_DETAIL = "not checked: an earlier check failed"


class Outcome(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class CheckResult:
    check: str  # one of STANDARD_CHECKS or a server-specific name
    outcome: Outcome
    detail: str
    surface: str | None = None  # e.g. "ers" for a per-surface reachability row
    hint: str | None = None  # what to change, when the outcome isn't ok
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["outcome"] = self.outcome.value
        return {k: v for k, v in out.items() if v not in (None, {})}


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in re.split(r"[.\-]", text.strip().lstrip("vV")):
        m = re.match(r"\d+", piece)
        if not m:
            break
        parts.append(int(m.group()))
    return tuple(parts)


def _matches(found: tuple[int, ...], prefix: tuple[int, ...]) -> bool:
    return bool(prefix) and found[: len(prefix)] == prefix


class Preflight:
    """Results for one instance. Servers record checks with `reach`, `access`, `version` and ok/warn/fail."""

    def __init__(
        self,
        settings: BaseServerSettings,
        *,
        product: str,
        env_prefix: str,
        instance: str | None = None,
        least_privilege_doc: str = "docs/least-privilege.md",
    ) -> None:
        self.settings = settings
        self.product = product
        self.env_prefix = env_prefix
        self.instance = instance
        self.least_privilege_doc = least_privilege_doc
        self.results: list[CheckResult] = []
        self._reached = False

    # --- recording -----------------------------------------------------------------------

    def _add(
        self,
        check: str,
        outcome: Outcome,
        detail: str,
        *,
        surface: str | None = None,
        hint: str | None = None,
        **data: Any,
    ) -> CheckResult:
        result = CheckResult(
            check,
            outcome,
            scrub_text(detail),
            surface,
            scrub_text(hint) if hint else None,
            data,
        )
        self.results.append(result)
        return result

    def ok(self, check: str, detail: str, *, surface: str | None = None, **data: Any) -> CheckResult:
        return self._add(check, Outcome.OK, detail, surface=surface, **data)

    def warn(
        self, check: str, detail: str, *, hint: str | None = None, surface: str | None = None, **data: Any
    ) -> CheckResult:
        return self._add(check, Outcome.WARN, detail, surface=surface, hint=hint, **data)

    def fail(
        self, check: str, detail: str, *, hint: str | None = None, surface: str | None = None, **data: Any
    ) -> CheckResult:
        return self._add(check, Outcome.FAIL, detail, surface=surface, hint=hint, **data)

    def skip(self, check: str, detail: str, *, surface: str | None = None) -> CheckResult:
        return self._add(check, Outcome.SKIP, detail, surface=surface)

    def has(self, check: str) -> bool:
        return any(r.check == check for r in self.results)

    @property
    def failed(self) -> bool:
        return any(r.outcome is Outcome.FAIL for r in self.results)

    # --- probes --------------------------------------------------------------------------

    def record_error(self, e: BaseException, *, surface: str | None = None, check: str | None = None) -> None:
        """Classify an exception from a probe: TLS trust, reachability, credentials (401) or role (403)."""
        p = self.env_prefix
        text = str(e) or type(e).__name__
        tls_text = "CERTIFICATE" in text.upper() or "SSL" in text.upper()
        if isinstance(e, ssl.SSLError) or (isinstance(e, Unreachable) and tls_text):
            self.fail(
                "tls",
                text,
                surface=surface,
                hint=f"Set {p}CA_BUNDLE to the PEM of the CA that signed the {self.product} certificate "
                f"(or {p}VERIFY_SSL=false in a lab only).",
            )
        elif isinstance(e, Unreachable | UpstreamTimeout | asyncio.TimeoutError | TimeoutError | OSError):
            self.fail(
                "reachability",
                text,
                surface=surface,
                hint=f"Check {p}URL (scheme, host, port), DNS and any firewall between this host and "
                f"{self.product}.",
            )
        elif isinstance(e, AuthError) and e.status_code == 403:
            self.fail(
                check or "role",
                text,
                surface=surface,
                hint="The account logs in but its role doesn't allow this API: see "
                f"{self.least_privilege_doc}.",
            )
        elif isinstance(e, AuthError):
            self.fail(
                check or "auth",
                text,
                surface=surface,
                hint=f"Check {p}USERNAME and {p}PASSWORD (or {p}PASSWORD_FILE / {p}PASSWORD_REF), and that "
                "the account is enabled for API access.",
            )
        else:
            self.fail(check or "reachability", text, surface=surface)

    async def reach(
        self,
        surface: str,
        call: Awaitable[T],
        *,
        detail: Callable[[T], str] | str | None = None,
        seconds: float | None = None,
        check: str = "reachability",
    ) -> T | None:
        """Await one probe of `surface`. On success records `check` ok (with the time taken) and returns the
        value; on failure records the classified error and returns None."""
        started = time.monotonic()
        try:
            value = await (asyncio.wait_for(call, seconds) if seconds else call)
        except (UpstreamError, OSError, TimeoutError) as e:
            self.record_error(e, surface=surface, check=None if check == "reachability" else check)
            return None
        ms = round((time.monotonic() - started) * 1000)
        self._reached = True
        text = detail(value) if callable(detail) else detail
        self.ok(check, f"{text or 'answered'} ({ms} ms)", surface=surface, ms=ms)
        return value

    # --- verdicts ------------------------------------------------------------------------

    def access(
        self,
        *,
        read: bool | None = True,
        write: bool | None,
        dangerous: bool | None = None,
        role: str | None = None,
        detail: str | None = None,
    ) -> CheckResult:
        """Compare what the account may do (True / False / None = unknown) with the tiers this server enables.
        Fails when an enabled tier is not allowed, warns when it can't tell or when the account can do more
        than the enabled tiers need (least privilege)."""
        s = self.settings
        who = f"role {role!r}" if role else "the account"
        wanted = {"read": True, "write": s.mcp_enable_writes, "dangerous": s.mcp_enable_dangerous}
        can = {"read": read, "write": write, "dangerous": write if dangerous is None else dangerous}
        flag = {"write": "MCP_ENABLE_WRITES", "dangerous": "MCP_ENABLE_DANGEROUS"}
        data: dict[str, Any] = {
            "role": role,
            "allowed": {k: v for k, v in can.items() if v is not None},
            "enabled": [k for k, on in wanted.items() if on],
        }
        prefix = f"{detail}; " if detail else ""
        missing = [t for t, on in wanted.items() if on and can[t] is False]
        if missing:
            return self.fail(
                "role",
                f"{prefix}{who} cannot use the {', '.join(missing)} tier(s) this server enables",
                hint=f"Give the account a role that allows them ({self.least_privilege_doc}), or turn them "
                f"off ({', '.join(flag[t] + '=false' for t in missing if t in flag) or 'read is required'}).",
                **data,
            )
        unknown = [t for t, on in wanted.items() if on and can[t] is None]
        if unknown:
            return self.warn(
                "role",
                f"{prefix}could not tell whether {who} allows the {', '.join(unknown)} tier(s)",
                hint=f"Compare the account's role with {self.least_privilege_doc}; tools fail with 403 "
                "if it doesn't.",
                **data,
            )
        extra = [t for t in ("write", "dangerous") if not wanted[t] and can[t] is True]
        if extra:
            return self.warn(
                "role",
                f"{prefix}{who} can also {' and '.join(extra)} but this server only needs "
                f"{'/'.join(t for t, on in wanted.items() if on)}",
                hint=f"A shared host should use a read-only account until writes are enabled "
                f"({self.least_privilege_doc}).",
                **data,
            )
        return self.ok(
            "role", f"{prefix}{who} allows the enabled tier(s): {', '.join(data['enabled'])}", **data
        )

    def version(
        self,
        found: str | None,
        *,
        tested: Sequence[str],
        minimum: str | None = None,
        detail: str | None = None,
    ) -> CheckResult:
        """`found` against the supported matrix: ok when it starts with a `tested` release ("3.5" matches
        3.5.0.527), warn when newer than `minimum` but untested, fail when older than `minimum`."""
        data: dict[str, Any] = {"found": found, "tested": list(tested), "minimum": minimum}
        suffix = f" ({detail})" if detail else ""
        if not found or not _version_tuple(found):
            return self.warn("version", f"{self.product} did not report a version{suffix}", hint=None, **data)
        have = _version_tuple(found)
        if any(_matches(have, _version_tuple(t)) for t in tested):
            return self.ok("version", f"{self.product} {found}: tested{suffix}", **data)
        if minimum and have < _version_tuple(minimum):
            return self.fail(
                "version",
                f"{self.product} {found} is older than the supported minimum {minimum}{suffix}",
                hint=f"Upgrade {self.product}, or expect tools to fail on missing APIs.",
                **data,
            )
        return self.warn(
            "version",
            f"{self.product} {found} is not in the tested matrix ({', '.join(tested)}){suffix}",
            hint="It should work; report anything that doesn't.",
            **data,
        )

    # --- framework -----------------------------------------------------------------------

    def _config_checks(self) -> None:
        s = self.settings
        p = self.env_prefix
        tiers = (
            ["read"]
            + (["write"] if s.mcp_enable_writes else [])
            + (["dangerous"] if s.mcp_enable_dangerous else [])
        )
        self.ok(
            "config",
            f"{scrub_url(s.url)}; tiers enabled: {', '.join(tiers)}"
            + (" (read-only)" if tiers == ["read"] else ""),
            url=scrub_url(s.url),
            tiers=tiers,
        )
        if s.url.startswith("http://"):
            self.warn(
                "config",
                "the URL uses plain HTTP: credentials cross the network unencrypted",
                hint=f"Use https:// in {p}URL.",
            )

    def _finish(self) -> None:
        s = self.settings
        p = self.env_prefix
        if not self.has("tls"):
            if not self._reached:
                self.skip("tls", _SKIPPED_DETAIL)
            elif s.url.startswith("http://"):
                self.warn("tls", "not used (plain HTTP)", hint=f"Use https:// in {p}URL.")
            elif not s.verify_ssl and s.ca_bundle is None:
                self.warn(
                    "tls",
                    f"certificate NOT verified ({p}VERIFY_SSL=false)",
                    hint=f"Set {p}CA_BUNDLE to the CA's PEM and {p}VERIFY_SSL=true outside a lab.",
                )
            else:
                where = f"CA bundle {s.ca_bundle}" if s.ca_bundle else "the system trust store"
                self.ok("tls", f"certificate verified against {where}")
        if not self.has("auth"):
            if self._reached:  # probes passed to reach() are authenticated calls
                self.ok("auth", "credentials accepted")
            else:
                self.skip("auth", _SKIPPED_DETAIL)
        for check in ("role", "version"):
            if not self.has(check):
                self.skip(check, _SKIPPED_DETAIL if self.failed else "not reported by this server")

    def ordered(self) -> list[CheckResult]:
        rank = {c: i for i, c in enumerate(STANDARD_CHECKS)}
        return sorted(self.results, key=lambda r: rank.get(r.check, len(rank)))

    def report(self) -> dict[str, Any]:
        results = self.ordered()
        return {
            "instance": self.instance,
            "url": scrub_url(self.settings.url),
            "ok": not self.failed,
            "counts": {o.value: sum(r.outcome is o for r in results) for o in Outcome},
            "checks": [r.as_dict() for r in results],
        }


PreflightFn = Callable[[Any, Preflight], Awaitable[None]]


async def run_instance(
    settings: BaseServerSettings,
    preflight: PreflightFn,
    *,
    product: str,
    env_prefix: str,
    instance: str | None,
    seconds: float = 120.0,
) -> Preflight:
    pf = Preflight(settings, product=product, env_prefix=env_prefix, instance=instance)
    pf._config_checks()
    try:
        await asyncio.wait_for(preflight(settings, pf), seconds)
    except TimeoutError:
        pf.fail("reachability", f"the checks did not finish within {seconds:.0f} s")
    except (UpstreamError, OSError) as e:  # a probe the server didn't wrap in pf.reach
        pf.record_error(e)
    except Exception as e:  # noqa: BLE001 - report, don't crash: this is a diagnostic command
        pf.fail("internal", f"{type(e).__name__}: {e}")
    pf._finish()
    return pf


async def run_preflight(
    settings: BaseServerSettings,
    preflight: PreflightFn,
    *,
    server: str,
    product: str,
    env_prefix: str,
    version: str | None = None,
    instances: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run `preflight` against every configured instance (or only `instances`); returns the JSON report."""
    from stutters_mcp_core import __version__ as core_version
    from stutters_mcp_core.targets import target_settings

    names = settings.target_names()
    multi = len(names) > 1
    unknown = [n for n in instances or [] if n not in names]
    if unknown:
        raise ValueError(f"unknown instance(s) {', '.join(unknown)}; configured: {', '.join(names)}")
    selected = list(instances) if instances else names
    reports = []
    for name in selected:
        cfg = settings if name == names[0] else target_settings(settings, name)
        # Hints name the instance's own variables (ISE_DR_URL for instance "dr").
        prefix = env_prefix if name == names[0] else f"{env_prefix}{name.upper()}_"
        pf = await run_instance(
            cfg, preflight, product=product, env_prefix=prefix, instance=name if multi else None
        )
        reports.append(pf.report())
    return {
        "server": server,
        "version": version,
        "core_version": core_version,
        "ok": all(r["ok"] for r in reports),
        "instances": reports,
    }


_LABEL = {"ok": "OK", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}


def render_text(report: dict[str, Any]) -> str:
    name = " ".join(filter(None, [report["server"], report.get("version")]))
    lines = [f"{name} check (core {report['core_version']})"]
    for inst in report["instances"]:
        where = f"instance {inst['instance']}: " if inst["instance"] else ""
        lines.append(f"\n{where}{inst['url']}")
        for c in inst["checks"]:
            name = f"{c['check']}[{c['surface']}]" if c.get("surface") else c["check"]
            lines.append(f"  {_LABEL[c['outcome']]:<5} {name:<22} {c['detail']}")
            if c.get("hint"):
                lines.append(f"  {'':<5} {'':<22} -> {c['hint']}")
    totals = {o: sum(i["counts"][o] for i in report["instances"]) for o in ("fail", "warn")}
    verdict = "FAILED" if not report["ok"] else "passed"
    lines.append(f"\n{verdict}: {totals['fail']} failed, {totals['warn']} warning(s)")
    return "\n".join(lines) + "\n"


def exit_code(report: dict[str, Any], *, strict: bool = False) -> int:
    if not report["ok"]:
        return 1
    if strict and any(i["counts"]["warn"] for i in report["instances"]):
        return 1
    return 0


def dumps(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, default=str) + "\n"
