# Eko and SpeedZone API Ingestion Design

## Goal

Replace the direct database requirement for scheduled Eko and SpeedZone customer
and sales ingestion with an authenticated API mode. The POS OAuth server exposes
two stable, tenant-aware, read-only endpoints; HyperP owns normalization into its
existing identity and sales source-record envelopes.

This change applies to `eko_phppos`, `eko_phppos:sales`, `speedzone_phppos`, and
`speedzone_phppos:sales`. Existing direct-database and dump ingestion remain
compatible.

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
