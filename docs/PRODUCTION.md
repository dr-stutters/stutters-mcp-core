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

## Secrets (core 0.7.0, roadmap G4)

Three ways to supply any secret setting (`<P>_PASSWORD`, `<P>_TOKEN`, product-specific `SecretStr` fields);
set exactly one per secret:

| Form | Example | Notes |
|---|---|---|
| `<VAR>` | `ISE_PASSWORD=...` in `.env` | Simplest; visible in `docker inspect` when passed as env. |
| `<VAR>_FILE` | `ISE_PASSWORD_FILE=/run/secrets/ise_password` | Docker / Kubernetes secrets. One trailing newline stripped. |
| `<VAR>_REF` | `ISE_PASSWORD_REF=file:/run/secrets/creds.json#ise_password` | Resolved by a secret loader at startup. |

`MCP_AUTH_TOKENS_FILE` and `MCP_ROLES_FILE` take files directly.

**Docker secrets.** `docs/templates/docker-compose.secrets.yml` is an override that mounts the appliance
password and the token list as secrets: `docker compose -f docker-compose.yml -f docker-compose.secrets.yml up -d`.
The files live in `secrets/` next to the compose file (mode 600, git-ignored, excluded from the build context).

**Secret loaders** (`<VAR>_REF=<scheme>:<ref>`). Built in: `file:<path>#<key>` reads `<key>` from a JSON
object or a `KEY=VALUE` file (`file:<path>` alone reads the whole file). Add others with
`MCP_SECRET_LOADERS="scheme=package.module:Class"`; the class has a no-argument constructor and
`load(ref: str) -> str`, runs once per secret at startup, and raises `ValueError` (without the secret in the
message) when it can't. The module must be importable in the server's environment (install it into the
image). Examples (not shipped; no accounts are needed to use core itself):

```python
# HashiCorp Vault (KV v2) with hvac: ISE_PASSWORD_REF=vault:secret/data/ise#password
import os, hvac

class VaultLoader:
    def __init__(self) -> None:
        self.client = hvac.Client(url=os.environ["VAULT_ADDR"], token=open("/run/secrets/vault_token").read().strip())

    def load(self, ref: str) -> str:
        path, _, key = ref.partition("#")
        mount, _, rest = path.partition("/data/")
        data = self.client.secrets.kv.v2.read_secret_version(path=rest, mount_point=mount)["data"]["data"]
        if key not in data:
            raise ValueError(f"{path} has no key {key!r}")
        return str(data[key])
```

```python
# 1Password CLI (service account; OP_SERVICE_ACCOUNT_TOKEN in the environment):
# ISE_PASSWORD_REF=op:op://Lab/ISE admin/password
import subprocess

class OnePasswordLoader:
    def load(self, ref: str) -> str:
        done = subprocess.run(["op", "read", ref], capture_output=True, text=True, timeout=30)
        if done.returncode != 0:
            raise ValueError(f"op read failed for {ref!r} (exit {done.returncode})")
        return done.stdout.rstrip("\n")
```

Register them with `MCP_SECRET_LOADERS="vault=my_loaders:VaultLoader,op=my_loaders:OnePasswordLoader"`.

**What keeps secrets out of logs and status** (checked by `tests/unit/test_secrets.py`):
- Settings use `SecretStr`; validation errors never echo input values (`hide_input_in_errors`).
- At startup the CLI installs a log filter on every handler (root and audit) that masks the configured
  secret values (every `SecretStr` setting and raw `MCP_AUTH_TOKENS` entries) and credential shapes
  (`user:pass@` in URLs, `?token=` / `password=` / `apikey=` parameters, `Bearer` / `Basic` values),
  including in tracebacks.
- Upstream error messages (`detail`, redirect `Location`, connection errors) are scrubbed of credential
  shapes; audit `error` fields are scrubbed and tool arguments with secret-looking names are masked.
- `<prefix>_status` shows the URL without userinfo, masks secret-looking keys in the product status, and
  masks the configured secret values in error text.
- Never logged: the HTTP startup log names tokens, not values; `<server> token` prints the raw token only
  to the terminal that ran it.
