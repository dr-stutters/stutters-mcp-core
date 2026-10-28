This suite workflow uses these servers: catsdwan (Catalyst SD-WAN Manager), ise (Cisco ISE), fmc (Secure Firewall Management Center), catc (Catalyst Center), cml (Cisco Modeling Labs).

Segmentation audit at site 'br1' for PCI DSS 4.0 req 1.3: intended verdict per segment pair, with its source, against an observed test.

1. Inventory, read-only, at site 'br1'. Per platform:
   - SD-WAN: `catsdwan_list_devices`(site=...) for the WAN edges at site 'br1'; `catsdwan_list_config_groups` then `catsdwan_get_config_group`(group) for each edge's service VPNs (VPN id, name, interfaces and subnets). Route leaking and drops: `catsdwan_list_policies`(kind='all') and `catsdwan_get_policy` for each activated centralised policy (control policy sequences that export or import routes between VPNs, data policy sequences that drop or permit between prefixes); `catsdwan_list_policy_groups` and `catsdwan_get_firewall_policy`(group) for the edge firewall's zone pairs (zones are VPNs). Then `catsdwan_omp_routes`(edge, vpn=<the source segment's VPN>) per segment: a prefix of another segment's VPN in this VPN's table is a leak in the control plane, intended or not.
   - ISE: `ise_list_sgts` (every SGT and its tag), `ise_list_ip_sgt_mappings` (static IP or subnet to SGT) and `ise_list_sgt_vn_vlan` (SGT to virtual network and VLAN), which tie each segment to its SGTs. Then `ise_get_egress_matrix`(source=<SGT>, destination=<SGT>) for every SGT pair of two segments: the SGACL and its permit/deny lines, or the default rule when the cell is empty (say which).
   - FMC: `fmc_find_address`(<each segment subnet>) for the objects and zones that cover it, and `fmc_list_access_rules` on the policy of the firewall between the segments (rules whose source and destination zones or networks are two different segments). Per pair, `fmc_rule_lookup`(source=<test IP in A>, destination=<test IP in B>, port, protocol) gives the deciding rule offline, and `fmc_prove_policy`(flows=<the pair table with expected verdicts>, device) traces every pair on the device itself (packet tracer: the device's verdict, not traffic).
   - Catalyst Center: `catc_get_sda_fabric`(site=...) for the fabric site's layer 3 virtual networks, their anycast gateways and the extranet policies (a provider VN shared with subscriber VNs leaks routes between them); `catc_list_ip_pools`(site=...) for each VN's subnets; `catc_get_group_policy_enforcement`(device=<a fabric edge>, source=<SGT>, destination=<SGT>) for what that edge actually enforces between two SGTs.
   - CML, lab 'secure-branch': `cml_get_layer3_addresses`(lab) and pick one test host per segment (a host or a loopback in that segment's VPN/VN, not a router's other interface). Record the node, the IP and, for a router interface or loopback, its VRF (a service VPN or VN); a segment with no test host is 'untested'.
2. Segments: one table for the segments 'IoT', 'Corporate': name, SD-WAN VPN, SD-Access VN, SGTs, subnets, FMC zones, test host. Match a segment across products by its subnets first and its name second; a segment that exists on one platform only is a finding (its controls elsewhere are 'unknown').
3. Declared intent: only these flows are allowed, everything else between segments is denied: Corporate -> IoT (tcp/443). Pairs are directional (A->B and B->A are two rows); each allowed flow keeps its service, every other pair is tested as 'any'.
4. Intended verdict per pair: one entry per control on the pair's path, each with its verdict (allow, deny, not_on_path, unknown), the tool it came from and the rule, sequence, SGACL or route that decides it:
   - sdwan_vpn: separate service VPNs deny unless a route is leaked (source: the configuration group and the OMP routes)
   - sdwan_control_policy: the sequence that leaks the route (source: the control policy)
   - sdwan_data_policy: a drop or permit sequence matching the pair (source: the data policy)
   - sdwan_firewall: the zone pair's action and rule (source: the edge firewall policy)
   - trustsec_sgacl: the egress matrix cell's SGACL, or the matrix default (source: the egress matrix)
   - fmc_rule: the deciding access rule, or the default action (source: the rule lookup)
   - sda_vn: separate virtual networks deny unless an extranet policy or a fusion router joins them (source: the fabric)
   - sda_extranet: the extranet policy that shares the routes (source: the fabric)
   The pair's intended verdict is deny if any control on the path denies, else allow; the deciding control is the first one on the path that denies (or the last that allows). Controls that disagree with each other on an allowed flow are a policy_conflict.
5. Observed verdict: `cml_check_reachability`(lab, nodes=[<test host of A>], targets=[<test IP of every other segment>], count=3), one call per source segment (up to 20 targets each), with vrf=<the test host's VRF> when it is a router interface or loopback (a ping from the global table proves nothing about the segment and would hide a leak); pass wait_seconds=0 (the network is converged; a timeout is the verdict). Any reply is 'reachable', none is 'unreachable'. This is ICMP: for a pair whose only allowed service is TCP/UDP, say the ping proves the deny side only (an unreachable ping there is not over_blocked), and use the FMC device trace for the service.
6. Status per pair: pass (declared intent, intended and observed agree), leak (observed reachable or intended allow where the declared intent denies: the most serious), over_blocked (declared allow but intended deny or observed unreachable), policy_conflict (controls disagree), untested (no observed verdict). One matrix: rows source, columns destination, each cell the status and the deciding control.
7. Findings, most serious first: every leak with the control that lets it through (a leaked route, a permit sequence, an SGACL permit or empty cell with a permit default, an FMC rule, an extranet policy) and the fix proposed (not applied); then over-blocked flows, conflicts, segments missing on a platform and untested pairs.
8. Evidence pack. (a) Markdown report: title with the framework 'PCI DSS 4.0 req 1.3', scope, time, servers checked and skipped, the segment table, the pair matrix, the findings, and a 'Not checked' list (skipped legs, refused reads, segments without a test host). (b) One JSON document in a fenced json block, exactly this shape, every value from tool output:
{
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
}
9. This workflow only reads: call no write tool. Never show shared secrets or keys. Say 'segmented as intended' only when every pair is pass; otherwise lead with the number of leaks.
