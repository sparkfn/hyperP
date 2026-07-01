# OneDiver Ingestion — Design

**Date:** 2026-06-30
**Branch:** `onediver-ingestion` (from `development`)
**Source:** `.dumps/onediver (1).sql` — phpMyAdmin MySQL dump of database `onediver`
(server `l3omnie.cbz1r9hxfuzz.ap-southeast-1.rds.amazonaws.com:3306`, MySQL 5.7,
generated Jan 15 2025, ~889 MB, 128 tables). OneDiver is a scuba / water-sports
business-management platform used by Singapore dive schools (Fishermen Scuba,
SEAduction Water Sports, Living Seas, Scubahub).

## Goal

Ingest OneDiver person data into the HyperP graph as `identity`, `relationship`,
and `sales` SourceRecords so the same diver is unified across OneDiver and the
other sources (Fundbox, Eko, SpeedZone, Bitrix, WhatsApp, SG government).

## Scope decisions (confirmed with user)

- **Full scope:** identity + emergency-contact relationships + sales.
- **Delivery:** dump-only. No live `batch` connector (no SSH gateway required).
- **Source keys:** `onediver` (identity + relationships) and `onediver:sales`.
- **Module layout (Approach A):** new `services/ingestion/src/connectors/onediver/`
  package; two dump-connector classes registered in the `factories` dict of
  `dumps/connectors.py`. Reuse `build_envelope`, `IdentifierBag`,
  `address_from_row`, `serialize_row`, `to_iso` from `fundbox/builders.py` /
  `dumps/reader.py` — same pattern as eko/speedzone.
- **limited-100 dumps:** extend `.dumps/limited-100/generate_limited_dumps.py`
  to emit `onediver_100.sql` and `onediver_sales_100.sql` for local/dev testing.

## Source shape

### `onediver` (identity + relationships)

Tables read: `profiles`, `profile_emergencies`, `users`, `accounts`.

`profiles` (PK `id`, FK `user_id → users.id`, `account_id → accounts.id`) is the
rich person table. Columns of interest:

- **Identifiers:** `email`, `Alternative_email`, `contact_number` (+ `lk_contact_country_code`),
  `secondary_contact_number` (+ `lk_secondary_contact_country_code`),
  `ic_number` (+ `ic_type`) — Singapore NRIC, `passport_number`,
  `ssi_master_id` (SSI dive membership), `membership_id` (internal membership).
- **Attributes:** `first_name`, `last_name`, `gender`, `birthday`,
  `passport_full_name`, `lk_nationality_code`, `race`, `maritial_status`,
  dive fields (`dive_level`, `dives`, `last_dive`).
- **Address:** `address`, `address2`, `city`, `state`, `lk_country_code`,
  `zip_code` (plus a separate `per_address` permanent-address block).
- **Deletion / status:** `is_deleted` (filter `= 0`), `status`, `old_status`.

`profile_emergencies` (FK `profile_id → profiles.id`) holds up to two
next-of-kin (`kin1_*`, `kin2_*`: `contact_first_name`/`contact_last_name`,
`contact_number`, `lk_contact_relationship_id`/`relation`) plus
insurance/medical data. `users` carries the `username` and `account_id`;
`accounts` carries the dive-shop `name` (source-system context).

### `onediver:sales`

Tables read (header-level for MVP): `sales_orders` (+ `profiles` to build the
email→profile lookup). Item tables (`sales_order_items`) are deferred —
header-level sales records first.

**Evidence from profiling the dump (58,951 profiles, 58,895 unique emails):**

- `sales_orders`: 551 rows. `billing_contact_email` matches a `profiles.email`
  for 529/551 (96%). `billing_contact_id → profiles.id`: 2/551. `reference_id →
  profiles.id`: 1/551. **The reliable sales→person link is `billing_contact_email`,
  not any integer FK** — the integer FKs reference a shop-side address book not
  present in this dump.
- `sales_invoices`: 62,027 rows. `customer_id → profiles.id`: 651/62,027 (1%);
  `customer_id != 0` for 62,019 of them, so it is a populated FK to a *different*
  namespace. `sales_order_id → sales_orders.id`: 3. No customer-email column.
  **Not linkable to a Person** — deferring avoids flooding the graph with 62k
  dangling, name-only sales records (precision-over-recall).
- `bills`: 23 rows. `biller_contact_id → profiles.id`: 0. Not linkable. Deferred.

For each `sales_orders` row, emit **one `sales` SourceRecord**:
- `source_record_id`: `onediver-salesorder-{id}`
- `record_type`: `"sales"`
- `observed_at`: `to_iso_first(order_date, accepted_date, created)`
  (zero-date safe — see `identity` observed_at).
- **identifiers:** `[]` — sales records are not an identifier source (the person
  link is carried solely via `customer_link`); matches every sibling sales
  connector (phppos, fundbox, eko, speedzone all pass `identifiers=[]`). Emitting
  billing email/phone as identifiers would let a gift order billed to a spouse
  generate a spurious cross-person candidate.
- **attributes:** `{}` — order facts live only in `raw_payload.order` (sibling
  convention); `total_amount` is coerced to `float` via the shared `coerce_float`
  helper (handles the string form dump-reader cells arrive in; the live-DB
  `_decimal_to_float` helpers only accept `Decimal`/`int`/`float`), not left as
  the dump string.
- **raw_payload:** `{"order": {source_order_id, order_no, ordered_at, status,
  currency (default "SGD" if empty), total_amount (float), raw},
  "customer_link": {"identity_source_record_id": f"onediver-profile-{profile_id}",
  "source_system_key": "onediver"}}` — matches the `pipeline_sales._CustomerLink`
  shape (which requires `source_system_key`) used by every sibling sales source.

**Person link resolution:** build `email_to_profile_id` from **non-deleted**
`profiles` (`is_deleted = 0`, lower-cased `email` → `id`); deleted profiles never
emit an identity source record, so linking a sales order to one would produce a
dangling `customer_link`. For each sales_order, look up
`billing_contact_email.lower()`. When it resolves, set
`customer_link.identity_source_record_id = "onediver-profile-{profile_id}"`.
When it does not (22/551 — external customers), `customer_link` is omitted; the
sales record is still emitted (unlinked).

## Envelope design

### `identity` record (one per non-deleted `profiles` row)

- `source_record_id`: `onediver-profile-{profiles.id}`
- `record_type`: `"identity"`
- `observed_at`: `to_iso_first(profiles.modified, profiles.created)` — returns the
  first value that parses to a real timestamp. `to_iso` treats MySQL `0000-*`
  zero-date sentinels as `None` (a shared-helper change: `serialize_row` routes
  every column through `to_iso`, so any zero-date cell across every connector
  now serializes as `None` — a deliberate one-time `record_hash` invalidation,
  since the old literal was never a real timestamp), so a zero-date `modified`
  falls back to `created` rather than emitting the garbage string. `to_iso_first`
  is the shared multi-column helper; siblings still use the buggy
  `to_iso(a or b)` form — migrating them is a tracked follow-up.
- **identifiers** (via `IdentifierBag.add`, junk-filtered):
  - `email` ← `profiles.email` (unverified — `verified` is reserved for govt IDs)
  - `email` ← `profiles.Alternative_email` (unverified)
  - `phone` ← `profiles.contact_number`, `region_hint` ← `lk_contact_country_code`
  - `phone` ← `profiles.secondary_contact_number`, `region_hint` ← `lk_secondary_contact_country_code`
  - `nric` ← `profiles.ic_number` (verified) — govt-ID; salt-hashing is a
    codebase-wide pending item (NRIC itself is not yet hashed), tracked separately
  - `passport` (`profiles.passport_number`) is intentionally **not** emitted as an
    identifier: it has no downstream normalizer / fanout cap / govt-ID gate entry,
    so emitting it would persist an inert, un-hashed Identifier node (sensitive-data
    exposure with no match value). The raw number remains in `raw_payload.profile`.
    Registering `passport` downstream is a tracked follow-up.
- **attributes:** `full_name` (computed `first_name + last_name`), `first_name`,
  `last_name`, `gender`, `dob` ← `birthday`, `nationality` ← `lk_nationality_code`,
  `race`, `passport_full_name`, `dive_level`, `dives`, `last_dive`,
  `username` ← `users.username`, `shop_name` ← `accounts.name`.
- **addresses:** one `address_from_row`-style address from `address`/`address2`/
  `city`/`state`/`lk_country_code`/`zip_code`. The `per_address` block is stored
  in `raw_payload` only (not a second Address node) for MVP.
- **raw_payload:** `{"profile": serialize_row(profile), "user": ..., "account": ...}`.

`ssi_master_id` and `membership_id` are **attributes for MVP** (OneDiver-internal;
no cross-source match partner yet). They can be promoted to identifier types
later without a schema change (the graph accepts free-string `identifier_type`).

### `relationship` records (per `profile_emergencies` row, kin1 + kin2)

- `source_record_id`: `onediver-emergency-{row.id}-kin1` / `-kin2`
- `record_type`: `"relationship"`
- **identifiers:** `phone` ← `kin1_contact_number` (the named next-of-kin's phone).
  Name-only kin (no phone) still yields a relationship record with empty
  identifiers — the engine links it to the profile by `linked_to_source_record_id`.
- **attributes:** `full_name` ← kin first+last name, `relationship_to_referrer`
  ← `lk_contact_relationship_id`/`relation`, `kin_slot` ← `"kin1"`/`"kin2"`.
- **raw_payload:** `{"emergency": serialize_row(row), "kin_slot": ...,
  "linked_to_source_record_id": f"onediver-profile-{row.profile_id}",
  "link_type": relation}` — mirrors `_build_fundbox_contact`.

The ingestion pipeline creates `KNOWS` Person→Person edges from these records,
same as fundbox `contacts`.

### `sales` records (one per `sales_orders` row)

- `source_record_id`: `onediver-salesorder-{id}`
- `record_type`: `"sales"`
- `observed_at`: `to_iso_first(order_date, accepted_date, created)`
- **identifiers:** `[]` (sales records are not an identifier source — see above).
- **attributes:** `{}` (order facts live only in `raw_payload.order`).
- **raw_payload:** `{"order": {…, total_amount (float), …}, "customer_link":
  {"identity_source_record_id": …, "source_system_key": "onediver"}}`.

**Person link resolution:** build `email_to_profile_id` from **non-deleted**
`profiles` (`is_deleted = 0`, lower-cased `email` → `id`); each sales_order's
`billing_contact_email` is looked up. `customer_link.identity_source_record_id`
is set to `onediver-profile-{profile_id}` when the email resolves; omitted
otherwise (external customer). With `identifiers=[]`, an unlinked sales record
stays unlinked (no denormalized-identifier fallback) — accepted: the ~4%
external orders are real sales with no resolvable person in this dump.

## SourceSystem bootstrap

`onediver` and `onediver:sales` are seeded in `_SOURCE_SYSTEMS`
(`services/ingestion/src/graph/bootstrap.py`) under entity `onediver`
(`_ENTITIES`, `entity_type="retailer"`, `country_code="SG"`), `system_type=
"consumer_backend"`, with a dedicated `_ONEDIVER_TRUST` field-trust map
(phone/email/name tier_3; dob/nric/address tier_4). This is **required**:
ingestion write queries (`CREATE_SOURCE_RECORD`, `CREATE_INGEST_RUN`,
`MERGE_ORDER`) only `MATCH` the `SourceSystem` node — they never `MERGE`/`CREATE`
it — so without a bootstrap entry the writes silently no-op and onediver
ingestion produces zero graph records. Every other source (including dump-only
`sgbankruptcy`/`sgrentalflats`) is bootstrapped the same way.

## Component layout

```
services/ingestion/src/connectors/onediver/
  __init__.py          # exports OneDiverDumpConnector, OneDiverSalesDumpConnector, table sets
  schema.py            # SQLAlchemy Core reflections for the ~7 tables read
  connector.py         # _build_identity_envelope, _build_relationship_envelope,
                       # _build_sales_envelope + the two *DumpConnector classes
```

`dumps/connectors.py` changes:
- import `OneDiverDumpConnector`, `OneDiverSalesDumpConnector`, `ONEDIVER_TABLES`,
  `ONEDIVER_SALES_TABLES`
- add `"onediver": OneDiverDumpConnector` and `"onediver:sales": OneDiverSalesDumpConnector`
  to the `factories` dict

Table-set constants (in `onediver/connector.py`):
- `ONEDIVER_TABLES`: `profiles`, `profile_emergencies`, `users`, `accounts`.
- `ONEDIVER_SALES_TABLES`: `sales_orders`, `profiles` (profiles re-read so the
  sales dump is self-contained for the email→profile lookup; the reader de-dups
  by table name so reading profiles twice across the two source keys is fine).

`.dumps/limited-100/generate_limited_dumps.py` changes:
- import `ONEDIVER_TABLES`, `ONEDIVER_SALES_TABLES`
- generate `onediver_100.sql`: 100 non-deleted `profiles` (sorted by `id`) +
  their `profile_emergencies` (by `profile_id`) + linked `users`/`accounts`.
- generate `onediver_sales_100.sql`: 100 `sales_orders` (sorted by `id`) + the
  `profiles` rows whose `email` matches those orders' `billing_contact_email`
  (so the sales dump is self-contained for the email-link test).

## Tests

`services/ingestion/tests/test_onediver.py` — mirrors existing connector tests:

- identity envelope: identifiers (email/phone/nric/passport), attributes, address,
  `is_deleted` filtering, `record_type == "identity"`, stable `source_record_id`.
- relationship envelopes: kin1/kin2 split, `linked_to_source_record_id`,
  `record_type == "relationship"`, name-only kin (no phone) still emitted.
- sales envelopes: `record_type == "sales"`, `customer_link` populated when
  `billing_contact_email` resolves to a profile, denormalized billing
  identifiers carried, external-customer orders still emit without
  `customer_link`.
- limited-100 generator: `onediver_100.sql` / `onediver_sales_100.sql` parse back
  via `load_dump_tables` with the expected row counts and FK closure.

Fixtures: a tiny hand-built MySQL dump fragment (a few `profiles`,
`profile_emergencies`, `sales_orders` rows) written through `write_mysql` or a
local `tmp_path` dump — same approach as the eko/fundbox test fixtures.

## Out of scope (deferred)

- Live `batch` connector (SSH-gated MySQL pull).
- `sales_invoices` (62,027 rows) and `bills` (23 rows) — profiling shows neither
  has a reliable link to a `profiles` person (`sales_invoices.customer_id →
  profiles.id` is 1%; `bills.biller_contact_id → profiles.id` is 0; neither has a
  customer-email column). Emitting them would add ~62k dangling, name-only
  sales records — pure noise under precision-over-recall. Revisit if a use case
  needs walk-in invoice/bill totals.
- Sales line-item tables (`sales_order_items`, `sales_invoice_items`,
  `bill_items`) — header-level only for MVP.
- `profile_certifications`, `profile_rental_equipments`, `reservations`,
  `fulfillments` — not person-identifying; skip for MVP.
- Promoting `ssi_master_id` / `membership_id` to identifier types.

## Validation

- `uv run --package profile-unifier-ingestion ruff check services/ingestion/src`
- `uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src`
- `uv run --package profile-unifier-ingestion pytest services/ingestion/tests/test_onediver.py`
- Host-local runs only for the one-shot limited-100 **generation** (data prep,
  not verification). All code verification goes through the Woodpecker PR
  pipeline (`wpci home`) per repo agent policy.

## Risks / notes

- **Sales→person link is email-based, not an integer FK.** Profiling showed
  `sales_orders.billing_contact_email` matches a `profiles.email` 96% of the
  time, while every integer FK (`billing_contact_id`, `reference_id`,
  `customer_id`) references a shop-side address book absent from this dump. The
  connector builds `email_to_profile_id` from `profiles` and resolves each order
  by `billing_contact_email`.
- **Country-code columns** (`lk_contact_country_code`, `billing_country_code`)
  are 2-letter country codes from a lookup table, not dial codes — emitted as
  `region_hint`, not prepended to the phone value, to avoid malformed phone
  identifiers.
- **`onediver (1).sql` filename** contains a space and parentheses; the
  `dump_path` argument and limited-100 `ROOT / dump_name` must quote it. The
  limited-100 generator reads it as `ROOT / "onediver (1).sql"`.