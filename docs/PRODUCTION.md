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
| `MCP_BLAST_RADIUS_OVERRIDES` | `fmc_deploy=5,cml_*=100` | Per-tool limits (exact names win over globs, then the first matching glob). |
| `MCP_TWO_PERSON_TOOLS` | `@dangerous,fmc_deploy` | The confirm token must be redeemed by a **different** caller. |

**Tool selectors** (deny, allow, two-person): comma-separated shell globs over tool names plus tier
selectors `@read`, `@write`, `@dangerous`. `<prefix>_status` and `<prefix>_review_change` are never
withheld. Status lists withheld tools under `server.safety.withheld_tools`.

**Change windows**, several separated by `;` (any open window allows the change):
- `<days> <HH:MM>-<HH:MM> [Area/City]`: days are `Mon`..`Sun`, ranges (`Mon-Fri`), lists (`Sat,Sun`) or
  `daily`. The window opens on the listed day; an end at or before the start runs past midnight
  (`22:00-02:00`), equal times mean 24 hours. The zone defaults to UTC.
- `<minute hour day-of-month month day-of-week> <duration> [Area/City]`, cron style:
  `0 22 * * 1-5 4h Australia/Sydney` opens at each match for the duration (`30m`, `4h`, `1h30m`, `2d`; at most 7 days).
- Times are wall-clock in the window's zone; a DST change inside a window moves its end by an hour.

**Blast radius.** A write tool sets `Plan(blast_radius=N)` when it can count what a change touches
(devices a deploy goes to, endpoints affected, rules rewritten). It is shown in the preview. Tools that
don't count are never limited; each server's README says which tools report it.

**Two-person confirm.** The requester previews as usual and gets a token, but can't redeem it. They pass
the token to a second person (another named bearer token or OAuth identity), whose client calls
`<prefix>_review_change(token)` to see the exact preview, arguments (secrets masked) and requester, and
then repeats the call with `confirm=<token>`. The applied result and the audit log carry `requested_by`
and `approved_by`. Over stdio every caller is `local`, so two-person tools can't be confirmed there.

Audit events added: `blocked` (preview refused by a blocker), `refused` (confirm refused by a rail);
`preview` / `applied` / `apply_failed` carry the `policy` section when rails are on.
