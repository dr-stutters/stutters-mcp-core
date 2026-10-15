# ADR-004: Several appliances per server (multi-instance), selected per call (`instance=`)

- Status: accepted
- Date: 2026-10-15
- Roadmap: G10 (#27)

## Context

Production sites run more than one of each product: a production and a lab ISE deployment, an FMC per
region, Catalyst Center per site. Today each server talks to exactly one appliance (`<P>_URL`), so an
operator runs one container per appliance and the model sees duplicate tool sets that differ only by the
server name. Builders asked for a way to reach several targets from one server without rewriting tools.

Options considered:

1. **Per-target tool namespaces** (mount one sub-server per target: `prod_ise_list_nads`, `lab_ise_list_nads`).
   Multiplies the tool count by the number of targets (ISE has ~70 tools), breaks the `<prefix>_` naming
   convention, prompts and cross-server manifests, and wastes context.
2. **An `instance` argument on every tool**, injected by core. One tool set; the model picks the appliance
   per call; tools don't change. (The issue said `target=`; several existing tools already have a `target`
   parameter, e.g. ise `ise_delete_object`, cml planning, so the injected argument is named `instance`.)
3. One container per appliance (status quo). Still supported and the right answer when targets need
   different credentials scopes, networks or write policy.

## Decision

Option 2, opt-in, with single-target behaviour unchanged.

- **Configuration.** The existing `<P>_URL` / `<P>_USERNAME` / ... settings are the **default instance**
  (named `<P>_DEFAULT_INSTANCE`, default `default`). `<P>_INSTANCES=prod,dr` adds named instances, each read with
  its own prefix `<P>_<NAME>_` (`ISE_PROD_URL`, `ISE_PROD_USERNAME`, `ISE_PROD_PASSWORD(_FILE|_REF)`, product
  fields such as `ISE_PROD_ERS_PORT`). An instance must set its own URL and credentials; any other field it
  doesn't set is inherited from the default instance (timeouts, retries, rate limits, product options),
  except TLS checking (`VERIFY_SSL`, `CA_BUNDLE`), every secret, and the fields a product lists in
  `Settings.per_instance_fields` (e.g. FMC's domain). The merged settings are validated as a whole.
  `MCP_*` server settings are global.
- **Selection.** When more than one instance is configured, `ToolRegistrar` wraps every tool with an optional
  `instance` argument (an enum of the configured names; omitted = the default instance) and sets a context
  variable for the call. `get_client(ctx)`, `get_settings(ctx)` and the spec store resolve per target
  through the lifespan state, so tool code is unchanged. With one target, no argument is added and schemas
  are identical to before.
- **Lifecycle.** The lifespan builds one client (and spec store) per instance and closes them all; the
  startup probe checks each. `<prefix>_status(instance=...)` reports one instance and lists all of them;
  `<server> check` checks every instance.
- **Safety.** A confirm token is bound to the instance it was previewed on (it can't be redeemed against
  another appliance); previews, results and audit events name the instance. Safety rails and roles are server-wide
  in this first version.

## Consequences

- Servers get multi-instance support by bumping core; no tool changes. Tools that cache per-appliance
  state outside the client object (module globals) must move it onto the client.
- Per-target write policy (e.g. read-only prod, two-person only on prod) and per-target roles are follow-ups;
  until then, run a separate container for a target that needs a different policy.
- Prompts and manifests are unaffected (tool names don't change); the `instance` argument appears in tool
  schemas only when configured.
- The selection is a context variable: it follows the call into awaited code, `asyncio.create_task`,
  `anyio`/`asyncio.to_thread`, but not `loop.run_in_executor` or a background task queue (FastMCP tasks);
  work handed off that way runs against the default instance. Pass the client explicitly there.
- The lifespan state for several instances is a `TargetState` dict: look entries up by key
  (`get_client(ctx)`); iterating or copying it shows the shared entries only.
- A tool that declares its own `instance` parameter can't be wrapped; registration fails loudly when
  instances are configured.
