# SG Gov Source Ingestion Design

## Scope

Add manual-dispatch ingestion support for the Singapore government dump sources present under `.dumps/`:

- `.dumps/sgbankruptcy_2026-05-11.sql`
- `.dumps/sgrentalflats_2026-05-11.sql`

No Celery beat schedules are added. Ingestion remains explicitly dispatched through `run_ingestion_task.delay(...)`.

## Source metadata

Bootstrap a new `Entity`:

- `entity_key`: `sggov`
- `display_name`: `SG Gov`
- `entity_type`: `government`
- `country_code`: `SG`

Bootstrap two `SourceSystem` nodes under that entity:

- `sgbankruptcy` — Singapore bankruptcy register dump.
- `sgrentalflats` — Singapore rental flats dump.

`sgrentalflats` is metadata-only in this pass. It is not registered as a person-ingestion connector because the dump contains flat/address inventory, not person records; sending those address-only rows through the current pipeline would create non-person `Person` nodes.

## Dump profiling summary

`sgbankruptcy_2026-05-11.sql` contains:

- 484 `bankruptcy_cases` rows.
- 484 `case_events` rows.
- 38 `source_documents` rows.
- NRIC-like `identification_number` values and `person_name` are fully populated in the case/event rows.

`sgrentalflats_2026-05-11.sql` contains:

- 280 `flats` rows.
- 26 `towns` rows.
- 280 `flat_changes` rows.
- Flat address fields are populated, but there are no person identifiers.

## Bankruptcy ingestion behavior

Add an `sgbankruptcy` connector that parses the PostgreSQL dump `COPY` sections directly instead of restoring a PostgreSQL database. It should read only the tables needed for person and case materialization:

- `bankruptcy_cases`
- `case_events`
- `source_documents` when document context is available

The connector yields one system source record per bankruptcy case. Each record includes:

- `source_record_id`: stable case-based key such as `bankruptcy_case:{id}`.
- `observed_at`: use the case `last_seen_at`.
- `record_hash`: SHA-256 over the normalized raw payload.
- identifiers: verified `nric` from `identification_number` when present.
- attributes: `full_name` plus bankruptcy-specific facts such as case number, document type, document date, event type, trustee name, and trustee firm when present.
- raw payload: joined case/event/document data for auditability.

## Bankruptcy graph extension

Keep normal person resolution behavior: the source record should resolve to or create a `Person` via the existing ingestion pipeline using NRIC/name.

After a bankruptcy source record is linked to a person, materialize a first-class case node:

```cypher
(:BankruptcyCase {
  source_system_key,
  source_case_id,
  case_number,
  document_type,
  document_date,
  event_type,
  event_date,
  trustee_name,
  trustee_firm,
  first_seen_at,
  last_seen_at,
  raw_payload
})
```

Relationships:

```cypher
(Person)-[:HAS_BANKRUPTCY_CASE {source_record_pk, observed_at}]->(BankruptcyCase)
(SourceRecord)-[:DESCRIBES_CASE {linked_at}]->(BankruptcyCase)
```

Do not add separate `BankruptcyEvent` nodes in this pass. Event details remain on the case node and in the source record raw payload.

## Schema changes

Add idempotent Neo4j schema statements to `infra/neo4j/init.cypher`:

- unique constraint for `BankruptcyCase` on `(source_system_key, source_case_id)`.
- index for `BankruptcyCase.case_number`.
- index for `BankruptcyCase.event_date`.

The ingestion service already applies this script on startup, so the new schema will be applied before ingestion runs.

## Implementation units

1. Add source/entity seed entries in `services/ingestion/src/graph/bootstrap.py`.
2. Add a reusable PostgreSQL dump `COPY` parser scoped to connector needs.
3. Add `SGGovernmentBankruptcyConnector` and register it as `sgbankruptcy`.
4. Add bankruptcy case Cypher queries and graph-write helper.
5. Call the helper after the existing source record is linked to a person for `sgbankruptcy` records.
6. Add tests for dump parsing, connector output, and bankruptcy graph query parameters.
7. Run ingestion lint/type/test checks for the touched service.

## Out of scope

- No Celery beat schedule entries.
- No PostgreSQL restore container or runtime source database.
- No `sgrentalflats` person-ingestion connector.
- No UI/API pages for bankruptcy cases.
- No separate `BankruptcyEvent` node model in this pass.
