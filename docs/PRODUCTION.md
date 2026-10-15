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

`MCP_SECRET_LOADERS` is trusted configuration (it imports code): only name modules installed in the image.
Register them with `MCP_SECRET_LOADERS="vault=my_loaders:VaultLoader,op=my_loaders:OnePasswordLoader"`.

**What keeps secrets out of logs and status** (checked by `tests/unit/test_secrets.py`):
- Settings use `SecretStr`; validation errors never echo input values (`hide_input_in_errors`).
- At startup the CLI wraps the log record factory, so every record from every logger (FastMCP's and
  uvicorn's included) masks the configured secret values (every `SecretStr` setting, the Basic-auth base64,
  raw `MCP_AUTH_TOKENS` entries, values passed to `register_secret`) as whole words, and credential shapes
  (`user:pass@` in URLs, whole-word secret keys such as `password=` / `?token=` / `"secret": "..."`,
  Authorization / Cookie / `X-*-Token` headers, `Bearer <credential>`), including tracebacks.
- Tool errors returned to the MCP client are scrubbed the same way.
- Limits: value masking is whole-word (so a weak password like `admin` doesn't mangle "administrator"); a
  secret glued to other letters or digits is only caught by the shape rules. Use strong, unique credentials.
  Tokens obtained at runtime (session cookies) are masked only if the server calls `register_secret`.
- Upstream error messages (`detail`, redirect `Location`, connection errors) are scrubbed of credential
  shapes; audit `error` fields are scrubbed and tool arguments with secret-looking names are masked.
- `<prefix>_status` shows the URL without userinfo, masks secret-looking keys in the product status, and
  masks the configured secret values in error text.
- Never logged: the HTTP startup log names tokens, not values; `<server> token` prints the raw token only
  to the terminal that ran it.

## Metrics (core 0.7.0, roadmap G5)

Prometheus text format at `/metrics`. Off by default.

| Variable | Default | Effect |
|---|---|---|
| `MCP_METRICS` | `false` | Serve metrics |
| `MCP_METRICS_PORT` | unset | Serve on a separate, **unauthenticated** listener on this port (works with stdio too) |
| `MCP_METRICS_HOST` | `127.0.0.1` | Bind address of that listener; keep it on loopback or a monitoring network |

Without `MCP_METRICS_PORT`, an HTTP server serves `GET /metrics` on its MCP port and requires
`Authorization: Bearer <token>` with any `MCP_AUTH_TOKENS` entry (give Prometheus its own, e.g.
`prometheus:sha256:...`; with roles on, give it `ro`). Prometheus:

```yaml
scrape_configs:
  - job_name: ise-mcp
    metrics_path: /metrics
    authorization: { credentials_file: /etc/prometheus/ise-mcp.token }
    static_configs: [{ targets: ["cortex:8036"] }]
```

| Series | Type | Labels |
|---|---|---|
| `mcp_info` | gauge | `server`, `version`, `core_version` |
| `mcp_tool_calls_total` | counter | `server`, `tool`, `outcome` (`ok` / `error`) |
| `mcp_tool_duration_seconds` | histogram | `server`, `tool` |
| `mcp_write_events_total` | counter | `server`, `tool`, `event` (`preview`, `applied`, `apply_failed`, `blocked`, `refused`) |
| `mcp_upstream_requests_total` | counter | `product`, `method`, `status` (HTTP code, `timeout`, `error`) |
| `mcp_upstream_duration_seconds` | histogram | `product`, `method` |
| `mcp_upstream_retries_total` | counter | `product`, `reason` (HTTP code, `timeout`, `error`) |
| `mcp_rate_limit_waits_total`, `mcp_rate_limit_wait_seconds_total` | counter | `product` |

Labels never carry ids, paths, arguments or callers; unknown tool names are recorded as `_unknown` and
non-standard HTTP methods as `OTHER`, so cardinality is bounded by the tool list. With `MCP_METRICS` off
nothing is recorded. A listener port that is already in use is logged and skipped (the MCP server keeps
running); a non-loopback `MCP_METRICS_HOST` logs a warning. Useful
alerts: `rate(mcp_tool_calls_total{outcome="error"}[5m])`, `rate(mcp_upstream_requests_total{status=~"5..|timeout|error"}[5m])`,
`increase(mcp_write_events_total{event="applied"}[1h])` (changes made), `rate(mcp_rate_limit_wait_seconds_total[5m])`.

## OAuth 2.1 for HTTP (core 0.8.0, roadmap G1)

Interactive users log in through an identity provider instead of holding a long-lived bearer token;
automation keeps its named tokens (`MCP_AUTH_TOKENS`). Both are accepted at once. Off unless
`MCP_OAUTH_PROVIDER` is set; with it set, HTTP may run with OAuth only (no tokens).

| Variable | Example | Notes |
|---|---|---|
| `MCP_OAUTH_PROVIDER` | `github` | `github`, or `oidc` for any OIDC IdP (Entra ID, Okta, Keycloak, Google...) |
| `MCP_OAUTH_CLIENT_ID` | `Ov23li...` | from the OAuth app you register (steps below) |
| `MCP_OAUTH_CLIENT_SECRET` / `_FILE` | | required for GitHub; for OIDC optional with `MCP_OAUTH_JWT_KEY` (public client) |
| `MCP_OAUTH_BASE_URL` | `https://mcp.lab.example:8443` | public URL clients use; must be `https://` (or `http://localhost`) |
| `MCP_OAUTH_ALLOWED_USERS` | `octocat,id:583231` | **required**: who may log in. Shell globs over the login (case-insensitive; `*`, `?`, `[...]`), or `id:<n>` for the IdP's stable user id (GitHub numeric id: survives renames and can't be re-registered by someone else, so prefer it). Anyone else the IdP authenticates gets 401. A bare `*` logs a warning |
| `MCP_OAUTH_LOGIN_CLAIM` | `oid` | `oidc` only: the claim used as the login. Default `email`, accepted only with `email_verified: true`. Prefer a stable, admin-controlled claim (Entra `oid`, Keycloak `sub`) |
| `MCP_OAUTH_CONFIG_URL` | `https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration` | `oidc` only |
| `MCP_OAUTH_SCOPES` | `read:user` | IdP scopes to require (GitHub default `user`; OIDC default `openid`) |
| `MCP_OAUTH_REDIRECT_URIS` | `http://localhost:*,https://claude.ai/api/mcp/auth_callback` | MCP-client redirect URIs allowed to register. Default: loopback only (`http://localhost:*`, `http://127.0.0.1:*`), which covers Claude Code and Desktop. `*` allows anything (warned) |
| `MCP_OAUTH_JWT_KEY` / `_FILE` | `openssl rand -base64 32` | signs the tokens this server issues and keys the storage encryption; at least 32 characters. Default: derived from the client secret. Set it so tokens survive secret rotation |
| `MCP_OAUTH_STORAGE_DIR` | `/cache/oauth` | encrypted client registrations and upstream tokens, directory mode 0700. Default `<MCP_CACHE_DIR>/oauth` (in the image: the persistent `/cache` volume) |

How it works: FastMCP's OAuth proxy publishes OAuth metadata (`/.well-known/oauth-authorization-server`),
lets MCP clients register themselves (dynamic client registration), shows a consent page, sends the user to
the IdP, and issues the client its own short-lived token bound to the upstream one, which it re-checks on
each request. The caller identity is `<provider>:<login>` (`github:octocat`, `oidc:alice@example.com`):
use it in `MCP_ROLES` (`github:octocat:rw`), and it appears in previews, two-person confirms and the audit
log. Logins are lowercased (`MCP_ROLES` entries should be lowercase too). Named-token callers keep their token name.

Notes: dynamic client registration is unauthenticated by design (MCP clients register themselves), and a
user the IdP authenticates but the allow-list rejects can still complete the login dance (they get 401 on
every request, and their upstream token sits encrypted in storage until it expires). Put the server behind a
reverse proxy with rate limiting if it is reachable beyond your network. Named bearer tokens satisfy the
IdP-scope check (they are trusted as before).

### Steps for the repo owner (not done overnight: needs your GitHub account)

**GitHub (reference IdP).**
1. Decide the public URL. The server must be reachable over HTTPS by your browser and MCP client:
   - a TLS reverse proxy in front of the container, e.g. Caddy on cortex: `mcp-ise.lab.example { reverse_proxy 127.0.0.1:8036 }`
     with your internal CA or a public certificate → `MCP_OAUTH_BASE_URL=https://mcp-ise.lab.example`; or
   - for one user, an SSH tunnel (`ssh -L 8036:127.0.0.1:8036 cortex`) → `MCP_OAUTH_BASE_URL=http://localhost:8036`.
2. GitHub → Settings → Developer settings → **OAuth Apps** → *New OAuth App* (one per server, or one per URL):
   - Application name: `ise-mcp (lab)`; Homepage URL: the base URL;
   - **Authorization callback URL: `<base URL>/auth/callback`** (exactly);
   - leave *Enable Device Flow* off. Register, then *Generate a new client secret*.
3. On cortex, in the server's `secrets/` (see the Docker-secrets override): `printf '%s' '<client secret>' > secrets/<server>_oauth_secret`,
   and `openssl rand -base64 32 > secrets/<server>_oauth_jwt_key`; mount both and set
   `MCP_OAUTH_CLIENT_SECRET_FILE=/run/secrets/<server>_oauth_secret`, `MCP_OAUTH_JWT_KEY_FILE=/run/secrets/<server>_oauth_jwt_key`.
4. In `.env`: `MCP_OAUTH_PROVIDER=github`, `MCP_OAUTH_CLIENT_ID=<client id>`, `MCP_OAUTH_BASE_URL=<base URL>`,
   `MCP_OAUTH_ALLOWED_USERS=id:<your numeric GitHub id>` (`curl https://api.github.com/users/<login>` shows it), `MCP_OAUTH_STORAGE_DIR=/cache/oauth`; optionally
   `MCP_ROLES=github:<login>:admin`. Keep `MCP_AUTH_TOKENS` for scripts. `docker compose up -d`.
5. Client: `claude mcp add --transport http ise <base URL>/mcp` (no header), then `/mcp` in Claude Code →
   *Authenticate*; the browser opens the consent page, then GitHub. `ise_status` shows `server.access` with
   `github:<login>` when roles are on.

**OIDC (e.g. Entra ID; not tested against a real tenant).** Use a **single-tenant** app and config URL (a
multi-tenant `common` endpoint would admit other tenants' users). Register an app (Entra: *App registrations → New*, platform *Web*, redirect URI
`<base URL>/auth/callback`, create a client secret); set `MCP_OAUTH_PROVIDER=oidc`,
`MCP_OAUTH_CONFIG_URL=https://login.microsoftonline.com/<tenant>/v2.0/.well-known/openid-configuration`,
`MCP_OAUTH_CLIENT_ID`, `MCP_OAUTH_CLIENT_SECRET_FILE`, `MCP_OAUTH_LOGIN_CLAIM=oid`, and allow-list the users'
object ids. Entra access tokens for the default Graph audience can't be validated by JWKS: *Expose an API*
on the app registration, add a scope (e.g. `api://<client-id>/mcp`), and set
`MCP_OAUTH_SCOPES=api://<client-id>/mcp,openid,offline_access` (offline_access for refresh tokens).

Tested in CI with a mocked GitHub: registration, consent, callback, PKCE token exchange, MCP request with the
issued token, named tokens alongside, and an authenticated-but-not-allowed login refused (tests/unit/test_oauth.py).

## Several appliances per server (core 0.9.0, roadmap G10, [ADR-004](adr/004-multi-instance.md))

One server can reach several instances of its product (prod and lab ISE, an FMC per region). Off unless
`<P>_INSTANCES` is set; with one instance nothing changes.

```bash
ISE_URL=https://ise-lab.example        # the default instance ...
ISE_DEFAULT_INSTANCE=lab               # ... and its name (default: "default")
ISE_INSTANCES=prod,dr                  # more instances, each with its own prefix:
ISE_PROD_URL=https://ise-prod.example
ISE_PROD_USERNAME=mcp-ro
ISE_PROD_PASSWORD_FILE=/run/secrets/ise_prod_password   # _FILE / _REF work per instance
ISE_PROD_CA_BUNDLE=/certs/prod-ca.pem  # TLS checking, secrets, URL and credentials are per instance;
                                       # other fields not set (timeouts, rate limits...) are inherited
```

Every tool then takes an optional `instance` argument (an enum of the names; omitted = the default
instance). `<prefix>_status(instance=...)` shows that instance and lists them all; `<server> check` checks
each. A confirm token only works on the instance it was previewed on, and previews, results and audit
events carry `instance`. Safety rails, roles and the tier switches are server-wide: run a separate
container for an instance that needs a different write policy (e.g. read-only production).

## Long-running calls as MCP tasks (core 0.10.0, roadmap C12)

Deploys, discoveries, provisioning and image distribution can take 10+ minutes. With `MCP_TASKS=true`,
tools a server marks as long-running become MCP background tasks (SEP-2663, `io.modelcontextprotocol/tasks`):
a client that supports tasks gets a task handle at once and polls `tasks/get` for progress and the result;
clients that don't support tasks still call them synchronously, exactly as before.

| Variable | Default | Notes |
|---|---|---|
| `MCP_TASKS` | `false` | needs the `tasks` extra: the server depends on `stutters-mcp-core[tasks]` |
| `MCP_TASKS_URL` | `memory://` | Docket backend: `memory://` (one process; tasks are lost on restart) or `redis://host:6379/0` / Valkey for durability |

The caller's identity travels into the task (roles, previews, two-person and the audit log see the same
caller), and the `instance` argument works as usual. The audit log's `tool_call` event and the
`mcp_tool_*` metrics record the task *submission*; the write events (`preview`, `applied`, ...) are logged when
the task runs. `<prefix>_status` lists the task-capable tools under `server.tasks`.
