"""`suite_access_review` (roadmap C63): privileged access review across every platform the client reaches.

Auditors ask "who can change what": admin accounts, roles and the external groups that grant them, on ISE,
Catalyst Center, FMC, SD-WAN Manager and CML, with Active Directory resolving the groups to people. One table
of accounts and one list of findings. Read-only.

Some leg tools are small reads still to ship on their servers (ise I74, catc D60, fmc and cml follow-ups);
`PENDING_TOOLS` lists them (the prompt's `pending`): `assert_suite_pack` waives them on every server until
they ship. Drop a tool from it once every server's committed manifest of that peer lists it.
"""

from typing import Annotated

from pydantic import Field

from stutters_mcp_core.prompts import tool_ref
from stutters_mcp_core.suite import SERVERS_ARG, SuiteLeg, numbered, plan_legs, suite_prompt

LEGS = (
    SuiteLeg(
        "ise",
        "ISE admin accounts, admin groups, their RBAC menu and data access, and the AD groups mapped to them",
        ("ise_list_admin_users", "ise_get_admin_access"),
    ),
    SuiteLeg(
        "catc",
        "Catalyst Center users, roles and external authentication",
        ("catc_list_users", "catc_get_external_authentication"),
    ),
    SuiteLeg(
        "fmc",
        "FMC user accounts, roles and their permissions, external authentication (LDAP/RADIUS group maps)",
        ("fmc_list_users", "fmc_list_roles", "fmc_get_external_auth"),
    ),
    SuiteLeg(
        "catsdwan",
        "SD-WAN Manager users, user groups and AAA order (RADIUS/TACACS, local fallback)",
        ("catsdwan_list_users", "catsdwan_get_aaa"),
    ),
    SuiteLeg(
        "cml",
        "CML users, admin rights and the authentication backend (local or LDAP)",
        ("cml_list_users", "cml_get_auth_config"),
    ),
    SuiteLeg(
        "win",
        "Active Directory: members of Domain Admins and of the AD groups the platforms map to admin roles",
        ("win_list_ad_groups", "win_get_ad_user", "win_run_powershell_json"),
    ),
)

# Leg tools not shipped yet (2026-10-24): ise I74 (#52), catc D60 (#107), cml L54 (#29) for cml_list_users;
# fmc_list_roles, fmc_get_external_auth and cml_get_auth_config are not filed on their servers yet.
PENDING_TOOLS = (
    "ise_list_admin_users",
    "ise_get_admin_access",
    "catc_list_users",
    "fmc_list_roles",
    "fmc_get_external_auth",
    "cml_list_users",
    "cml_get_auth_config",
)

# What counts as privileged (can change configuration or grant access), per platform.
PRIVILEGED = (
    "ISE Super Admin, System Admin, Network Device Admin, Policy Admin, or any group whose RBAC policy "
    "allows write data access; Catalyst Center SUPER-ADMIN-ROLE, NETWORK-ADMIN-ROLE or a custom role with "
    "write; FMC Administrator, Network Admin, Access Admin, Security Approver or a custom role with modify "
    "permissions; "
    "SD-WAN Manager netadmin or a user group with write on any feature; CML admin; AD Domain Admins, "
    "Enterprise Admins and every AD group a platform maps to one of these roles"
)


def parse_stale_days(stale_days: str) -> int:
    try:
        days = int(stale_days.strip() or "90")
    except ValueError:
        raise ValueError(f"stale_days must be a whole number of days, not {stale_days!r}") from None
    if not 1 <= days <= 3650:
        raise ValueError("stale_days must be between 1 and 3650")
    return days


@suite_prompt(
    title="Privileged access review across platforms",
    legs=LEGS,
    example={"stale_days": "90", "scope": "privileged"},
    pending=PENDING_TOOLS,
)
def suite_access_review(
    stale_days: Annotated[
        str, Field(description="An account with no login for this many days is stale (default 90)")
    ] = "90",
    scope: Annotated[
        str,
        Field(
            description="'privileged' (default): only accounts that can change configuration or grant "
            "access; 'all': every account, read-only ones included"
        ),
    ] = "privileged",
    servers: SERVERS_ARG = "",
) -> str:
    """Who can change what, on every connected platform: admin accounts, roles and the external groups that
    grant them on ISE, Catalyst Center, FMC, SD-WAN Manager and CML, with Active Directory resolving those
    groups to people. One table of accounts (platform, role, source, enabled, last login) and one list of
    findings: shared or generic admins, extra local admins, stale or disabled people still privileged,
    groups that grant more than intended, external auth without a local fallback. Skips the platforms whose
    server is not connected and says so. Read-only."""
    days = parse_stale_days(stale_days)
    scope = scope.strip().lower() or "privileged"
    if scope not in ("privileged", "all"):
        raise ValueError("scope must be 'privileged' or 'all'")
    plan = plan_legs(LEGS, servers)
    if not plan.legs:
        return plan.header

    steps: list[str] = []
    if plan.has("ise"):
        steps.append(
            f"ISE: {tool_ref('ise_list_admin_users')} (every admin: group, external or internal, enabled, "
            "never-disabled, change-password flags), then "
            f"{tool_ref('ise_get_admin_access')}(group=...) for each admin group that has members or an "
            "external AD group mapped: its RBAC menu and data access and the AD groups that grant it."
        )
    if plan.has("catc"):
        steps.append(
            f"Catalyst Center: {tool_ref('catc_list_users')} (users, roles, local or external) and "
            f"{tool_ref('catc_get_external_authentication')} (is external login on, the AAA server, the "
            "'Role=<role>' attribute ISE returns, whether local fallback is allowed)."
        )
    if plan.has("fmc"):
        steps.append(
            f"FMC: {tool_ref('fmc_list_users')} (roles, internal or external, enabled, password lifetime), "
            f"{tool_ref('fmc_list_roles')} (what each role in use may modify; custom roles in full) and "
            f"{tool_ref('fmc_get_external_auth')} (LDAP/RADIUS objects, group-to-role maps, CLI/shell access "
            "filter). Listing users needs FMC's User Management permission: if refused, report 'not "
            "readable with this account', not 'no users'."
        )
    if plan.has("catsdwan"):
        steps.append(
            f"SD-WAN Manager: {tool_ref('catsdwan_list_users')} (users, user groups, status, lock state; "
            f"externally authenticated users appear only in sessions) and {tool_ref('catsdwan_get_aaa')} "
            "(authentication order, RADIUS/TACACS servers, whether local fallback works when they fail)."
        )
    if plan.has("cml"):
        steps.append(
            f"CML: {tool_ref('cml_list_users')} (admin flag, groups, last login) and "
            f"{tool_ref('cml_get_auth_config')} (local or LDAP; the LDAP user and admin filter groups)."
        )
    if plan.has("win"):
        others = [leg.product for leg in plan.legs if leg.peer != "win"]
        mapped = (
            f"and every AD group the other legs ({', '.join(others)}) map to an admin role"
            if others
            else "and any AD group the user names as granting platform admin rights"
        )
        steps.append(
            f"Active Directory: resolve Domain Admins, Enterprise Admins {mapped}. Confirm each group exists "
            f"with {tool_ref('win_list_ad_groups')}, then list members recursively with "
            f"{tool_ref('win_run_powershell_json')} running read-only cmdlets only, e.g. "
            "\"Get-ADGroupMember -Identity '<group>' -Recursive | Get-ADUser -Properties Enabled,"
            "LastLogonDate,PasswordNeverExpires | Select-Object SamAccountName,Name,Enabled,LastLogonDate,"
            'PasswordNeverExpires". Without that tool, use '
            f"{tool_ref('win_get_ad_user')} on each account the other legs found (its group memberships)."
        )
    who = "privileged accounts only" if scope == "privileged" else "every account, privileged ones flagged"
    steps.append(
        f"Privileged means: {PRIVILEGED}. Table ({who}), one row per account per platform: person (the AD "
        "account when an external group grants it, else the local username), platform, role, source (local, "
        "AD group <name>, RADIUS/TACACS via ISE), enabled, last login, stale (no login for "
        f"{days} days or more, or never), findings. Sort by person, so one person's rights on every platform "
        "sit together."
    )
    steps.append(
        "Findings, most serious first, each with the platform and account: generic or shared admin accounts "
        "(admin, root, cisco, test); local admins beyond one documented break-glass account per platform; "
        f"privileged accounts stale for {days}+ days, disabled in AD but still local, or with passwords that "
        "never expire; AD groups that grant a broader role than their name suggests or that nobody owns; "
        "external authentication with no working local fallback (lockout risk) or local-only login (no "
        "central control); service and API accounts with more than a read or least-privilege role."
    )
    steps.append(
        "Name every platform not reviewed (leg skipped, permission refused, tool missing on that server) so "
        "the review is not read as complete. Never show passwords, shared secrets or keys. This workflow "
        "only reads: call no write tool, and list fixes (disable, remove from group, change role) as "
        "proposals."
    )
    intro = f"Privileged access review ({scope}; stale after {days} days)."
    return f"{plan.header}\n\n{intro}\n\n{numbered(steps)}"
