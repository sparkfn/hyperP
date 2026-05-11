# Ingestion Internal Exclusions Design

## Goal

Prevent internal actors and tenant-owned identifiers from entering identity ingestion and profile matching. The first implementation covers Fundbox, Eko PHP POS, SpeedZone PHP POS, Bitrix chat, and WhatsApp chat.

Excluded records must not create `SourceRecord`, `Identifier`, `Person`, candidate matches, review cases, merge decisions, or golden-profile facts. Sales records that point to excluded customer identities should be skipped rather than linked to intentionally absent identity records.

## Approach

Use a hybrid model:

1. Source-native exclusion where the source schema exposes roles, staff, employees, agents, or session ownership.
2. Shared normalized identifier exclusion for company mobile numbers and other known tenant-owned identifiers that are not always discoverable from source metadata.

This keeps source-specific evidence close to each connector while giving chat extraction a common safety net.

## Fundbox system sources

Declare and use the Fundbox `roles`, `model_has_roles`, and `merchant_staff` tables.

A Fundbox user is excluded when either condition is true:

- `model_has_roles.model_id = users.id` joins to `roles.name IN ('merchant', 'admin')`.
- `merchant_staff.user_id = users.id`.

The rule applies to:

- current user identity records from `fundbox_consumer_backend`;
- legacy profile records from `fundbox_consumer_backend:legacy` when `log_legacy_profiles.user_id` is excluded;
- merged-user lineage from `fundbox_consumer_backend:merged` when the old or surviving user is excluded;
- Fundbox sales records from `fundbox_consumer_backend:sales` when `orders.user_id` is excluded.

Emergency contact records are not excluded merely because their referrer user is excluded. The contact may be a real external person. If the referrer identity is absent, relationship materialization can remain unresolved rather than creating an internal Person.

## Eko and SpeedZone PHP POS sources

Declare and use `phppos_employees.person_id` in both PHP POS schemas.

A PHP POS identity record is excluded when its `phppos_people.person_id` appears in `phppos_employees.person_id`. This applies to both the normal `phppos_customers JOIN phppos_people` path and the `phppos_people` fallback path.

PHP POS sales records are skipped when their `customer_id` resolves to an excluded employee person.

## Bitrix chat source

Bitrix already exposes chat agent membership through `agents` and `agent_chat`. Agent data should remain in `raw_payload.chat_members` for provenance, but should not become identity evidence.

After LLM extraction, remove extracted persons and identifiers that match the chat's known agents by agent name or agent ID. If all extracted identity evidence is removed, skip the conversation identity envelope.

## WhatsApp chat source

WhatsApp chat extraction must exclude the tenant/session side of each conversation. The connector should derive tenant-owned phones and JIDs from:

- `sessions.whatsapp_user_id`;
- `sessions.expected_phone_number`;
- message endpoints marked by `from_me` and session ownership;
- configured company mobile denylist values.

After LLM extraction, remove extracted persons and identifiers that match those tenant-owned phones/JIDs. If all extracted identity evidence is removed, skip the conversation identity envelope.

## Shared exclusion helper

Add a small shared helper in the ingestion service for normalized exclusion checks. It should support:

- exact normalized phones;
- exact normalized emails;
- exact normalized names for source-derived staff/agent names;
- exact source IDs where a connector has explicit IDs.

Connectors remain responsible for discovering source-native exclusions. The helper handles consistent normalization and post-extraction filtering, especially for chat sources.

## Observability

Log exclusion counts per source run and include non-sensitive exclusion reasons such as `fundbox_role:merchant`, `fundbox_merchant_staff`, `phppos_employee`, `bitrix_agent`, or `company_phone`. Do not log raw secret values beyond existing connector payload behavior.

## Testing

Add focused unit tests for:

- Fundbox current user exclusion by merchant/admin role and merchant staff membership;
- Fundbox legacy, merged, and sales skips for excluded users;
- PHP POS customer and people-only fallback exclusion when person IDs are employees;
- Bitrix post-extraction agent filtering;
- WhatsApp session/company phone filtering;
- shared helper normalization for phone/email/name checks.

Run ingestion tests plus lint/type checks for the ingestion package.
