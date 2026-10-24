"""`suite_asbuilt_drift` (roadmap C62): NetBox interfaces, VLANs and cables vs what Catalyst Center sees.

The second slice of C37: the documented intent (NetBox dcim interfaces, VLANs, cables) against the network as
built (Catalyst Center interfaces and its CDP/LLDP-derived physical topology), per site, each difference with
the side to fix. Read-only: the prompt proposes fixes, it never applies them.
"""

from typing import Annotated

from pydantic import Field

from stutters_mcp_core.prompts import tool_ref
from stutters_mcp_core.suite import SERVERS_ARG, SuiteLeg, numbered, plan_legs, suite_prompt

LEGS = (
    SuiteLeg(
        "netbox",
        "the documented intent (NetBox devices, interfaces, VLANs and cables)",
        ("netbox_site_summary", "netbox_list_objects"),
    ),
    SuiteLeg(
        "catc",
        "the network as built (Catalyst Center devices, interfaces, VLANs and CDP/LLDP links)",
        (
            "catc_list_devices",
            "catc_list_device_interfaces",
            "catc_get_physical_topology",
            "catc_get_vlan_topology",
        ),
    ),
)

CHECKS = ("interfaces", "vlans", "cables")

# Short and long IOS interface names, so 'Gi1/0/1' (CDP, NetBox imports) matches 'GigabitEthernet1/0/1'.
INTERFACE_ABBREVIATIONS = (
    "Fa=FastEthernet, Gi=GigabitEthernet, Te=TenGigabitEthernet, Tw=TwoGigabitEthernet, "
    "Fi=FiveGigabitEthernet, Twe=TwentyFiveGigE, Fo=FortyGigabitEthernet, Hu=HundredGigE, "
    "Po=Port-channel, Eth=Ethernet, Lo=Loopback, Vl=Vlan"
)


def parse_checks(checks: str) -> list[str]:
    """The checks to run from a comma-separated `checks` argument (empty = all), in a fixed order."""
    wanted = {c.strip().lower() for c in checks.replace(";", ",").split(",") if c.strip()}
    if not wanted or wanted == {"all"}:
        return list(CHECKS)
    unknown = sorted(wanted - set(CHECKS))
    if unknown:
        raise ValueError(f"checks: unknown {', '.join(unknown)}; use any of {', '.join(CHECKS)} (or empty)")
    return [c for c in CHECKS if c in wanted]


@suite_prompt(
    title="As-built drift: NetBox vs Catalyst Center",
    legs=LEGS,
    example={"site": "branch-1", "checks": "interfaces,cables"},
)
def suite_asbuilt_drift(
    site: Annotated[
        str,
        Field(
            description="Site to compare: the NetBox site (name or slug) and the matching Catalyst Center "
            "site (name or hierarchy, e.g. 'Global/Area/Branch-1'); give both as 'netbox-slug=CatC path' "
            "when they differ"
        ),
    ],
    device: Annotated[
        str, Field(description="Only this device (hostname, serial or management IP); empty = every device")
    ] = "",
    checks: Annotated[
        str,
        Field(
            description="Comma-separated checks: interfaces (existence, enabled, mode, VLANs, description), "
            "vlans (site VLANs vs VLANs on the switches), cables (NetBox cables vs CDP/LLDP links); "
            "empty = all three"
        ),
    ] = "",
    servers: SERVERS_ARG = "",
) -> str:
    """Has the documentation drifted from the wiring? Compares one site's NetBox interfaces, VLANs and cables
    (the intent) with Catalyst Center's interfaces and CDP/LLDP topology (as built) and reports every
    difference with the side to fix: NetBox (the record is stale), the network (the device or patching
    departs from the intent) or a site visit (cannot tell from here). Needs both the NetBox and the
    Catalyst Center server. Read-only: proposes fixes, applies none."""
    wanted = parse_checks(checks)
    plan = plan_legs(LEGS, servers)
    if not plan.legs:
        return plan.header
    if plan.known and plan.skipped:
        have = plan.legs[0]
        return (
            f"{plan.header}\n\n"
            f"A drift check compares the two sides, so with only {have.peer} ({have.product}) connected "
            "there is nothing to compare: say so in one line, name the missing server, and stop. Do not "
            f"report {have.peer}'s data on its own as drift."
        )

    netbox_site, _, catc_site = site.partition("=")
    netbox_site, catc_site = netbox_site.strip(), (catc_site.strip() or netbox_site.strip())
    only = f" (only device {device})" if device else ""
    steps: list[str] = []
    if not plan.known:
        steps.append(
            "If either the `netbox_*` or the `catc_*` tools are missing, there is nothing to compare: give "
            "the skipped-leg line and stop."
        )
    steps.append(
        f"Scope{only}. Call {tool_ref('netbox_site_summary')} with site='{netbox_site}', then "
        f"{tool_ref('netbox_list_objects')}('dcim.devices', filters={{'site': '{netbox_site}'}}, "
        "limit=1000, fields=['id','name','serial','primary_ip','role','status']). Call "
        f"{tool_ref('catc_list_devices')} with site='{catc_site}' (limit=500, response_format='json'). Pair "
        "the devices: by serial, then management IP, then hostname (case-insensitive, domain suffix "
        "dropped). Devices on one side only are not compared here: list them under 'Not compared' "
        "(inventory reconciliation is a separate check). More than 20 paired devices: ask the user to "
        "narrow with `device` before going on."
    )
    if "interfaces" in wanted:
        steps.append(
            "Interfaces, per paired device: "
            f"{tool_ref('netbox_list_objects')}('dcim.interfaces', filters={{'device_id': <id>}}, "
            "limit=1000, fields=['name','type','enabled','mode','untagged_vlan','tagged_vlans','description',"
            f"'cable']) and {tool_ref('catc_list_device_interfaces')}(device, limit=500, "
            f"response_format='json'). Match names after expanding abbreviations "
            f"({INTERFACE_ABBREVIATIONS}). Compare: present on one side only; "
            "enabled vs admin status; mode (access/tagged) vs port mode; untagged/tagged VLAN ids vs the "
            "VLAN Catalyst Center reports; description (info only). Skip virtual interfaces (Vlan, Loopback, "
            "Tunnel, Null, AppGigabitEthernet) unless NetBox records them."
        )
    if "vlans" in wanted:
        steps.append(
            f"VLANs: the site's VLANs from {tool_ref('netbox_site_summary')} (direct and site-scoped groups) "
            f"against {tool_ref('catc_get_vlan_topology')} with no vlan (the names Catalyst Center knows), "
            f"then {tool_ref('catc_get_vlan_topology')}(vlan) for each VLAN that differs, to see which "
            "switches carry it. Report VLANs on the switches with no NetBox record, NetBox VLANs (status "
            "active) carried by none of the site's switches, and name/VID mismatches."
        )
    if "cables" in wanted:
        steps.append(
            f"Cables: {tool_ref('netbox_list_objects')}('dcim.cables', filters={{'site': '{netbox_site}'}}, "
            "limit=1000) against "
            f"{tool_ref('catc_get_physical_topology')}(response_format='json') (pass device=<name> per "
            "device when the site is large). For each NetBox cable between two paired devices, look for a "
            "CDP/LLDP link on the same two ports; for each CDP/LLDP link between paired devices, look for a "
            "cable. Report: cable with no link (and whether Catalyst Center shows the port down), link with "
            "no cable, and a port whose neighbour differs between the two (patching moved or record stale). "
            "Cables to devices Catalyst Center does not manage (hosts, patch panels, circuits) are info only."
        )
    steps.append(
        "Side to fix, for each difference: 'NetBox' when the physical fact is clear and the record is "
        "missing or stale (interface or CDP/LLDP link exists but is not recorded, record for a port that "
        "does not exist); 'network' when NetBox holds the intent and the device departs from it (enabled, "
        "mode, VLANs, a VLAN that should not be there); 'site visit' when the two disagree about which "
        "neighbour is on a port, or a cabled port is down and nothing says why. A recent approved change is "
        "the exception: then the record is what lags, say so."
    )
    steps.append(
        f"Report, NetBox site '{netbox_site}' vs Catalyst Center site '{catc_site}': one table per check, "
        "rows sorted by device then port: device, port, NetBox says, Catalyst Center sees, side to fix, the "
        "fix in a few words (e.g. 'set untagged VLAN 20 on Gi1/0/5 in NetBox', 'shut Gi1/0/7'). Then the "
        "counts per check, 'Not compared' (devices on one side only, ports left out), and any read that "
        "failed or was truncated. No differences in a check: say 'no drift' for it."
    )
    steps.append("This workflow only reads: call no write tool, and propose each fix without applying it.")
    intro = f"As-built drift for site {site}{only}: checks {', '.join(wanted)}."
    return f"{plan.header}\n\n{intro}\n\n{numbered(steps)}"
