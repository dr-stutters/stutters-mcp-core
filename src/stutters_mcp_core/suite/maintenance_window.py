"""`suite_maintenance_window` (roadmap C51): one maintenance window across SD-WAN edges, Catalyst Center
switches and ISE patches at a site, with one plan and one before/after.

The prompt inventories what runs where (read-only), writes one plan (order, blast radius, rollback, go/no-go),
takes the C53 pre snapshots, then upgrades product by product with each product's own dangerous upgrade tool,
preview first: `catsdwan_upgrade_software` for the WAN edges, Catalyst Center SWIM (`catc_distribute_image`,
then `catc_activate_image` one device at a time) for the switches. ISE has no patch-install API: its leg
checks the patch level on every node, takes a configuration backup (`ise_create_backup`, previewed) and leaves
the install to the GUI, which the user runs. Each product is compared against its pre snapshot before the next
one starts; the window ends with the C53 post compare on every leg and one report. Dangerous tier: devices
reload.

Order (smallest blast radius first, and nothing that cuts the management path before it is proven): ISE
checks and backup first (they change no device), then the SD-WAN edges (the site's path to the controllers
and to Catalyst Center), then the switches. A dual-edge site upgrades one edge at a time
(`catsdwan_upgrade_software` refuses two edges of one site in one run).
"""

from typing import Annotated

from pydantic import Field

from stutters_mcp_core.prompts import tool_ref
from stutters_mcp_core.suite import SERVERS_ARG, SuiteLeg, numbered, plan_legs, suite_prompt
from stutters_mcp_core.suite.decommission_site import parse_site
from stutters_mcp_core.writes import Tier

LEGS = (
    SuiteLeg(
        "catsdwan",
        "the site's WAN edges: software they run and have installed, the repository and release ceiling, "
        "and their install and activation",
        (
            "catsdwan_list_devices",
            "catsdwan_list_software",
            "catsdwan_snapshot",
            "catsdwan_upgrade_software",
            "catsdwan_compare_snapshot",
        ),
    ),
    SuiteLeg(
        "catc",
        "the site's switches and other Catalyst Center devices: running image against the golden image, and "
        "SWIM distribution and activation",
        (
            "catc_list_device_images",
            "catc_list_images",
            "catc_snapshot",
            "catc_distribute_image",
            "catc_activate_image",
            "catc_compare_snapshot",
        ),
    ),
    SuiteLeg(
        "ise",
        "the ISE patch level on every node, deployment health and a configuration backup before patching",
        (
            "ise_list_nodes",
            "ise_list_patches",
            "ise_deployment_health",
            "ise_get_backup_status",
            "ise_snapshot",
            "ise_create_backup",
            "ise_compare_snapshot",
        ),
    ),
)

MODES = ("upgrade", "stage")


def parse_mode(mode: str) -> str:
    """'upgrade' (install and activate: devices reload) or 'stage' (copy and install only, no reload)."""
    m = mode.strip().lower() or "upgrade"
    if m not in MODES:
        raise ValueError("mode must be 'upgrade' (install and activate) or 'stage' (copy only, no reload)")
    return m


@suite_prompt(
    title="Maintenance window across products",
    legs=LEGS,
    tier=Tier.DANGEROUS,
    example={
        "site": "br2",
        "sdwan_version": "17.18.03",
        "catc_image": "cat9k_iosxe.17.15.04.SPA.bin",
        "ise_patch": "3",
        "mode": "upgrade",
        "window": "Sat 02:00-04:00",
    },
)
def suite_maintenance_window(
    site: Annotated[
        str,
        Field(
            description="The site the window covers, as the products name it (SD-WAN site id or name, "
            "Catalyst Center site hierarchy such as 'Global/AU/Branch2')"
        ),
    ],
    sdwan_version: Annotated[
        str,
        Field(
            description="Target WAN edge version from the Manager's repository (e.g. '17.18.03'); empty = "
            "propose the newest one inside the release ceiling and ask"
        ),
    ] = "",
    catc_image: Annotated[
        str,
        Field(
            description="Target Catalyst Center image name or id (catc_list_images); empty = the site's "
            "golden image for each device family"
        ),
    ] = "",
    ise_patch: Annotated[
        str,
        Field(description="ISE patch number the window installs (e.g. '3'); empty = check and report only"),
    ] = "",
    mode: Annotated[
        str,
        Field(
            description="'upgrade' (default: install and activate, devices reload) or 'stage' (copy and "
            "install only, no reload: pre-stage before the window)"
        ),
    ] = "upgrade",
    window: Annotated[
        str,
        Field(description="The approved window in a few words (e.g. 'Sat 02:00-04:00 AEST'); empty = none"),
    ] = "",
    servers: SERVERS_ARG = "",
) -> str:
    """Run one maintenance window at a site across products: inventory what runs where, write one plan
    (order, blast radius, rollback, go/no-go), take the pre snapshots, then upgrade product by product with
    each product's preview-first upgrade tool: SD-WAN edges (install, activate), Catalyst Center switches
    (SWIM distribute, then activate one device at a time) and the ISE patch check with a backup first (ISE
    patches install from its GUI). Each product is compared against its pre snapshot before the next starts;
    the window ends with a post compare on every product and one report. mode=stage copies images only (no
    reload). Dangerous: devices reload."""
    name = parse_site(site)
    how = parse_mode(mode)
    stage = how == "stage"
    plan = plan_legs(LEGS, servers)
    if not plan.legs:
        return plan.header
    win = window.strip()
    sd_ver = sdwan_version.strip()
    img = catc_image.strip()
    patch = ise_patch.strip()

    inv: list[str] = []
    pre: list[str] = []
    up: list[str] = []
    rollback: list[str] = []
    if plan.has("ise"):
        target = (
            f"is Patch {patch} on every node already? Then the ISE leg installs nothing"
            if patch
            else "the patch level each node runs (check only: no target patch given)"
        )
        inv.append(
            f"ISE: {tool_ref('ise_list_nodes')} (every node and its personas), "
            f"{tool_ref('ise_list_patches')} ({target}), {tool_ref('ise_deployment_health')} (node status, "
            "replication, certificates) and "
            f"{tool_ref('ise_get_backup_status')} (the last configuration backup)."
        )
        if patch and not stage:
            up.append(
                "ISE (no patch-install API): call "
                f"{tool_ref('ise_create_backup')}(repository, name) without confirm, show the preview and "
                "confirm it once approved; wait until the backup succeeds. Then the user installs Patch "
                f"{patch} from the Primary Administration Node's GUI (Administration > System > Maintenance "
                "> Patch Management), which rolls it to the nodes one at a time; nodes restart their "
                "services, so authentication fails over between the Policy Service Nodes. Wait for the user "
                f"to say it finished, then {tool_ref('ise_list_patches')} must show it, and "
                f"{tool_ref('ise_compare_snapshot')}(before=the ISE pre blob) must show no unexpected change "
                "before the next product."
            )
            rollback.append(
                "ISE: roll the patch back from the same GUI page; the backup restores from the GUI."
            )
        else:
            up.append(
                "ISE: nothing installed in this window ("
                + ("mode=stage" if stage else "no target patch")
                + f"). If the last backup is older than a day, offer {tool_ref('ise_create_backup')} "
                "(preview first) before any device reloads, since endpoints re-authenticate when their "
                "switch returns."
            )
    if plan.has("catsdwan"):
        target = f"version {sd_ver}" if sd_ver else "the newest repository version inside the release ceiling"
        inv.append(
            f"SD-WAN: {tool_ref('catsdwan_list_devices')}(site={name!r}) (WAN edges, reachability, hub or "
            f"protected) and {tool_ref('catsdwan_list_software')}(site={name!r}) (what each edge runs and "
            f"has installed, the repository, the release ceiling). Target: {target}; edges never run newer "
            "than the Manager and controllers."
        )
        if stage:
            act = (
                "activate=false): installs only, no reload. An edge that already has the version installed "
                "needs nothing: leave it out."
            )
        else:
            act = (
                "activate=true): the edge reboots into it (a version already installed is only activated). A "
                "dual-edge site goes one edge at a time: the second only once the first is back with its "
                "control connections and BFD sessions up."
            )
        up.append(
            f"SD-WAN, one WAN edge per call (the tool refuses two edges of one site in one run): "
            f"{tool_ref('catsdwan_upgrade_software')}(devices=[edge], version, {act} A blocker in the "
            "preview (empty repository, newer than the ceiling, low bootflash, unreachable, busy) stops this "
            f"product. Then {tool_ref('catsdwan_compare_snapshot')}(before=the SD-WAN pre blob)."
        )
        if not stage:
            rollback.append(
                f"SD-WAN: {tool_ref('catsdwan_upgrade_software')}(devices=[edge], version=<the version it "
                "ran before>, activate=true): it stays installed, so this only activates it (one reboot)."
            )
    if plan.has("catsdwan") and plan.has("catc") and not stage:
        up.append(
            "Before the switches: the site's devices must be reachable from Catalyst Center again after the "
            "edge upgrade (its management path runs over the WAN); if not, stop here."
        )
    if plan.has("catc"):
        target = f"image {img!r}" if img else "the site's golden image (golden=true) for each device family"
        inv.append(
            f"Catalyst Center: {tool_ref('catc_list_device_images')}(site={name!r}) (running image, golden "
            f"compliance, last update status) and {tool_ref('catc_list_images')}(site={name!r}, golden=true) "
            f"(golden images; imported=true for the repository). Target: {target}; the devices to upgrade "
            "are those not on it."
        )
        steps_c = (
            f"Catalyst Center: first {tool_ref('catc_distribute_image')}(device, image) per device (copies "
            "to flash, no reload; it can run for all devices before any activation)"
        )
        if stage:
            steps_c += "; mode=stage: no activation"
        else:
            steps_c += (
                f"; then {tool_ref('catc_activate_image')}(device, image), one device per call and per "
                "confirm (each RELOADS that device): access switches first, one as a canary, then the rest; "
                "the distribution/core devices and the wireless controller last, one member of a redundant "
                "pair at a time, each back and reachable before the next"
            )
        up.append(
            f"{steps_c}. Then {tool_ref('catc_compare_snapshot')}(before=the Catalyst Center pre blob)."
        )
        if not stage:
            rollback.append(
                f"Catalyst Center: {tool_ref('catc_activate_image')}(device, image=<the image it ran "
                "before>), if that image is still in the repository (one more reload)."
            )
    for leg in plan.legs:
        pre.append(f"{tool_ref(f'{leg.peer}_snapshot')}(site={name!r})")

    order = ", then ".join(
        text
        for peer, text in (
            ("ise", "ISE checks and backup first (no device changes)"),
            ("catsdwan", "the WAN edges (the site's path to its controllers)"),
            ("catc", "the Catalyst Center switches"),
        )
        if plan.has(peer)
    )
    when = f"Window: {win}. " if win else ""
    action = "copy and install only, no reload" if stage else "devices reload"
    steps: list[str] = [
        f"Inventory, read-only. Site {name!r}. Per product:\n" + "\n".join(f"   - {s}" for s in inv),
        "Plan: one table per product (device, role, runs now, target, action, reload yes/no) and the order: "
        f"{order}. State the blast radius per step (which users and links lose "
        "service, for how long), the rollback per product, and the go/no-go: no target version in a "
        "repository, a device unreachable, a target newer than its controllers, or a health problem already "
        f"present is a no-go for that product. {when}Never start a reload the window cannot also roll back. "
        "Ask the user to approve the plan; change nothing before that.",
        "Pre snapshots, read-only: "
        + "; ".join(pre)
        + ". Keep every blob exactly as returned: they are the 'before' of every compare below.",
        f"Changes, one product at a time in the plan's order ({action}). For each product: call its tool "
        "without confirm (preview only), show the preview with the blast radius it states, and wait for the "
        "user to approve. Then confirm each preview with its own token, one call at a time, follow it to "
        "success or failure, and report it before the next. Never confirm a preview naming a device outside "
        "the plan, never confirm in a loop, and stop the product at its first failure (offer its rollback). "
        "Do not start the next product while this one's compare shows an unexpected major change.\n"
        + "\n".join(f"   - {s}" for s in up),
        "Post compare, read-only: each product's compare tool once more with before = its pre blob, "
        "unchanged (no `after`: it takes the after snapshot now). One table, most severe first; the reloads "
        "and version changes the plan expected in a separate 'Expected' list.",
        "Report: per product, the devices upgraded (from, to), skipped (leg not connected, already on "
        "target, no-go, user declined) or failed (why, rollback done or offered); the unexpected changes "
        "from the compare; and the rollback for anything left on a new version. Say 'window complete' only "
        "when every planned device is on its target and the compare shows nothing unexpected.",
    ]
    if rollback:
        steps.insert(
            2, "Rollback per product (put it in the plan):\n" + "\n".join(f"   - {r}" for r in rollback)
        )
    intro = (
        f"Maintenance window at site {name!r} across products, mode={how}. Dangerous: "
        + (
            "images are copied and installed; nothing reloads."
            if stage
            else "devices reload; plan the outage."
        )
        + " Each product changes only after the user approves its preview."
    )
    return f"{plan.header}\n\n{intro}\n\n{numbered(steps)}"
