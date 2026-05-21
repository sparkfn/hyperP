# MachineUnit Sales-First Link-Only Chat Design

## Goal

MachineUnit nodes are created from deterministic sales data, while chat evidence only links to existing machine units and existing person/order context.

## Approved approach

Use sales ingestion as the source of truth for MachineUnit creation. Chat evidence must not create MachineUnit nodes.

## Fundbox sales

Fundbox dump sales must resolve product identity through the real dump path:

1. `order_items.merchant_product_id`
2. `merchant_products.id`
3. `merchant_products.product_variant_id`
4. `product_variants.id`
5. `product_variants.product_id`
6. `products.id`

The limited-100 Fundbox sales dump must include all rows required for that path for the selected order items. MachineUnit observations continue to read `metadata.lta_tag` and `metadata.serial_no`, with product/variant/model provided by the resolved product payload.

## PHPPOS sales

PHPPOS sales dumps must expose valid serial numbers to the shared machine-unit extractor. The line payload should preserve the raw line data and also place serial evidence in the shape consumed by `observations_from_sales_lines`, so sales ingestion creates MachineUnit nodes consistently.

The limited-100 Eko and SpeedZone sales dumps should be curated from full dumps using sales rows with valid non-placeholder `serialnumber`, plus their corresponding `phppos_sales`, `phppos_sales_items`, and `phppos_items` rows.

## Chat evidence

Chat machine-unit evidence is link-only:

- A chat inquiry with LTA tag or serial number attempts to resolve an existing MachineUnit.
- If product is present, matching remains product-scoped.
- Participant phone is used only to resolve an existing Person.
- Order/invoice number is used only to resolve existing Order context and any existing unit/person pair.
- Chat may create evidence relationships to existing MachineUnit nodes.
- Chat must not create new MachineUnit nodes.
- A Person→MachineUnit relationship from chat is created or confirmed only when the existing graph yields one unambiguous Person/MachineUnit pair.

If no existing MachineUnit is found, the chat evidence remains in the conversation SourceRecord payload only.

## Scope

In scope:

- Fundbox dump connector product lookup fix.
- PHPPOS dump sales payload alignment for serial evidence.
- Limited-100 dump updates needed to exercise MachineUnit creation.
- Chat link-only MachineUnit resolution.
- Regression tests for sales creation and chat non-creation/link behavior.

Out of scope:

- Chat-created MachineUnit nodes.
- Broad graph migration for already-ingested records.
- Changing the product-scoped MachineUnit identity rule.
- UI changes.

## Verification

Expected after reset and limited-100 ingestion:

- Sales ingestions create MachineUnit nodes when product plus LTA/serial exists.
- Fundbox sales line items resolve product payloads through merchant products and variants.
- PHPPOS limited sales fixtures contain valid serial-bearing rows and create machine-unit observations.
- Chat ingestion does not create MachineUnit nodes, but can link to existing units when deterministic sales data already created them.
