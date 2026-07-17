# Fundbox API Ingestion Design

## Goal and scope

Replace direct database access for the three scheduled Fundbox ingestion sources
with an authenticated, cursor-paginated backdoor API exposed by the Fundbox
consumer backend. The supported API sources are:

- `fundbox_consumer_backend` (users);
- `fundbox_consumer_backend:contacts`;
- `fundbox_consumer_backend:sales`.

Legacy, merged-user, and junk connectors remain database/dump-only. Manual batch
and dump ingestion remain available for the supported sources as operational
fallbacks.

## Ownership and boundaries

Fundbox owns relational extraction and assembly. It exposes source-shaped
composite records and does not generate HyperP source IDs, matching signals,
graph models, ingestion envelopes, retirement events, or HyperP lifecycle state.

HyperP owns conversion from the source-shaped records into its existing identity,
relationship, and sales envelopes. API connectors must preserve the source IDs,
record types, raw-payload semantics, exclusions, sales filters, and customer links
produced by the existing database connectors.

## Fundbox endpoints

The Fundbox backend adds three routes under a dedicated `auth.hyperp`
Basic-auth middleware so ingestion credentials cannot authorize other backdoor routes:

- `GET /api/v1/hyperp/ingestion/users`;
- `GET /api/v1/hyperp/ingestion/contacts`;
- `GET /api/v1/hyperp/ingestion/sales`.

Each route accepts:

- `limit`: bounded positive integer with server-defined default and maximum;
- `cursor`: optional opaque continuation cursor;
- `updated_since`: optional timezone-aware ISO-8601 timestamp.

Responses use this envelope:

```json
{
  "data": [],
  "meta": {
    "next_cursor": null,
    "has_more": false
  }
}
```

Controllers validate requests and delegate to source-specific query/services.
Services return explicit composite DTOs so each endpoint has a stable, testable
schema.

## Incremental traversal

`updated_since` is inclusive. Replaying the boundary is intentional because
HyperP ingestion is idempotent. A record is eligible when its effective update
timestamp is greater than or equal to the watermark.

The effective update timestamp is the maximum available timestamp across the
root and dependent rows that affect the composite:

- users: user, basic/basic-plus profile, addresses, social accounts, devices,
  and last-login data;
- contacts: the contact row;
- sales: order, order items, merchant/product associations, variants, products,
  merchant, and customer identity fields embedded for matching.

Tables without a usable update timestamp cannot independently advance the
effective timestamp; this limitation must be explicit in the query service and
covered by contract tests where timestamps exist.

Results are ordered by `(effective_updated_at, root_id)` ascending. The opaque
cursor contains that tuple, the source discriminator, and the original watermark
needed to continue the same traversal. Cursors are rejected when used for another
source. Keyset pagination uses a strict tuple comparison after the last
record, preventing duplicates within a traversal and preventing records with tied
timestamps from being skipped.

## Source behavior

### Users

Each record contains the user plus its basic profile, basic-plus profile,
addresses, social accounts, device IDs, and last login. Users excluded by the
existing Fundbox exclusion rules are omitted. HyperP maps each composite through
the existing user envelope builder.

### Contacts

Each record contains the contact and its owning Fundbox user ID. HyperP emits a
relationship record and preserves the existing link to
`fundbox_consumer_backend-user-{user_id}`.

### Sales

Each record contains the order, eligible order items, merchant details, resolved
product/variant information, and customer identity fields used by vehicle and
customer matching. Fundbox preserves the existing realized-sale policy: only
`acknowledged`, `to release`, and `completed` non-deleted orders are returned.
HyperP preserves existing order, line-item, product, and customer-link IDs.

## HyperP client and connectors

HyperP adds a Fundbox API client with Basic authentication, bounded retry/backoff
for transport failures and 5xx responses, pagination, and strict response
validation. Authentication failures, request validation failures, and malformed
response data fail immediately.

Three Fundbox API connectors consume the client and reuse shared Fundbox mapping
helpers. `get_connector(..., mode="api")` supports only the three scheduled
Fundbox source keys in addition to the existing POS API sources. Unsupported
Fundbox source keys fail before opening a network connection.

Configuration includes the API base URL, Basic-auth username and protected
password, page size, timeout, retry count, and incremental overlap duration.
Credentials and sensitive source fields must never be logged.

The staging Compose file is intentionally managed on the staging host outside
Git (commit `547572b`). Before deployment, operators must mirror the tracked
root Compose `FUNDBOX_API_*` environment keys into
`.docker/staging/docker-compose.yml`. The staging workflow fails fast when any
required key is absent.

## Scheduling and checkpoints

The three Fundbox Celery Beat entries switch from `batch` to `api`. Each source
has an independent durable successful watermark and a Redis snapshot of source
root IDs. Every run first reads its watermark, subtracts a small configurable
overlap, and performs an incremental traversal using `updated_since`. It then
performs a complete unfiltered traversal for reconciliation. Records returned by
the full traversal but not the incremental traversal are reprocessed; existing
record-hash idempotency makes unchanged records inexpensive while detecting
deleted children and records that leave or re-enter source eligibility.

After the full traversal, HyperP compares its prior root-ID snapshot with the
current IDs. Missing roots retire only their Fundbox-owned source records and
provenance-keyed projections; unified people, entities, and audit history remain.
The first successful run establishes a baseline and retires nothing.

The watermark and current-ID snapshot are stored only after the entire ingestion
run succeeds. Failed, rejected, or partial runs replace neither checkpoint. The
new watermark is based on the maximum effective timestamp successfully traversed,
not worker wall-clock time. Existing source locks continue to prevent concurrent
runs for the same source.

## Error handling and security

- Routes use dedicated Fundbox `auth.hyperp` Basic-auth credentials.
- Invalid limits, timestamps, and cursors return request validation errors.
- Cursors are opaque and integrity-protected so clients cannot alter traversal
  state.
- HyperP retries only transient failures with a bounded attempt count.
- A page is validated completely before its records are yielded.
- Logs contain source keys, request outcomes, and counts but exclude credentials,
  NRICs, contact data, and payload bodies.

## Testing and acceptance

Fundbox feature and service tests cover authentication, validation, page bounds,
cursor integrity, deterministic traversal, tied timestamps, inclusive watermarks,
sidecar-only updates, exclusions, sales status/deletion filters, and all three
composite response schemas.

HyperP tests cover authentication and request parameters, pagination, transient
retries, terminal errors, strict response validation, user/contact/sales envelope
parity, API-mode routing, unsupported sources, overlap behavior, full-snapshot
reconciliation, first-run baselining, source-scoped retirement, checkpoint
advancement only after success, and Celery schedules selecting API mode.

Validation includes targeted PHP and Python tests followed by relevant Fundbox
tests and HyperP ingestion formatting, lint, strict type, and test checks. Before
handoff, changes receive a hostile review for correctness, boundary traversal,
sensitive-data exposure, brittle tests, duplication, and compatibility with
database and dump modes.

## Non-goals

- API mode for legacy, merged-user, or junk Fundbox sources;
- removing database or dump connectors;
- push/webhook ingestion;
- changing HyperP matching, exclusion, sales-realization, or graph policies;
- emitting HyperP envelopes from the Fundbox application.
