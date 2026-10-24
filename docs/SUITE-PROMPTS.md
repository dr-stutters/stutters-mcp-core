# Suite prompt pack (`suite_*`)

Some workflows span several products and have no natural owner: a change check across SD-WAN, ISE, Catalyst
Center and FMC, a maintenance window, certificate expiry, a site decommission. If one server owned such a
prompt it would be missing wherever that server is not deployed. So core ships these prompts as the **suite
pack** (roadmap C48), and **every server registers the whole pack**. A client connected to several servers
sees the same `suite_*` prompt from each one; any copy renders the same text.

## Catalogue

| Prompt | Item | Legs (peers) | Tier | What it does |
|---|---|---|---|---|
| _(none yet)_ | | | | C53 adds `suite_change_check`; C51 `suite_maintenance_window`; later waves C46, C47, C49, C50, C52 |

## How a suite prompt works

- **Legs.** Each prompt is made of legs, one per peer server (`SuiteLeg(peer, purpose, tools)`). The peers are
  published in the prompt's `meta` as `requires` (and `uses`, which clients and the prompt test kit already
  read), with `"suite": true` and every leg tool under `tools`.
- **Absent servers.** A server cannot see which other servers the client has connected, so every suite prompt
  takes a `servers` argument: comma-separated prefixes or server names (`catsdwan,ise`,
  `cat-sdwan-mcp-server, ise-mcp-server`).
  - Given: the body holds only the connected legs. Its first lines name every skipped leg and what is
    therefore not checked (`catc leg skipped: server not connected; not checked: device and site health.`),
    and the model repeats that at the top of its answer.
  - Empty: the body lists every leg and tells the model to check which `<prefix>_*` tools it has, skip the
    other legs and start its answer with one line per skipped leg.
- **Tiers.** A prompt that needs write tools sets `tier=Tier.WRITE` (or `DANGEROUS`) and is hidden on a server
  where that tier is off, like the server's own prompts.

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
    # not checked. `pending` waives leg tools a peer has not shipped yet; a waiver for a tool that now
    # exists fails, so the list shrinks as peers ship.
    await assert_suite_pack(server_rw, "ise", "tests/manifests", pending=["fmc_snapshot"])
```

The existing kit adapts on its own: `assert_prompt_contract` accepts `suite_*` names and checks their `uses`
against the suite peers instead of `known_prefixes`, and leaves their leg tools to `assert_suite_pack`;
`assert_inbound_references` ignores suite prompts in other servers' manifests (core owns them); `tools --json`
marks them `"suite": true`. Registering the pack adds one snapshot file per suite prompt to
`assert_prompt_snapshots`; commit them.

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

- Name `suite_<workflow>`; a 30–1500 character docstring; every argument has `Field(description=...)`;
  a `servers: SERVERS_ARG = ""` argument.
- Peers are keys of `suite.PEERS`; every tool a leg lists has that leg's prefix; every backticked tool in the
  body belongs to a declared leg.
- Rendered with each peer alone, the body calls no tool of a skipped leg and names every skipped leg
  (`plan.header` does that; keep each leg's steps behind `plan.has(peer)`).
- Add a row to the catalogue above.
