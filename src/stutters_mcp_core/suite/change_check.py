"""`suite_change_check` (roadmap C53, C78): pre/post change check across products with the snapshot contract.

Post phase passes the change's own configuration objects to each compare as `expected` (snapshot schema 2), so
a configuration object that changed outside the change is reported apart from the change itself."""

from typing import Annotated

from pydantic import Field

from stutters_mcp_core.prompts import tool_ref
from stutters_mcp_core.suite import SERVERS_ARG, SuiteLeg, numbered, plan_legs, suite_prompt

LEGS = (
    SuiteLeg(
        "catsdwan",
        "the SD-WAN overlay (BFD sessions, OMP peers, app-route SLA, control connections, alarms)",
        ("catsdwan_snapshot", "catsdwan_compare_snapshot"),
    ),
    SuiteLeg(
        "ise",
        "ISE (authentication and failure rates per NAD, node health, pxGrid clients)",
        ("ise_snapshot", "ise_compare_snapshot"),
    ),
    SuiteLeg(
        "catc",
        "Catalyst Center (device and site health, compliance, open issues)",
        ("catc_snapshot", "catc_compare_snapshot"),
    ),
    SuiteLeg(
        "fmc",
        "the firewalls (deployment state, HA, interface and VPN status)",
        ("fmc_snapshot", "fmc_compare_snapshot"),
    ),
)


@suite_prompt(
    title="Change check across products (pre/post)",
    legs=LEGS,
    example={"site": "br1", "phase": "post", "change": "banner change on br1", "expected": "mcp-banner"},
)
def suite_change_check(
    site: Annotated[
        str,
        Field(
            description="Site the change touches, as the products name it (SD-WAN site id or name, "
            "Catalyst Center site, ISE location or NAD group, FMC device or group); empty = everything each "
            "server sees"
        ),
    ] = "",
    phase: Annotated[
        str, Field(description="'pre' before the change (baseline snapshots) or 'post' after it (compare)")
    ] = "pre",
    change: Annotated[
        str, Field(description="The planned change in a few words, so expected effects are told apart")
    ] = "",
    before: Annotated[
        str,
        Field(
            description="Post phase only: the pre-phase snapshot blobs (JSON, one per server) when they are "
            "not already in this conversation"
        ),
    ] = "",
    servers: SERVERS_ARG = "",
    expected: Annotated[
        str,
        Field(
            description="Post phase: the configuration objects the change itself touches, comma-separated "
            "(access rule, policy set or authorization rule, template, configuration group names, or the "
            "snapshot's config item keys); passed to each compare as `expected`, so whatever else changed "
            "is reported as unexpected. Empty = name them from `change` where you can"
        ),
    ] = "",
) -> str:
    """Did anything else break? Snapshot every connected product before a change (phase=pre), then compare
    after it (phase=post): SD-WAN overlay, ISE authentication, Catalyst Center health and the firewalls, one
    report of what changed by severity, expected effects apart, and configuration objects that changed
    outside the change (snapshot config sections). Skips the products whose server is not connected and says
    so. Read-only: the snapshots are kept in the conversation, never on a server."""
    phase = phase.strip().lower()
    if phase not in ("pre", "post"):
        raise ValueError("phase must be 'pre' or 'post'")
    plan = plan_legs(LEGS, servers)
    where = f'site="{site}"' if site else 'site="" (everything each server sees)'
    about = f" ({change})" if change else ""
    if not plan.legs:
        return plan.header

    if phase == "pre":
        steps = [
            f"Call {tool_ref(f'{leg.peer}_snapshot')} with {where}. Keep the blob it returns exactly as "
            f"returned: it is the {leg.peer} 'before' for the post phase."
            for leg in plan.legs
        ]
        steps.append(
            "Summarise the baseline in two or three lines per product, listing anything already unhealthy "
            "(the change did not cause it). Do not paste the blobs into the summary."
        )
        steps.append(
            "Tell the user to make the change, then run suite_change_check with phase=post in this "
            "conversation (or pass the blobs as `before` in a new one)."
        )
        intro = f"Change check, phase pre: baseline snapshots before the change{about}."
        return f"{plan.header}\n\n{intro}\n\n{numbered(steps)}"

    source = (
        "the pre-phase blobs given at the end of this message (match each by its `prefix`)"
        if before.strip()
        else "the blob that server's snapshot tool returned in the pre phase of this conversation"
    )
    names = ", ".join(e.strip() for e in expected.replace("\n", ",").split(",") if e.strip())
    objects = (
        f'expected="{names}"'
        if names
        else "expected = the configuration objects the planned change itself touches (rule, policy set, "
        "template or configuration group names), when you can name them from the change, else no `expected`"
    )
    steps = [
        f"Call {tool_ref(f'{leg.peer}_compare_snapshot')} with before = "
        f"{source.replace('that server', leg.peer)}, unchanged, {objects}, and no `after` (it takes the "
        f"after snapshot now). No {leg.peer} blob? Say so, call {tool_ref(f'{leg.peer}_snapshot')} with "
        f"{where} as a new baseline, and report {leg.peer} as not compared."
        for leg in plan.legs
    ]
    steps.append(
        "Merge the results into one table, most severe first: severity, product, section, item, what "
        f"changed. Put changes that are effects of the planned change{about}, and each result's `expected` "
        "entries, in a separate 'Expected' list. Changes marked `config: true` left in `changes` are "
        "configuration that changed outside the change: list them as 'Changed outside the change'."
    )
    steps.append(
        "End with the verdict: 'Nothing else changed' when no unexpected major or minor change is left, else "
        "the unexpected changes with the tool to look closer (e.g. BFD down -> the SD-WAN tunnel tools). "
        "Name every leg that was skipped, not compared, `integrity: altered`, truncated or unreadable, and "
        "every `expected_unchanged` object (the change may not have been made)."
    )
    steps.append("This workflow only reads: call no write tool.")
    intro = f"Change check, phase post: what changed since the pre snapshots{about}."
    body = f"{plan.header}\n\n{intro}\n\n{numbered(steps)}"
    if before.strip():
        body += f"\n\nPre-phase blobs:\n{before.strip()}"
    return body
