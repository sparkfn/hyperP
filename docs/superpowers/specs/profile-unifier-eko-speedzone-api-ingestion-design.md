# Eko and SpeedZone API Ingestion Design

## Goal

Replace the direct database requirement for scheduled Eko and SpeedZone customer
and sales ingestion with an authenticated API mode. The POS OAuth server exposes
two stable, tenant-aware, read-only endpoints; HyperP owns normalization into its
existing identity and sales source-record envelopes.

This change applies to `eko_phppos`, `eko_phppos:sales`, `speedzone_phppos`, and
`speedzone_phppos:sales`. Existing direct-database and dump ingestion remain
compatible.

The same four scopes also expose a durable **bounded** contract (issue #434) so a
scheduled delta run can pause, resume, and replay without re-reading a whole
tenant. The bounded adapter shares the OAuth transport and the canonical
envelope mappers with the legacy API mode but has its own continuation, retry,
and writer contract. Legacy traversal is never selected as a bounded fallback.

## Architecture

The POS OAuth server adds two static custom endpoints:

- `GET /api/v1/custom/hyperp/customers`
- `GET /api/v1/custom/hyperp/sales`

Static endpoints are preferred over dynamic table-read descriptors because the
sales resource requires explicit multi-table assembly and composite pagination.
They use the gateway's existing OAuth authentication, tenant selection, scope
checks, per-instance database pool, sanitized errors, discovery, and auditing.

HyperP adds API connector implementations behind the existing `SourceConnector`
boundary. Customer and sales connectors share an HTTP transport and response
validation layer, while source-specific mappers retain Eko and SpeedZone custom
field behavior. `run_ingestion(..., mode="api")`, its Celery task, and the CLI
select these connectors only for the four supported source keys.

## Endpoint contract

Both endpoints accept:

- `limit`: optional page size with a conservative default and enforced maximum.
- `cursor`: optional opaque, versioned continuation token.
- `updated_since`: optional timezone-aware ISO 8601 timestamp.

Both return:

```json
{
  "data": [],
  "pagination": {
    "next_cursor": null,
    "has_more": false
  }
}
```

The cursor encodes only continuation state, is validated by the server, and is
not interpreted by HyperP. Invalid cursors or query parameters return HTTP 400.
The server fetches one extra record to determine `has_more`; it emits a next
cursor only when another page exists.

### Customers

Customer rows contain the `phppos_people` and active `phppos_customers` fields
currently consumed by the Eko and SpeedZone connectors. Optional loyalty and
custom fields are nullable or omitted when an older POS schema lacks them.
Employees and deleted customers are excluded by the endpoint.

Rows are ordered and continued by `person_id`. `updated_since` selects rows whose
`phppos_people.last_modified`, falling back to `create_date`, is at or after the
requested timestamp. The timestamp comparison is inclusive so a caller can
replay the boundary safely; immutable HyperP source facts and idempotent source
record IDs handle duplicates.

### Sales

Each sales row contains one `phppos_sales` record with its line items, referenced
item and category values, and the customer/person values required by the existing
sales mapper. This prevents partial sales from being split across pages.

Rows are ordered and continued by `(sale_time, sale_id)`. `updated_since` applies
an inclusive lower bound to `sale_time`. A sale is either returned completely or
not returned; page limits count sales rather than line items.

## Configuration and authentication

HyperP configuration supplies the POS OAuth base URL, confidential OAuth client
credentials, and tenant identifier for Eko and SpeedZone. Credentials remain in
environment configuration and never appear in Celery arguments, cursor payloads,
or logs. The confidential client is associated with an active service principal
whose tenant assignments authorize the required read scopes.

The shared client obtains short-lived, access-token-only credentials through the
OAuth `client_credentials` grant using HTTP Basic client authentication. It
requests the least-privilege scope set for the selected source: customer
ingestion requests `pos.customers.read`; sales ingestion requests
`pos.sales.read`, `pos.items.read`, and `pos.customers.read`. The client caches
the token and absolute expiry in process, refreshing shortly before expiry. A
401 response invalidates the local token and triggers one request retry with a
new token. Token endpoint transport failures, HTTP 429 responses, and HTTP 5xx
responses retain the bounded exponential-backoff policy.

Service-principal tokens are independently renewable and contain no refresh
token. HyperP therefore has no refresh-token setting, Redis credential bundle,
distributed token-rotation lock, or token-rotation idempotency key. Each worker
maintains its own local cache. The client continues to send the
`x-pos-tenant-id` header expected by the gateway on each ingestion request.

The endpoints declare the least-privilege read scopes needed for customer and
sales data. Scope enforcement remains in the gateway and endpoint context. The
OpenAPI discovery document exposes both operations and their parameters.

## Ingestion behavior

API mode streams one validated page at a time and yields canonical envelopes to
the unchanged pipeline. It does not accumulate a complete tenant snapshot in
memory. The customer endpoint feeds identity connectors; the sales endpoint
feeds sales connectors.

Transient connection failures, HTTP 429 responses, and HTTP 5xx responses use
bounded exponential backoff. The first API 401 invalidates the cached token and
gets one retry with a newly issued token. A repeated 401, other authentication or
authorization failures, malformed payloads, and other non-transient 4xx failures
fail immediately. A failed page is not treated as complete and its continuation
cursor is not advanced.

Each source has an independent durable successful watermark. HyperP sends it as
the inclusive `updated_since` bound on every page and replaces it only in the
same graph transaction that completes the ingestion run.

## Compatibility and errors

`batch`, `backfill`, and `dump` retain their current behavior. `dump_path` remains
valid only for dump mode. API mode rejects unsupported source keys before opening
an HTTP connection.

Missing required POS tables or unexpected database failures become sanitized
gateway errors with trace IDs; internal driver details are logged only on the
POS server. HyperP validates every response at the HTTP boundary with strict
typed models and reports the endpoint, tenant, page context, and trace ID without
logging credentials or sensitive row contents.

## Bounded resumable deltas

Everything below is the bounded contract. It is additive: the sections above
describe the legacy unbounded API mode, which keeps its existing behavior.

### Adapter-local descriptors

`connectors/phppos_api/bounded_descriptor.py` exports `DESCRIPTORS` with one
descriptor per scope (`eko_phppos`, `eko_phppos:sales`, `speedzone_phppos`,
`speedzone_phppos:sales`). `connectors.registry` discovers the module by the
`*.bounded_descriptor` naming convention, so the adapter is registered without a
central list. Each descriptor owns its connector version (`phppos-bounded-v1`),
configuration version (`phppos-bounded-config-v1`), checkpoint schema version
(1), and its own writer instance.

Bootstrap and delta are supported; there is no one-time mode. A bounded run never
falls back to legacy API, dump, or direct-database traversal — an inadmissible
window, cursor, or response fails the unit instead.

### Tenant isolation

Each scope resolves a tenant-dedicated credential set and principal. The
transport refuses to read a tenant with a principal that is not that tenant, and
the connector refuses any page whose tenant differs from the tenant recorded in
the durable frozen window, so a run cannot continue another tenant's cursor,
records, or credentials.

### Frozen window and capabilities

Every request carries `snapshot_id` and `upper_change_version` from the frozen
window; the bounded path never sends `updated_since`. A window declares
`contract_version: phppos-bounded-v1`, `snapshot_id`, `upper_change_version`, a
timezone-aware `retention_until`, and the source capabilities
`effective_changes`, `tombstones`, `complete_sale_aggregates`, and
`independent_tenant_principal`, plus `replay_retention_days` (at least 30). All
four capabilities are required before a run is admitted. A window whose retention
has lapsed is refused as `expired`.

The same frozen window pins every page of the run, so a replayed unit re-fetches
an identical page.

### Records and mappings

A page carries discriminated records: `upsert` (a complete row aggregate) and
`tombstone` (an explicit source removal with a reason). Absence from a page is
never a tombstone. Page-local `(source_id, effective_change_version)` pairs must
be unique.

`source_id` is the source row's own identity — `person_id` for customers and
`sale_id` for sales — and must equal the identity inside the row aggregate. The
adapter derives the HyperP source record id as
`{source_key}-customer-{source_id}` or `{source_key}-sale-{source_id}`, exactly
the ids the canonical mappers produce, so a tombstone retires the record its
upsert created.

Upserts are mapped with the existing `build_customer_envelope` /
`build_sales_envelope` helpers, so bounded source facts are identical to
direct-database and dump facts. Sale aggregates must be complete (every line
belongs to the sale) and explicitly bounded (at most 200 lines). A tombstone is
written through the shared retirement path, which retires the source record,
appends the retired identity-link revision, and recomputes CRM deal counts.

### Cursor and phases

There is exactly one phase per stream: `phppos_api:customers` and
`phppos_api:sales`. Continuation lives entirely in the typed cursor:

- `page_cursor`: the opaque source cursor, or `null` for the first page;
- `record_offset`: how many records of the current frozen page are already
  committed;
- `page_replay_id`: a deterministic replay identity hashed from the window
  fingerprint, page cursor, offset, and terminal flag;
- `terminal` + `terminal_marker`: the stream is complete.

A unit converts at most `max_records_per_unit` (500) records. When a frozen page
still has unconverted records, the next checkpoint keeps the same page cursor and
advances the offset; otherwise it moves to the next page cursor at offset `0`, or
closes the stream with the terminal marker when the source reports no further
page. Offsets never skip ahead: a page that shrank below a committed offset fails
the unit.

### Bounded transport

One fetch operation reads exactly one frozen page and streams its bytes. Per unit
the adapter bounds requests (8, covering three OAuth attempts, three page
attempts, and one re-authorization pair), bytes (2,000,000), records (500), and
pages (1). Retry loops are limited by both the attempt policy and the remaining
allowance, so the declared ceiling is never exceeded.

HTTP 429 becomes a source backoff with the source's `retry_at` (moved to the next
scheduled occurrence when it lands beyond the drain window); transport errors and
5xx responses retry with bounded backoff and then fail sanitized; a single 401
re-authorizes once, and a repeated 401 fails. Every failure message is a fixed
string — no token, raw payload, tenant secret, trace, or opaque cursor is echoed,
and cancellation or a passed deadline stops the read before more bytes are
consumed.

### Unit output and resume

`PhpposBoundedWriter.apply(tx, context, unit)` performs every write of a unit
inside the transaction the bounded control store already opened for that unit, so
unit output, the unit receipt, and the checkpoint commit atomically and a
checkpoint never advances ahead of its output. It calls the canonical pipelines
inside that transaction (`IngestPipeline.ingest_in_transaction`,
`ingest_sales_record_in_transaction`, `retire_source_evidence_in_transaction`) —
those entry points exist only so a caller can supply the transaction; the
existing session-owning entry points are unchanged.

Each record yields exactly one disposition: `committed`, `duplicate` (an
idempotent replay or an already-retired record), `excluded` (the shared run-level
exclusion policy) or `policy_dropped` (a match-only drop). An unsupported record,
a malformed retirement marker, or any pipeline error fails the whole unit, which
leaves the checkpoint unadvanced and lets the next attempt replay it.

Because the window is frozen and the replay identity is deterministic, replaying
a crashed attempt re-fetches the same page, re-slices it at the same offset, and
produces byte-identical records.

### Legacy versus bounded contract

| Concern | Legacy API mode | Bounded deltas |
|---|---|---|
| Continuation | in-memory page loop | durable typed cursor, unit by unit |
| Selection | `run_ingestion(..., mode="api")` | descriptor registry (`bootstrap`/`delta`) |
| Deletion | not reported | explicit tombstones retire source records |
| Resume after a crash | restarts from the beginning | resumes at the committed cursor |
| Progress watermark | Redis watermark per source | checkpoint `source_window` + cursor |
| Memory | one page at a time | one page slice at a time |
| Retry ceiling | client attempts | per-unit request/byte/record allowance |
| Failure surface | sanitized exception | sanitized exception, `source_backoff`, or a failed unit |

## Testing

The POS OAuth server tests:

- authentication, required scopes, and tenant isolation;
- customer employee/deleted filtering and optional legacy columns;
- customer and sales `updated_since` boundaries;
- keyset continuation without omissions or duplicates;
- complete sale aggregation across line items;
- invalid cursor and limit handling;
- sanitized database failures; and
- OpenAPI/custom-endpoint registration.

HyperP tests:

- client-credentials token requests, least-privilege source scopes, and
  access-token-only responses;
- process-local token reuse, expiry refresh, and one refresh after an HTTP 401;
- strict response validation and opaque cursor traversal;
- transient retry and non-transient failure behavior;
- Eko and SpeedZone mapping parity with direct-DB fixtures;
- API mode routing for all four supported source keys;
- rejection of unsupported API sources and invalid `dump_path` combinations;
- Celery task argument forwarding; and
- CLI mode selection.

Additional bounded-delta tests run without live source credentials, using the
synthetic fixtures under `services/ingestion/tests/fixtures/phppos_api/`:

- frozen-window capability admission, retention expiry, and source/tenant drift;
- cursor replay identity, terminal rules, and tampered or malformed state;
- page discrimination of upserts and tombstones and duplicate-identity rejection;
- request, row, byte, deadline, and cancellation bounds, and sanitized 429/5xx/401
  failures that never echo a token, payload, or opaque cursor;
- in-page continuation, page advance, terminal closure, and deterministic replay
  of a frozen page;
- tombstone retirement through the shared path, exclusion dispositions, and
  fail-closed behavior on unsupported or malformed records; and
- registry discovery of all four scopes, least-privilege scopes per resource, and
  refusal to serve a tenant with another tenant's principal or window.

Implementation follows test-driven development: each contract behavior is first
captured by a focused failing test, then implemented minimally. Final validation
includes targeted tests, TypeScript build/tests in the POS server, and ingestion
Ruff, strict mypy, and pytest checks in HyperP. A final hostile review covers
cursor edge cases, authorization, sensitive-data logging, contract drift,
duplicate records, and schema compatibility.

## Out of scope

- API mode for sources other than Eko and SpeedZone.
- Changes to HyperP canonical identity, sales, or graph contracts.
- Write access to either POS database.
- Removal of direct-database or dump connectors.
- Shared or durable access-token caching across ingestion workers.
- Bounded-run scheduling, admission, and delivery wiring: this contract defines
  the adapter the shared bounded runtime drives. Selecting the four scopes for a
  scheduled occurrence, and deriving each delta run's frozen window from the
  previous committed `upper_change_version`, remain with the shared dispatcher.
- Changes to the bounded runner, control store, task runtime, scheduler, or
  Compose topology.
