# ADR-003: Events (sources, buffer, receivers) and workflow prompts

- Status: accepted
- Date: 2026-10-15 (core v0.2.0, roadmap C2–C4)

## Context

Priority 2 of the suite roadmap is real-time awareness: ISE MnT and pxGrid, FMC health/audit polling and
FTD syslog, Catalyst Center issue polling and webhooks, the CML event websocket. Four servers will build
on whatever core provides, so the shape has to be right once. Constraints from the roadmap:

- No shared broker, database or extra service. Each server keeps its own events.
- The model reads events through tools (and a resource); MCP push notifications are not reliable in
  clients yet (C10, Later).
- dCloud appliances cannot open connections to cortex (P1). Push sources reach the server through an
  SSH-tunnelled relay in CML: an outbound SSH session from cortex with remote port forwards, **TCP only**,
  landing on cortex's loopback.
- Servers on core 0.1.x must keep working unchanged.

## Decision

### Model

`Event(type, summary, time, keys, severity, data, id)`; the buffer adds `seq` and `received`, the
source adds `source`.

- `type` is dotted and lower-case, most general first (`auth.fail`, `health.alert`, `syslog.430002`) so
  prefix filters (`auth.*`) work.
- `keys` are the correlation keys (`mac`, `ip`, `user`, `device`, `site`, `rule`...). They are plain
  strings now; C7 adds normalisation and cross-server joins.
- `time` is the source's occurrence time (UTC); `received - time` is the **lag**.
- `id` is the source's own id. `(source, id)` is deduplicated by the buffer, so a poller and a webhook can
  both deliver the same thing.

### Buffer

`EventBuffer(capacity=MCP_EVENT_BUFFER_SIZE=5000)`: a ring buffer in process memory.

- `seq` is strictly increasing and never reused, so `after_seq` is a stable cursor for "what's new since
  my last look". Consumer cursors (`read_new(consumer)`) serve internal readers, e.g. a correlator.
- Per-source statistics: received, duplicates, evicted, last event time, lag.
- Filters: source, type (exact or prefix), key (`name=value` or a bare value against every key),
  severity, time window (ISO 8601 or an age like `15m`), summary text. Results come newest first.
- In memory only; a restart empties it. SQLite write-through is C6 (Next).

### Sources

`EventSource` is a supervised asyncio task, started after the status probe in the server lifespan and
stopped before the client closes.

- `run()` connects, calls `set_connected()`, then `emit()`s. When it returns or raises, the supervisor
  restarts it with exponential backoff (initial 1 s, cap 60 s, ±20 % jitter). The backoff resets after a
  run that emitted events or stayed up for `healthy_after` seconds. `SourceConfigError` stops retries
  (state `failed`), for example when a secret is missing.
- `status()` reports state (`connecting`, `connected`, `reconnecting`, `failed`, `stopped`, `disabled`
  with the reason), events, last event time, lag, errors, the last error, restarts, drops by reason and
  buffer evictions, plus source-specific fields (listen address, poll interval).
- `enabled=False` sources are listed but never started. `<prefix>_event_sources` then tells the model
  which setting turns them on.
- `PollingEventSource(name, poll(since), interval)` covers every server's first source. It dedupes by
  `id`, `id_of(event)` or a content hash, using a bounded seen-set that survives retries. It can swallow
  the first poll (`emit_initial=False`) and sorts each batch by time.

### Wiring and tools

`build_server(..., event_sources=lambda app: [...], event_options=EventOptions(...))`. Without
`event_sources` nothing changes (0.1.x compatibility). With it:

- `<prefix>_list_events`: filters (source, type, key, severity, since, until, text, after_seq,
  include_data) plus limit, offset and response_format; returns `latest_seq`.
- `<prefix>_event_sources`: source status and a buffer summary.
- The resource `events://<prefix>/recent`: the 50 newest events as JSON.
- `<prefix>_status` gains an `events` brief, and the instructions get one line about events.

**Tool-name collision.** Catalyst Center already has an assurance tool called `catc_list_events`. All
names come from `EventOptions` (`"{prefix}_list_events"` by default). Setting a name to `None` drops
that tool, and the resource can be turned off. The suite convention is that every server keeps the
**generic** name for the buffer, so prompts and the model can rely on `<prefix>_list_events`.
Catalyst Center therefore renames its assurance tool to `catc_list_assurance_events` (catc D2). A
server that cannot rename passes `EventOptions(list_tool="{prefix}_list_buffered_events")`.

### Inbound receivers (C4)

Both receivers are EventSources (`kind="push"`) configured by `MCP_*` settings. They are **off by
default** and enabled through `from_settings(settings, ...)`.

**WebhookReceiver**: `POST <MCP_WEBHOOK_PATH>/<source>` (default `/events/<source>`).

- Served on the MCP HTTP app, or on a dedicated listener at `MCP_WEBHOOK_HOST:MCP_WEBHOOK_PORT`. The
  dedicated listener works with stdio too and can be bound to 127.0.0.1 for the tunnel.
- **Security model.** The route sits outside MCP bearer auth, because the product can't hold our MCP
  token, and authenticates per source instead. Supported methods:
  - a shared-secret header (optionally with a scheme, e.g. `Bearer`);
  - an HMAC of the raw body (sha256 by default, hex or base64, optional `sha256=` prefix);
  - HTTP Basic.
- Secrets come from `MCP_WEBHOOK_SECRETS="source:secret,..."` (Basic: `source:user:password`) or from
  code, and are compared in constant time. There is **no unauthenticated mode**: without a secret (or
  with one shorter than 16 characters) the source fails closed and answers 503.
- The `/mcp` path keeps its bearer auth. Settings refuse a webhook path that overlaps `MCP_PATH` or
  `/health`.
- Checks run cheapest first: known source (404), sender allow-list (403, `MCP_WEBHOOK_ALLOW_IPS`), a
  per-source rate limit (429), header/basic auth (401), body size (413, `MCP_WEBHOOK_MAX_BODY`, checked
  against Content-Length and while streaming), HMAC (401), JSON (400). A parser error answers 422,
  never 500.
- `X-Forwarded-For` is never trusted. Behind the relay or a proxy the peer is the tunnel end, so the
  allow-list can only name that, and the secret is the real control.
- Replay protection (timestamped signatures) is product-specific and left to each parser.

**SyslogReceiver**: asyncio UDP and/or TCP listeners on `MCP_SYSLOG_HOST`
(default 127.0.0.1) and `MCP_SYSLOG_UDP_PORT` / `MCP_SYSLOG_TCP_PORT` (default 5514; empty disables one).

- Parses RFC 5424 (structured data, BOM) and RFC 3164, including the Cisco IOS form (sequence number,
  `*` timestamps) and the FTD/ASA form (year in the timestamp, `host : %FTD-6-430002:`). It extracts the
  Cisco `%FAC-SEV-MNEMONIC`. The parser never raises.
- TCP accepts both RFC 6587 framings per message: octet counting and newline. An oversized octet count
  closes the connection, because the stream can't be resynchronised. There is an idle timeout and a
  connection cap (`MCP_SYSLOG_MAX_CONNECTIONS`).
- Per-sender allow-list (`MCP_SYSLOG_ALLOW_IPS`): datagrams from other senders are dropped and TCP
  connections are closed. An empty list allows all senders and logs a warning.
- Per-sender rate limit (`MCP_SYSLOG_RATE_LIMIT` msgs/s). UDP drops excess messages and counts them.
  TCP applies backpressure (it stops reading) instead of dropping.
- `to_event(msg)` is the product hook. It returns None to filter a message, and an exception there is
  counted as `parse_error`.

**Deployment.** Inside a container the receivers listen on 0.0.0.0, and the compose `ports` mapping
chooses the exposure. Publishing on `127.0.0.1:` keeps them local while still reachable through the CML
relay's remote forwards. Use syslog over TCP through the relay, because it carries no UDP.

### Prompts (C2)

Workflows ship as MCP prompts named `<prefix>_<workflow>`, registered with `reg.prompt(...)`. Each
prompt carries `meta={"uses": [server prefixes], "tools": [...], "tier": ...}`.

- A prompt whose tier is disabled is hidden, the same way tools are.
- Bodies reference tools with `tool_ref("name")`, which renders the name in backticks.
- `assert_prompt_contract` renders every prompt and fails on unknown local tools, references to servers
  missing from `uses`, missing argument docs, or a bad name or description.
- Cross-server name checks are C9.

## Consequences

- Each server adds events with a factory and, usually, one `PollingEventSource` plus a product parser.
  Receivers are a few lines with `from_settings`.
- Events are lost on restart, and the buffer bounds how far back they reach. `<prefix>_event_sources`
  makes both visible (the oldest event and the eviction count).
- Webhooks and syslog are reachable from outside, so the receivers are off by default, fail closed
  without secrets, and are limited by size, rate and sender.
- Several replicas of one server would each hold different events. That is acceptable for the single
  container per server on cortex.
