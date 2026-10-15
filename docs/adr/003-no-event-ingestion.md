# ADR-003: Servers are pull-only (no event ingestion)

- Status: accepted
- Date: 2026-10-15
- Supersedes the event framework shipped in 0.2.0 (removed in 0.3.0)

## Context

A roadmap priority called "real-time / event-driven" was read two ways. The design that shipped in 0.2.0
had MCP servers *receive* data: webhook and syslog receivers fed an in-memory event buffer, background
pollers filled it, and a CML relay was planned to tunnel pushed traffic into the servers. The user's intent
was different: use the MCP servers to **look at logs and events on the platforms** when asked.

Receiving data would also turn each MCP server into a log collector, with listeners, buffers, retention,
inbound firewall paths and relays. Collecting logs is the job of a SIEM or syslog platform, not an MCP server.

## Decision

- MCP servers are **pull-only and on-demand**. A tool call queries the platform's own logs, events and
  history (e.g. ISE MnT auth history, Catalyst Center events/issues/audit logs, FMC audit records and
  health alerts, CML lab events, device `show logging` via CLI tools) and returns the answer.
- No background pollers, no event buffers, no inbound listeners (webhooks, syslog, eStreamer, pxGrid
  subscriptions), and no relays.
- The core's event package was removed in 0.3.0.

## Consequences

- Simpler servers: no new ports, firewall rules or state to manage.
- No historical record beyond what the platforms themselves keep. If one is needed later, the answer is an
  MCP server *in front of* a log platform (e.g. Splunk), not ingestion inside these servers.
