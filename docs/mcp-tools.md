# MCP tools and resources

All tools return the application envelope as `structuredContent` (SPEC §3.2)
plus a concise text fallback:

```json
{
  "ok": true,
  "request_id": "req_...",
  "timestamp": "2026-08-20T04:20:00+00:00",
  "data": { },
  "error": null,
  "warnings": [],
  "item_errors": [],
  "metadata": { }
}
```

`ok: true` means the gateway completed the operation lifecycle; per-item
Bloomberg failures appear in `item_errors` alongside useful data. Gateway-wide
failures set `isError` on the MCP result.

Every tool advertises `inputSchema` and `outputSchema` (`additionalProperties:
false` on inputs, so unknown input elements are rejected) and tool
annotations (`readOnlyHint`, `idempotentHint`, `openWorldHint`).

Protocol note: on `2026-07-28` each call carries the per-request `_meta`
envelope (`io.modelcontextprotocol/protocolVersion`,
`io.modelcontextprotocol/clientCapabilities`); the SDK client sets this and
the routing headers (`MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`)
automatically.

## Discovery tools (scope `bloomberg:discover`)

| Tool | Purpose |
|---|---|
| `blpapi_list_services` | Known services (configured ∪ opened ∪ allowlisted) with open/discover/execute state |
| `blpapi_open_service` | Open an allowlisted service (idempotent) |
| `blpapi_describe_service` | Operations list, optionally with JSON Schemas |
| `blpapi_describe_operation` | Request/response JSON Schemas, schema hash, discovery/execution policy |
| `blpapi_validate_request` | Validate parameters against the live schema without submitting; returns cost estimate |

## Generic request tools

| Tool | Scope | Purpose |
|---|---|---|
| `blpapi_send_request` | `bloomberg:generic-request` + operation scope | Execute any policy-permitted schema-defined request |
| `blpapi_get_request` | `bloomberg:result-read` | State/results of a principal-owned request; page large results via `cursor` (`"1"`, `"2"`, ...) |
| `blpapi_cancel_request` | `bloomberg:result-read` | Idempotent cancellation |

`blpapi_send_request` accepts `client_request_id` for idempotency (dedup
window `requests.deduplication_window_seconds`; replays return the existing
request with `idempotent_replay: true`), and `execution`:

- `wait_seconds` — how long to block before returning a handle;
- `overall_deadline_seconds` — hard deadline → native cancel + `TIMED_OUT`;
- `response_mode` — `canonical` (default), `typed` (`$blp_type` markers), or
  `normalized` (registered operations only);
- `allow_canonical_fallback` — fall back to canonical when no normalizer
  exists instead of `NORMALIZER_NOT_AVAILABLE`;
- `preview_items` — preview size for resource-linked large results.

Large results exceeding `requests.inline_result_bytes` are stored as JSONL
artifacts and returned as an `artifact` block with `resource_uri`
(`bloomberg-result://res_.../metadata`) and a bounded preview.

## Subscription tools (scope `bloomberg:subscribe`)

| Tool | Purpose |
|---|---|
| `blpapi_subscribe` | Create a group of market-data topics (per-item native correlation tokens, bounded buffer, TTL) |
| `blpapi_read_subscription` | `mode: latest` snapshot or `mode: changes` with cursors; long-poll via `wait_seconds` (bounded) |
| `blpapi_resubscribe` | Whole-group replacement: same id, new generation, new item ids, cursors invalidated, gap warning |
| `blpapi_cancel_subscription` | Idempotent cancel |
| `blpapi_list_subscriptions` | Caller's groups (all groups with admin scope) |

## Curated tools (normalized output, generic engine underneath)

| Tool | Underlying operation |
|---|---|
| `get_reference_data` | `//blp/refdata ReferenceDataRequest` |
| `get_historical_data` | `//blp/refdata HistoricalDataRequest` |
| `get_intraday_bars` | `//blp/refdata IntradayBarRequest` |
| `get_intraday_ticks` | `//blp/refdata IntradayTickRequest` |
| `search_instruments` | `//blp/instruments instrumentListRequest` |
| `search_curves` | `//blp/instruments curveListRequest` |
| `search_government_securities` | `//blp/instruments govtListRequest` |
| `search_fields` | `//blp/apiflds FieldSearchRequest` |
| `get_market_snapshot` | `//blp/refdata ReferenceDataRequest` (default snapshot fields) |

Normalized payloads carry `normalized_schema_version`, `source_service`,
`source_operation`, `schema_hash`, `columns`, `rows` and
`untrusted_text_fields`. Reference/historical tools accept `overrides`
(object form, converted to the Bloomberg `fieldId`/`value` override
sequences; duplicates and empty names rejected).

## Resources (SPEC §3.11)

| URI | Content |
|---|---|
| `bloomberg://services` | Known-service summaries |
| `bloomberg://service/{percent-encoded}` | One service descriptor |
| `bloomberg://operation?service={enc}&name={enc}` | Operation schemas + policy |
| `bloomberg-result://{id}/metadata` | Result metadata (owner-bound) |
| `bloomberg-result://{id}/page/{n}` | Bounded result page |
| `bloomberg-subscription://{id}/latest` | Latest subscription values (owner-bound) |

URIs are parsed structurally; service names are percent-encoded.
