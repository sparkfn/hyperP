# Chat Ingestion Extraction Design

## Goal

Improve chat ingestion so Bitrix and WhatsApp conversations produce fuller summaries and separate strong identity identifiers from weak contextual identifiers.

This spec builds on `2026-05-14-ingestion-data-adjustments-design.md`, especially timestamp ordering and MachineUnit chat observations.

## Scope

This design covers:

- richer sectioned summaries while keeping `summary` as a string;
- extraction of strong and weak identifiers from chat conversations;
- promotion rules for strong identifiers;
- storage rules for weak identifiers as review evidence;
- prompt, typed parser, payload, and test changes.

This design does not change the LLM provider, add UI, or replace MachineUnit graph writes from the ingestion data-adjustments spec.

## Current behavior

The chat prompt currently asks for a concise factual summary and returns `summary: string | null`.

The extraction result includes `persons`, `transactions`, `chat_members`, `inquiries`, `summary`, `customer_sentiment`, and `confidence`.

Connector payloads persist summary and extracted chat details in raw payloads. Only phone and email from extracted persons currently become normalized identifiers. Names become attributes. Address and NRIC are parsed but not consistently promoted into downstream identity evidence.

## Sectioned summary string

Keep `summary` as a string for compatibility, but make it longer and sectioned.

The prompt should require a thorough factual summary of the whole conversation. It should preserve material details and avoid speculation.

Recommended section headings inside the string:

```text
Customer / Participants:
...

Identity Evidence:
...

Products / Machine Units:
...

Orders / Commercial Terms:
...

Timeline / Follow-ups:
...

Uncertainties:
...
```

The extractor may omit a section only when no evidence exists. `Uncertainties` should list ambiguous names, identifiers, ownership claims, products, order IDs, missing timestamps, or source conflicts.

The raw payload key remains `summary`; downstream consumers should not need a new object shape in this slice.

## Strong identifiers

Strong identifiers are structured values that may enter the normalized identifier flow for chat records, while still respecting the existing rule that conversation-sourced evidence cannot deterministically auto-merge.

Examples:

- phone number;
- email address;
- NRIC/FIN or other government ID when explicitly stated;
- source/customer reference only when it unambiguously identifies a customer profile in a trusted source namespace.

Strong identifiers should be normalized and emitted through the existing `identifiers` flow with conversation provenance and unverified status unless the source provides verification.

Phone and email already mostly follow this pattern. The design extends typed extraction and helper logic so other strong identifiers are not silently dropped after parsing.

## Weak identifiers

Weak identifiers are useful for review and context but must not become auto-match keys from chat extraction.

Examples:

- name;
- address;
- date of birth;
- machine-unit references such as LTA tag, serial number, unit label, product, or model;
- order IDs or invoice references mentioned in conversation when they are not trusted customer identity keys;
- partial or ambiguous phone/email fragments;
- relationship labels, roles, and customer context.

Weak identifiers should be retained in raw payload and, where existing ingestion supports it, normalized attributes/facts. They may later feed review-only matching with caps, but they should not be promoted into deterministic identity keys.

## Extraction output additions

Keep existing top-level fields and add compatibility-safe fields:

- `strong_identifiers`: array of structured identifier objects.
- `weak_identifiers`: array of structured evidence objects.

Suggested strong identifier fields:

- `type`: `phone`, `email`, `government_id`, or `source_customer_ref`.
- `value`: extracted value.
- `label`: optional label such as `nric`, `fin`, or `customer_id`.
- `person_name`: optional associated person name when stated.
- `confidence`: extraction confidence for this value.
- `notes`: short evidence context.

Suggested weak identifier fields:

- `type`: `name`, `address`, `dob`, `machine_lta_tag`, `machine_serial_number`, `machine_unit`, `product`, `order_ref`, `relationship`, or `other`.
- `value`: extracted value.
- `label`: optional source label.
- `person_name`: optional associated person name when stated.
- `confidence`: extraction confidence for this value.
- `notes`: short evidence context.

The parser should tolerate missing `strong_identifiers` and `weak_identifiers` so older model output still works.

## Backward compatibility

The prompt may continue asking for `persons`, `transactions`, `chat_members`, and `inquiries` because connectors and tests already use them.

`strong_identifiers` and `weak_identifiers` should complement those fields, not replace them in the first implementation.

When both legacy `persons[].phone/email` and `strong_identifiers` contain the same value, de-duplicate before writing normalized identifiers.

## Promotion rules

Promote to normalized identifiers:

- valid phone;
- valid email;
- valid government ID only if normalization/hash handling exists for chat evidence;
- trusted source customer references only when a source namespace is explicit and supported.

Do not promote:

- names;
- addresses;
- DOB;
- MachineUnit identifiers from chat;
- ambiguous order references;
- partial identifiers.

Names, addresses, DOB, and machine-unit values remain weak evidence or facts.

## Prompt changes

Update the extraction prompt to:

- require sectioned `summary` text;
- explicitly ask for strong and weak identifiers;
- explain that weak identifiers are evidence, not identity keys;
- keep conservative extraction: only extract explicitly stated information;
- keep customer/staff separation so agents and company representatives do not become customer identifiers;
- state that the supplied conversation is ordered oldest first, newest last after the timestamp-ordering change.

## Payload changes

For Bitrix and WhatsApp raw payloads, persist:

- `summary` as the sectioned string;
- `strong_identifiers`;
- `weak_identifiers`;
- existing `inquiries`, `transactions`, `chat_members`, and sentiment fields.

For normalized payloads, continue storing `summary` and `customer_sentiment` for conversation records.

## Testing

Add or update focused tests for:

- prompt includes sectioned summary requirements;
- prompt includes strong and weak identifier instructions;
- parser accepts output with and without new identifier arrays;
- valid phone/email strong identifiers promote to normalized identifiers;
- duplicate legacy and new strong identifiers are de-duplicated;
- weak identifiers are preserved in raw payload but not promoted to identity identifiers;
- sectioned summary persists through Bitrix and WhatsApp connectors;
- malformed identifier entries are ignored without failing the whole extraction.

Run ingestion tests, lint, and strict type checks for changed ingestion code.
