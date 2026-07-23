# Subdivide the `system` source-record type into `identity` / `public_record` / `relationship`

**Date:** 2026-06-05
**Status:** Approved (design)

## Problem

`RecordType` currently has three members — `system`, `conversation`, `sales`. The
`system` bucket conflates conceptually different records that may eventually warrant
different matching criteria against existing persons:

- **identity** — first-party identity from a transactional system of record
  (Fundbox users/legacy/merged, Eko, SpeedZone).
- **public_record** — government / third-party registers about a person
  (SG bankruptcy) or about places (SG rental flats).
- **relationship** — a record whose subject is a *different* person, e.g. Fundbox
  emergency contacts that feed `KNOWS`.

## Goal & hard invariant

Replace `RecordType.SYSTEM` with the three subtypes so they can carry distinct
matching rules later. **For this change, matching behaviour is unchanged**: all
three subtypes behave exactly as `system` does today. The mechanism is a
`SYSTEM_FAMILY` set that every former `== SYSTEM` / `!= SYSTEM` branch consults.

This is a representation change, not a behaviour change. The only user-visible
effect is the `record_type` value shown on source records.

## Decisions (from brainstorming)

- **Representation:** replace `SYSTEM` in the `RecordType` enum (not an additive
  sub-field). Requires a Neo4j backfill + widening the API `Literal`.
- **`fundbox:merged` → `identity`** (carries the person's own
  NRIC/email/phone; matches today's behaviour).
- **`sgrentalflats` → `public_record`** (SG gov dataset; routing is unaffected —
  it is already routed as address-only by `source_system`, never reaching the
  match engine, so the value is purely a label).
- **Scope:** full vertical — ingestion + matching + Neo4j backfill + API types +
  frontend types/labels + docs. Fold in the known `sales` gap (API `Literal`
  never included `sales`) while widening.

## Source → subtype mapping

| Source (`source_system`) | New `record_type` |
|---|---|
| `fundbox`, `:legacy`, `:merged`, `eko_phppos`, `speedzone_phppos` | `identity` |
| `fundbox:contacts` | `relationship` |
| `sgbankruptcy`, `sgrentalflats` | `public_record` |
| WhatsApp / Bitrix | `conversation` (unchanged) |
| sales connectors | `sales` (unchanged) |

## Implementation

### 1. Ingestion domain model — `services/ingestion/src/models.py`
- `RecordType` members become `IDENTITY`, `PUBLIC_RECORD`, `RELATIONSHIP`,
  `CONVERSATION`, `SALES`.
- Add `SYSTEM_FAMILY: frozenset[RecordType] = {IDENTITY, PUBLIC_RECORD, RELATIONSHIP}`.
- `SourceRecordEnvelope.record_type` default `SYSTEM` → `IDENTITY`.
- The `_check_record_type_invariants` validator keys on `== CONVERSATION` with an
  else-branch covering all non-conversation types — logic unchanged; update the
  docstring wording (`system` → `non-conversation`).

### 2. Envelope builder — `services/ingestion/src/connectors/fundbox/builders.py`
- `build_envelope` `record_type` param: `Literal["system","conversation","sales"]`
  default `"system"` → `Literal["identity","public_record","relationship","conversation","sales"]`
  default `"identity"`. Update docstring.

### 3. Connectors set explicit non-identity subtypes
- Contacts — `connectors/fundbox/contacts.py` and `connectors/dumps/connectors.py`
  `_build_fundbox_contact` → `record_type="relationship"`.
- `connectors/sggov/bankruptcy.py` → `record_type="public_record"`.
- `connectors/sggov/rental_flats.py` → `record_type="public_record"`.
- Identity connectors (Fundbox users/legacy/merged, Eko, SpeedZone) rely on the
  new `identity` default — no edits.

### 4. Matching — behaviour-preserving core
- `matching/deterministic.py`: `== RecordType.SYSTEM` → `in SYSTEM_FAMILY`;
  `!= RecordType.SYSTEM` → `not in SYSTEM_FAMILY`.
- `matching/engine.py`, `matching/heuristic.py`: default params
  `= RecordType.SYSTEM` → `= RecordType.IDENTITY`; update docstrings.
- Conversation-keyed logic (`heuristic.py` promotion, `pipeline.py:253` chat
  machine-units) untouched.

### 5. Neo4j backfill — idempotent, run at startup
- New `BACKFILL_RECORD_TYPE_SUBTYPES` Cypher constant in
  `services/ingestion/src/graph/queries/maintenance.py` (exported via the
  queries `__init__`).
- New `backfill_record_type_subtypes(client)` in
  `services/ingestion/src/graph/migrations.py`, called from
  `initialize_ingestion_graph()` after `bootstrap_entities_and_sources`.
- Cypher:
  ```cypher
  MATCH (sr:SourceRecord) WHERE sr.record_type = 'system'
  SET sr.record_type = CASE
    WHEN sr.source_system ENDS WITH ':contacts' THEN 'relationship'
    WHEN sr.source_system IN ['sgbankruptcy','sgrentalflats'] THEN 'public_record'
    ELSE 'identity' END
  RETURN count(sr) AS updated
  ```
  Idempotent: after the first pass no `record_type='system'` rows remain.

### 6. API — `services/api/src`
- Add shared alias in `types.py`:
  `SourceRecordTypeLiteral = Literal["identity","public_record","relationship","conversation","sales"]`.
- `types.py` `SourceRecord` and `PersonTimelineGroup` `record_type` use the alias;
  default `"system"` → `"identity"`.
- `types_requests.py` imports the alias for `IngestRecord.record_type`; default
  `"identity"`; validator else-branch already covers all non-conversation values.
- Source-records route `record_type` filter param is `str | None` — passes the
  new values through unchanged.

### 7. Frontend — `services/frontend2/src`
There is **no** `record_type` filter UI (source records filter by entity), so this
is types + display only:
- Widen unions to the five values: `api-types-person.ts` `SourceRecordType`,
  `api-types-ops.ts` `IngestRecordType`, and the inline union in `api-types.ts`
  `SourceRecord.record_type`.
- Display: `titleCase` already renders `public_record` → "Public Record". Keep the
  existing `=== "conversation"` colour/badge ternaries (the system-family subtypes
  fall through to the neutral/system styling, which is correct). Render badge text
  via `titleCase` rather than the raw value where it currently shows the raw value.

### 8. Docs
- `CLAUDE.md`: update the "Source record types" decision bullet and record-type
  prose to the five values + the `SYSTEM_FAMILY` concept.
- Canonical `docs/profile-unifier-graph-schema.md` and
  `docs/profile-unifier-entity-and-sales.md` record-type references.

### 9. Tests
- Update fixtures asserting `"system"`: ingestion `test_sggov_*` →
  `public_record`; API `test_person_mappers`, `test_person_timeline`,
  `test_source_records_tab` → `identity`.
- Add:
  - Per-connector subtype assertions (bankruptcy/rental → `public_record`,
    contacts → `relationship`, fundbox/eko/speedzone → `identity`).
  - **Matching-equivalence regression** (linchpin): `identity`, `public_record`,
    `relationship` produce identical `MatchResult`s on the same inputs
    (deterministic verified-NRIC merge, heuristic score, no-match).
  - Backfill migration test (system → correct subtype by `source_system`;
    idempotent on second run).

## Risks
- Behaviour drift is the only real risk; the matching-equivalence test pins it.
- Backfill correctness depends on stored `source_system` values; the plan verifies
  the exact stored strings before finalizing the `WHERE` lists.
