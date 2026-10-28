# Suite prompt pack (`suite_*`)

Some workflows span several products and have no natural owner: a change check across SD-WAN, ISE, Catalyst
Center and FMC, a maintenance window, certificate expiry, a site decommission. If one server owned such a
prompt it would be missing wherever that server is not deployed. So core ships these prompts as the **suite
pack** (roadmap C48), and **every server registers the whole pack**. A client connected to several servers
sees the same `suite_*` prompt from each one; any copy renders the same text.

## Catalogue

| Prompt | Item | Legs (peers) | Tier | What it does |
|---|---|---|---|---|
| `suite_change_check(site, phase, change, before, servers)` | C53 | catsdwan, ise, catc, fmc | read | phase=pre: `<prefix>_snapshot` per connected server, blobs kept in the conversation; phase=post: `<prefix>_compare_snapshot` per server, one table by severity with expected effects apart, verdict "nothing else changed" or the unexpected changes ([SNAPSHOTS.md](SNAPSHOTS.md)) |
| `suite_asbuilt_drift(site, device, checks, servers)` | C62 | netbox, catc | read | NetBox interfaces, VLANs and cables (intent) vs Catalyst Center interfaces, VLANs and CDP/LLDP links (as built) for one site; one table per check with the side to fix (NetBox, network, site visit) and the fix proposed, not applied; with only one of the two servers connected it says there is nothing to compare and stops |
| `suite_access_review(stale_days, scope, servers)` | C63 | ise, catc, fmc, catsdwan, cml, win | read | Who can change what: admin accounts, roles and the external groups granting them on each platform, AD resolving those groups to people; one table by person and a findings list (generic/extra local admins, stale or AD-disabled people still privileged, broad groups, no local fallback). Leg tools still to ship (pending): `suite.access_review.PENDING_TOOLS` |
| `suite_decommission_site(site, subnets, dns_zone, servers)` | C52 | catsdwan, catc, ise, fmc, win | dangerous | Remove one site everywhere: inventory (WAN edges and their config group, Catalyst Center site, devices, fabric site and pool reservations, ISE NADs and the location group, FMC objects for the site's subnets, Windows DHCP scope and DNS records), then a removal preview per product, each product removed only after the user approves its preview, then a verify pass that must come back empty. Controllers are never removed; FMC objects still referenced are listed, not deleted. Windows has no DHCP-scope delete tool: the prompt writes its own preview and uses `win_run_powershell_json`. Leg tools still to ship (pending): `suite.decommission_site.PENDING_TOOLS` (`catsdwan_remove_device`, in cat-sdwan 0.7.0; an older server falls back to dissociating and invalidating the edge) |
| `suite_maintenance_window(site, sdwan_version, catc_image, ise_patch, mode, window, servers)` | C51 | catsdwan, catc, ise | dangerous | One maintenance window at a site: inventory (WAN edge software and the repository's release ceiling, Catalyst Center running vs golden image, ISE patch level per node, deployment health, last backup), one plan (order ISE checks -> WAN edges -> switches, blast radius, rollback, go/no-go), `<prefix>_snapshot` pre, then per product its preview-first upgrade tool (`catsdwan_upgrade_software` one edge per call; `catc_distribute_image` then `catc_activate_image` one device at a time, access switches first; ISE: `ise_create_backup`, the patch installs from the ISE GUI since there is no API), a `<prefix>_compare_snapshot` after each product and on every leg at the end, one report. mode=stage copies and installs images only (no reload) |

Planned: later waves C46, C47, C49, C50.

## How a suite prompt works

- **Legs.** Each prompt is made of legs, one per peer server (`SuiteLeg(peer, purpose, tools)`). The peers are
  published in the prompt's `meta` as `requires` (and `uses`, which clients and the prompt test kit already
  read), with `"suite": true` and every leg tool under `tools`.
- **Absent servers.** A server cannot see which other servers the client has connected, so every suite prompt
  takes a `servers` argument: comma-separated prefixes or server names (`catsdwan,ise`,
  `cat-sdwan-mcp-server, ise-mcp-server`; the Windows MCP's `Windows_Server` is the `win` peer).
  - Given: the body holds only the connected legs. Its first lines name every skipped leg and what is
    therefore not checked (`catc leg skipped: server not connected; not checked: device and site health.`),
    and the model repeats that at the top of its answer.
  - Empty: the body lists every leg and tells the model to check which `<prefix>_*` tools it has, skip the
    other legs and start its answer with one line per skipped leg.
- **Tiers.** A prompt that needs write tools sets `tier=Tier.WRITE` (or `DANGEROUS`) and is hidden on a server
  where that tier is off, like the server's own prompts.
- **Pending leg tools.** A leg may call a tool its server has not shipped yet (the suite prompt lands in core
  first, the small read tool on its server later). The prompt lists it in `@suite_prompt(pending=...)` (the
  module's `PENDING_TOOLS`); `suite.pending_tools()` is the registry across the pack. Every server's
  `assert_suite_pack` waives these on its own: missing, the leg counts as skipped (the prompt's own text
  tells the model to skip a leg whose tool it does not have), not a failure; present, nothing is reported
  either. So a new suite prompt, or a peer shipping its tool, never blocks a server's core pin bump. Core
  drops a tool from `PENDING_TOOLS` once every server's committed manifest of that peer lists it (core's
  local `test_against_sibling_manifests` tests say when).

## Registering the pack (every server)

```python
from stutters_mcp_core import suite          # <pkg>._core.suite in a bundled server (ADR-005)

def create_server(settings, *, auth=None):
    return build_server(SPEC, settings, ..., register=[system.register, ..., suite.register])
```

Then, in the server's prompt tests (all tiers on):

```python
from stutters_mcp_core.testing import assert_suite_pack

async def test_suite_pack():
    # Every pack prompt is registered; this server's own leg tools exist; every peer leg tool exists in
    # the committed manifest of that peer (tests/manifests/<peer>.json). Peers without a manifest are
    # not checked. The pack's own pending leg tools (suite.pending_tools()) are waived without being
    # listed. `pending` adds this server's own waivers, e.g. a peer tool its committed copy of that peer's
    # manifest predates; a waiver for a tool that now exists fails, so the list shrinks as you refresh.
    await assert_suite_pack(server_rw, "ise", "tests/manifests", pending=["fmc_snapshot"])
```

The existing kit adapts on its own: `assert_prompt_contract` accepts `suite_*` names and checks their `uses`
against the suite peers instead of `known_prefixes`, and leaves their leg tools to `assert_suite_pack`;
`assert_inbound_references` ignores suite prompts in other servers' manifests (core owns them); `tools --json`
marks them `"suite": true`. Registering the pack adds one snapshot file per suite prompt to
`assert_prompt_snapshots`; commit them.

## Adopting a new suite prompt (every server, on a core pin bump)

A core release that adds a suite prompt (or changes one) changes what each server registers, so after
bumping the core pin (`[tool.bundle-core]` tag and commit in `pyproject.toml`, then
`python3 scripts/bundle_core.py vendor` in a bundled server):

1. **Regenerate this server's manifest.** Its prompt list now has the new `suite_*` name, so
   `test_manifest_of_this_server_is_current` / `test_manifest_is_current` fail until you commit it:
   `uv run <server> tools --json --out tests/manifests/<prefix>.json`.
2. **Accept the new prompt snapshots.** `UPDATE_PROMPT_SNAPSHOTS=1 uv run pytest tests/unit -k snapshot`
   writes `tests/snapshots/prompts*/suite_<workflow>.md` (one per server configuration); read and commit them.
3. **Run `assert_suite_pack`.** Leg tools the prompt marks pending need nothing. A leg tool reported missing
   from a peer's manifest means your committed copy of that peer is older than the peer's release: copy the
   peer's current `tests/manifests/<peer>.json` from its repo (or regenerate it from a checkout), or, until
   then, add the tool to `pending=`. A tool of *this* server reported missing is a real gap: ship it, or ask
   core to mark it pending.
4. Optionally refresh the other servers' manifests under `tests/manifests/` at the same time, so cross-server
   prompt checks see their newest tools.

## Adding a suite prompt (core)

Put it in a module under `src/stutters_mcp_core/suite/` and import that module at the bottom of
`suite/__init__.py`:

```python
LEGS = (
    SuiteLeg("catsdwan", "BFD sessions and app-route SLA", ("catsdwan_bfd_sessions",)),
    SuiteLeg("ise", "authentication failures per NAD", ("ise_recent_auth_failures",)),
)

@suite_prompt(title="Site health across products", legs=LEGS, example={"site": "100"})
def suite_site_health(
    site: Annotated[str, Field(description="Site id or name")],
    servers: SERVERS_ARG = "",
) -> str:
    """One site's health across SD-WAN and ISE, skipping the products whose server is not connected."""
    plan = plan_legs(LEGS, servers)
    steps = []
    if plan.has("catsdwan"):
        steps.append(f"Call {tool_ref('catsdwan_bfd_sessions')} for site {site}.")
    if plan.has("ise"):
        steps.append(f"Call {tool_ref('ise_recent_auth_failures')} for the site's NADs.")
    return plan.header + "\n\n" + numbered(steps)
```

Rules (checked by `suite.pack_problems()` in core's unit tests):

- Don't use `from __future__ import annotations` in a pack module: FastMCP resolves the prompt's argument
  types at render time and cannot resolve the `SERVERS_ARG` alias from a string.
- Name `suite_<workflow>`; a 30–1500 character docstring; every argument has `Field(description=...)`;
  a `servers: SERVERS_ARG = ""` argument.
- Peers are keys of `suite.PEERS`; every tool a leg lists has that leg's prefix; every backticked tool in the
  body belongs to a declared leg.
- Rendered with each peer alone, the body calls no tool of a skipped leg and names every skipped leg
  (`plan.header` does that; keep each leg's steps behind `plan.has(peer)`).
- A leg tool no server has shipped yet goes in `pending=` (the module's `PENDING_TOOLS`); it must be one of
  the legs' tools. Keep the leg useful without it (a fallback step, or say what is not checked).
- Add a row to the catalogue above.
