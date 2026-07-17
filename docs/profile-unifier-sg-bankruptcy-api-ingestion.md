# SG Bankruptcy API Ingestion

## Goal

Allow HyperP to ingest Singapore bankruptcy records directly from the
`sgBankruptcyScraper` service while retaining the existing dump connector for
local and development workflows.

## Scope

This change spans two repositories:

- `sgBankruptcyScraper` exposes an authenticated, ingestion-specific export API.
- HyperP adds an API connector and accepts `api` mode for the `sgbankruptcy`
  ingestion source.

The scraper, extraction, matching, and bankruptcy graph-materialization rules
remain unchanged.

## Scraper export contract

The scraper adds a versioned endpoint:

```text
GET /api/v1/export/bankruptcy-records
```

The endpoint uses the existing API-key authentication mechanism. It accepts a
bounded `limit` and an opaque `cursor`. Results contain one row per bankruptcy
case and are ordered by the case's `last_seen_at` and `id`, both ascending. The
next cursor encodes the final returned row's ordering values, so cases sharing a
timestamp cannot be skipped. Cases without events remain present; cases with
multiple events include only the latest event by `(updated_at, id)`.

Each exported item contains the fields HyperP currently reconstructs from the
SQL dump:

- bankruptcy case ID and case number;
- identification number and person name;
- latest document type and date;
- event ID, type, and date;
- trustee name and firm;
- source document ID and document metadata needed for provenance;
- case first-seen and last-seen timestamps.

The response contains `items` and `next_cursor`. A null cursor means the export
is complete. Invalid cursors return a validation error, and database failures use
the scraper's existing error response contract.

## HyperP connector

HyperP adds a dedicated SG bankruptcy API client and connector. The connector:

1. requests pages until `next_cursor` is null;
2. validates every response at the HTTP boundary with typed Pydantic models;
3. maps each item to the same `SourceRecordEnvelope` shape emitted by the current
   dump connector;
4. preserves stable source record IDs and bankruptcy provenance fields;
5. raises a clear connector error for authentication, transport, response-shape,
   and non-success HTTP failures.

The API base URL, API key, request timeout, and page size are server-side
settings. Secrets are never logged or returned in task results.

Transport failures, HTTP 429, and 5xx responses use bounded exponential retry.
Authentication, other client errors, and response-schema failures are not
retried. `SGBANKRUPTCY_INGEST_CRON` enables the Celery beat entry that dispatches
`run_ingestion_task` with `("sgbankruptcy", "api")`; an empty value disables the
schedule.

Dump and API connectors use one canonical bankruptcy envelope builder so the
same case produces the same raw payload and record hash in either mode. Adopting
the canonical payload causes a deliberate one-time hash update for records last
ingested by the older full-row dump representation.

## Ingestion dispatch

`sgbankruptcy` supports two explicit modes:

- `api`: use the scraper export endpoint;
- `dump`: use a path relative to `DUMPS_ROOT`, preserving the existing local and
  development behavior.

API mode does not accept or require `dump_path`. Dump mode continues to require
it. Ingestion remains dispatched through `run_ingestion_task.delay`; the task
selects the connector and then uses the existing ingestion pipeline, including
match-only behavior for bankruptcy records.

No cursor checkpoint is persisted in HyperP for this change. Each run scans the
stable paginated export and relies on the existing idempotent source-record
upsert/update lifecycle. This avoids introducing checkpoint recovery semantics
while still bounding memory and request size.

## Testing

The scraper tests cover authentication, first and subsequent pages, stable
timestamp tie-breaking, terminal cursors, invalid cursors, and exported field
mapping.

HyperP tests cover multi-page retrieval, response validation, mapping parity with
dump mode, HTTP/authentication failures, source dispatch, required settings, and
Celery task selection for `api` and `dump` modes.

## Compatibility and security

The existing scraper UI endpoints and HyperP dump ingestion remain compatible.
The export endpoint returns sensitive identification numbers only to an
authenticated API principal. Logs must omit authorization headers, API keys, and
full response bodies containing protected identifiers. Successful export
responses set `Cache-Control: no-store`.
