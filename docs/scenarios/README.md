# Scenarios

A **scenario** strings two or three prompts from different suite servers into one end-to-end story (roadmap
C23, [ROADMAP.md](../../ROADMAP.md#how-to-approach-the-cross-architecture-piece)). There is no orchestrator
server: each prompt stays in the server that owns it, and the model runs them in order.

A scenario has five parts:

1. **An epic issue** in this repo (`C#`, label `scenario`) with a checklist of the per-server issues and lab
   tasks it needs. The epic closes when the live run passes and the evidence is attached.
2. **A scenario manifest**, `docs/scenarios/<name>.yaml` (this directory): the servers, the ordered steps and
   their hand-offs, and what the lab must provide (`requires:`). CI checks it against the servers' tool
   manifests, so a renamed or removed tool or prompt breaks the build, not the demo.
3. **A CML recipe** (existing or new) and a **lab runbook** in the private lab repo (`scenarios/<name>/`:
   seed, run, verify, reset), driven by `mcp_call.py`. Its preflight reads the manifest's `requires:` block.
4. **A public talk track**, `docs/scenarios/<name>.md`: the story, the prompts in order, the expected tool
   sequence, what to show at each step, the proof. Lab-agnostic: placeholders and documentation addresses
   (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, `example.com`), never lab names or addresses.
5. **Evidence** from the live run (tool output, previews, device CLI), attached to the epic.

## Catalogue

| Scenario | Epic | Manifest | Opening prompt (owner) | Servers | Lab |
|---|---|---|---|---|---|
| Incident, find and contain a host | [C26](https://github.com/dr-stutters/stutters-mcp-core/issues/73) | [incident.yaml](incident.yaml) | `ise_incident_response` (ise) | ise, catc, fmc | recipes `ise-fmc-identity` + `catc-routers` |
| Zero-touch site buildout | [C27](https://github.com/dr-stutters/stutters-mcp-core/issues/74) | [zero-touch-site.yaml](zero-touch-site.yaml) | `catc_new_site_buildout` (catc) | catc, ise | PnP lab |
| Secure branch | [C24](https://github.com/dr-stutters/stutters-mcp-core/issues/71) | [secure-branch.yaml](secure-branch.yaml) | `catc_new_site_buildout` (catc), closes with `fmc_prove_policy` | catc, ise, fmc, cml | recipe `secure-branch` |
| User-based firewall | [C25](https://github.com/dr-stutters/stutters-mcp-core/issues/72) | [user-firewall.yaml](user-firewall.yaml) | `ise_employee_onboarding` (ise) | Windows Server MCP, ise, fmc | recipe `ise-fmc-identity` |
| Edge security and FTD service insertion | [C55](https://github.com/dr-stutters/stutters-mcp-core/issues/104) | [edge-security.yaml](edge-security.yaml) | `catsdwan_trace_through_firewall` (catsdwan), audited with `suite_segmentation_audit` (suite) | catsdwan, fmc, ise, catc, cml | recipe `cat-sdwan-secure` |

Names a server has not shipped yet sit in each manifest's `pending:` list; the checker reports them once
they exist, so the list shrinks as the epic's checklist is ticked. A `suite_*` prompt step (core's suite pack,
registered on every server) uses the pseudo-server `suite`: list it in `servers` and in `no_manifest`; its
`calls` are still checked against each peer's manifest.

## Manifest template

```yaml
scenario: 1                          # manifest format version
name: my-scenario                    # lowercase, '-' or '_'; matches the file and the talk track
title: What the audience sees
epic: 0                              # the stutters-mcp-core epic issue number
servers: [ise, fmc]                  # tool prefixes taking part (every step's prefix must be here)
no_manifest: []                      # servers with no `tools --json` manifest yet: their steps aren't checked
pending: []                          # tools or prompts known to be missing (not shipped yet)
steps:                               # in order; each is `prompt:` or `tool:`
  - prompt: ise_some_workflow
    calls: [ise_endpoint_360, fmc_find_address]   # tools the prompt is expected to drive
    leaves: [endpoint_ip]            # hand-off: what this step leaves behind
  - tool: fmc_contain_host
    needs: [endpoint_ip]             # must be left by an earlier step
    note: free text for the talk track
requires:                            # what the lab must provide; the lab repo's preflight reads it
  recipes: [my-recipe]               # CML recipes to build
  labs: []                           # standing labs that must already exist
  ram_gb: 32                         # CML RAM the run needs
  cml_licences: 12                   # running nodes counted against the licence
  fmc_device_slots: 1                # FTDs the FMC must be able to register
  fmc_eval_days: 7                   # evaluation days that must be left
  ise_eval_days: 7
  seed_objects: [mcp-test-endpoint]  # objects the seed step creates (names start mcp-)
```

Unknown keys, wrong types and a malformed `requires:` are errors, so a typo cannot hide a step.

## What the checker reports

`stutters_mcp_core.testing.scenario` (exported from `stutters_mcp_core.testing`):

- a step's server is not in `servers`, or has no committed manifest (and is not in `no_manifest`);
- a prompt step names no prompt in that server's manifest, a tool step no tool (or the wrong kind);
- a `calls` entry the manifest does not list, or that the prompt no longer references (when the manifest
  says the prompt rendered);
- a prompt whose `uses` names a server the scenario leaves out;
- a `needs` entry no earlier step `leaves`;
- a `pending` name that exists now or that no step uses, and a `no_manifest` server that has a manifest now.

Run it in the server repo that owns the opening prompt, where the peers' manifests are committed in
`tests/manifests/` (copy the scenario manifest to `tests/scenarios/`):

```python
from stutters_mcp_core.testing import assert_scenarios

async def test_scenarios() -> None:
    server = create_server(settings(writes=True, dangerous=True))   # this server's own surface, all tiers on
    await assert_scenarios("tests/scenarios", manifests="tests/manifests", server=server, prefix="ise")
```

Lower-level helpers: `load_scenario(path)`, `scenario_problems(scenario, manifests)` and
`scenario_file_problems(paths, manifests)` return the problems as strings instead of failing.
