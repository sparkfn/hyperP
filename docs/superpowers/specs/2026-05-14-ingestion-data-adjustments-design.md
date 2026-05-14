# Ingestion Data Adjustments Design

## Goal

Upgrade ingestion so the platform can load machine-unit evidence, use editable hard exclusions, reprocess changed source records safely, and make chat extraction deterministic by timestamp order.

This spec follows the approved MachineUnit graph design in `2026-05-14-machine-unit-graph-design.md`.

## Scope

This design covers:

- extracting machine-unit observations from PHP POS sales, Fundbox sales, Bitrix chat, and WhatsApp chat;
- writing MachineUnit links from sales orders and resolved buyers;
- treating chat machine-unit observations as inquiry/claim evidence unless explicit ownership is stated;
- loading a JSON exclusion-list file for company and personnel identifiers;
- adding a placeholder editable file, gitignore entry, and Docker bind mounts;
- versioned reingest for changed existing source records;
- matching-engine adjustment boundaries and stopping point;
- sorting chat messages by timestamp before LLM extraction.

This design does not add UI for machine-unit or ownership review. It should preserve current ingestion dispatch through Celery.

## Current context

Fundbox sales line payloads already include unit metadata fields:

- `metadata.lta_tag`
- `metadata.serial_no`

PHP POS sales line payloads already include:

- `metadata.serialnumber`

The chat extraction prompt and typed extraction result already include inquiry fields:

- `inquiries[].lta_tag`
- `inquiries[].serial_number`
- `inquiries[].unit`
- `inquiries[].machine_product`

Sales records currently bypass identity normalization and matching, then write `Order`, `LineItem`, `Product`, and `Person -[:PURCHASED]-> Order` when the customer identity record has resolved.

Identity source records currently skip only when the same source, same source record ID, and same record hash already exist. A changed hash currently creates another SourceRecord without explicit version/deactivation semantics.

## Machine-unit observation model

Add a shared ingestion-side observation shape for machine-unit evidence. It should be concrete and typed, not an unstructured dictionary.

Fields:

- `lta_tag`: string or null.
- `serial_number`: string or null.
- `machine_product`: string or null.
- `unit_label`: string or null.
- `source_kind`: one of `sales`, `chat_inquiry`, `explicit_ownership_claim`.
- `source_system_key`: source system key.
- `source_record_id`: source record ID.
- `observed_at`: source observation timestamp.
- `confidence`: numeric confidence where available.
- `quality_flag`: normalized quality flag.
- `raw_context`: short source-specific context safe to persist, such as line item ID or chat inquiry note.

At least one of `lta_tag` or `serial_number` must be present after normalization before a MachineUnit node is written.

## Source extraction

### Fundbox sales

Extract from each sales line item:

- `metadata.lta_tag` → MachineUnit LTA tag.
- `metadata.serial_no` → MachineUnit serial number.
- product display/model fields → `machine_product` when available.
- line item identifier → `raw_context`.

For each sales order with one or more valid observations:

- upsert/find `MachineUnit` nodes;
- link `Order -[:INVOLVES_UNIT]-> MachineUnit`;
- when the sales customer resolves to a Person, link `Person -[:BOUGHT_UNIT]-> MachineUnit`.

Do not create `OWNS_UNIT` from Fundbox purchase data unless a source field explicitly states current ownership.

### PHP POS sales

Extract from each sales line item:

- `metadata.serialnumber` → MachineUnit serial number.
- product name/display fields → `machine_product` when available.
- line description or line item ID → `raw_context`.

Do not infer LTA tag from PHP POS text fields unless a confirmed LTA-specific field or validated parser is added later.

For valid observations, write the same sales links as Fundbox:

- `Order -[:INVOLVES_UNIT]-> MachineUnit`;
- `Person -[:BOUGHT_UNIT]-> MachineUnit` when the customer resolves.

Do not infer `OWNS_UNIT` from purchase data.

### Bitrix chat

Use existing LLM extraction output:

- `inquiries[].lta_tag`
- `inquiries[].serial_number`
- `inquiries[].machine_product`
- `inquiries[].unit`
- `inquiries[].notes`

Default chat observations are `chat_inquiry` evidence. They may support review-only matching but do not imply purchase or ownership.

Create `explicit_ownership_claim` only when the extracted note or future structured field explicitly states current ownership. If the existing LLM output cannot reliably distinguish ownership, the first implementation should not create `OWNS_UNIT` from chat.

### WhatsApp chat

Use the same chat inquiry fields as Bitrix. Preserve participant/session/company-phone exclusions before machine-unit observations are written.

Default WhatsApp observations are inquiry evidence, not purchase or ownership.

## MachineUnit writes

MachineUnit writes should live in a focused graph write module, separate from connector extraction.

Sales write behavior:

1. Persist or update the sales `SourceRecord` version.
2. Merge `Order`, `LineItem`, and `Product` as today.
3. Upsert/find each MachineUnit from valid observations.
4. Link `Order -[:INVOLVES_UNIT]-> MachineUnit`.
5. If customer Person is resolved, link `Person -[:BOUGHT_UNIT]-> MachineUnit`.
6. If customer Person resolves later through pending sales drain, create missing `BOUGHT_UNIT` links at that time.

Chat write behavior:

1. Persist the conversation `SourceRecord` version.
2. Upsert/find each MachineUnit from valid inquiry observations.
3. Link the conversation SourceRecord to the MachineUnit with an inquiry relationship or source-record provenance relationship.
4. Create `OWNS_UNIT` only for explicit current ownership claims.
5. Flag ownership conflicts when multiple active explicit owners are linked to the same MachineUnit.

Relationship `MERGE` keys should include source provenance fields that uniquely identify the observation version. For versioned reingest, include `source_record_pk` when the relationship represents evidence from a specific SourceRecord version; use stable business keys only for durable business relationships that should be refreshed in place, such as one Person purchasing one Order.

## Exclusion-list file

Add JSON file support for editable hard exclusions.

Recommended committed placeholder:

- `config/ingestion-exclusions.example.json`

Recommended gitignored local file:

- `config/ingestion-exclusions.local.json`

The placeholder should contain empty arrays and documented keys, without real identifiers:

```json
{
  "phones": [],
  "emails": [],
  "names": [],
  "source_ids": []
}
```

Add `config/ingestion-exclusions.local.json` to `.gitignore`.

Add a setting such as `INGESTION_EXCLUSIONS_FILE=/app/config/ingestion-exclusions.local.json`. If the file path is configured but the file is missing, ingestion should fail clearly so a missing bind mount does not silently disable exclusions. If the setting is blank, file-based exclusions are disabled and only environment-configured exclusions apply.

Bind mount the local file into ingestion containers that need it:

- `worker`
- one-shot/CLI ingestion container if used through compose profiles
- `beat` only if scheduled ingestion loads settings in-process and needs the file path

Because this repository requires compose parity, any root `docker-compose.yml` change must be mirrored in `.docker/staging/docker-compose.yml` with the corresponding relative path.

File exclusions should merge with existing environment-configured exclusions:

- `company_mobile_numbers`
- `company_email_addresses`
- `internal_person_names`

Normalization should reuse the existing exclusion helper for phone, email, name, and source ID checks. Malformed JSON should fail fast at ingestion startup with a clear error, not silently ignore exclusions.

## Versioned reingest workflow

Use versioned SourceRecords for changed existing records.

Behavior by source, source record ID, and hash:

1. If no SourceRecord exists for the source record ID, ingest normally as version 1.
2. If the latest version has the same record hash, skip unchanged data.
3. If the latest version has a different record hash, create a new SourceRecord version and rerun downstream processing from the new version.

Do not update changed records in place. Preserve previous SourceRecord versions for audit and lineage.

Recommended SourceRecord version fields:

- `source_record_version`: incrementing integer per source record ID.
- `is_latest`: boolean.
- `superseded_at`: timestamp or null.
- `superseded_by`: relationship from old SourceRecord to new SourceRecord, or equivalent relationship such as `SUPERSEDED_BY`.

When a new version is created:

- mark the old latest version as not latest;
- set its `superseded_at`;
- link old version to new version;
- deactivate old derived relationships that should no longer contribute active evidence;
- write new normalized evidence and rerun matching.

Derived data handling:

- Identifier, address, and fact links created from the old SourceRecord should become inactive or no longer be considered active.
- New links from the new SourceRecord become active evidence.
- Golden profile recomputation should run after new identity evidence is linked.
- Sales writes should MERGE/update the existing Order/LineItem/Product graph and refresh source-version-specific links without duplicating purchases or MachineUnit relationships.

The implementation must distinguish unchanged duplicates from changed reingests before deciding whether to skip.

## Matching engine adjustments and stopping point

MachineUnit matching is a review-only signal.

Candidate generation may add candidates by traversing from incoming MachineUnit observations to Persons connected through:

- `BOUGHT_UNIT`
- `OWNS_UNIT`

Scoring/reasoning may add features such as:

- `same_lta_tag`
- `same_serial_number`
- `same_machine_unit_purchase`
- `same_machine_unit_owner_claim`
- `machine_unit_ownership_conflict`

Rules:

- MachineUnit evidence must never trigger deterministic merge.
- MachineUnit-only evidence must stay in review range at most.
- MachineUnit evidence must be fanout-capped. Units linked to too many Persons should be ignored for matching and logged as high-fanout.
- Chat inquiry evidence should be weaker than sales purchase evidence.
- Explicit ownership claims should create stronger review reasons but still not auto-merge.
- Ownership conflicts should route to review/ops handling instead of resolving automatically.

Stopping point before implementation:

- confirm exact review reasons;
- confirm max score contribution for sales purchase, chat inquiry, and explicit ownership claim;
- confirm fanout caps;
- confirm whether the first implementation should only create review cases from MachineUnit evidence or also modify heuristic scoring.

Do not proceed past this stopping point without user approval.

## Chat timestamp ordering

Conversation text sent to the LLM must be ordered oldest to newest, matching the prompt text `newest messages last`.

### WhatsApp

Ensure every message list is sorted by source timestamp before formatting and before selecting `observed_at`.

Live WhatsApp paths that already order by timestamp should keep doing so. Dump/import paths should use the same sort helper so row order cannot change extraction output or record hash.

### Bitrix

Build a single combined event list from available message-log sources, including personalized messages and sent template messages. Sort the combined list by timestamp before formatting.

Keep deal title as context header. Do not treat deal title as a timestamped chat message unless a reliable deal timestamp is available.

Missing timestamps should sort after timestamped messages from the same source chunk, with a stable secondary key such as source table and row ID.

## Error handling and observability

- Log counts of excluded records/persons by reason, without logging sensitive raw identifiers beyond existing connector behavior.
- Log changed reingest versions separately from unchanged duplicate skips.
- Log MachineUnit observations created, skipped as invalid, and skipped as high-fanout.
- Malformed exclusion JSON should fail ingestion startup with a clear error.
- MachineUnit write failures should fail the enclosing record transaction so partial graph updates are not committed.

## Testing

Add focused tests for:

- JSON exclusion file loading and merging with env exclusions.
- Invalid exclusion JSON failing clearly.
- Fundbox sales extracting LTA tag and serial number observations.
- PHP POS sales extracting serial number observations.
- Chat extraction payloads producing inquiry observations without inferred ownership.
- Sales writes creating `INVOLVES_UNIT` and `BOUGHT_UNIT` without `OWNS_UNIT`.
- Pending-customer sales drain creating missing `BOUGHT_UNIT` after identity resolution.
- Versioned reingest skipping unchanged records.
- Versioned reingest creating a new SourceRecord version for changed records.
- Old evidence deactivation/supersession after changed reingest.
- MachineUnit evidence capped to review-only behavior.
- WhatsApp and Bitrix message formatting sorted by timestamp.

Run ingestion tests, lint, and strict type checks for changed ingestion code.
