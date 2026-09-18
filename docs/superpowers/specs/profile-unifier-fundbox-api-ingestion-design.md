# Fundbox API Ingestion Design

## Goal and scope

Replace direct database access for the three scheduled Fundbox ingestion sources
with an authenticated, cursor-paginated backdoor API exposed by the Fundbox
consumer backend. The supported API sources are:

- `fundbox` (users);
- `fundbox:contacts`;
- `fundbox:sales`.

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
`fundbox-user-{user_id}`.

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

The three Fundbox Celery Beat entries switch from `batch` to `api`. Scheduled
Fundbox execution uses the bounded change window described below: each admitted
occurrence advances one page and commits its receipt, usage, and resume position
in the same graph transaction, completing only at a terminal page. Occurrence
deadlines, leases, backoff, and budget are shared bounded-ingestion concerns
rather than Fundbox-specific ones.

The legacy `api` delta path remains available for manual runs. It performs a
single filtered traversal with no reconciliation pass and no inferred
retirement: a one-record delta never triggers an unfiltered population
traversal, and deletions are owned exclusively by the bounded tombstone stream
(root removals and eligibility changes), never inferred from a partial window.

The watermark and current-ID snapshot remain stored only after the entire legacy
ingestion run succeeds, in the same graph transaction that completes the
IngestRun. Failed, rejected, or partial runs replace neither checkpoint. Existing
source locks continue to prevent concurrent runs for the same source.

## Bounded change window

Status: **required by HyperP, not yet verified against a Fundbox deployment.**
Everything in this section is exercised against synthetic fixtures only (see
below). Fixture success is not upstream capability evidence and must never be
recorded as such.

The bounded request shape is a frozen change window rather than a timestamp
watermark. `GET /api/v1/hyperp/ingestion/{resource}` accepts, in addition to the
parameters above:

- `snapshot_id`: opaque identifier of one frozen source snapshot;
- `after_change_version`: exclusive lower bound of the window;
- `through_change_version`: inclusive upper bound of the window;

and `cursor` continues the same frozen window. The response repeats the frozen
window in `meta` and is rejected when it does not match the requested window:

```json
{
  "data": [
    {
      "kind": "upsert",
      "change_version": 41,
      "root_id": 7,
      "effective_updated_at": "2026-09-17T06:00:00Z",
      "composite": {}
    },
    {
      "kind": "tombstone",
      "change_version": 42,
      "root_id": 9,
      "effective_updated_at": "2026-09-17T06:05:00Z",
      "tombstone_reason": "deleted"
    }
  ],
  "meta": {
    "snapshot_id": "snap-2026-09-17",
    "lower_change_version": 0,
    "upper_change_version": 99,
    "next_cursor": null,
    "terminal": true,
    "cursor_expires_at": "2026-09-18T00:00:00Z"
  }
}
```

Changes are ordered strictly by `(change_version, root_id)`. An `upsert` carries
the complete composite, including joined children and relationship targets, so a
changed address, contact, or order item appears as a new complete composite for
the same root. A `tombstone` carries a deletion reason and no composite and is
the only signal that a root left the source. `terminal: true` with `data: []` is
a valid empty window and is the normal end of a traversal. `cursor_expires_at`
bounds how long HyperP may resume the same window; an expired cursor is reported
as `expired` and the run fails rather than silently rescanning.

Resume position: HyperP commits `continuation`, `last_position`
(`change_version`, `root_id`), `terminal`, and `cursor_expires_at` in the receipt
transaction of each page. A paused or crashed run resumes from that position
inside the same frozen window on the next eligible occurrence without a
population pass, and only reaches completion on a terminal page.
`cursor_retention_days` must cover the gap between occurrences (at least 30 days)
so an intervening period does not force a rescan.

`capability_evidence` inside the frozen window records how the window shape was
verified. It must equal `verified_upstream_contract`; every other value,
including self-reported or fixture-derived ones, is rejected as `rejected`, so
synthetic fixture success can never authorize a scheduled rescan.

Bounding is enforced on the HyperP side: record, nested-child, snapshot-length,
and cursor-length caps; a response byte cap with the body streamed and abandoned
as soon as it passes the cap (checked against `Content-Length` when present);
and the absolute occurrence deadline, which refuses any new upstream call at or
after the cutoff.

### Synthetic fixture scenarios

`services/ingestion/tests/test_fundbox_api_bounded.py` covers:

- bootstrap and bounded delta: one frozen page becomes exactly one unit, with no
  unfiltered population pass on either path;
- continuation: the persisted cursor is sent, and a repeated cursor or
  non-advancing source position is rejected;
- empty terminal window: `terminal: true`, `data: []`, checkpoint advances to
  terminal without error;
- joined-child-only change: the same root ID re-upserts with changed child data
  and reconciles in place;
- root deletion and eligibility loss/re-entry: a tombstone retires only the
  Fundbox-owned source evidence for that root; a removed child or a re-entered
  root is expressed as a new complete composite, never inferred from a partial
  window;
- expired cursor: reported as `expired`;
- invalid scope: unknown resource, malformed or foreign frozen window, oversized
  snapshot or cursor, unverified capability evidence, and response drift fail
  closed;
- rate limit and outage: `429`, `5xx`, and transport failures become a durable
  bounded backoff with no inline retry, while cursor expiry (`410`) and
  authentication (`401`/`403`) fail immediately;
- cutoff: no upstream call is made at or after the operation deadline or once
  cancellation is requested;
- write path: upserts and tombstones are applied in the caller-owned graph
  transaction, and the recorded unit usage carries the measured response bytes.

These scenarios exercise the HyperP adapter, its checkpoint arithmetic, and its
bounding only. They are evidence about this repository, not about a Fundbox
deployment; the `capability_evidence` marker is the only accepted proof of an
upstream contract.

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
parity, API-mode routing, unsupported sources, no-population-pass delta
behavior, checkpoint advancement only after success, and Celery schedules
selecting API mode. The bounded change-window contract and its synthetic fixture
scenarios are listed above; they are repository-level evidence only.

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
