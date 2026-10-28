"""`suite_decommission_site` (roadmap C52): remove a site everywhere, previewed and confirmed per product.

Decommissions leave orphans: WAN edges and config-group associations in SD-WAN Manager, NADs and a location
group in ISE, the site, its devices, fabric and pool reservations in Catalyst Center, network objects in FMC,
a DHCP scope and DNS records on Windows. The prompt inventories everything tied to the site (read-only),
previews the removal product by product, removes each product only after the user approves that product's
preview, and ends with a verify pass that must come back empty. Dangerous tier: it deletes devices and sites.

`catsdwan_remove_device` (cat-sdwan W53, #81) shipped in cat-sdwan 0.7.0 but stays in `PENDING_TOOLS` (the
prompt's `pending`) until every server's committed catsdwan manifest lists it. On an older cat-sdwan server
the SD-WAN leg falls back to dissociating the edge from its configuration group and invalidating it, and says
the device record stays.

The Windows server has no DHCP-scope delete tool and its tools are not preview-first, so the Windows leg
writes its own preview (the exact cmdlet) and removes the scope with `win_run_powershell_json`.
"""

import ipaddress
from typing import Annotated

from pydantic import Field

from stutters_mcp_core.prompts import tool_ref
from stutters_mcp_core.suite import SERVERS_ARG, SuiteLeg, numbered, plan_legs, suite_prompt
from stutters_mcp_core.writes import Tier

LEGS = (
    SuiteLeg(
        "catsdwan",
        "the site's WAN edges, their configuration group or device template, and their removal from the "
        "Manager",
        (
            "catsdwan_list_devices",
            "catsdwan_get_device_config",
            "catsdwan_remove_device",
            "catsdwan_associate_config_group",
            "catsdwan_set_edge_validity",
        ),
    ),
    SuiteLeg(
        "catc",
        "the Catalyst Center site and its child sites, assigned devices, SD-Access fabric site and IP pool "
        "reservations",
        (
            "catc_get_site",
            "catc_list_devices",
            "catc_get_sda_fabric",
            "catc_list_ip_pools",
            "catc_delete_fabric_site",
            "catc_delete_device",
            "catc_delete_ip_pool",
            "catc_delete_site",
        ),
    ),
    SuiteLeg(
        "ise",
        "the site's network devices (NADs) and its location network device group",
        ("ise_list_ndgs", "ise_list_nads", "ise_get_nad", "ise_delete_nad", "ise_delete_object"),
    ),
    SuiteLeg(
        "fmc",
        "FMC network objects and groups for the site's subnets and name, and what still references them",
        ("fmc_find_address", "fmc_list_objects", "fmc_where_used", "fmc_delete_object", "fmc_delete_objects"),
    ),
    SuiteLeg(
        "win",
        "the site's DHCP scope (with leases and reservations) and its DNS records",
        (
            "win_list_dhcp_scopes",
            "win_list_dhcp_leases",
            "win_list_dns_zones",
            "win_get_dns_records",
            "win_remove_dns_record",
            "win_run_powershell_json",
        ),
    ),
)

# Leg tools not in every server's committed peer manifest yet (the prompt's `pending`, waived by
# assert_suite_pack). catsdwan_remove_device (cat-sdwan W53, #81) shipped in cat-sdwan 0.7.0; ise's committed
# catsdwan manifest (0.4.0) predates it (2026-10-28).
PENDING_TOOLS = ("catsdwan_remove_device",)

# Never removed by this workflow, whatever site they sit in.
PROTECTED = (
    "controllers and management appliances (SD-WAN Manager, Controllers and Validators, ISE nodes, Catalyst "
    "Center, FMC, NetBox, the Windows domain controller and DHCP/DNS server itself)"
)

_GLOBAL_NAMES = {"global", "all", "*", "/"}


def parse_site(site: str) -> str:
    """The site argument, trimmed; refuses empty and whole-network values."""
    s = site.strip()
    if not s:
        raise ValueError("site is required: the site to decommission, as the products name it")
    if s.lower().strip("/ ") in _GLOBAL_NAMES or s.lower().strip("/ ") == "":
        raise ValueError(f"site {site!r} names the whole network, not one site: refusing")
    return s


def parse_subnets(subnets: str) -> list[str]:
    """Comma- or space-separated IPv4/IPv6 networks, normalised ('192.0.2.0/24'); refuses /0..../7 (IPv4)
    and /0..../31 (IPv6), which are never one site."""
    out: list[str] = []
    for raw in subnets.replace(",", " ").split():
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            raise ValueError(f"subnets: {raw!r} is not an IP network (e.g. 192.0.2.0/24)") from None
        floor = 8 if net.version == 4 else 32
        if net.prefixlen < floor:
            raise ValueError(f"subnets: {net} is wider than a site (shorter than /{floor}): refusing")
        if str(net) not in out:
            out.append(str(net))
    return out


@suite_prompt(
    title="Decommission a site across products",
    legs=LEGS,
    tier=Tier.DANGEROUS,
    example={"site": "br9", "subnets": "192.0.2.0/24, 198.51.100.0/26", "dns_zone": "example.com"},
    pending=PENDING_TOOLS,
)
def suite_decommission_site(
    site: Annotated[
        str,
        Field(
            description="The site to decommission, as the products name it (SD-WAN site id, Catalyst Center "
            "site hierarchy such as 'Global/AU/Branch9', ISE location group, FMC object name part)"
        ),
    ],
    subnets: Annotated[
        str,
        Field(
            description="The site's address ranges, comma-separated (e.g. '192.0.2.0/24, 198.51.100.0/26'): "
            "used to find FMC objects, ISE NADs, the DHCP scope and DNS records; empty = by site name only"
        ),
    ] = "",
    dns_zone: Annotated[
        str,
        Field(description="DNS zone holding the site's records (e.g. 'example.com'); empty = every zone"),
    ] = "",
    servers: SERVERS_ARG = "",
) -> str:
    """Remove one site everywhere it exists, without leaving orphans: SD-WAN edges and their configuration
    group association, ISE NADs and the site's location group, the Catalyst Center site with its devices,
    fabric site and pool reservations, FMC network objects for the site's subnets, and the Windows DHCP scope
    and DNS records. First an inventory per product (read-only), then a removal preview per product; each
    product is removed only after the user approves its preview, one product at a time. Ends with a verify
    pass that must come back empty. Never removes controllers or management appliances. Dangerous: deletes
    devices and sites, no undo."""
    name = parse_site(site)
    nets = parse_subnets(subnets)
    zone = dns_zone.strip()
    plan = plan_legs(LEGS, servers)
    if not plan.legs:
        return plan.header
    net_text = ", ".join(nets) if nets else "none given (match by site name only)"
    zone_text = f"zone {zone}" if zone else "every zone"

    inv: list[str] = []
    rem: list[str] = []
    ver: list[str] = []
    if plan.has("catsdwan"):
        inv.append(
            f"SD-WAN: {tool_ref('catsdwan_list_devices')}(site={name!r}) with include_unjoined=true (every "
            "device at the site, joined or not), then for each WAN edge "
            f"{tool_ref('catsdwan_get_device_config')} (configuration group or device template, or CLI "
            "mode). A controller, validator or manager at the site is protected: list it, never remove it."
        )
        rem.append(
            f"SD-WAN, per WAN edge: {tool_ref('catsdwan_remove_device')}(chassis) (dissociates its "
            "configuration group or detaches its template, invalidates it, decommissions a joined software "
            "edge, deletes it from the Manager). Without that tool: "
            f"{tool_ref('catsdwan_associate_config_group')}(group, remove=[edges]), then "
            f"{tool_ref('catsdwan_set_edge_validity')}(validity='invalid'), and report that the device "
            "record stays in the Manager (remove it in the GUI). A hub or a protected site in the preview: "
            "stop and ask."
        )
        ver.append(f"{tool_ref('catsdwan_list_devices')}(site={name!r}, include_unjoined=true): no WAN edge.")
    if plan.has("catc"):
        inv.append(
            f"Catalyst Center: {tool_ref('catc_get_site')}({name!r}) (child sites, device count), "
            f"{tool_ref('catc_list_devices')}(site={name!r}) for every child site too, "
            f"{tool_ref('catc_get_sda_fabric')}(site={name!r}) (is it a fabric site) and "
            f"{tool_ref('catc_list_ip_pools')}(site={name!r}) (pool reservations)."
        )
        rem.append(
            "Catalyst Center, in this order: "
            f"{tool_ref('catc_delete_fabric_site')}(cascade=true) when it is a fabric site; "
            f"{tool_ref('catc_delete_device')} per device, asking the user for clean_config (true: the "
            "device is still reachable and gets reused; false: it is already gone); "
            f"{tool_ref('catc_delete_ip_pool')} per reservation; then "
            f"{tool_ref('catc_delete_site')}({name!r}, recursive=true)."
        )
        ver.append(
            f"{tool_ref('catc_get_site')}({name!r}) is not found, and "
            f"{tool_ref('catc_list_ip_pools')} shows no reservation for it."
        )
    if plan.has("ise"):
        lookup = (
            f" and {tool_ref('ise_get_nad')} for each subnet ({net_text}) to catch NADs outside the group"
            if nets
            else ""
        )
        found = [label for p, label in (("catsdwan", "SD-WAN"), ("catc", "Catalyst Center")) if plan.has(p)]
        legs = "legs" if len(found) > 1 else "leg"
        also = f"; add the NADs of the devices the {' and '.join(found)} {legs} found, by IP" if found else ""
        inv.append(
            f"ISE: {tool_ref('ise_list_ndgs')} (the location group for {name!r}), "
            f"{tool_ref('ise_list_nads')}(group={name!r}, detail=true){lookup}{also}."
        )
        rem.append(
            f"ISE: {tool_ref('ise_delete_nad')} per NAD (RADIUS/TACACS+ from its IPs is rejected from then "
            f"on), then {tool_ref('ise_delete_object')}(kind='ndg') for the site's location group, only "
            "once no NAD is left in it and only if it is the site's own group, never a parent or device-type "
            "group."
        )
        ver.append(
            f"{tool_ref('ise_list_nads')}(group={name!r}) is empty"
            + (" and no subnet resolves to a NAD" if nets else "")
            + f"; {tool_ref('ise_list_ndgs')} has no group for the site."
        )
    if plan.has("fmc"):
        by = (
            f"{tool_ref('fmc_find_address')}(address=...) for each subnet ({net_text}), areas objects and "
            f"rules, and {tool_ref('fmc_list_objects')}(name_or_value={name!r}) for network, host, range "
            "and group objects named after the site"
            if nets
            else f"{tool_ref('fmc_list_objects')}(name_or_value={name!r}) for network, host, range and "
            "group objects named after the site"
        )
        inv.append(f"FMC: {by}; {tool_ref('fmc_where_used')} on each object found (policies, rules, groups).")
        rem.append(
            f"FMC: delete only objects nothing references: {tool_ref('fmc_delete_object')} per object, or "
            f"{tool_ref('fmc_delete_objects')}(names=[...], unused_only=true) for many. Objects a rule or "
            "group still uses are not deleted: list each with its rule or group as a follow-up policy "
            "change. An FTD at the site is outside this workflow: name it."
        )
        ver.append("the FMC searches above find no object left except the ones listed as still referenced.")
    if plan.has("win"):
        dns_scan = (
            f"{tool_ref('win_get_dns_records')} in {zone_text} ("
            + ("A and PTR records in the subnets, CNAMEs to them, " if nets else "")
            + "names containing the site)"
        )
        scopes = "scopes named after the site"
        if nets:
            scopes = f"scopes inside {', '.join(nets)} or named after the site"
        if not zone:
            dns_scan = f"{tool_ref('win_list_dns_zones')}, then " + dns_scan
        inv.append(
            f"Windows: {tool_ref('win_list_dhcp_scopes')} ({scopes}), "
            f"{tool_ref('win_list_dhcp_leases')} per scope (active leases, reservations), and {dns_scan}."
        )
        rem.append(
            "Windows (its tools are not preview-first): write the preview yourself, the exact commands, and "
            "wait for approval. DHCP has no delete tool here: "
            f"{tool_ref('win_run_powershell_json')} with "
            '"Remove-DhcpServerv4Scope -ScopeId <scope> -Force" per scope (its leases and reservations '
            "go with it; say how many); then "
            f"{tool_ref('win_remove_dns_record')}(zone, name, record_type) per record, PTRs included."
        )
        ver.append(
            f"{tool_ref('win_list_dhcp_scopes')} has no site scope and {tool_ref('win_get_dns_records')} "
            "finds no site record."
        )

    steps: list[str] = [
        f"Inventory, read-only. Site {name!r}; subnets: {net_text}; DNS: {zone_text}. Per product:\n"
        + "\n".join(f"   - {s}" for s in inv),
        "Show one inventory table: product, object, kind, id, why it is tied to the site (site membership, "
        "subnet, name), and what still references it. Mark as protected and leave out of every removal "
        f"step: {PROTECTED}. Ask the user to confirm the list is the site and only the site; remove nothing "
        "before that.",
        "Removal, one product at a time in this order (edges off the overlay first, address records last). "
        "For each product: call its delete tools without confirm (previews only), show that product's "
        "previews together with the blast radius they state, and wait for the user to approve that product. "
        "Then confirm each preview with its own token, one call at a time, and report each result before the "
        "next product. Never confirm a preview naming something outside the inventory, never confirm in a "
        "loop, and stop the product at its first failure.\n" + "\n".join(f"   - {s}" for s in rem),
        "Verify: run the inventory reads again; each must come back empty.\n"
        + "\n".join(f"   - {s}" for s in ver),
        "Report: one line per product: removed (count), skipped (leg not connected or user declined), or "
        "left (each object and why). Say 'decommission complete' only when every verify read is empty; "
        "otherwise list what is left. Never show shared secrets, keys or passwords.",
    ]
    intro = (
        f"Decommission site {name!r} across products. Dangerous: devices and sites are deleted, no undo; "
        "each product is removed only after the user approves its preview."
    )
    return f"{plan.header}\n\n{intro}\n\n{numbered(steps)}"
