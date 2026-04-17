# Profile Unifier — Entity grouping + Sales data extraction

Status: design draft (2026-04-15).
Scope: infrastructure change before a full data reset + re-ingestion. Adds
Entity grouping above SourceSystem, renames the three live source systems,
and introduces a sales sub-graph (Order / LineItem / Product / Payment)
driven by a new `"sales"` `SourceRecord.record_type`.

Reading order: this document supplements
[`profile-unifier-graph-schema.md`](profile-unifier-graph-schema.md) and
[`profile-unifier-architecture.md`](profile-unifier-architecture.md).
Terms from [`profile-unifier-glossary.md`](profile-unifier-glossary.md) are
used throughout.

---

## 1. Entity node

### Motivation

Today `SourceSystem` is the only provenance anchor. Operationally we think
one level higher: **Fundbox**, **SpeedZone**, **Eko** are *businesses* we
work with; each business runs one or more source systems (a consumer
backend, a POS, a CRM, etc.). Reporting ("how much revenue did Fundbox
contribute?", "how many unique customers across all SpeedZone outlets?")
and governance ("deactivate every source system under Eko") need that
entity layer.

### Node

```cypher
CREATE (e:Entity {
  entity_id: randomUUID(),
  entity_key: 'fundbox',         // slug, unique, stable
  display_name: 'Fundbox',
  entity_type: 'lender',         // lender | retailer | ...
  country_code: 'SG',
  is_active: true,
  created_at: datetime(),
  updated_at: datetime()
})
```

Uniqueness constraint on `entity_key`. Scalar properties for display;
richer attrs (brand portfolio, contacts, contracts) can be added later
without reshaping the graph.

### Relationship

```
(SourceSystem)-[:OPERATED_BY]->(Entity)
```

One SourceSystem belongs to exactly one Entity; one Entity operates many
SourceSystems. No properties on the relationship — the Entity owns the
metadata.

Why a node, not a scalar on SourceSystem: later work (entity-level sales
roll-ups, contracts, `Product-[:SOLD_BY]->Entity`) all want to attach to
something. A scalar would need to be reconstituted into a node as soon as
the first of those features lands.

### Seeded entities and renamed source systems

| entity_key | display_name | entity_type | source_key                   | source display_name          |
|------------|--------------|-------------|------------------------------|------------------------------|
| fundbox    | Fundbox      | lender      | `fundbox_consumer_backend`   | Fundbox Consumer Backend     |
| speedzone  | SpeedZone    | retailer    | `speedzone_phppos`           | SpeedZone phppos             |
| eko        | Eko          | retailer    | `eko_phppos`                 | Eko phppos                   |

Slugs use lowercase + underscore because `source_system_key` is embedded
verbatim in every `IDENTIFIED_BY`, `LIVES_AT`, and `KNOWS` relationship.
Human-readable names live in `SourceSystem.display_name` and
`Entity.display_name`.

The secondary Fundbox feeds (`fundbox:contacts`, `fundbox:legacy`, etc.)
are renamed analogously: `fundbox_consumer_backend:contacts`,
`fundbox_consumer_backend:legacy`, `fundbox_consumer_backend:merged`,
`fundbox_consumer_backend:junk`. They all point at the same Entity.

### Schema changes

- `infra/neo4j/init.cypher` — add `CREATE CONSTRAINT entity_key_unique`
  on `(e:Entity) REQUIRE e.entity_key IS UNIQUE`.
- Seed script (`infra/neo4j/seed_entities.cypher` or an idempotent Python
  bootstrap run from `pipeline.py`) creates the three Entity nodes, the
  renamed SourceSystem nodes, and the `OPERATED_BY` edges.

### Impact on connectors

Each connector's `get_source_key()` returns the new slug. No other
connector code changes — they never queried `SourceSystem` by its old key,
the pipeline does that when it creates `FROM_SOURCE` edges.

Since the data is being reset, no `source_system_key` rewrites on existing
edges are needed.

---

## 2. Sales sub-graph

### Motivation

The current ingestion extracts identity signals (NRIC, phone, email,
addresses). We also need purchase behaviour — which customers bought what,
when, for how much, through which merchant — to drive sales intelligence
(LTV, basket analysis, churn, cross-entity customer value).

### Design principles

- **Identity signals remain the priority.** Sales data is additive; it must
  not alter matching semantics. Sales `SourceRecord`s carry no identifiers
  that would participate in Layer 1 deterministic rules unless the upstream
  row *itself* carries identity columns.
- **Order-first, line-item-second.** Start with Order granularity
  (`Person -[:PURCHASED]-> Order`) and add `LineItem`s in the same release
  — the Fundbox schema already has clean order/order_items; phppos has
  sales/sales_items. The extraction cost is the same for both.
- **Products are entity-scoped.** SKUs do not reconcile across Fundbox /
  SpeedZone / Eko. A Product node belongs to one Entity via
  `(Product)-[:SOLD_BY]->(Entity)`. Cross-entity product resolution is
  explicitly deferred.
- **Sales records flow through the same pipeline.** We do not build a
  parallel ingestion path. A sales row becomes a `SourceRecord` with
  `record_type='sales'`; after Person resolution (if the row has identity
  columns) the pipeline writes the `Order` / `LineItem` sub-graph and
  connects it via `PURCHASED`.
- **No Person merges from sales records alone.** Sales records have no
  identifiers most of the time (phppos sales carry just a `customer_id`);
  they rely on being linked to a Person *by the foreign key to the already-
  resolved customer row*. If the customer row has not been ingested,
  the sales row is parked (`link_status='pending_customer'`) and
  re-evaluated when the customer appears. For Fundbox, `orders.user_id`
  joins to the already-resolved `users`/`basic_profiles` record.

### Nodes

#### `Order`

```cypher
CREATE (o:Order {
  order_id: randomUUID(),                  // graph-local UUID
  source_system_key: 'fundbox_consumer_backend',
  source_order_id: '12345',                // native PK from source (string)
  order_no: 'FBX-0001',                    // merchant-facing number if present
  ordered_at: datetime(),                  // source timestamp
  status: 'completed',                     // source status, free-form
  total_amount: 1299.00,
  currency: 'SGD',
  item_count: 3,                           // convenience rollup
  metadata: {},                            // native map — receipt no., channel, register, etc.
  retention_expires_at: null,
  created_at: datetime(),
  updated_at: datetime()
})
```

Dedup key: `(source_system_key, source_order_id)`. Uniqueness constraint.

#### `LineItem`

```cypher
CREATE (li:LineItem {
  line_item_id: randomUUID(),
  source_system_key: 'fundbox_consumer_backend',
  source_line_item_id: '98765',
  line_no: 1,                              // 1-indexed position in the order
  quantity: 2,
  unit_price: 499.00,
  line_total: 998.00,
  currency: 'SGD',
  discount_amount: 0.00,
  tax_amount: 0.00,
  metadata: {},                            // LTA tag, serial, modifiers — source-specific
  created_at: datetime()
})
```

Dedup key: `(source_system_key, source_line_item_id)`.

#### `Product`

```cypher
CREATE (p:Product {
  product_id: randomUUID(),                // graph-local UUID
  source_system_key: 'fundbox_consumer_backend',
  source_product_id: 'variant-4321',       // SKU-equivalent from source
  sku: 'IPH-15-256-BLK',                   // native SKU if present
  name: 'iPhone 15 256GB Black',
  display_name: 'iPhone 15 256GB Black',
  category: 'electronics',                 // source-reported category, free-form
  subcategory: 'phones',
  manufacturer: 'Apple',
  attributes: {},                          // native map from source (colour, size, etc.)
  is_active: true,
  first_seen_at: datetime(),
  last_seen_at: datetime()
})
```

Dedup key: `(source_system_key, source_product_id)`. Uniqueness constraint.

#### `Payment` (optional, Phase 2)

```cypher
CREATE (pay:Payment {
  payment_id: randomUUID(),
  source_system_key: '...',
  source_payment_id: '...',
  payment_method: 'card',
  amount: 100.00,
  currency: 'SGD',
  paid_at: datetime(),
  status: 'settled'
})
```

Not in the first sales pass — the Fundbox `payments` / `payment_transactions`
schema is loan-centric and will be modelled alongside a `Loan` node in a
separate follow-up.

### Relationships

| Name            | From     | To           | Properties                                                                   | Purpose                                                                     |
|-----------------|----------|--------------|------------------------------------------------------------------------------|-----------------------------------------------------------------------------|
| `PURCHASED`     | Person   | Order        | `source_system_key`, `source_record_pk`, `first_seen_at`, `last_seen_at`     | The resolved customer placed this order. Rewired on merge like `KNOWS`.     |
| `CONTAINS`      | Order    | LineItem     | —                                                                            | Order → line items.                                                         |
| `OF_PRODUCT`    | LineItem | Product      | —                                                                            | Which product was bought.                                                   |
| `SOLD_BY`       | Product  | Entity       | —                                                                            | Product catalogue ownership (entity-scoped).                                |
| `SOLD_THROUGH`  | Order    | SourceSystem | —                                                                            | Which system booked the order. Cheap provenance hop.                        |
| `FROM_SOURCE`   | Order / LineItem / Product | SourceSystem | — (existing edge type, reused)                                 | Same provenance pattern used for `SourceRecord`.                            |

Why `PURCHASED` as an edge (not via `SourceRecord`): queries like "what
did this customer buy" or "who bought this product" are first-class and
should not require walking through `HAS_FACT` / `SourceRecord`. The
`source_record_pk` property on the edge preserves lineage.

### `SourceRecord.record_type` extension

Add `"sales"` to the enum alongside `"system"` and `"conversation"`:

- **`sales`** — extracted deterministically from a POS/commerce system's
  sales or order rows. Never eligible for deterministic auto-merge on its
  own identifiers (sales rows rarely carry strong IDs). Linking to a
  Person happens by foreign-key to an already-resolved customer record in
  the same source system (see pipeline flow below). Identity signals
  present on the row (rare — e.g. a guest-checkout phone) flow through
  heuristic scoring (Layer 2).

A sales `SourceRecord` still carries `raw_payload` (the joined row) and
`normalized_payload` (the extracted order/line/product shape). Its
`HAS_FACT` edges fan out to `Order` / `LineItem` / `Product` nodes.

### Why not a separate `SalesRecord` label?

Reusing `SourceRecord` keeps one ingestion pipeline, one retention/audit
surface, one review queue. The `record_type` discriminator is already the
lever for policy differences.

### Person-link indirection

1. The sales connector resolves the customer FK to a source row already in
   the graph: e.g. `orders.user_id → users.id → fundbox SourceRecord → Person`.
2. The sales `SourceRecord` is linked with
   `(sales_sr)-[:FOR_CUSTOMER_RECORD]->(identity_sr)`.
3. A single traversal through
   `(sales_sr)-[:FOR_CUSTOMER_RECORD]->(identity_sr)-[:LINKED_TO]->(Person)`
   writes `(Person)-[:PURCHASED]->(Order)`.
4. If the identity `SourceRecord` is missing, the sales record is parked
   with `link_status='pending_customer'` and re-visited at the end of the
   ingest run (and on every subsequent run) until the identity side arrives.

This preserves the "sales data never forces identity resolution" rule:
the only Person resolution it depends on is the one already performed on
the identity record.

---

## 3. Per-connector extraction plan

### Fundbox (`fundbox_consumer_backend`)

Existing identity ingestion: `users` ⋈ `basic_profiles` ⋈ `basic_plus_profiles`
plus sidecar tables.

New sales extraction:

| Graph node | Source tables                                                        |
|------------|----------------------------------------------------------------------|
| Order      | `orders` (17,176 rows)                                               |
| LineItem   | `order_items` (17,186 rows)                                          |
| Product    | `product_variants` ⋈ `products` (3,644 × 1,231 rows)                 |
| —          | `merchant_products` used to join `order_items.merchant_product_id` → `product_variant_id` → `product_id` |

Field mapping (Order):

| Graph             | Source                            |
|-------------------|-----------------------------------|
| source_order_id   | `orders.id`                       |
| order_no          | `orders.order_no`                 |
| ordered_at        | `orders.created_at`               |
| status            | `orders.status`                   |
| total_amount      | `orders.total_amount`             |
| currency          | const `'SGD'` (Fundbox is SG)     |
| item_count        | `orders.total_items`              |
| metadata          | `{transaction_reference, release_date, merchant_id, merchant_staff_id, expiry_at}` |

Field mapping (LineItem):

| Graph                | Source                                 |
|----------------------|----------------------------------------|
| source_line_item_id  | `order_items.id`                       |
| line_no              | row position within the order          |
| quantity             | `order_items.quantity`                 |
| unit_price           | `order_items.price`                    |
| line_total           | `quantity * price`                     |
| metadata             | `{lta_tag, serial_no, merchant_product_id}` |

Field mapping (Product):

| Graph              | Source                                           |
|--------------------|--------------------------------------------------|
| source_product_id  | `product_variants.id` (stringified)              |
| sku                | `product_variants.sku`                           |
| name               | `product_variants.name`                          |
| display_name       | `products.name`                                  |
| category           | `products.category`                              |
| subcategory        | `products.sub_category`                          |
| manufacturer       | `products.make`                                  |
| attributes         | merge of `product_variants.attributes` json + `{type, sub_type, model, has_serial_number, has_lta_tag}` |

Customer link: `orders.user_id → users.id → existing fundbox SourceRecord
(source_record_id = 'fundbox-user-{user_id}')`. The new source_key is
`fundbox_consumer_backend`, so identity source_record_ids also update
accordingly (`'fundbox_consumer_backend-user-{user_id}'`).

Notes:
- Fundbox *orders* are BNPL-financed; every order has an associated `loans`
  row. The `Loan` modelling is deferred to a follow-up doc to keep this
  change focused on pure sales semantics.
- `order_refunds` and `order_cancels` are Phase 2 — start with successful
  orders only (`status` in `('completed', 'released', 'disbursed', …)` —
  exact filter to be calibrated against Fundbox ops team guidance).

### SpeedZone (`speedzone_phppos`) and Eko (`eko_phppos`)

The two POS systems are PHP Point of Sale forks and share a schema. The
live databases seen during inspection only contain the config subset of
phppos tables (no `phppos_sales`, `phppos_sales_items`, `phppos_items`,
`phppos_customers`). **This has to be resolved before sales ingestion can
run for these connectors** — either the dev fixture is incomplete or the
real sales data lives in a different database than the one currently
mounted. *Action item: confirm the production DB path for SZ and Eko
before Phase C.*

Expected phppos sales schema and mapping (standard OSPOS/phppos lineage):

| Graph node | Source tables                                                     |
|------------|-------------------------------------------------------------------|
| Order      | `phppos_sales`                                                    |
| LineItem   | `phppos_sales_items`                                              |
| Product    | `phppos_items` (+ optional `phppos_item_kits`)                    |

Field mapping (Order from `phppos_sales`):

| Graph             | Source                                      |
|-------------------|---------------------------------------------|
| source_order_id   | `sale_id`                                   |
| order_no          | `invoice_number` (falls back to `sale_id`)  |
| ordered_at        | `sale_time`                                 |
| status            | derived from `suspended` / `sale_status`    |
| total_amount      | sum of `sales_items.item_unit_price * quantity - discount`, or `sale_total` column where present |
| currency          | const `'SGD'`                               |
| item_count        | `COUNT(sales_items)` for the sale           |
| metadata          | `{customer_id, employee_id, register_id, payment_type, comment, sale_type_id}` |

Field mapping (LineItem from `phppos_sales_items`):

| Graph                | Source                                                  |
|----------------------|---------------------------------------------------------|
| source_line_item_id  | composite `sale_id:line`                                |
| line_no              | `line`                                                  |
| quantity             | `quantity_purchased`                                    |
| unit_price           | `item_unit_price`                                       |
| line_total           | `item_unit_price * quantity_purchased - discount`       |
| discount_amount      | `discount`                                              |
| metadata             | `{item_id, item_variation_id, serialnumber, description}` |

Field mapping (Product from `phppos_items`):

| Graph              | Source                              |
|--------------------|-------------------------------------|
| source_product_id  | `item_id`                           |
| sku                | `item_number`                       |
| name               | `name`                              |
| display_name       | `name`                              |
| category           | `phppos_categories.name` via `category_id` |
| manufacturer       | `phppos_manufacturers.name`         |
| attributes         | `{size, cost_price, unit_price, description, tags}`    |

Customer link: `phppos_sales.customer_id` → `phppos_customers.person_id`
→ existing identity SourceRecord (`speedzone_phppos-customer-{customer_id}`
or `eko_phppos-customer-{customer_id}`).

Taxes and payments: out of scope for the first pass. Both are
well-structured in phppos (`phppos_sales_items_taxes`,
`phppos_sales_payments`) and can be added later without reshaping the
core `Order` / `LineItem` graph.

---

## 4. Schema / code changes checklist

- [ ] `infra/neo4j/init.cypher`:
  - `CREATE CONSTRAINT entity_key_unique IF NOT EXISTS FOR (e:Entity) REQUIRE e.entity_key IS UNIQUE;`
  - `CREATE CONSTRAINT order_dedup_unique IF NOT EXISTS FOR (o:Order) REQUIRE (o.source_system_key, o.source_order_id) IS UNIQUE;`
  - `CREATE CONSTRAINT line_item_dedup_unique IF NOT EXISTS FOR (li:LineItem) REQUIRE (li.source_system_key, li.source_line_item_id) IS UNIQUE;`
  - `CREATE CONSTRAINT product_dedup_unique IF NOT EXISTS FOR (p:Product) REQUIRE (p.source_system_key, p.source_product_id) IS UNIQUE;`
  - Indexes: `(Order.ordered_at)`, `(Product.category)`, `(Product.sku)`.
- [ ] `services/ingestion/src/graph/queries/entities.py` — Cypher for Entity + OPERATED_BY + SourceSystem seed.
- [ ] `services/ingestion/src/graph/queries/sales.py` — Cypher for Order / LineItem / Product writes + PURCHASED / CONTAINS / OF_PRODUCT / SOLD_BY / SOLD_THROUGH.
- [ ] Bootstrap step in `pipeline.py` that seeds the three Entity nodes
      and the renamed SourceSystem nodes on first run (idempotent, MERGE).
- [ ] Connector `get_source_key()` rename in
      `fundbox/*.py` (`fundbox` → `fundbox_consumer_backend`, plus `:contacts`, `:legacy`, `:merged`, `:junk`),
      `speedzone/connector.py` (`speedzone` → `speedzone_phppos`),
      `eko/connector.py` (`eko` → `eko_phppos`).
- [ ] New sales connectors:
  - `services/ingestion/src/connectors/fundbox/sales.py`
  - `services/ingestion/src/connectors/speedzone/sales.py`
  - `services/ingestion/src/connectors/eko/sales.py`
- [ ] Pipeline: recognise `record_type='sales'` and route to the sales
      write query module; implement the `FOR_CUSTOMER_RECORD` → Person
      resolution step and the pending-customer parking list.
- [ ] Task dispatch: add Celery tasks for sales ingestion per connector
      (same dispatch pattern as the existing identity tasks).
- [ ] `docs/profile-unifier-graph-schema.md` — add Entity section,
      sales node section, new relationships table rows, update node
      inventory.
- [ ] `docs/profile-unifier-glossary.md` — new terms (Entity, Order,
      LineItem, Product, PURCHASED).
- [ ] `docs/profile-unifier-architecture.md` — update the ingestion
      pipeline diagram to show the sales branch.
- [ ] `docs/profile-unifier-openapi-3.1.yaml` + `api-spec.md` — sales
      endpoints (`GET /v1/persons/{id}/orders`, `GET /v1/products`) in a
      follow-up; not strictly required for the ingestion-layer change.

---

## 5. Rollout sequence

1. Land schema + connector renames + Entity seed (PR #1).
2. Land sales connectors + pipeline routing (PR #2).
3. Reset Neo4j (`docker compose down neo4j -v && up -d`). Kick off all
    ingestion tasks via Celery (beat or manual dispatch — never direct
    CLI exec).
4. Verify counts: one Entity per entity_key; renamed SourceSystem nodes
    only; expected Order/LineItem/Product counts per connector; PURCHASED
    edges from resolved Persons.
5. Audit the KNOWS wiring (covered in a separate task, see Phase D of the
    project plan).

---

## 6. Open questions

- SZ / Eko sales tables not present in the currently mounted dev DBs —
  need to locate the real data source before Phase C can finish.
- Fundbox order status filter: which statuses count as "completed"
  revenue vs. abandoned?
- Do SZ / Eko `phppos_sales.customer_id` allow NULL (walk-in sales)? If
  yes, unlinked Orders attach only to a SourceSystem, not to a Person —
  still useful for entity-level revenue, skipped for personal LTV.
- `Loan` modelling as a separate follow-up — confirm scope before writing.
