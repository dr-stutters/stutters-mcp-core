# Argument vocabulary (C68)

One name per idea across every server in the suite. A model that has learned `since_hours` on the SD-WAN
server should be able to use it on ISE, Catalyst Center and FMC without reading each schema again. This page
is the vocabulary; `stutters_mcp_core.vocabulary` checks a server's tools against it.

## The names

| Name | Type | Meaning | Instead of |
|---|---|---|---|
| `since_hours` | number | Look back this many hours from now. The default unit for "recent" reads (events, alarms, audit, history). | `hours`, `last_hours`, `lookback_hours`, `window_hours` |
| `since_minutes` | number | Look back this many minutes, where hours are too coarse (live counters, profiles). | `minutes`, `last_minutes`, `lookback_minutes` |
| `since_days` | number | Look back this many days, where hours are too fine (reports, trends). | `days`, `last_days`, `lookback_days` |
| `wait` | boolean | `true`: block until the job or task finishes and return its result; `false`: return its id at once. | `wait_for`, `wait_for_completion`, `blocking`; an integer `wait` |
| `timeout_seconds` | number | How long a wait (`wait=true`, a convergence retry, a poll) may take before giving up, in seconds. | `timeout`, `wait_timeout`, `wait_seconds`, `max_wait`, `max_wait_seconds`, `timeout_minutes` |
| `response_format` | `"markdown"` \| `"json"` | Output shape: markdown for people, json for the complete data (`ResponseFormat`). | `format`, `output_format`, `fmt` |
| `limit` | integer | Page size: at most this many items. | `max_results`, `max_items`, `page_size` |
| `offset` | integer | Items to skip. Offset counts items, not pages. | `page`, `skip` |
| `detail` | string (or boolean) | How much to return per item: `summary` / `full`, or a section name. | `verbose` |
| `instance` | string | Which configured appliance on a multi-instance server (ADR-004). | |
| `device` | string | A device by host name, IP, serial or id; the tool resolves it. | `device_id` |
| `confirm` | string | The preview's single-use confirm token (ADR-002). | `confirm_token`, `confirmation` |

Rules of thumb:
- A lookback is `since_<unit>`, never a bare unit. Pick the coarsest unit that still fits the data's
  resolution; take one unit per tool.
- A duration you wait for is `timeout_seconds`, always in seconds. A product setting that happens to be a
  timeout (a RADIUS server's timeout, a session timeout) is not a wait: keep the product's name for it.
- `wait` is a yes/no. If you need to say how long, that is `timeout_seconds` next to it.
- `format` meaning a **file** format (`mermaid`, `drawio`, `csv`) is a different idea from `response_format`:
  keep `format` and add it to the lint's allow list.

## The lint

Warnings, never failures, so CI stays green while servers rename on their own schedule:

```python
from <pkg>._core.testing import warn_vocabulary

async def test_vocabulary():
    server = create_server(make_settings(mcp_enable_writes=True, mcp_enable_dangerous=True))
    await warn_vocabulary(server, "cml", allow={"cml_export_topology.format"})
```

Each finding is a `VocabularyWarning` in pytest's warnings summary, for example:

    cml_start_lab: parameter 'timeout': use 'timeout_seconds' (how long a wait may take before giving up, in seconds)
    fmc_download_realm: parameter 'wait' is integer, the vocabulary says boolean (for a duration use 'timeout_seconds')

- **alias** findings: a parameter uses one of the synonyms in the "Instead of" column.
- **type** findings: a parameter uses a vocabulary name with the wrong shape (an integer `wait`, a string
  `since_hours`, a `response_format` without `markdown` and `json`).
- `allow` silences a deliberate different meaning: `"tool.param"`, a bare `"param"` (every tool), or a glob
  (`"cml_*.timeout"`).
- `vocabulary_problems(server, prefix, allow=...)` returns the same findings as strings, for a server that
  wants to assert a budget (`len(...) <= N`) or to fail on new findings.
- `stutters_mcp_core.vocabulary.check_tool(name, input_schema)` works on a bare JSON Schema, with no server.

## Renaming a parameter

A rename keeps the old name working for at least one minor release:

1. Add the vocabulary name as the parameter the tool documents.
2. Keep the old name as a second, optional parameter whose description starts with `Deprecated: use <new>.`
   (the lint skips parameters described as deprecated). When both are given, the new name wins; when only the
   old one is, convert units if needed (`timeout_minutes * 60`).
3. Say it in the server's CHANGELOG (the release PR) and remove the alias in a later minor release.

## Where the suite stood (2026-10-24)

The lint run against each server's `main` with all tiers on: cml 9 findings (73 tools), ise 4 (150), catc 18
(191), fmc 9 (219), cat-sdwan 11 (82). Most are `hours` / `window_hours` -> `since_hours` (catc, cat-sdwan) and
`timeout` -> `timeout_seconds` (cml). The renames are per-server follow-ups.
