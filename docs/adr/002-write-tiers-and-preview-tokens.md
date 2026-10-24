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
2. The token is a random id held server-side with the tool name, a hash of the canonical arguments,
   the caller identity, a fingerprint of the target's current state and an expiry
   (`MCP_CONFIRM_TTL`, default 600 s). It is single-use.
3. Re-issuing the identical call with `confirm=<token>` recomputes the plan and executes it. Different
   args, a different caller, a changed target, a reused or expired token are rejected with an
   explanatory error, and any mismatch burns the token.
4. Tokens live in process memory: a restart invalidates outstanding previews (acceptable; the model
   just previews again). Multiple replicas would need sticky sessions or a shared store.

(Amended 2026-10-14 during M1: a server-side store replaced the originally proposed stateless HMAC
token, because single-use and target-changed checks need state anyway; this is the design proven in
netbox-mcp.)
5. Preview and execution are both written to the audit log with the caller identity.

(Amended 2026-10-24, C67: **confirm by elicitation.** `MCP_CONFIRM_MODE=token|elicit|auto`, default
`token`. With `auto` or `elicit` and a client that advertises form elicitation, step 3 happens inside the
preview call: the server asks the user "Apply this change?" through the client (a server-initiated
`elicitation/create` on handshake-era sessions; an `InputRequiredResult` whose sealed `request_state`
carries the step-2 token on 2026-07-28 sessions, redeemed against a fresh plan on the client's retry) and
applies on accept. `elicit` also refuses `confirm=<token>` for one-person changes, so only a human answer
confirms. Two-person changes, dry-run, blocked previews and closed change windows keep the token flow or no
confirm at all. The dialog's schema is a single boolean and its text is masked like the audit log: a
confirm never asks for a secret. Clients without elicitation keep tokens (`auto`) or get a blocked preview
(`elicit`).)

The generic `<prefix>_api_call` escape hatch is GET-only (a `read` tool) unless **dangerous** is
enabled; then it also accepts POST/PUT/PATCH/DELETE as a `dangerous`, preview-first tool. It is not
gated by `write`, because a raw call can reach deploys and deletions of live policy. Its responses go
through secret masking like every other tool.

## Consequences

- Two round trips per write; acceptable given the blast radius.
- Every write tool needs a `preview()` hook that can describe the change; core supplies diff helpers.
