# Machine Unit Graph Design

## Goal

Represent machine units as first-class graph nodes so purchase and explicit ownership evidence can support relationship intelligence and weak, review-only identity matching.

This design intentionally does **not** change name modeling. A Person may already have multiple observed names through `HAS_FACT` relationships, and names remain non-identifier facts used as weak scoring evidence.

## Scope

This first schema-focused slice covers:

- `MachineUnit` graph model.
- Order-level, purchase, and explicit ownership relationships.
- Targeted Neo4j constraints and indexes.
- Matching semantics for machine-unit evidence as weak review-only evidence.
- Follow-up requirements for the next ingestion spec.

This slice does not implement source-specific extraction from PHP POS, Fundbox, Bitrix chat, or WhatsApp chat. Those changes belong in the next ingestion spec.

## Graph model

Add a `MachineUnit` node independent of `Order`, `LineItem`, `Product`, and `Person`.

A `MachineUnit` represents a real-world machine unit that may be observed across orders, chats, ownership claims, or service records. It can be identified by one or both of:

- `lta_tag`
- `serial_number`

Recommended properties:

- `machine_unit_id`: internal UUID, unique.
- `lta_tag`: original or display LTA tag when available.
- `normalized_lta_tag`: normalized lookup value when available.
- `serial_number`: original or display serial number when available.
- `normalized_serial_number`: normalized lookup value when available.
- `created_at`: creation timestamp.
- `updated_at`: last update timestamp.

At least one normalized identifier must be present when creating a `MachineUnit`.

## Relationships

### Order to MachineUnit

Use `Order -[:INVOLVES_UNIT]-> MachineUnit` when a future ingestion source can associate an order with a specific machine unit.

This is order-level by design. Do not require line-item modeling to create machine-unit purchase evidence.

Suggested relationship properties:

- `source_system_key`
- `source_record_pk`
- `observed_at`
- `created_at`
- `confidence`
- `quality_flag`

### Person purchase evidence

Use `Person -[:BOUGHT_UNIT]-> MachineUnit` when a resolved Person bought an order involving that unit.

This is historical evidence. It should not imply current ownership.

Suggested relationship properties:

- `source_system_key`
- `source_record_pk`
- `order_id` or `order_key` when available.
- `observed_at`
- `created_at`
- `confidence`
- `quality_flag`

### Explicit ownership evidence

Use `Person -[:OWNS_UNIT]-> MachineUnit` only when a source explicitly asserts current ownership.

Do not infer current ownership from a purchase alone. Purchase can suggest a relationship but is not ownership.

Suggested relationship properties:

- `source_system_key`
- `source_record_pk`
- `observed_at`
- `first_seen_at`
- `last_seen_at`
- `last_confirmed_at`
- `is_active`
- `confidence`
- `quality_flag`
- `conflict_flag`

## Ownership conflicts

If two or more active `OWNS_UNIT` relationships from different Persons point to the same `MachineUnit`, flag a conflict rather than automatically resolving it.

Conflict flagging should preserve all asserted ownership relationships. No automatic owner replacement should happen in this schema slice.

A later operations/review design can decide how conflicts are surfaced in UI and resolved manually.

## Matching semantics

Machine-unit evidence is weak and review-only.

The matching engine may use shared machine-unit evidence to:

- generate candidates;
- add review reasons such as `same_lta_tag`, `same_serial_number`, `same_machine_unit_purchase`, or `same_machine_unit_owner_claim`;
- open review cases for possible identity matches;
- open or flag review cases for conflicting ownership claims.

The matching engine must not use machine-unit evidence to create deterministic auto-merges.

A match supported only by machine-unit evidence must stay below the auto-merge threshold. Machine-unit evidence may contribute to review-band confidence, but the scoring model must cap it so it cannot silently merge profiles.

## Targeted Neo4j schema changes

Add targeted constraints/indexes in the schema bootstrap file.

Recommended constraints:

- Unique constraint on `MachineUnit.machine_unit_id`.

Recommended lookup indexes:

- Index on `MachineUnit.normalized_lta_tag`.
- Index on `MachineUnit.normalized_serial_number`.

Do not require both normalized values to be unique independently unless implementation can safely handle partial observations and later reconciliation. A unit may first arrive with only an LTA tag and later with only a serial number.

Initial upsert strategy:

- If both LTA tag and serial number are present, first look for an existing unit matching either normalized value.
- If exactly one existing unit matches, update missing normalized/display fields on that unit.
- If the LTA tag matches one unit and the serial number matches another, do not merge those units automatically; flag a machine-unit identifier conflict for review.
- If no unit matches, create a new `MachineUnit`.

Recommended traversal support:

- Indexes for `Order.order_id` and other existing order keys should remain targeted to observed query shapes.
- Relationship traversal should rely on anchored node lookups: find `MachineUnit` by normalized LTA tag or serial number, then traverse to `Person` through `BOUGHT_UNIT` or `OWNS_UNIT`.

## Normalization

Add machine-unit normalization helpers when implementation begins:

- LTA tags: trim whitespace, uppercase, remove obvious separators if domain-safe.
- Serial numbers: trim whitespace, uppercase, preserve meaningful punctuation unless source evidence proves it is cosmetic.

Invalid, placeholder, or generic values should not create `MachineUnit` nodes.

## Data flow after later ingestion work

Future ingestion sources will emit machine-unit observations alongside existing identity and sales envelopes.

Expected write flow:

1. Normalize LTA tag and serial number.
2. Upsert or find the `MachineUnit` by normalized LTA tag and/or normalized serial number.
3. Link `Order -[:INVOLVES_UNIT]-> MachineUnit` when order evidence exists.
4. Link `Person -[:BOUGHT_UNIT]-> MachineUnit` when the resolved buyer is known.
5. Link `Person -[:OWNS_UNIT]-> MachineUnit` only for explicit owner assertions.
6. Flag active ownership conflicts for the same unit.
7. Use machine-unit links as review-only matching evidence.

## Follow-up ingestion spec requirements

The next ingestion spec must include adjustments for all ingestion tasks to extract `MachineUnit` data from:

- PHP POS sales sources.
- Fundbox sales sources.
- Bitrix chat extraction.
- WhatsApp chat extraction.

The next spec should also cover:

- the editable exclusion-list file for company and personnel identifiers;
- gitignore and Docker bind-mount changes for that file;
- reingest behavior for existing records: skip unchanged data, update changed data, and rerun matching;
- matching-engine adjustments with a stopping point to discuss before implementation;
- timestamp ordering for chat ingestion messages.

## Testing

Schema-slice implementation should include focused tests for:

- MachineUnit normalization.
- Upsert behavior with LTA-only, serial-only, and both-values observations.
- Order-level `INVOLVES_UNIT` writes.
- `BOUGHT_UNIT` writes without implying `OWNS_UNIT`.
- `OWNS_UNIT` writes only for explicit ownership.
- Conflict flagging when multiple active owners exist for the same unit.
- Matching cap behavior that keeps machine-unit-only evidence in review, never auto-merge.

Run ingestion tests, Neo4j query tests where available, lint, and strict type checks for changed Python files.
