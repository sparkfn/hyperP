# MachineUnit Product-Scoped Identity Design

## Goal

MachineUnit nodes should represent a real machine unit identified by its product/variant/model name plus a unit identifier: either an LTA tag or a manufacturer/source serial number.

## Current behavior

The ingestion service extracts `machine_product`, `lta_tag`, and `serial_number` from sales line metadata and chat inquiries. A machine unit observation is currently valid when either LTA tag or serial number normalizes. The graph upsert matches `MachineUnit` nodes globally by normalized LTA tag or globally by normalized serial number, without scoping either identifier by product/model.

## Approved behavior

Use product-scoped unit identity:

- Normalize product/variant/model name into `normalized_machine_product`.
- Treat an observation as valid only when it has a normalized product name and at least one normalized unit identifier.
- Match a `MachineUnit` by `(normalized_machine_product, normalized_lta_tag)` when an LTA tag exists.
- Match a `MachineUnit` by `(normalized_machine_product, normalized_serial_number)` when a serial number exists.
- Store both display and normalized product values on the `MachineUnit` node.
- Preserve the existing conflict behavior when one observation's product+LTA and product+serial resolve to two different existing units.

## Scope

In scope:

- Machine unit normalization helpers.
- Machine unit observation validation.
- Sales and chat machine-unit upsert parameters.
- `UPSERT_MACHINE_UNIT` Cypher matching and persisted properties.
- Unit tests for helper validation and query shape.

Out of scope:

- Creating separate `MachineModel` or `Product` links for machine units.
- Migrating existing graph data.
- Changing Person, Order, Product, or ownership conflict semantics.
- Reworking chat extraction prompts beyond the current `machine_product`, `lta_tag`, and `serial_number` fields.

## Testing

Add or update tests to prove:

- Product names normalize consistently.
- Observations without a product name are invalid even if they have LTA or serial values.
- Observations with product plus one unit identifier remain valid.
- The upsert query includes product-scoped matching and persists product fields.
