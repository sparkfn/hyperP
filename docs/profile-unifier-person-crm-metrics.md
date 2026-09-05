# Profile Unifier — Live Bitrix CRM activity metrics

**Status:** Implemented by issue #387
**Last updated:** 5 September 2026

## 1. Purpose

The person CRM dashboard combines two independently fetched sources without
pretending that an unavailable live source is a confirmed zero:

1. effective-active CRM deals and Open Lines conversations already represented in
   Neo4j; and
2. bounded, request-time Bitrix CRM activity metadata for the person's active deal
   owners.

The former combined `/crm/metrics` contract is removed. It could not distinguish a
complete zero from missing graph activity data.

## 2. API contracts

Both operations are authenticated, require `persons:read`, are mounted through the
active `/app/v2` application, are catalogued for MCP, and return `ApiResponse[T]`.

### `GET /v1/persons/{person_id}/crm/deal-metrics`

Operation ID: `get_person_crm_deal_metrics`.

Returns graph-backed:

- deal totals, stages, first/last dates, 30-day counts, daily trend, and change;
- Open Lines conversation totals, last date, 30-day count, trend, and change;
- graph CRM last-touch metadata;
- deal/conversation entity attribution.

It does not expose activity or call fields.

### `GET /v1/persons/{person_id}/crm/activity-metrics`

Operation ID: `get_person_crm_activity_metrics`.

Returns the discriminated union `PersonCrmActivityMetrics`, selected by `status`:

- `complete`: exact aggregates, including a confirmed zero;
- `partial`: lower-bound aggregates from validated rows read before a bound or
  upstream failure stopped the request;
- `unavailable`: no aggregate can safely be reported.

All variants include source, source-instance, fetch time, cache disposition,
resolved/query deal counts, and request/page/row counters. Partial and unavailable
variants carry a safe typed failure reason. The stable source value is
`bitrix_crm_activity`.

## 3. Deal scope and graph authority

`Neo4jCrmDealMetricsRepository` follows one `MERGED_INTO` edge to the canonical
person and applies the existing effective-active reader predicates:

- active `LINKED_TO` relationship;
- `lifecycle_status = active`, or the migration fallback `is_latest = true` when
  lifecycle status is absent;
- Bitrix source authority;
- configured `source_instance_id`;
- `source_entity_type = deal` and a non-null `source_entity_id`.

The scope query requests `deal_limit + 1` identifiers. Seeing the extra row proves
the deal ceiling was exceeded without scanning or sending an unbounded owner set to
Bitrix. In that case live metrics are `unavailable` with `deal_limit` and no Bitrix
I/O occurs.

Graph metric subqueries keep deals and conversations isolated so cardinality does
not multiply. Daily series count distinct records, not distinct timestamps. Entity
authority prefers `OWNED_BY` and falls back to the source system's `OPERATED_BY`
entity.

## 4. Bounded Bitrix read

Every `crm.activity.list` request includes both:

- `OWNER_TYPE_ID = 2` (deal); and
- a non-empty bounded `@OWNER_ID` list.

A portal-wide request is forbidden. Owner IDs are split into configured batches.
The adapter selects metadata only: identifiers, owner fields, activity type,
timestamp candidates, direction, completion, provider, and result status. It never
selects or retains subject, description, comments, recordings, attachments, or raw
payloads.

The event timestamp uses this precedence:

1. `START_TIME`;
2. `CREATED`;
3. `LAST_UPDATED`.

Returned rows must belong to a requested deal owner. Malformed rows, malformed JSON
or envelopes, foreign owners, and invalid/naive timestamps stop the read safely.
Activity IDs are deduplicated across pages and owner batches before complete or
partial aggregation.

## 5. Bounds and failure semantics

Configuration provides hard ceilings for:

- deal owners;
- owner batch size;
- elapsed time;
- attempts and total requests;
- total pages;
- total examined rows;
- concurrency;
- cache TTL and entry count.

Shared counters reserve request, page, and row budget under an async lock before the
corresponding work, so concurrent batches cannot overshoot a ceiling. Retries consume
the same request budget. Non-integer, boolean, repeated, or non-advancing cursors are
rejected.

A failure after at least one validated activity produces `partial`; a failure before
any validated activity produces `unavailable`. Partial values are lower bounds and
must be displayed with `≥`. Unknown activity and call classifications remain
explicit rather than being discarded.

## 6. Cache, coalescing, and cancellation

The in-process cache stores complete aggregate models only. It never stores raw
Bitrix responses or activity rows. Cache hits retain the original `fetched_at` and
change only `cache_disposition`.

Identical concurrent reads coalesce onto one shared task. Waiters are counted. A
caller's cancellation does not cancel work still needed by another waiter; when the
last waiter leaves, unfinished shared work is cancelled. Task completion cleanup is
independent of the original caller, preventing stale in-flight entries. Complete
results are inserted deterministically and the oldest insertion is evicted when the
entry bound is exceeded.

## 7. Frontend behavior

Browser code calls only the split Next.js BFF routes:

- `/bff/persons/{id}/crm/deal-metrics`;
- `/bff/persons/{id}/crm/activity-metrics`.

Each BFF handler is a thin proxy and forwards the request `AbortSignal`. The panel
starts both reads independently. Deal and conversation presentation remains visible
while the live request is loading, partial, unavailable, or failed.

Presentation rules:

- unavailable/loading activity and call cards show an em dash and `Unavailable`,
  never `0`;
- partial totals and breakdown counts show `≥`;
- complete zero is rendered as zero and may show the empty-activity message;
- activity/call chart series are included only for complete data;
- the parent badge adds activity and call totals only for complete data;
- entity rows show only graph-backed deals and chats because live activities are not
  attributed per entity.

## 8. Configuration

`BITRIX_ACTIVITY_API_URL` is a server-only secret-bearing webhook URL. It is never
exposed through `NEXT_PUBLIC_*`, responses, logs, documentation examples, or browser
code. Root and staging Compose define the same activity configuration variables.

Other settings are:

- `BITRIX_ACTIVITY_SOURCE_INSTANCE`;
- `BITRIX_ACTIVITY_TIMEOUT_SECONDS`;
- `BITRIX_ACTIVITY_ELAPSED_SECONDS`;
- `BITRIX_ACTIVITY_DEAL_LIMIT`;
- `BITRIX_ACTIVITY_OWNER_BATCH_SIZE`;
- `BITRIX_ACTIVITY_MAX_ATTEMPTS`;
- `BITRIX_ACTIVITY_MAX_REQUESTS`;
- `BITRIX_ACTIVITY_MAX_PAGES`;
- `BITRIX_ACTIVITY_MAX_ROWS`;
- `BITRIX_ACTIVITY_MAX_CONCURRENCY`;
- `BITRIX_ACTIVITY_CACHE_TTL_SECONDS`;
- `BITRIX_ACTIVITY_CACHE_MAX_ENTRIES`.

## 9. Explicit non-goals

This change does not:

- persist CRM activities, calls, or Bitrix response payloads;
- change ingestion or Intelligence inputs;
- perform portal-wide Bitrix reads;
- attribute live activities to graph entities;
- call FastAPI directly from browser code;
- expose the webhook URL or other credentials.

## 10. Validation

Behavioral coverage includes owner scoping, no-network deal ceiling, pagination,
request/page/row bounds, retries, timeouts, malformed data, deduplication, complete
only cache behavior, coalescing and cancellation, graph mapping/scope, split route
responses, MCP parity, BFF signal forwarding, and frontend complete/partial/
unavailable rendering.
