# Snapshot contract (pre/post change checks)

Roadmap C53 (health) and C78 (configuration). Every change window ends with two questions: "did anything else
break?" and "did anything change that the change ticket did not name?". A server answers both with two tools:

| Tool | Tier | Arguments | Returns |
|---|---|---|---|
| `<prefix>_snapshot` | read | `site: str = ""` (empty = everything the server sees), plus optional server-specific narrowing | a **snapshot blob** (JSON object, schema below) |
| `<prefix>_compare_snapshot` | read | `before` (blob), `after` (blob, optional: empty = take a fresh snapshot with `before`'s scope now), `expected` (optional: the change's own configuration objects, list or comma-separated) | a **compare result** naming what changed by severity, the change's own objects apart |

The **client holds the blobs**. The server stores nothing between calls (ADR-003: no server-side state). The
blob goes back to the client in the conversation, and the client passes it to the compare unchanged. The suite
prompt `suite_change_check` (docs/SUITE-PROMPTS.md) runs these tools on every connected server, pre and post.

Planned implementations: cat-sdwan W46, ise I82, catc D118, fmc F89. Suggested contents are under
[What each server snapshots](#what-each-server-snapshots).

## The blob, schema versions 1 and 2

Version 2 (C78) adds **config sections** (`"kind": "config"`, [below](#config-sections-schema-2)). A blob is
written as 2 only when it has a config section, so a health-only blob stays 1. Core reads both: a v1 blob taken
before an upgrade compares with a v2 blob taken after it (the new config sections are noted as only in the after
snapshot).

```json
{
  "snapshot": 1,
  "prefix": "catsdwan",
  "server": "cat-sdwan-mcp-server",
  "server_version": "0.4.0",
  "instance": "lab",
  "taken_at": "2026-10-24T09:00:00Z",
  "scope": {"site": "br1"},
  "sections": {
    "bfd": {
      "title": "BFD sessions",
      "items": {
        "br1|biz-internet|hub1|biz-internet": {"ok": true, "state": "up"}
      }
    },
    "app_route_sla": {
      "title": "App-route SLA",
      "metrics": {"loss_pct": {"better": "lower", "tolerance": 1, "limit": 5}},
      "items": {
        "br1|mpls|hub1|mpls": {"ok": true, "loss_pct": 0, "latency_ms": 12.4, "sla_class": "Voice"}
      }
    },
    "alarms": {
      "title": "Active alarms",
      "truncated": 12,
      "items": {
        "br1|bfd-state-change|biz-internet": {"ok": false, "state": "major", "label": "br1 BFD down on biz-internet"}
      }
    },
    "vpn": {"title": "VPN tunnels", "error": "HTTP 503 from ...", "items": {}}
  },
  "digest": "sha256:0123456789abcdef"
}
```

| Field | Meaning |
|---|---|
| `snapshot` | Schema version (integer). A server reads only the versions it knows and refuses others with "take a new snapshot". |
| `prefix`, `server` | The server that took it. A server compares only its own blobs (`prefix` must match). |
| `server_version`, `instance` | Optional. `instance` is the multi-instance target (ADR-004); the compare notes when instances differ. |
| `taken_at` | UTC, ISO 8601, whole seconds. |
| `scope` | The arguments that define what was snapshotted (`{"site": "br1"}`; empty values left out). A fresh `after` is taken with the same scope. The compare notes when scopes differ. |
| `sections` | Snake_case name to section. One section per kind of thing (sessions, peers, devices, alarms, ...). |
| section `title` | Human name, used in the compare's `what` lines. |
| section `kind` | Schema 2. `"config"` for a config section; absent means a health section. |
| section `items` | Stable key to item (below). |
| section `metrics` | Optional. Numeric item fields compared with a tolerance (below). |
| section `truncated` | Optional. How many items were left out to stay within the size budget. The compare then reports no added or removed items in that section, only changes to items present in both blobs. |
| section `error` | Optional. The product API failed for this section (≤ 300 chars, no secrets). The compare reports `unreadable` and does not diff the section. |
| `digest` | `sha256:` plus the first 16 hex digits of SHA-256 over the blob without `digest`, as canonical JSON (`sort_keys`, separators `,` and `:`, UTF-8, `ensure_ascii=False`). It detects a blob the client retyped or cut. A mismatch does not refuse the compare: the result carries `integrity: {"before": "altered"}` and a note. |

### Items

- **Key**: stable across snapshots and 1–200 characters. Build it from names (`br1|biz-internet|hub1|biz-internet`,
  `nad:br1-edge`, `device:fw-hub1`), not from ids that change on a rebuild or on every read. Keys sort in the blob.
- **Reserved fields**:
  - `ok` (bool): is it healthy? This is the main signal, so set it wherever the product says.
  - `state` (string): the product's own word (`up`, `down`, `ACTIVE`, `Deployed`).
  - `label` (string): a human name when the key is not readable. It is dropped when equal to the key.
- **Other fields** are scalars (string, number, bool). Fields named in the section's `metrics` are compared
  numerically. **All others are compared for equality**, so a change in `version` or `ha_role` is reported.
  Leave out anything volatile: counters, uptimes, timestamps, "last seen" and rates that move with traffic
  (unless declared as a metric with a tolerance). Two snapshots of an unchanged network must compare equal.
- Numbers are normalised: integral floats become integers and others are rounded to 3 decimals. Nulls are
  left out.

### Metrics

`{"better": "lower" | "higher", "tolerance": <absolute change ignored>, "limit": <threshold, optional>}`.
Tolerance is the noise floor (loss_pct 1, latency_ms 20). The limit is the line between acceptable and
not (the SLA class's loss/latency/jitter, a failure-rate ceiling).

### Size

A blob must survive the round trip intact, and `MCP_MAX_RESPONSE_CHARS` (default 40,000) would cut it, so a blob
stays **≤ 24,000 characters** (`snapshot.MAX_CHARS`) and each section holds at most 100 items by default. Over
budget, the builder halves the biggest section until the blob fits. Unhealthy items (`ok: false`) are kept
first, and the section is marked `truncated`. Prefer a narrower scope (one site) to a cut.

## Config sections (schema 2)

A config section holds **one digest per configuration object**: what the object is, not how healthy it is. It
turns "nothing else broke" into "nothing else changed" for a method of procedure's pre/post check: an
accidental rule edit, a policy-set reorder or an extra template push shows up even when health is fine.

```json
"access_rules": {
  "title": "Access rules",
  "kind": "config",
  "items": {
    "mcp-acp|mcp-allow-web": {"digest": "sha256:9f2c0e41d6b7a813"},
    "mcp-acp|mcp-deny-telnet": {"digest": "sha256:1b44e0c27a90d5fe", "label": "Deny telnet (rule 3)"}
  }
}
```

| Item field | Meaning |
|---|---|
| key | Stable name of the object (`<policy>\|<rule name>`, `<device>\|<template>`), as for health items. |
| `digest` | `snapshot.config_digest(obj, ignore=...)`: `sha256:` plus 16 hex digits over the object's canonical JSON. `ignore` drops volatile keys at any depth (`metadata`, `links`, timestamps, hit counts), so the digest moves only when the configuration does. |
| `version` | The product's own version or revision (a template version, a configuration group's version). |
| `label` | A human name when the key is not one; also matched by `expected`. |

An item has a digest, a version or both, and nothing else: **digests only** keeps the blob within its budget
(no configuration text, no secrets: hash the object as the API returns it, with secret values masked or dropped through `ignore`, since a short digest of a weak secret can be guessed). Build them with
`Snapshot.section(name, title, kind="config")` and `Section.config(key, digest=..., version=..., label=...)`.

The compare reports, for config sections only:

| Severity | Kind | When |
|---|---|---|
| minor | `added` | a new object (`what`: "new object") |
| minor | `removed` | an object is gone |
| minor | `changed` | its `version` or `digest` moved, or its label changed (renamed) |

Each such change carries `"config": true`. **`expected`** names the change's own objects, by key, by
`section/key` or by label. Their changes go to the result's `expected` list (severity `expected`) instead of
`changes`, and do not count in `counts` or the verdict, so what is left in `changes` changed **outside the
change**. An expected object that did not change is listed in `expected_unchanged` (was the change made?), and
one in neither blob in `expected_not_found`; both get a note. The verdict ends `; N expected config changes.`
(`No unexpected change (...)` when nothing else moved). `expected` does not apply to health sections: health
effects of the change are told apart by the reader (suite_change_check's 'Expected' list).

## The compare result

```json
{
  "prefix": "catsdwan", "server": "cat-sdwan-mcp-server", "scope": {"site": "br1"},
  "before_taken_at": "2026-10-24T09:00:00Z", "after_taken_at": "2026-10-24T09:20:00Z",
  "verdict": "4 major changes; 11 items unchanged.",
  "counts": {"major": 4, "minor": 0, "info": 0},
  "unchanged_items": 11,
  "changes": [
    {"severity": "major", "kind": "degraded", "section": "bfd", "item": "br1|biz-internet|hub1|biz-internet",
     "what": "BFD sessions br1|biz-internet|hub1|biz-internet: healthy -> unhealthy; state up -> down"}
  ],
  "notes": ["..."], "integrity": {"before": "altered"}
}
```

`changes` is sorted by severity, then section, then item. It holds at most 100 entries; `changes_omitted` counts
the rest. `notes` and `integrity` appear only when they apply; `expected` (and `expected_unchanged`,
`expected_not_found`) only when `expected` was passed.

| Severity | Kind | When |
|---|---|---|
| major | `degraded` | `ok` true -> false; a metric crosses its `limit` the wrong way |
| major | `removed` | an item with `ok: true` is gone (a session or peer disappeared) |
| major | `added` | a new item with `ok: false` (a new alarm or issue) |
| minor | `changed` | `state` or another compared field changed, or `ok` changed to or from unset |
| minor | `worse` | a metric got worse by more than its tolerance, without crossing the limit |
| minor | `added` / `removed` | an item without `ok` appeared or went away |
| minor | `unreadable` | the section has an `error` in the after blob |
| info | `recovered` | `ok` false -> true; a metric back within its limit |
| info | `improved` | a metric got better by more than its tolerance |
| info | `added` / `removed` | a healthy item appeared; an unhealthy item went away (an alarm cleared) |

One item yields at most one change. Its `what` lists every detail, and its severity and kind are the most
severe detail's. A `state` change that comes with an `ok` change takes that change's severity (up -> down is
the degradation, down -> up the recovery). Sections present in only one blob, unreadable sections, truncated
sections and differing scopes or instances are named in `notes`.

## Implementing it in a server

Use core's helper unless the tool needs extra arguments:

```python
from stutters_mcp_core.snapshot import Metric, Snapshot, register_snapshot_tools

async def take(ctx: Context, site: str) -> Snapshot:
    snap = Snapshot("ise", "ise-mcp-server", scope={"site": site}, server_version=__version__)
    nads = snap.section("nad_auth", "Authentications per NAD",
                        metrics={"failure_pct": Metric("lower", 5, limit=20)})
    try:
        for row in await auth_rates(ctx, site):            # one product read per section
            nads.item(f"nad:{row.name}", ok=row.failure_pct < 20, failure_pct=row.failure_pct)
    except UpstreamError as e:
        nads.fail(str(e))                                  # never fail the whole snapshot for one section
    ...
    return snap

def register(reg: ToolRegistrar) -> None:
    register_snapshot_tools(reg, take, product="Cisco ISE",
                            covers="authentications and failure rates per NAD, node health, pxGrid clients",
                            site_doc="ISE location (NDG) or NAD name prefix; empty = every NAD")
```

`register_snapshot_tools` registers `<prefix>_snapshot(site)` and `<prefix>_compare_snapshot(before, after)`
(read tier). The snapshot returns the blob unshaped: `object_result(..., shape=False)`, because `compact()`
would drop empty values and break the digest. The compare checks the prefix, takes a fresh `after` when none is
given (`site` from `before.scope`), and returns `compare(before, after)`. A hand-written pair must behave the
same. Use `snapshot.parse(blob, prefix=...)` for input and `snapshot.compare(...)` for the result, so every
server reports changes the same way.

Rules:

- Read tier only. No writes, no stored state, no background polling (ADR-003).
- One section per product read and one call per section where possible. A snapshot should take seconds,
  not minutes.
- Secrets never go in a blob (no shared secrets, keys or tokens, even masked). Hostnames and names are fine;
  the blob stays in the user's own conversation.
- Mark health with `ok` from the product's own verdict where there is one (BFD `up`, ISE node `RUNNING`,
  CatC health score ≥ its "good" band, FMC deployment `DEPLOYED`).
- Tests: build blobs from recorded fixtures (respx), check that two snapshots of the same recorded state
  compare to "No change", and that a recorded failure (one session down, one NAD failing) is named as the
  only major change.
- The schema version changes only for an incompatible change in core. New sections and fields are not
  schema changes. Config sections were one (schema 2, C78).
- Config sections: one product read per section, the same reads the server's list/get tools already make (no
  new endpoint). Test that a recorded object edit shows as `changed` and is moved to `expected` when named.

## What each server snapshots

These are suggestions from the C53 issue. Each server's item decides the final sections.

| Server | Item | Sections |
|---|---|---|
| cat-sdwan | W46 | `control` (control connections per edge, colour, peer), `bfd` (sessions by local/remote system and colour), `omp` (peers; routes received as a metric), `app_route_sla` (loss/latency/jitter per tunnel against its SLA class limits), `alarms` (active, by device and type) |
| ise | I82 | `nad_auth` (authentications and failure % per NAD over a fixed recent window, failure % as a metric), `nodes` (persona and state per node), `pxgrid_clients` (state per client) |
| catc | D118 | `devices` (reachability, health score as a metric, software version), `sites` (health per site), `compliance` (status per device), `issues` (open P1/P2 issues by device and name) |
| fmc | F89 | `deployment` (deploy state per device: pending changes or not), `ha` (role and state per pair), `interfaces` (link state of named interfaces), `vpn` (tunnel state per topology and peer) |

### Config sections (C78)

Suggestions; each server's adoption item decides. Keys are names, never UUIDs where a name exists.

| Server | Section (`kind: "config"`) | Key | Digest / version |
|---|---|---|---|
| fmc | `access_rules` | `<access policy>\|<rule name>` | digest of the rule (ignore `metadata`, `links`, hit counts) |
| fmc | `nat_rules` | `<NAT policy>\|<section>\|<rule index or description>` | digest of the rule (ignore `metadata`, `links`) |
| ise | `policy_sets` | `<policy set name>` | digest of the set (condition, allowed protocols, rank, state) |
| ise | `authz_rules` | `<policy set>\|<rule name>` | digest of the rule (condition, profiles, security group, rank) |
| catc | `templates` | `<device>\|<template name>` | `version`: the committed template version deployed to the device |
| catsdwan | `config_groups` | `<configuration group name>` | `version` of the group plus a digest of its feature profiles |
| catsdwan | `device_variables` | `<device>\|<configuration group>` | digest of the device's variable values (secret variables masked or left out) |

The change's own objects, named by these keys or labels, go to `<prefix>_compare_snapshot(expected=...)`;
`suite_change_check(phase="post", expected=...)` passes them to every leg.

For ISE, use a fixed window (e.g. the last 15 minutes before the snapshot), so the failure % compares like with
like.
