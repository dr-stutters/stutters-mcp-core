# Production controls

Everything on this page is **opt-in**: with none of these variables set, a server behaves exactly as it
did before core 0.5.0. They apply to every server built on stutters-mcp-core (cml, fmc, catc, ise), and
are read from the environment or the server's `.env` file.

## Safety rails (core 0.5.0, roadmap G7)

The rails act on the write tiers (`MCP_ENABLE_WRITES` / `MCP_ENABLE_DANGEROUS` still decide what exists
at all). They are evaluated by `preview_or_apply` (dry-run, change window, blast radius, two-person) and
by tool registration (deny / allow lists), shown in every preview under `policy`, reported by
`<prefix>_status` under `server.safety`, and recorded in the audit log.

| Variable | Example | Effect |
|---|---|---|
| `MCP_DRY_RUN` | `true` | Previews work; no confirm token is issued and every confirm is refused. |
| `MCP_CHANGE_WINDOW` | `Mon-Fri 22:00-02:00 Australia/Sydney` | Confirms outside the window are refused with the next opening time. Previews still work (and warn). |
| `MCP_DENY_TOOLS` | `*_delete_*,@dangerous` | Matching tools are not registered. |
| `MCP_ALLOW_TOOLS` | `fmc_list_*,fmc_get_*` | Only matching tools are registered. |
| `MCP_MAX_BLAST_RADIUS` | `20` | A change whose `blast_radius` is larger is blocked (no token). |
| `MCP_BLAST_RADIUS_OVERRIDES` | `fmc_deploy=5,cml_*=100` | Per-tool limits (an exact name wins; otherwise the smallest matching glob). |
| `MCP_TWO_PERSON_TOOLS` | `@dangerous,fmc_deploy` | The confirm token must be redeemed by a **different** caller. |

**Tool selectors** (deny, allow, two-person): comma-separated shell globs over tool names plus tier
selectors `@read`, `@write`, `@dangerous`. `<prefix>_status` and `<prefix>_review_change` are never
withheld. Status lists withheld tools under `server.safety.withheld_tools`.

**Change windows**, several separated by `;` (any open window allows the change):
- `<days> <HH:MM>-<HH:MM> [Area/City]`: days are `Mon`..`Sun`, ranges (`Mon-Fri`), lists (`Sat,Sun`) or
  `daily`. The window opens on the listed day; an end before the start runs past midnight
  (`22:00-02:00`); a whole day is `00:00-24:00` (other equal times are rejected as a likely typo). The zone
  defaults to UTC.
- `<minute hour day-of-month month day-of-week> <duration> [Area/City]`, cron style:
  `0 22 * * 1-5 4h Australia/Sydney` opens at each match for the duration (`30m`, `4h`, `1h30m`, `2d`; at most 7 days).
  Day-of-week ranges may wrap (`fri-sun`); 0 and 7 are Sunday; as in cron, when both day fields are restricted
  either may match.
- Times are wall-clock in the window's zone; a DST change inside a window moves its end by an hour.

**Blast radius.** A write tool sets `Plan(blast_radius=N)` when it can count what a change touches
(devices a deploy goes to, endpoints affected, rules rewritten). It is shown in the preview. Tools that
don't count are never limited; each server's README says which tools report it.

**Two-person confirm.** The requester previews as usual and gets a token, but can't redeem it. They pass
the token to a second person (another named bearer token or OAuth identity), whose client calls
`<prefix>_review_change(token)` to see the exact preview, arguments (secrets masked) and requester, and
then repeats the call with `confirm=<token>`. The applied result and the audit log carry `requested_by`
and `approved_by`. Over stdio every caller is `local`, so two-person tools can't be confirmed there.
The identity is the **token name**: give each person their own token (the server warns at startup when
fewer than two tokens exist). Secret arguments are masked in the review, and the approver's call must carry
the identical arguments, so for tools with secret arguments the requester has to share them out of band.
If a tool's tier is unknown, tier selectors such as `@dangerous` count as matching (fail closed).

Audit events added: `blocked` (a preview with blockers, including a product tool's own `Plan.blockers`),
`refused` (a confirm refused by a rail or blocker);
`preview` / `applied` / `apply_failed` carry the `policy` section when rails are on.

## Per-user roles (core 0.6.0, roadmap G2)

Map caller identities to roles; each role unlocks tool tiers. Off unless `MCP_ROLES` or `MCP_ROLES_FILE` is set.

| Variable | Example | Effect |
|---|---|---|
| `MCP_ROLES` | `alice:admin, bob:rw, ci-*:ro, github:octocat:rw` | `name:role` entries; names are exact or shell globs |
| `MCP_ROLES_FILE` | `/run/secrets/mcp_roles` | Same format, one entry per line, `#` comments; added to `MCP_ROLES` |
| `MCP_DEFAULT_ROLE` | `ro` (default) | Role for callers no entry matches |

| Role | Tools |
|---|---|
| `ro` | read |
| `rw` | read, write |
| `admin` | read, write, dangerous |

- The identity is the bearer-token name from `MCP_AUTH_TOKENS` (or `<provider>:<login>` with OAuth). An
  exact name wins; then the most specific matching glob (most literal characters; on a tie the lower role),
  so `*:ro, ops-*:admin` gives `ops-amy` admin and everyone else ro; then `MCP_DEFAULT_ROLE`.
- The stdio caller (`local`) is `admin` unless an entry names it: it is the operator's own process. `local`
  is a reserved name no bearer token can use.
- Roles only narrow: the server's `MCP_ENABLE_WRITES` / `MCP_ENABLE_DANGEROUS` still decide what exists.
- Tools above a caller's role are hidden from their tool list and refused if called (audit event `denied`).
  Prompts follow their declared tier. A tool or prompt registered outside the core registrars (no known
  tier) needs `admin`.
- `<prefix>_api_call` is one dangerous-tier tool (it can send any method), so with writes on it is `admin`
  only; `ro` and `rw` callers use the curated tools and `<prefix>_search_spec` / `<prefix>_get_definition`.
- Identity and role are shown in previews (`requested_by: {caller, role}`), in `<prefix>_status`
  (`server.access`: your identity and role, the default role, the number of entries) and in every audit
  event (`role`). At HTTP startup the log lists each token name with its role.
- `<prefix>_review_change` (two-person review) needs `rw` or `admin`, like the change it reviews.
- Combine with two-person confirm: e.g. `MCP_TWO_PERSON_TOOLS=@dangerous` with two `admin` users.
