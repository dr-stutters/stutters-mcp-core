"""`suite_segmentation_audit` (roadmap C49): "can segment A reach segment B?" across products, with an
evidence pack an auditor can keep.

Segmentation is enforced in several places at once: SD-WAN service VPNs (and the control policy that leaks
routes between them, the data policy and edge firewall that drop between them), ISE's TrustSec egress matrix
(SGACLs between SGTs), FMC access rules between zones and networks, and Catalyst Center SD-Access virtual
networks (and the extranet policies that leak between them). Auditors (PCI DSS 1.3 style) want, per segment
pair, the intended verdict with the control and tool it came from, and an observed verdict from a live test.

The prompt inventories the segments on every connected platform (read-only), builds the pair matrix, takes
each layer's intended verdict with its source tool, tests each pair from a CML test host in the source
segment with `cml_check_reachability` (the observed verdict; ICMP), and writes the evidence pack: a markdown
report plus one JSON document with every pair, layer and test. Read-only: fixes are proposals.

All leg tools have shipped on their servers (2026-10-28), so the prompt has no `pending` tools.
"""

import re
from typing import Annotated

from pydantic import Field

from stutters_mcp_core.prompts import tool_ref
from stutters_mcp_core.suite import SERVERS_ARG, SuiteLeg, numbered, plan_legs, suite_prompt

LEGS = (
    SuiteLeg(
        "catsdwan",
        "SD-WAN service VPNs per edge, route leaking between them (control policy, OMP routes), and the data "
        "policy and edge firewall that permit or drop between them",
        (
            "catsdwan_list_devices",
            "catsdwan_list_config_groups",
            "catsdwan_get_config_group",
            "catsdwan_list_policies",
            "catsdwan_get_policy",
            "catsdwan_list_policy_groups",
            "catsdwan_get_firewall_policy",
            "catsdwan_omp_routes",
        ),
    ),
    SuiteLeg(
        "ise",
        "TrustSec: the SGTs, the IP-to-SGT mappings and the egress matrix (SGACL) between every SGT pair",
        (
            "ise_list_sgts",
            "ise_list_ip_sgt_mappings",
            "ise_list_sgt_vn_vlan",
            "ise_get_egress_matrix",
        ),
    ),
    SuiteLeg(
        "fmc",
        "FMC access rules between the segments' zones and networks, and the deciding rule per pair",
        ("fmc_list_access_rules", "fmc_find_address", "fmc_rule_lookup", "fmc_prove_policy"),
    ),
    SuiteLeg(
        "catc",
        "SD-Access virtual networks at the fabric site, their IP pools, extranet policies leaking between "
        "them, and the SGACLs a fabric edge enforces",
        ("catc_get_sda_fabric", "catc_list_ip_pools", "catc_get_group_policy_enforcement"),
    ),
    SuiteLeg(
        "cml",
        "the observed verdict: a live reachability test from a test host in each segment to every other",
        ("cml_get_layer3_addresses", "cml_check_reachability"),
    ),
)

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]*$")
_SERVICE_RE = re.compile(r"^(?:any|icmp|(?:tcp|udp)/\d{1,5}(?:-\d{1,5})?)$")


def parse_segments(segments: str) -> list[str]:
    """Comma-separated segment names ('IoT, Corporate'), trimmed and de-duplicated; empty = all found."""
    out: list[str] = []
    for raw in segments.split(","):
        name = raw.strip()
        if not name:
            continue
        if not _SEGMENT_RE.match(name):
            raise ValueError(f"segment {name!r}: use letters, digits, spaces, '-', '_' or '.'")
        if name.lower() not in {s.lower() for s in out}:
            out.append(name)
    if len(out) == 1:
        raise ValueError("segments: name at least two segments to pair, or leave it empty for all of them")
    return out


def parse_intent(intent: str) -> list[tuple[str, str, str]]:
    """Allowed flows, comma-separated: 'Corporate->Servers' or 'Corporate->Servers:tcp/443' (service any,
    icmp, tcp/<port>, udp/<port> or a port range). Returns (source, destination, service) tuples."""
    out: list[tuple[str, str, str]] = []
    for raw in intent.split(","):
        item = raw.strip()
        if not item:
            continue
        pair, _, service = item.partition(":")
        src, sep, dst = pair.partition("->")
        src, dst, service = src.strip(), dst.strip(), (service.strip().lower() or "any")
        if not sep or not src or not dst:
            raise ValueError(f"intent {item!r}: write each allowed flow as 'A->B' or 'A->B:tcp/443'")
        if not (_SEGMENT_RE.match(src) and _SEGMENT_RE.match(dst)):
            raise ValueError(f"intent {item!r}: segment names use letters, digits, spaces, '-', '_' or '.'")
        if src.lower() == dst.lower():
            raise ValueError(f"intent {item!r}: source and destination are the same segment")
        if not _SERVICE_RE.match(service):
            raise ValueError(f"intent {item!r}: service must be any, icmp, tcp/<port> or udp/<port>")
        flow = (src, dst, service)
        if flow not in out:
            out.append(flow)
    return out


# The evidence pack's JSON document; every field is filled from tool output, never guessed.
EVIDENCE_JSON = """{
  "audit": "segmentation",
  "framework": "<framework or null>",
  "scope": {"site": "<site or null>", "segments": ["<name>", ...]},
  "generated_at": "<ISO 8601 time of the last tool call>",
  "servers": {"checked": ["<prefix>", ...], "skipped": ["<prefix>", ...]},
  "segments": [{"name": "<name>", "sdwan_vpn": <id or null>, "catc_vn": "<name or null>",
                "sgts": ["<name (tag)>"], "subnets": ["<cidr>"], "fmc_zones": ["<zone>"],
                "test_host": {"node": "<cml node or null>", "ip": "<ip or null>"}}],
  "pairs": [{"source": "<segment>", "destination": "<segment>", "service": "<any|icmp|tcp/443>",
             "declared_intent": "allow|deny",
             "layers": [{"control": "<a control from the intended-verdict step>",
                         "verdict": "allow|deny|not_on_path|unknown", "source_tool": "<tool name>",
                         "evidence": "<rule, sequence, SGACL or route that decides it>"}],
             "intended": "allow|deny", "deciding_control": "<control>",
             "observed": {"verdict": "reachable|unreachable|not_tested", "tool": "<tool name or null>",
                          "from": "<node>", "to": "<ip>", "success_pct": <0-100 or null>},
             "status": "pass|leak|over_blocked|policy_conflict|untested"}],
  "findings": [{"severity": "high|medium|low", "pair": "<A->B>", "what": "<one line>",
                "fix_proposed": "<one line, not applied>"}],
  "not_checked": ["<what was not checked and why>"]
}"""


@suite_prompt(
    title="Segmentation audit across products",
    legs=LEGS,
    example={
        "site": "br1",
        "segments": "IoT, Corporate",
        "intent": "Corporate->IoT:tcp/443",
        "lab": "secure-branch",
        "framework": "PCI DSS 4.0 req 1.3",
    },
)
def suite_segmentation_audit(
    site: Annotated[
        str,
        Field(
            description="Site the audit covers, as the products name it (SD-WAN site id or name, Catalyst "
            "Center fabric site hierarchy); empty = every site"
        ),
    ] = "",
    segments: Annotated[
        str,
        Field(
            description="Segments to pair, comma-separated names as the products call them (VPN, VN or SGT "
            "names, e.g. 'IoT, Corporate'); empty = every segment the inventory finds"
        ),
    ] = "",
    intent: Annotated[
        str,
        Field(
            description="Flows that should be allowed, comma-separated 'A->B' or 'A->B:tcp/443' "
            "(directional); every other pair should be denied. Empty = fully segmented (every pair denied)"
        ),
    ] = "",
    lab: Annotated[
        str,
        Field(
            description="CML lab with a test host in each segment, for the observed test; empty = find it "
            "with the cml tools or ask"
        ),
    ] = "",
    framework: Annotated[
        str,
        Field(description="Audit framework for the evidence pack (e.g. 'PCI DSS 4.0 req 1.3'); empty = none"),
    ] = "",
    servers: SERVERS_ARG = "",
) -> str:
    """Can segment A reach segment B? Inventory the segments on every connected platform (SD-WAN service
    VPNs, route leaking, data policy and edge firewall; ISE SGTs and the TrustSec egress matrix; FMC rules
    between zones and networks; Catalyst Center SD-Access virtual networks and extranet policies), then per
    segment pair: each layer's intended verdict with the tool it came from, the observed verdict from a live
    CML reachability test, and a status (pass, leak, over-blocked, policy conflict, untested). Ends with an
    evidence pack: a markdown report plus one JSON document. Skips the platforms whose server is not
    connected and says so. Read-only."""
    segs = parse_segments(segments)
    allowed = parse_intent(intent)
    plan = plan_legs(LEGS, servers)
    if not plan.legs:
        return plan.header
    where = f"site {site.strip()!r}" if site.strip() else "every site"
    lab_name = lab.strip()
    fw = framework.strip()

    inv: list[str] = []
    layers: list[str] = []
    if plan.has("catsdwan"):
        inv.append(
            f"SD-WAN: {tool_ref('catsdwan_list_devices')}(site=...) for the WAN edges at {where}; "
            f"{tool_ref('catsdwan_list_config_groups')} then "
            f"{tool_ref('catsdwan_get_config_group')}(group) for each edge's service VPNs (VPN id, name, "
            "interfaces and subnets). Route leaking and drops: "
            f"{tool_ref('catsdwan_list_policies')}(kind='all') and {tool_ref('catsdwan_get_policy')} "
            "for each activated centralised policy (control policy sequences that export or import routes "
            "between VPNs, data policy sequences that drop or permit between prefixes); "
            f"{tool_ref('catsdwan_list_policy_groups')} and "
            f"{tool_ref('catsdwan_get_firewall_policy')}(group) for the edge firewall's zone pairs (zones "
            f"are VPNs). Then {tool_ref('catsdwan_omp_routes')}(edge, vpn=<the source segment's VPN>) per "
            "segment: a prefix of another segment's VPN in this VPN's table is a leak in the control plane, "
            "intended or not."
        )
        layers += [
            "sdwan_vpn: separate service VPNs deny unless a route is leaked (source: the configuration group "
            "and the OMP routes)",
            "sdwan_control_policy: the sequence that leaks the route (source: the control policy)",
            "sdwan_data_policy: a drop or permit sequence matching the pair (source: the data policy)",
            "sdwan_firewall: the zone pair's action and rule (source: the edge firewall policy)",
        ]
    if plan.has("ise"):
        inv.append(
            f"ISE: {tool_ref('ise_list_sgts')} (every SGT and its tag), "
            f"{tool_ref('ise_list_ip_sgt_mappings')} (static IP or subnet to SGT) and "
            f"{tool_ref('ise_list_sgt_vn_vlan')} (SGT to virtual network and VLAN), which tie each "
            f"segment to its SGTs. Then {tool_ref('ise_get_egress_matrix')}(source=<SGT>, "
            "destination=<SGT>) for every SGT pair of two segments: the SGACL and its permit/deny lines, or "
            "the default rule when the cell is empty (say which)."
        )
        layers.append(
            "trustsec_sgacl: the egress matrix cell's SGACL, or the matrix default "
            "(source: the egress matrix)"
        )
    if plan.has("fmc"):
        inv.append(
            f"FMC: {tool_ref('fmc_find_address')}(<each segment subnet>) for the objects and zones that "
            f"cover it, and {tool_ref('fmc_list_access_rules')} on the policy of the firewall between the "
            "segments (rules whose source and destination zones or networks are two different segments). "
            f"Per pair, {tool_ref('fmc_rule_lookup')}(source=<test IP in A>, destination=<test IP in B>, "
            "port, protocol) gives the deciding rule offline, and "
            f"{tool_ref('fmc_prove_policy')}(flows=<the pair table with expected verdicts>, device) traces "
            "every pair on the device itself (packet tracer: the device's verdict, not traffic)."
        )
        layers.append("fmc_rule: the deciding access rule, or the default action (source: the rule lookup)")
    if plan.has("catc"):
        inv.append(
            f"Catalyst Center: {tool_ref('catc_get_sda_fabric')}(site=...) for the fabric site's layer 3 "
            "virtual networks, their anycast gateways and the extranet policies (a provider VN shared with "
            f"subscriber VNs leaks routes between them); {tool_ref('catc_list_ip_pools')}(site=...) for each "
            f"VN's subnets; {tool_ref('catc_get_group_policy_enforcement')}(device=<a fabric edge>, "
            "source=<SGT>, destination=<SGT>) for what that edge actually enforces between two SGTs."
        )
        layers += [
            "sda_vn: separate virtual networks deny unless an extranet policy or a fusion router joins them "
            "(source: the fabric)",
            "sda_extranet: the extranet policy that shares the routes (source: the fabric)",
        ]
    if plan.has("cml"):
        where_lab = f"lab {lab_name!r}" if lab_name else "the lab that models these segments (ask if unsure)"
        inv.append(
            f"CML, {where_lab}: {tool_ref('cml_get_layer3_addresses')}(lab) and pick one test host per "
            "segment (a host or a loopback in that segment's VPN/VN, not a router's other interface). Record "
            "the node, the IP and, for a router interface or loopback, its VRF (a service VPN or VN); a "
            "segment with no test host is 'untested'."
        )

    seg_scope = (
        "the segments " + ", ".join(repr(s) for s in segs)
        if segs
        else "every segment the inventory finds (name each)"
    )
    if allowed:
        intent_text = (
            "Declared intent: only these flows are allowed, everything else between segments is denied: "
            + "; ".join(f"{a} -> {b} ({svc})" for a, b, svc in allowed)
            + "."
        )
    else:
        intent_text = "Declared intent: fully segmented, every pair of different segments is denied."
    steps: list[str] = [
        f"Inventory, read-only, at {where}. Per platform:\n" + "\n".join(f"   - {s}" for s in inv),
        f"Segments: one table for {seg_scope}: name, SD-WAN VPN, SD-Access VN, SGTs, subnets, FMC zones, "
        "test host. Match a segment across products by its subnets first and its name second; a segment that "
        "exists on one platform only is a finding (its controls elsewhere are 'unknown').",
        f"{intent_text} Pairs are directional (A->B and B->A are two rows); each allowed flow keeps its "
        "service, every other pair is tested as 'any'.",
    ]
    if layers:
        steps.append(
            "Intended verdict per pair: one entry per control on the pair's path, each with its verdict "
            "(allow, deny, not_on_path, unknown), the tool it came from and the rule, sequence, SGACL or "
            "route that decides it:\n"
            + "\n".join(f"   - {s}" for s in layers)
            + "\n   The pair's intended verdict is deny if any control on the path denies, else allow; the "
            "deciding control is the first one on the path that denies (or the last that allows). Controls "
            "that disagree with each other on an allowed flow are a policy_conflict."
        )
    if plan.has("cml"):
        steps.append(
            f"Observed verdict: {tool_ref('cml_check_reachability')}(lab, nodes=[<test host of A>], "
            "targets=[<test IP of every other segment>], count=3), one call per source segment (up to 20 "
            "targets each), with vrf=<the test host's VRF> when it is a router interface or loopback (a "
            "ping from the global table proves nothing about the segment and would hide a leak); pass "
            "wait_seconds=0 (the network is converged; a timeout is the verdict). Any reply is 'reachable', "
            "none is 'unreachable'. This is ICMP: for a pair whose only allowed service is TCP/UDP, say the "
            "ping proves the deny side only (an unreachable ping there is not over_blocked)"
            + (", and use the FMC device trace for the service." if plan.has("fmc") else ".")
        )
    else:
        steps.append(
            "Observed verdict: no live test (cml leg skipped), so every pair's observed verdict is "
            "'not_tested' and its status at best 'untested'"
            + (
                "; the FMC device trace above is the closest evidence: name it as a simulation, not traffic."
                if plan.has("fmc")
                else "."
            )
        )
    steps += [
        "Status per pair: pass (declared intent, intended and observed agree), leak (observed reachable or "
        "intended allow where the declared intent denies: the most serious), over_blocked (declared allow "
        "but intended deny or observed unreachable), policy_conflict (controls disagree), untested (no "
        "observed verdict). One matrix: rows source, columns destination, each cell the status and the "
        "deciding control.",
        "Findings, most serious first: every leak with the control that lets it through (a leaked route, a "
        "permit sequence, an SGACL permit or empty cell with a permit default, an FMC rule, an extranet "
        "policy) and the fix proposed (not applied); then over-blocked flows, conflicts, segments missing on "
        "a platform and untested pairs.",
        "Evidence pack. (a) Markdown report: title"
        + (f" with the framework {fw!r}" if fw else "")
        + ", scope, time, servers checked and skipped, the segment table, the pair matrix, the findings, "
        "and a 'Not checked' list (skipped legs, refused reads, segments without a test host). (b) One JSON "
        "document in a fenced json block, exactly this shape, every value from tool output:\n"
        + EVIDENCE_JSON,
        "This workflow only reads: call no write tool. Never show shared secrets or keys. Say 'segmented as "
        "intended' only when every pair is pass; otherwise lead with the number of leaks.",
    ]
    intro = (
        f"Segmentation audit at {where}"
        + (f" for {fw}" if fw else "")
        + ": intended verdict per segment pair, with its source, against an observed test."
    )
    return f"{plan.header}\n\n{intro}\n\n{numbered(steps)}"
