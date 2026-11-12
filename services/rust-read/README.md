# Rust read spike (#491)

A disposable, read-only experiment: the ingestion lifecycle worker projects persons and the
identity-link revision stream from Neo4j into PostgreSQL, and this static Rust/axum binary serves
the five read endpoints from that projection through a dedicated `SELECT`-only role.

## Non-goals

- Not a replacement for the FastAPI API, and not routed in production. The nginx route is a plain
  404 unless `RUST_SPIKE_ENABLED=1`.
- No Rust Neo4j access, no write endpoints, no schema migration, no OAuth/Google token issuance
  (one static bearer token), no scheduler or HTTP bridge of its own.
- No performance, freshness-SLO or 24 h measurement claims; that work is L4 (#498).

## Prerequisites

- Docker Compose v2 with profiles.
- A disposable scratch or staging environment with explicitly provisioned non-default secrets;
  never production. Running the comparison on staging belongs to L4 (#498).
- `services/rust-read/postgres/01-schema.sql` and `02-read-only-role.sh` are mounted into
  `rust-spike-pg`'s `/docker-entrypoint-initdb.d`; PostgreSQL runs them once, on an empty volume.

## Environment variables

| Variable | Purpose |
|---|---|
| `RUST_SPIKE_ENABLED` | `1` enables the nginx route and the lifecycle projection task. Default `0`. |
| `RUST_SPIKE_POSTGRES_PASSWORD` | Password of the writable DB owner `spike_writer` (owner/writer login). |
| `RUST_SPIKE_READ_PASSWORD` | Password of the read-only `spike_reader` login. |
| `RUST_SPIKE_DATABASE_URL` | `spike_reader` DSN for this binary, e.g. `postgresql://spike_reader:<pw>@rust-spike-pg:5432/spike_projection`. URL-encode the password. |
| `PROJECTION_POSTGRES_DSN` | Writer DSN for the lifecycle worker, e.g. `postgresql://spike_writer:<pw>@rust-spike-pg:5432/spike_projection`. Same password as `RUST_SPIKE_POSTGRES_PASSWORD`. URL-encode the password. |
| `PROJECTION_SYNC_INTERVAL_SECONDS` | Projection sync interval for the beat schedule. Default `30`. |
| `RUST_SPIKE_BEARER_TOKEN` | Static bearer token required on every spike route. |
| `RUST_SPIKE_CURSOR_SECRET` | HMAC-SHA256 key for identity-link cursors. |

Passwords must be URL-encoded inside DSNs. Passwords containing `@`, `:` or `/` will otherwise
break URL parsing.

## Run

```bash
RUST_SPIKE_ENABLED=1 \
RUST_SPIKE_POSTGRES_PASSWORD=... \
RUST_SPIKE_READ_PASSWORD=... \
RUST_SPIKE_DATABASE_URL='postgresql://spike_reader:...@rust-spike-pg:5432/spike_projection' \
PROJECTION_POSTGRES_DSN='postgresql://spike_writer:...@rust-spike-pg:5432/spike_projection' \
RUST_SPIKE_BEARER_TOKEN=... \
RUST_SPIKE_CURSOR_SECRET=... \
docker compose --profile rust-spike up --build rust-spike-pg rust-read lifecycle-worker beat web
```

The spike stack is on the internal `rust-spike` network only; nothing is published directly.

## Requests (through nginx)

```bash
TOKEN=...  # RUST_SPIKE_BEARER_TOKEN
curl -s -H "Authorization: Bearer $TOKEN" http://localhost/api/spike/v1/persons/summary
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost/api/spike/v1/persons?limit=20&sort_by=profile_completeness_score'
curl -s -H "Authorization: Bearer $TOKEN" http://localhost/api/spike/v1/persons/p-0001
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost/api/spike/v1/identity-links/events?limit=50'
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost/api/spike/v1/identity-links/snapshot?limit=50'
```

Every response uses the API envelope: `data`, `meta.request_id`/`next_cursor`/`total_count`, and
`display_items` (list/detail/summary); identity pages additionally carry `through_revision`
(events) or `snapshot_revision` (snapshot). Errors use
`{"error":{"code","message","details"},"meta":{...}}`. Send `X-Request-Id` to make it deterministic.

## Readiness and logs

The reader answers 503 with the standard error envelope until the first projection sync commits
(`summary_counter.last_success` is `NULL`): list, detail and summary return
`projection_unavailable`, identity events return `identity_link_stream_unavailable`, and snapshots
return `identity_link_snapshot_not_ready` until the baseline is marked ready. Watch sync progress
with:

```bash
docker compose logs -f lifecycle-worker
```

## Fixtures and tests

`tests/fixtures/*.json` are deterministic API-shaped documents (list item, detail, summary, one
resolved and one unresolved identity revision, error envelopes, and full list/events/snapshot
envelopes). Tests:

- `tests/contracts.rs` — no database: envelope/auth/validation shapes against a lazy pool, fixture
  round-trips, and identity/offset cursor rules.
- `tests/endpoints.rs` — PostgreSQL-backed; returns early unless `RUST_READ_TEST_DATABASE_URL` is
  set. Seeding/truncation additionally require `HYPERP_RUST_READ_TEST_DISPOSABLE=1` and
  `RUST_READ_TEST_WRITER_URL`; the schema and `spike_reader` role must already be applied
  (`01-schema.sql`, then `02-read-only-role.sh`).

CI runs `cargo test --locked` with those variables against a disposable database.

## Cleanup

```bash
docker compose --profile rust-spike stop rust-spike-pg rust-read
docker compose --profile rust-spike rm -f rust-spike-pg rust-read
docker volume rm <project>_rust-spike-pg-data
```

Init scripts run only on an empty volume: changing passwords, roles or the schema requires removing
the volume first.

## Known differences from the FastAPI list

- `q` is a case-insensitive substring match over name/NRIC/email/phone with a simple match-count
  relevance score; FastAPI uses the Lucene `person_name_search` index, so ranking differs. L4 (#498)
  chooses comparison queries.
- Invalid `sort_order`, `entity_key_mode`, `source_key_mode`, `cursor`, malformed date filters, or
  unknown query parameters return 400 `invalid_request` here; FastAPI is lenient (or fails
  differently) for some of these.
- Text ordering uses the PostgreSQL database collation; Neo4j orders by code point.
- `summary` and `person detail` reject any query string with 400 (FastAPI ignores extras).

## Known limitation

Hard-deleted non-Person graph elements that leave no timestamped affected-Person signal are not
reconciled by the incremental projection. Recover with a full projection reset (remove the
`rust-spike-pg-data` volume and let the writer backfill).
