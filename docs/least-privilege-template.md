# Least-privilege guide: template for `docs/least-privilege.md` in each server (roadmap G3)

Copy this file to `docs/least-privilege.md` in the server repo, replace `<Product>` / `<P>` / `<prefix>`,
and fill every section from a **tested** lab run. The guide is only done when the live test at the end
passes as the read-only user and the write user, and the role/user creation is in the lab seed script.

---

# Least-privilege accounts for `<prefix>-mcp-server`

The server needs one appliance account. Give it the smallest role that covers the tiers you enable:

| Server tiers enabled | Account | Role |
|---|---|---|
| read only (default) | `mcp-ro` | `<custom read-only role>` |
| read + write (`MCP_ENABLE_WRITES=true`) | `mcp-rw` | `<custom write role>` |
| + dangerous (`MCP_ENABLE_DANGEROUS=true`) | `mcp-admin` | `<built-in or custom role>`: say why the custom role isn't enough |

Never use the appliance's built-in super-admin for a hosted server. MCP-side roles (`MCP_ROLES`,
core docs/PRODUCTION.md) restrict *callers*; this guide restricts *the server itself*. Use both.

## 1. Read-only role: `<name>`

**What it allows**, as the product names it (copy exact privilege/permission names from the GUI or API):

| Area (product term) | Permission | Needed by |
|---|---|---|
| e.g. Network Resources → Network Devices | Read | `<prefix>_list_nads`, `<prefix>_get_nad` |
| ... | ... | ... |

**What it deliberately does not allow** (and which tools therefore return "denied"): e.g. admin-user
lists, certificate private keys, backup configuration.

**Create it** (GUI path *and* the API call the seed script uses):

```text
<Product GUI> → Administration → ... → Add role "<name>": ...
```

```bash
# scripts/seed_lab.py excerpt (idempotent: creates or updates)
curl -sk -u "$ADMIN" -X POST https://$HOST/api/... -d '{...}'
```

## 2. Write role: `<name>`

Same structure: permissions table (with the write tools each one enables), what stays denied, creation.
Note product quirks, e.g. "ERS writes need the ERS Admin group, not ERS Operator", "deploys need the
Deploy Configuration permission on the device's domain".

## 3. Dedicated users

| User | Role | Auth | Notes |
|---|---|---|---|
| `mcp-ro` | `<read-only role>` | local password / API token | password policy, expiry exemption |
| `mcp-rw` | `<write role>` | ... | ... |

Creation steps (GUI + seed script). Store the passwords as Docker secrets (`<P>_PASSWORD_FILE`), not in
`.env`, on hosted servers.

## 4. Verify (tested on: `<product version>`, `<date>`)

Put the read-only account in the server's `.env` as `<P>RO_USERNAME` / `<P>RO_PASSWORD` and run:

```bash
uv run pytest -m live tests/live/test_least_privilege.py -v
```

```python
# tests/live/test_least_privilege.py
import pytest
from stutters_mcp_core.testing.least_privilege import assert_read_tier_works, readonly_live_settings
from <package>.config import Settings
from <package>.server import create_server

SAMPLES = {"<prefix>_get_<thing>": {"name": "mcp-test-<thing>"}}  # objects the seed script creates

@pytest.mark.live
async def test_read_tier_as_readonly_user() -> None:
    settings = readonly_live_settings(Settings, "<P>_")
    report = await assert_read_tier_works(create_server(settings), SAMPLES, skip={"<prefix>_api_call"})
    print(report.summary())
```

The helper logs in as the read-only appliance account, calls every read tool as an MCP caller with role
`ro` (so it also proves write tools are hidden), and fails on any denial. Record the result:

| Result | Count | Tools |
|---|---|---|
| ok | | |
| denied (expected, see "does not allow") | | |
| skipped (no sample) | | |

For the write role, run the write live tests (`tests/live/test_writes.py`) with `<P>_USERNAME=mcp-rw`.

## 5. Lab rebuild

The seed script creates both roles and users (`scripts/seed_lab.py --least-privilege`), and
`~/MCP-Suite/LAB-REBUILD.md` lists them under this server.
