# ADR-002: Write tiers and preview-first confirmation

- Status: accepted
- Date: 2026-10-14

## Context

The servers act on production-grade systems (ISE policy, firewall rules, Catalyst Center provisioning).
An LLM can call a tool with wrong arguments, and some actions (FMC deploy, ISE CoA/ANC, CatC SWIM
activation) disrupt traffic. NetBox-MCP's preview-first writes worked well in practice.

## Decision

Every tool declares one tier:

| Tier | Registered when | MCP annotations |
|---|---|---|
| `read` | always | `readOnlyHint=True` |
| `write` | `MCP_ENABLE_WRITES=true` | `readOnlyHint=False`, `destructiveHint` per tool |
| `dangerous` | writes **and** `MCP_ENABLE_DANGEROUS=true` | `destructiveHint=True` |

Tools for disabled tiers are not registered at all, so the model never sees them.

Write and dangerous tools are **preview-first**:

1. A call without `confirm` performs no change. It returns a preview (method + path, field diff
   against current state, side effects such as "requires deploy to 3 devices") and a confirm token.
2. The token is `HMAC(secret, tool, canonical-args-hash, caller-identity, expiry)`, single-use, with a
   TTL (`MCP_CONFIRM_TTL`, default 300 s).
3. Re-issuing the identical call with `confirm=<token>` executes it. Different args, a different
   caller, a reused or expired token is rejected with an explanatory error.
4. The secret is random per process unless `MCP_CONFIRM_SECRET` is set (needed for restarts/replicas).
5. Preview and execution are both written to the audit log with the caller identity.

The generic `<prefix>_api_call` escape hatch is a `read` tool for GET and goes through the same
guard (as `write`) for any other method.

## Consequences

- Two round trips per write; acceptable given the blast radius.
- Every write tool needs a `preview()` hook that can describe the change; core supplies diff helpers.
