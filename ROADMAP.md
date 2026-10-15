# Suite roadmap (stutters-mcp-core + the four servers)

_Last updated 2026-10-15: event ingestion scrapped (ADR-003, core v0.3.0); servers are pull-only. Current releases: core v0.2.0, cml-mcp-server v0.3.0, fmc / catc / ise-mcp-server v0.2.0._

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
| [C8](https://github.com/dr-stutters/stutters-mcp-core/issues/13) | **XML helper in core** (if a second server needs it; ISE keeps `xmlutil.py` until then) | Avoid duplicates | none | S |
| [C9](https://github.com/dr-stutters/stutters-mcp-core/issues/14) | **Prompt test kit**: render every prompt, check its referenced tools exist on the target servers (by name/prefix), snapshot tests | Prompts rot silently otherwise | none | S |

### Later
_C6, C7, C10, C11 (event persistence, correlation, notifications, fan-out) were dropped with ADR-003._

| # | Item | Why | External needs | Size |
|---|---|---|---|---|
| [C12](https://github.com/dr-stutters/stutters-mcp-core/issues/17) | Long-running task registry (MCP tasks) for deploys, discoveries and SWIM | Better UX than wait/poll for 10+ minute jobs | Client support | M |

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

## Production gate

**Nothing goes to production networks or public release until every item below is done** for core and that server.
Labs and PoCs may use the servers before then.

| # | Item | Notes / external needs | Size |
|---|---|---|---|
| [G1](https://github.com/dr-stutters/stutters-mcp-core/issues/18) | **OAuth 2.1 for HTTP** (FastMCP auth; GitHub or customer IdP), keeping named bearer tokens for automation | An IdP app registration; public HTTPS if used with Claude Desktop connectors | M |
| [G2](https://github.com/dr-stutters/stutters-mcp-core/issues/19) | **Per-user roles** (`ro` / `rw` / `admin`) mapped to tiers; the identity shows in previews and the audit log | — | M |
| [G3](https://github.com/dr-stutters/stutters-mcp-core/issues/20) | **Least-privilege account guides** per product, tested: ISE custom admin roles (ERS operator, MnT), FMC custom user role, CatC custom role (RBAC), CML non-admin | Customer creates the accounts | M |
| [G4](https://github.com/dr-stutters/stutters-mcp-core/issues/21) | **Secrets**: `*_PASSWORD_FILE` / Docker secrets everywhere; no secrets in env dumps; optional Vault/1Password loader | A secret store (optional) | S |
| [G5](https://github.com/dr-stutters/stutters-mcp-core/issues/22) | **Observability**: structured logs, `/metrics` (Prometheus: calls, errors, latency, upstream status, rate-limit waits), request ids end to end | A Prometheus scraper (optional) | S |
| [G6](https://github.com/dr-stutters/stutters-mcp-core/issues/23) | **Support matrix + compatibility tests**: at least two versions per product (e.g. ISE 3.3/3.4/3.5, FMC 7.4/7.6/10.0, CatC 2.3.7/3.1, CML 2.8–2.10) | Labs on each version (dCloud/CML/customer) | L |
| [G7](https://github.com/dr-stutters/stutters-mcp-core/issues/24) | **Safety rails for production**: dry-run-only mode, change-window guard (`MCP_CHANGE_WINDOW`), per-tool deny-list, max blast radius (devices per deploy), two-person confirm (optional) | — | M |
| [G8](https://github.com/dr-stutters/stutters-mcp-core/issues/25) | **Security review + dependency/SBOM scanning** in CI (pip-audit, Trivy on images), signed images (cosign) | GitHub Actions | S |
| [G9](https://github.com/dr-stutters/stutters-mcp-core/issues/26) | **Public release checklist**: scrub history for lab data, docs site, CONTRIBUTING/SECURITY.md, PyPI trusted publishing, drop `CORE_READ_TOKEN` | PyPI account | M |
| G10 | **Multi-instance support** (several ISE/FMC/CatC targets per server, selected per call) | — | M |

## Prerequisites to unblock (ranked by items unblocked)

Compiled from the four server roadmaps (2026-10-15). Items in **Now** need none of these; they run on today's lab.

| # | Prerequisite | Who / effort | Unblocks |
|---|---|---|---|
| P2 | **ISE pxGrid persona** for the Catalyst Center ↔ ISE integration (appliance to appliance; our servers don't subscribe to pxGrid) | ISE UI/API toggle (lab) | catc D8, D9, D13 |
| P3 | **CML registered licence** (20 nodes + extra users) instead of EVAL (5 nodes) | Licence purchase/registration | cml L15, L17; relieves capacity limits on cml L10/L11/L13/L14, catc D13/D15, fmc F10/F11 |
| P4 | **Windows lab server (AD + CA)** reachable and configured (the `Windows_Server` MCP host is still a placeholder) | Point at a dCloud/CML Windows Server | ise I8 (cert lifecycle), I11 (AD join), cml L11/L13 (EAP-TLS clients); optional CA for P2 |
| P5 | **FMC UI-only setup**: a dedicated API user with a custom role | FMC UI (manual) | fmc F16 (gate) |
| P6 | **SWIM image**: a `c8000v-universalk9.<other-ver>.SPA.bin` from cisco.com (entitled account), plus an HTTP server CatC can reach | cisco.com download + a small web server | catc D12 |
| P7 | **Older product versions** for the support matrix: CML 2.7–2.9, ISE 3.2–3.4, FMC 7.4/7.6, CatC 2.3.7/3.1 | dCloud labs or customer labs | Production gate: cml L18, ise I17, catc D18, fmc F17 (core G6) |
| P8 | **Quick lab toggles**: SXP service on ISE | ISE UI/API (lab) | ise I9 |
| P9 | **Larger environments**: multi-node ISE (PAN/MnT/PSN) | dCloud lab | ise I19 (gate) |
| P10 | **One-offs**: physical AP (catc D14), ServiceNow instance (catc D16), SCC tenant for cdFMC (fmc F15), a Windows client with Secure Client for posture (ise I14), a self-hosted GitHub runner on cortex (cml L12) | Various | One item each |

Time-limited: the FMC Smart Licensing evaluation expires ≈2027-01-12; the ISE evaluation licence (Advantage, needed for pxGrid) also expires.

## External services: suite summary

| Service | Needed for | Provided by | Network path | Status |
|---|---|---|---|---|
| CML controller (EVAL: 5 running nodes) | All device testing | Lab | cortex → 198.18.128.10 | ✅ in use |
| ISE pxGrid persona | Catalyst Center ↔ ISE integration (appliance to appliance) | ISE node (currently OFF) | CatC → ISE :8910 | ⏳ catc Next (P2) |
| FTDv / FMCv / cat8000v / cat9000v images | Lab recipes | CML image library | — | ✅ present |
| Device images (.bin) for SWIM | CatC SWIM live tests | cisco.com download | upload to CatC | ❌ missing |
| Windows AD + CA | ISE AD identity, EAP-TLS, pxGrid certs | Existing Windows lab server (`Windows_Server` MCP) | ISE ↔ AD | ⏳ when needed |
| IdP (OAuth) | G1 | GitHub / customer IdP | Internet | Production gate |
| Public HTTPS ingress (Caddy/VPS or tunnel) | Claude Desktop connectors | VPS / tunnel provider | Internet → server | Production gate (optional) |
