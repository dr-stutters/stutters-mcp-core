# Suite roadmap (stutters-mcp-core + the four servers)

_Last updated 2026-10-17: **Wave 5 planned**: four cross-architecture scenario epics (C24–C27) and the scenario framework (C23); target core v0.15.0; suite board [MCP Suite roadmap](https://github.com/users/dr-stutters/projects/1). Current release v0.14.1. Earlier: 2026-10-15 (overnight 2): core v0.12.0. Production gate done in core except G6 (needs older product versions) and the owner-side parts of G9; Later C12 done; new items C13–C17 done. Servers are pull-only (ADR-003)._

This is the suite-level roadmap: the shared foundation, cross-cutting themes and the production gate.
Each server has its own `ROADMAP.md`:
[cml](https://github.com/dr-stutters/cml-mcp-server/blob/main/ROADMAP.md) ·
[ise](https://github.com/dr-stutters/ise-mcp-server/blob/main/ROADMAP.md) ·
[catc](https://github.com/dr-stutters/catc-mcp-server/blob/main/ROADMAP.md) ·
[fmc](https://github.com/dr-stutters/fmc-mcp-server/blob/main/ROADMAP.md).
Every item is also a GitHub issue under the matching milestone.

## Scope and principles

| | |
|---|---|
| **Audiences** | Labs (dCloud/CML) → customer PoCs and demos → production networks → public open source |
| **Buckets** | **Now** (next 1–2 releases) · **Next** · **Later** · **Production gate** (prerequisite for production or public use) |
| **Priorities** | 1. Depth per product · 2. On-demand log and event lookup (tools read each platform's own logs, events and history when asked) · 3. Cross-product workflows (as MCP prompts) |
| **Logs and events** | **Pull-only, on demand** ([ADR-003](docs/adr/003-no-event-ingestion.md)): tools query the platforms' logs, events and history when called. No receivers, background pollers, buffers or relays; the servers never accept pushed data. |
| **Workflows** | MCP **prompts shipped in each server**. No orchestrator service. Claude chains the servers. |
| **Out of scope** | Other products (NetBox, Splunk, Windows, WLC, SD-WAN...); a shared message bus; an orchestrator server. Revisit after Later. |

Sizes: **S** ≤ 1 day of agent work · **M** 1–3 days · **L** > 3 days or needs new infrastructure or lab capability.

## Core roadmap

### Now
| # | Item | Why | External needs | Size |
|---|---|---|---|---|
| [C1](https://github.com/dr-stutters/stutters-mcp-core/issues/6) | **Bump all servers to core v0.3.0** (each server's Phase 3 builder bumps its pin) | `list_result` keeps `Page.more`; CatC lists without a total stop looking complete | none | S |
| ✅ [C2](https://github.com/dr-stutters/stutters-mcp-core/issues/7) | **MCP prompts support** (✅ v0.2.0): `PromptRegistrar` with naming (`<prefix>_<workflow>`), argument docs, and a contract-linter extension (description, arguments, which servers a prompt expects) | Workflows ship as prompts in each server | none | S |
| ❌ [C3](https://github.com/dr-stutters/stutters-mcp-core/issues/8) | ~~Event framework~~: shipped in v0.2.0, **removed in v0.3.0** (ADR-003: pull-only) | — | — | — |
| ❌ [C4](https://github.com/dr-stutters/stutters-mcp-core/issues/9) | ~~Inbound webhook/syslog receivers~~: shipped in v0.2.0, **removed in v0.3.0** (ADR-003) | — | — | — |
| ✅ [C5](https://github.com/dr-stutters/stutters-mcp-core/issues/10) | **`fetch_page(max_page=...)` override** (deferred request from CatC) (✅ v0.2.0) | Per-endpoint page caps without several pagers | none | S |

### Next
| # | Item | Why | External needs | Size |
|---|---|---|---|---|
| ❌ [C8](https://github.com/dr-stutters/stutters-mcp-core/issues/13) | ~~XML helper in core~~: not planned (2026-10-15). Only ISE parses XML (MnT); CML, FMC and CatC are JSON-only. ISE keeps `xmlutil.py` | Avoid duplicates | none | S |
| ✅ [C9](https://github.com/dr-stutters/stutters-mcp-core/issues/14) | **Prompt test kit** (✅ v0.4.0): `<server> tools --json` manifests, cross-server reference checks both ways, prompt snapshots | Prompts rot silently otherwise | none | S |
| ✅ [C13](https://github.com/dr-stutters/stutters-mcp-core/issues/47) | **Audit log rotation** (✅ v0.11.0): `MCP_AUDIT_LOG_MAX_MB` / `_BACKUPS` | A file audit log grew without bound on hosted servers | none | S |
| ✅ [C14](https://github.com/dr-stutters/stutters-mcp-core/issues/48) | **Runtime session tokens masked automatically** (✅ v0.11.0) | G4 masked configured secrets only | none | S |
| ✅ [C15](https://github.com/dr-stutters/stutters-mcp-core/issues/49) | **Per-instance write rules** (✅ v0.11.0): `<P>_<NAME>_READ_ONLY/TWO_PERSON/CHANGE_WINDOW/MAX_BLAST_RADIUS` | Read-only production next to a lab in one server (ADR-004 follow-up) | none | M |
| ✅ [C16](https://github.com/dr-stutters/stutters-mcp-core/issues/50) | **Per-caller rate limit** (✅ v0.11.0): `MCP_CALLER_RATE_LIMIT` (+ by role) | One client or leaked token could monopolise the appliance | none | S |
| ✅ [C17](https://github.com/dr-stutters/stutters-mcp-core/issues/53) | **`<server> config`** (✅ v0.12.0): effective settings with sources, secrets set/unset only | ~60 settings and three secret sources were hard to verify on a container | none | S |

### Later
_C6, C7, C10, C11 (event persistence, correlation, notifications, fan-out) were dropped with ADR-003._

| # | Item | Why | External needs | Size |
|---|---|---|---|---|
| ✅ [C12](https://github.com/dr-stutters/stutters-mcp-core/issues/17) | Long-running task registry (MCP tasks) for deploys, discoveries and SWIM (✅ v0.10.0: `task=True` + `MCP_TASKS`, fastmcp-tasks) | Better UX than wait/poll for 10+ minute jobs | Client support | M |

## Cross-product workflow prompts (catalogue)

Each prompt lives in the server that **starts** the workflow and names the other servers it expects.
The model carries out the steps with each server's tools; every write step stays preview-first.

| Prompt | Owner | Uses | Bucket |
|---|---|---|---|
| `cml_build_test_lab` (FTD / routers / access-switch recipes) | cml | cml | Now |
| `ise_troubleshoot_endpoint_access` (MAC or user → sessions, failure reason, policy hit, switch port) | ise | ise, cml or catc (for device CLI) | Now |
| `fmc_why_blocked` (src/dst/port → rule lookup, packet tracer, hit counts, recent deploys) | fmc | fmc | Now |
| `catc_onboard_device` (credentials → discovery → site → provision → verify) | catc | catc | Now |
| `catc_onboard_switch_with_nac` (CatC onboarding + ISE NAD + AAA settings + first auth check) | catc | catc, ise | Next |
| `ise_quarantine_host` (ANC + CoA, then FMC dynamic object/rule if needed, then verify) | ise | ise, fmc | Next |
| `fmc_onboard_ftd` (CML FTDv recipe or existing FTD → register → policy → deploy → verify) | fmc | fmc, cml | Next |
| `catc_change_window` (template preview → deploy → compliance → rollback plan) | catc | catc | Next |
| `cml_replicate_site` (read CatC/ISE/FMC config for a site → build a CML replica) | cml | cml, catc, ise, fmc | Later |
| `ise_incident_timeline` (ISE auth history + FMC audit/health + CatC events/issues for a host, each queried on demand, merged by time) | ise | ise, fmc, catc | Later |
| `ise_incident_response` (indicator → find across ISE, CatC and FMC → timeline → contain → verify → release; scenario [C26](https://github.com/dr-stutters/stutters-mcp-core/issues/73)) | ise | ise, catc, fmc | Now |
| `catc_new_site_buildout` from a site spec (`catc_plan_site` → `ise_plan_site` + policy bundle → PnP or discovery → provision → verify; scenario [C27](https://github.com/dr-stutters/stutters-mcp-core/issues/74)) | catc | catc, ise | Now |
| `catc_new_site_buildout` → `ise_onboard_nad` → `fmc_onboard_branch` → `fmc_prove_policy` (scenario [C24](https://github.com/dr-stutters/stutters-mcp-core/issues/71) Secure branch) | catc, ise, fmc | catc, ise, fmc, cml | Next |
| `ise_employee_onboarding` → `fmc_prove_user_policy` → `ise_employee_offboarding` (scenario [C25](https://github.com/dr-stutters/stutters-mcp-core/issues/72) User-based firewall; AD through the existing `Windows_Server` MCP) | ise, fmc | ise, fmc | Next |

## Production gate

**Nothing goes to production networks or public release until every item below is done** for core and that server.
Labs and PoCs may use the servers before then.

| # | Item | Notes / external needs | Size |
|---|---|---|---|
| ✅ [G1](https://github.com/dr-stutters/stutters-mcp-core/issues/18) | **OAuth 2.1 for HTTP** (FastMCP auth; GitHub or customer IdP), keeping named bearer tokens for automation (✅ v0.8.0: GitHub/OIDC via FastMCP OAuth proxy + named tokens; app registration steps in docs/PRODUCTION.md (owner)) | An IdP app registration; public HTTPS if used with Claude Desktop connectors | M |
| ✅ [G2](https://github.com/dr-stutters/stutters-mcp-core/issues/19) | **Per-user roles** (`ro` / `rw` / `admin`) mapped to tiers; the identity shows in previews and the audit log (✅ v0.6.0) | — | M |
| ✅ [G3](https://github.com/dr-stutters/stutters-mcp-core/issues/20) | **Least-privilege account guides** per product, tested: ISE custom admin roles (ERS operator, MnT), FMC custom user role, CatC custom role (RBAC), CML non-admin (✅ core part v0.8.1: docs/least-privilege-template.md + `testing.least_privilege`; per-product guides are server items) | Customer creates the accounts | M |
| ✅ [G4](https://github.com/dr-stutters/stutters-mcp-core/issues/21) | **Secrets**: `*_PASSWORD_FILE` / Docker secrets everywhere (✅ core `<VAR>_FILE` for every secret setting, v0.4.0; servers' compose files still to adopt); no secrets in env dumps; optional Vault/1Password loader (✅ v0.7.0: `<VAR>_FILE`/`_REF` loaders, Docker-secrets compose override, scrubbing in logs/status/errors) | A secret store (optional) | S |
| ✅ [G5](https://github.com/dr-stutters/stutters-mcp-core/issues/22) | **Observability**: structured logs, `/metrics` (Prometheus: calls, errors, latency, upstream status, rate-limit waits), request ids end to end (✅ v0.7.0: `/metrics` (bearer or loopback listener)) | A Prometheus scraper (optional) | S |
| ⏸ [G6](https://github.com/dr-stutters/stutters-mcp-core/issues/23) | **Support matrix + compatibility tests**: at least two versions per product (e.g. ISE 3.3/3.4/3.5, FMC 7.4/7.6/10.0, CatC 2.3.7/3.1, CML 2.8–2.10) (skipped overnight 2: needs older product versions) | Labs on each version (dCloud/CML/customer) | L |
| ✅ [G7](https://github.com/dr-stutters/stutters-mcp-core/issues/24) | **Safety rails for production**: dry-run-only mode, change-window guard (`MCP_CHANGE_WINDOW`), per-tool deny-list, max blast radius (devices per deploy), two-person confirm (optional) (✅ v0.5.0) | — | M |
| ✅ [G8](https://github.com/dr-stutters/stutters-mcp-core/issues/25) | **Security review + dependency/SBOM scanning** in CI (pip-audit, Trivy on images), signed images (cosign) (✅ v0.7.0/v0.8.1: pip-audit, Trivy, SBOMs; cosign ready, off pending the key decision) | GitHub Actions | S |
| [G9](https://github.com/dr-stutters/stutters-mcp-core/issues/26) | **Public release checklist**: scrub history for lab data, docs site, CONTRIBUTING/SECURITY.md, PyPI trusted publishing, drop `CORE_READ_TOKEN` (groundwork ✅ v0.10.0: SECURITY/CONTRIBUTING, docs/PUBLIC-RELEASE.md, `scrub_check`; history rewrite, PyPI, visibility need the owner) | PyPI account | M |
| ✅ [G10](https://github.com/dr-stutters/stutters-mcp-core/issues/27) | **Multi-instance support** (several ISE/FMC/CatC targets per server, selected per call) (✅ v0.9.0, ADR-004: `<P>_INSTANCES` + `instance` argument) | — | M |

## Prerequisites to unblock (ranked by items unblocked)

Compiled from the four server roadmaps (2026-10-15). Items in **Now** need none of these; they run on today's lab.

| # | Prerequisite | Who / effort | Unblocks |
|---|---|---|---|
| P2 | **ISE pxGrid persona** for the Catalyst Center ↔ ISE integration (appliance to appliance; our servers don't subscribe to pxGrid) | ISE UI/API toggle (lab) | catc D8, D9, D13 |
| ✅ P3 | **CML licence: DONE 2026-10-15.** Enterprise, 70 running nodes, multi-user | — | Unblocked: cml L15, L17; capacity for cml L10/L11/L13/L14, catc D13/D15, fmc F10/F11 |
| P4 | **Windows lab server (AD + CA)** reachable and configured (the `Windows_Server` MCP host is still a placeholder) | Point at a dCloud/CML Windows Server | ise I8 (cert lifecycle), I11 (AD join), cml L11/L13 (EAP-TLS clients); optional CA for P2 |
| P5 | **FMC UI-only setup**: a dedicated API user with a custom role | FMC UI (manual) | fmc F16 (gate) |
| P6 | **SWIM image**: a `c8000v-universalk9.<other-ver>.SPA.bin` from cisco.com (entitled account), plus an HTTP server CatC can reach | cisco.com download + a small web server | catc D12 |
| P7 | **Older product versions** for the support matrix: CML 2.7–2.9, ISE 3.2–3.4, FMC 7.4/7.6, CatC 2.3.7/3.1 | dCloud labs or customer labs | Production gate: cml L18, ise I17, catc D18, fmc F17 (core G6) |
| P8 | **Quick lab toggles**: SXP service on ISE | ISE UI/API (lab) | ise I9 |
| P9 | **Larger environments**: multi-node ISE (PAN/MnT/PSN) | dCloud lab | ise I19 (gate) |
| P10 | **One-offs**: physical AP (catc D14), ServiceNow instance (catc D16), SCC tenant for cdFMC (fmc F15), a Windows client with Secure Client for posture (ise I14), a self-hosted GitHub runner on cortex (cml L12) | Various | One item each |
| P11 | **Domain-joinable Windows client VM** in the lab (the user will build it) | User | ise I32 posture, I33 machine+user EAP-TLS, I34 PassiveID |

Time-limited: the FMC Smart Licensing evaluation expires ≈2027-01-12; the ISE evaluation licence (Advantage, needed for pxGrid) also expires.

## External services: suite summary

| Service | Needed for | Provided by | Network path | Status |
|---|---|---|---|---|
| CML controller (Enterprise: 70 running nodes) | All device testing | Lab | cortex → 198.18.128.10 | ✅ in use |
| ISE pxGrid persona | Catalyst Center ↔ ISE integration (appliance to appliance) | ISE node (currently OFF) | CatC → ISE :8910 | ⏳ catc Next (P2) |
| FTDv / FMCv / cat8000v / cat9000v images | Lab recipes | CML image library | — | ✅ present |
| Device images (.bin) for SWIM | CatC SWIM live tests | cisco.com download | upload to CatC | ❌ missing |
| Windows AD + CA | ISE AD identity, EAP-TLS, pxGrid certs | Existing Windows lab server (`Windows_Server` MCP) | ISE ↔ AD | ⏳ when needed |
| IdP (OAuth) | G1 | GitHub / customer IdP | Internet | Production gate |
| Public HTTPS ingress (Caddy/VPS or tunnel) | Claude Desktop connectors | VPS / tunnel provider | Internet → server | Production gate (optional) |

## Wave 3 (planned 2026-10-16)

Driven by the real AD + CA (`mitchcloud.lab`, `mitchcloud-CA`, DC01 198.18.130.11). Scope: the four servers + core, plus a new
**Windows server**. NetBox stays out. Every item is a GitHub issue labelled `wave-3`; blocked items sit in **Later** with
the blocker named.

| Theme | Items | Order |
|---|---|---|
| **A. User-based firewall, end to end** | cml [**L34**](https://github.com/dr-stutters/cml-mcp-server/issues/81) `ise-fmc-identity` recipe · ise [**I30**](https://github.com/dr-stutters/ise-mcp-server/issues/71) pxGrid client management + certs for FMC · fmc [**F34**](https://github.com/dr-stutters/fmc-mcp-server/issues/79) ISE as identity source (pxGrid) + on-demand user↔IP lookup; prove `mcp-bob` blocked / `mcp-alice` allowed *by user name* on real traffic | 1st, in parallel with B |
| **B. Windows server on the core** | core [**C18**](https://github.com/dr-stutters/stutters-mcp-core/issues/61) → new repo `win-mcp-server` (W1–W7: AD read, AD writes, DNS/DHCP, CA, least privilege + safety rails, DC build as a tool, quality pass; deploy on cortex :8036; replaces the old `Windows_Server` MCP and the ad-hoc WinRM scripts) | 1st, in parallel with A |
| **C. ISE guest + sponsor** | ise [**I31**](https://github.com/dr-stutters/ise-mcp-server/issues/72) guest types, sponsor groups on AD (`mcp-netadmins` as sponsors), sponsor + guest portals, guest accounts; a real portal flow from CML's `firefox` node | 2nd (after I30, same appliance) |
| **D. Cross-product workflow pass** | core [**C19**](https://github.com/dr-stutters/stutters-mcp-core/issues/62): run every cross-server prompt live end to end and fix what breaks; add `win_onboard_employee`, `win_offboard_user` (AD disable → ISE disconnect → FMC contain), `fmc_prove_user_policy` | 3rd (needs A + B) |
| **E. Blocked: Windows client VM** | ise [**I32**](https://github.com/dr-stutters/ise-mcp-server/issues/73) posture (also needs the Secure Client package) · ise [**I33**](https://github.com/dr-stutters/ise-mcp-server/issues/74) machine + user EAP-TLS with GPO auto-enrolment / TEAP chaining · ise [**I34**](https://github.com/dr-stutters/ise-mcp-server/issues/75) PassiveID from AD logins (WMI) → user mappings without 802.1X (also feeds FMC) | when the VM exists |

New prerequisite: **P11, a domain-joinable Windows client VM** in the lab (you'll build it later): unblocks I32–I34.

## Wave 4 (started 2026-10-16): standalone servers

| Item | Scope | Status |
|---|---|---|
| core [**C20**](https://github.com/dr-stutters/stutters-mcp-core/issues/63) | Bundle core into each server package (`<pkg>._core`, ADR-005): `scripts/bundle_core.py` + reusable `bundle-core.yml`; no git dependency, no `CORE_READ_TOKEN`. Adopted by ise I35, fmc F35, catc D31, cml L35 | in review |
| core [**C21**](https://github.com/dr-stutters/stutters-mcp-core/issues/66) | Shared `check` preflight (`stutters_mcp_core.preflight`): reachability per surface, TLS, auth, role vs tiers, version vs matrix; text/`--json`; `serve --http`. For ise I36, fmc F36, catc D32, cml L37 | in review |
| core [**C22**](https://github.com/dr-stutters/stutters-mcp-core/issues/67) | Reusable `publish-image.yml`: amd64+arm64 GHCR image, cosign keyless, SBOM attestation, `-rc` tags. For ise I37, fmc F37, catc D33, cml L36 | in review |


## Wave 5 (planned 2026-10-17): cross-architecture scenarios
Plan: `planning/WAVE5-DRAFT.md` in the private lab repo, approved 2026-10-17. Servers: [fmc](https://github.com/dr-stutters/fmc-mcp-server/blob/main/ROADMAP.md#wave-5-planned-2026-10-17-scenarios-depth-demos) (F54–F60, v0.14.0) ·
[catc](https://github.com/dr-stutters/catc-mcp-server/blob/main/ROADMAP.md#wave-5-planned-2026-10-17-scenarios-depth-demos) (D46–D51, v0.11.0) ·
[ise](https://github.com/dr-stutters/ise-mcp-server/blob/main/ROADMAP.md#wave-5-planned-2026-10-17-scenarios-depth-demos) (I49–I54, v0.12.0) ·
[cml](https://github.com/dr-stutters/cml-mcp-server/blob/main/ROADMAP.md#wave-5-planned-2026-10-17) (L39–L40, v0.11.0); core C23–C27 (v0.15.0).
**Suite board:** [MCP Suite roadmap](https://github.com/users/dr-stutters/projects/1): every open `roadmap` issue from the five repos, with `Server`, `Wave`, `Scenario` and `Blocker` fields; kept in sync by `scripts/project_sync.py` in the lab repo.

Scoping answers (2026-10-17): full Wave 5 for fmc, catc and ise; cml and core get only the items the scenarios need. Themes: operator workflows and prompts, API depth, demo/PoC storylines; production gate items stay where they are (F17/D18/I17 need older product versions, G9 waits on the owner). Cross architecture: end-to-end scenarios as MCP prompts in the owning server, each proven live in CML, tracked as epics here. No orchestrator server, NetBox stays out. Blockers the owner clears: a domain-joinable Windows client VM (P11), a `c8000v` `.bin` on an HTTP server CatC can reach (P6); the FMC side of pxGrid identity is fmc F34's work.

### How to approach the cross-architecture piece
The suite already has the building blocks: prompts live in the server that starts a workflow and name the other
servers they expect (C2), the prompt test kit checks cross-server references both ways (C9), and the lab repo has
`mcp_call.py` to drive any suite tool from a script. What was missing is the layer above a single prompt: a **scenario**
that strings two or three prompts together, with a lab recipe, seed and reset, evidence of a live run, and a talk track.

A scenario (C23) is:
1. **An epic issue in this repo** (`C#`, label `scenario`) with a checklist of the per-server issues it needs and the lab
   tasks. The epic is done when the live run passes and the evidence is attached.
2. **A sequence of prompts**, each owned by its server (no new server, no orchestrator). The model runs them in order;
   the epic names the order and the hand-offs (what prompt 1 must leave behind for prompt 2).
3. **A CML recipe** (existing or new) and a **lab runbook** in `stutters-mcp-lab/scenarios/<name>/` (seed, run, verify,
   reset), driven by `mcp_call.py`. Lab-specific, private.
4. **A public talk track** in `docs/scenarios/<name>.md`: the story, the prompts in order, the expected tool sequence,
   what to show at each step, the proof. Lab-agnostic (placeholders, no lab addresses).
5. **Evidence**: tool output and device CLI from the live run, attached to the epic.

Why this and not a suite server: the four scenarios need almost no new cross-product *tools*. They need a few missing
per-product reads and writes (find the host in CatC, live flows on the FTD, bulk NADs in ISE), the prompts that already
exist, and a harness that proves the chain works. A suite server would duplicate tool surface and break the standalone
packaging that Wave 4 delivered. If a cross-product *tool* ever proves necessary (one "find this host everywhere" call),
that is the moment to reconsider, and it would still be one small server on the core.

**Order:** C26 Incident first (nearly everything exists; a demo in days), then C27 Zero-touch site, then C24 Secure
branch (builds on C27 and the SD-WAN work), then C25 User-based firewall (gated on fmc F34, size L, run in parallel
from the start).

### Core and cml items
| # | Item | Why (operator value) | External needs | Test plan | Size | Milestone | Issue |
|---|---|---|---|---|---|---|---|
| [C23](https://github.com/dr-stutters/stutters-mcp-core/issues/70) | **Scenario framework**: the definition above; `docs/scenarios/README.md` (catalogue + template); `scenario` label; prompt test kit extension: a scenario manifest (ordered prompts, hand-offs, servers) checked against each server's `tools --json` so a renamed tool or prompt breaks CI, not the demo | Scenarios rot silently otherwise, like prompts did before C9 | none | Unit; the four manifests below pass | S | Now | [#70](https://github.com/dr-stutters/stutters-mcp-core/issues/70) |
| [C24](https://github.com/dr-stutters/stutters-mcp-core/issues/71) | **Scenario: Secure branch** (epic). Site from C27 exists → ISE NAD + wired policy for the branch (I45, I49, I50) → branch FTD from a device template joins the SD-WAN hub (F54 on F51d) → client authenticates, lands in its VLAN/SGT → traffic to the DC allowed/blocked per policy (F56) → WAN failover keeps the session (F51) → talk track | The full "new branch, secured" story in one lab, every step preview-first | recipe L39; fmc v0.13.0 SD-WAN on main | Live on `secure-branch`; evidence on the epic | M (epic) | Next | [#71](https://github.com/dr-stutters/stutters-mcp-core/issues/71) |
| [C25](https://github.com/dr-stutters/stutters-mcp-core/issues/72) | **Scenario: User-based firewall** (epic). AD user and group (`Windows_Server` MCP) → ISE 802.1X login (PEAP-AD, I11) → session to FMC over pxGrid (F34) → AD-group rule: alice allowed, bob blocked (F56) → offboard: AD disable → `ise` disconnect → FMC contain (C19's `win_offboard_user`, owned by ise as `ise_employee_offboarding` until C18 exists) → talk track | Identity policy proven on real traffic, by user name | F34 (L); C18 stays Later: the prompts use the existing `Windows_Server` MCP tools | Live on `ise-fmc-identity` (L34) | M (epic) | Next | [#72](https://github.com/dr-stutters/stutters-mcp-core/issues/72) |
| [C26](https://github.com/dr-stutters/stutters-mcp-core/issues/73) | **Scenario: Incident, find and contain a host** (epic). One indicator (MAC, IP or user) → `ise_endpoint_360` + `catc_find_host` (D47) + `fmc_find_address` (F59) + `fmc_live_connections` (F55) → `ise_incident_timeline` → contain: ANC + CoA (I10), FMC dynamic object (F9), optional port shutdown (D48) → verify on switch, FTD and ISE → release and report. Prompt: `ise_incident_response` (I53) | The SOC story; most tools exist, the gaps are three reads and one write | none | Live on `ise-fmc-identity` + `catc-routers`; evidence on the epic | S (epic) | Now | [#73](https://github.com/dr-stutters/stutters-mcp-core/issues/73) |
| [C27](https://github.com/dr-stutters/stutters-mcp-core/issues/74) | **Scenario: Zero-touch site buildout** (epic). One site spec (YAML: hierarchy, settings, pools, SSID, AAA, profile, devices) → `catc_plan_site` (D46) → ISE NDG + NADs (I49) + branch policy from a parametrised bundle (I50) → PnP claim or discovery → provision → verify (D44) → talk track. Prompt: `catc_new_site_buildout` extended to take the spec | Day-0 site build driven by data, repeatable for every branch | PnP lab `mcp-catc-pnp` (in place) | Live: build `mcp-test-site2` from a spec, tear down, rebuild | M (epic) | Now | [#74](https://github.com/dr-stutters/stutters-mcp-core/issues/74) |
| [C19](https://github.com/dr-stutters/stutters-mcp-core/issues/62) | *(carry, Next → Now)* **Cross-product workflow pass**: re-run every existing cross-server prompt live and fix what breaks; the new AD-era prompts move into C25 (owned by ise) and `fmc_prove_user_policy` into fmc F56 | Unchanged | the lab | as issue | M | Now | [#62](https://github.com/dr-stutters/stutters-mcp-core/issues/62) |
| [L39](https://github.com/dr-stutters/cml-mcp-server/issues/13) | cml **Recipe `secure-branch`**: hub FTDv (from `ftd-sdwan-single`'s hub) + one branch: FTDv spoke with two WAN transports through IOL carriers, cat9000v access switch, Alpine 802.1X client, inside Alpine server at the hub; `cml_verify_recipe` asserts (tunnel up, client authenticated, DC reachable) | C24 needs one lab with switch, firewall and identity together | ~30 GB RAM, 2 FTDv | `recipes-verify.yml`; live build and verify | M | Next | [#13](https://github.com/dr-stutters/cml-mcp-server/issues/13) |
| [L40](https://github.com/dr-stutters/cml-mcp-server/issues/14) | cml **Recipe `extends:`** (optional): a recipe can start from another recipe's nodes and links and add to them, so `secure-branch` and the SD-WAN labs share the hub definition | Three SD-WAN recipes already duplicate the hub block | none | Unit on the recipe loader; `secure-branch` built both ways gives the same topology | S | Later | [#14](https://github.com/dr-stutters/cml-mcp-server/issues/14) |

[C18](https://github.com/dr-stutters/stutters-mcp-core/issues/61) (win-mcp-server) stays **Later**: C25 runs on the existing `Windows_Server` MCP. Revisit after the wave. Carry-over: [C19](https://github.com/dr-stutters/stutters-mcp-core/issues/62) Next → Now.

### Scenario catalogue
| Epic | Owner prompt | Prompts in order | Servers | Recipe | Milestone |
|---|---|---|---|---|---|
| [C26](https://github.com/dr-stutters/stutters-mcp-core/issues/73) **Incident, find and contain a host** | ise `ise_incident_response` (I53) | `ise_incident_response` → (`ise_endpoint_360`, `catc_find_host`, `fmc_find_address`, `fmc_live_connections`, `ise_incident_timeline`, ANC + CoA, FMC contain, `catc_set_interface`) → release and report | ise, catc, fmc | `ise-fmc-identity` + `catc-routers` (in place) | Now |
| [C27](https://github.com/dr-stutters/stutters-mcp-core/issues/74) **Zero-touch site buildout** | catc `catc_new_site_buildout` (D44, from a site spec) | `catc_new_site_buildout` → (`catc_plan_site`, `ise_plan_site`, I50 bundle import, PnP claim or discovery, `catc_provision_site`) → verify | catc, ise | `mcp-catc-pnp` (in place) | Now |
| [C24](https://github.com/dr-stutters/stutters-mcp-core/issues/71) **Secure branch** | catc `catc_new_site_buildout` opens; fmc `fmc_prove_policy` closes | `catc_new_site_buildout` → `ise_onboard_nad` → `fmc_onboard_branch` → `fmc_prove_policy` | catc, ise, fmc, cml | `secure-branch` (L39) | Next |
| [C25](https://github.com/dr-stutters/stutters-mcp-core/issues/72) **User-based firewall** | ise `ise_employee_onboarding` / `ise_employee_offboarding` | `ise_employee_onboarding` → `fmc_prove_user_policy` → `ise_employee_offboarding` | ise, fmc, `Windows_Server` MCP | `ise-fmc-identity` (L34) | Next |

### Shared spec: one site description for CatC and ISE (D46, I49, I50, C27)
```yaml
site:
  name: mcp-test-site2            # CatC site name and ISE NDG leaf
  hierarchy: Global/mcp-test-lab/mcp-test-site2
  ndg: "Location#All Locations#mcp-test-lab#mcp-test-site2"
  address: "..."
network_settings: {dns: [...], ntp: [...], aaa: {ise: ise.example.com, secret_ref: LAB_RADIUS_SECRET}}
pools: [{name: site2-data, cidr: 10.2.10.0/24, gateway: 10.2.10.1, vlan: 210}]
vlans: {employee: 210, contractor: 220, guest: 230, quarantine: 299}
sgts: {employee: Employees, contractor: Contractors}
wireless: {ssid: mcp-site2-corp, profile: mcp-site2-wireless}
switching_profile: mcp-test-sw-profile
devices: [{name: sw2, serial: ..., role: access, port_template: access-8021x}]
policy_bundle: wired-8021x-mab   # I50 bundle stamped with the values above
```
Secrets are referenced by name (`secret_ref`), never written into the spec. Addresses and hostnames above are placeholders.

### Order and parallelism (for the agents)
1. **Now, no dependencies, in parallel per repo:** fmc F55, F59, F57, F46; catc D47, D49, D41; ise I44, I47, I52; core C23.
2. **C26 Incident** as soon as F55, F59, D47, D48 land: I53, then the live run and talk track.
3. **Site spec track:** D46 + I49 + I50 together (one shared schema, written first), then D44 extended, D50, and the **C27 Zero-touch** live run.
4. **Branch track:** fmc v0.13.0 cut → F41, F54, L39, I45 → **C24 Secure branch** live run.
5. **Identity track (long):** F34 from day one → F56 → **C25 User-based firewall** live run; I33/I34 when the P11 VM exists.
6. **Prompts and demos** fill in around the tracks: F49, D42, D43 (after D12), D45, I46, I48, F60, D51, I54.
7. Release each server once its Now items are merged: fmc v0.14.0, catc v0.11.0, ise v0.12.0, core v0.15.0, cml v0.11.0.

### Counts
| Repo | New items | Carried into Wave 5 | Now | Next | Later (unchanged) |
|---|---|---|---|---|---|
| fmc | 7 (F54–F60) | 6 (F34, F40, F41, F43, F46, F49) | 9 | 4 | F15, F17 |
| catc | 6 (D46–D51) | 6 (D12, D41–D45) | 11 | 1 | D13, D15, D16, D18 |
| ise | 6 (I49–I54) | 8 (I32–I34, I44–I48) | 11 | 3 | I17, I36 |
| core | 5 (C23–C27) | 1 (C19) | 4 | 2 | C18, G6, G9 |
| cml | 2 (L39–L40) | 0 | 0 | 1 | L40 |
