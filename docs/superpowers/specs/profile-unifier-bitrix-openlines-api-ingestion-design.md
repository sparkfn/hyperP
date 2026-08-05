# Bitrix Open Lines API Ingestion Design

Date: 20 July 2026

## Goal

Add API-backed ingestion of customer conversations from selected Bitrix Open
Lines to the existing `bitrix_chat` source. `batch` continues to read CRM-deal
warmer data from the Bitrix Chat Manager MariaDB, while `dump` continues to read
its existing SQL dumps. The `api` and `backfill` modes call Bitrix REST methods
directly and do not require changes to the third-party Bitrix Chat Manager
application.

## Scope

The first version supports:

- manually dispatched historical backfill;
- incremental API synchronization with a saved watermark;
- WhatsApp Business API conversations;
- Facebook direct-message conversations;
- Instagram conversations;
- explicit Open Channel configuration-to-entity mappings;
- the existing HyperP chat extraction and person-ingestion flow.

The defaults exclude:

- WhatsApp Device and WhatsAdmin conversations;
- Facebook Comments;
- Telegram, Carousell, Bitrix Chat, Slack, and unknown connectors;
- selected conversations without an explicit entity mapping.

This work does not modify the Bitrix Chat Manager application, send messages,
intercept sessions, or change Bitrix configuration.

## Live-system findings

The ADA Kubernetes context contains the Bitrix Chat Manager in namespace
`bitrix-chatmanageradaasia`. Its public API service is
`https://chatmanager.ada.asia`, but it has no extraction endpoint. The deployed
application already calls Bitrix REST but remains an independent third-party
service.

The connected Bitrix account exposes the methods needed for the proposed read
path, including:

- `imopenlines.config.list.get`;
- `im.recent.list`;
- `im.dialog.get`;
- `im.dialog.messages.get`;
- `imopenlines.session.history.get`;
- `imopenlines.crm.chat.get`.

It does not expose a global method that enumerates every historical Open Line
session. Historical discovery therefore has a documented coverage limitation.

## Architecture

Reuse the `bitrix_openlines` connector infrastructure under the ingestion service
for the `bitrix_chat` API and backfill modes. Keep the
Bitrix HTTP client, response validation, discovery, classification, checkpoint,
and envelope-building responsibilities in focused modules.

The connector uses two discovery paths:

1. Enumerate CRM activities whose provider is `IMOPENLINES_SESSION` (the value
   used by this portal), then obtain numeric Bitrix chat IDs from provider
   metadata or `imopenlines.crm.chat.get`. A future compatibility alias may also
   recognize the older `IMOL` provider value.
2. Page through `im.recent.list` for dialogs visible to the authenticated Bitrix
   integration user.

Merge and deduplicate the results by numeric Bitrix chat ID. For every chat,
read dialog metadata, determine its Open Channel configuration and raw connector
identifier, classify its channel type, apply configuration filters, and fetch
the available ordered message history.

The historical backfill is the union of CRM activity discovery and available
recent dialogs. Old conversations that were never attached to CRM and are no
longer visible in the authenticated user's recent dialogs cannot be discovered
through the available REST contract. The connector must report this limitation
in operational documentation and must not claim exhaustive portal history.

## Ingestion modes

`backfill` performs the widest historical traversal supported by the available
REST methods. It is manually dispatched and does not establish that all portal
history was discovered.

`api` performs incremental synchronization. It reads the last committed durable
watermark and applies a configured overlap window so late changes are safely
re-read. Stable source-record IDs and the existing ingestion idempotency rules
prevent duplicate graph evidence.

The connector commits its watermark only after the full ingestion run completes
without record-processing errors. A failed or partially erroneous run leaves the
previous watermark unchanged.

## Configuration

Extend the consolidated ingestion configuration with a typed
`bitrix_openlines` block:

```json
{
  "bitrix_openlines": {
    "included_channel_types": [
      "whatsapp_business_api",
      "facebook_direct",
      "instagram"
    ],
    "included_config_ids": [],
    "excluded_config_ids": [],
    "entity_by_config_id": {
      "46": "speedzone",
      "47": "eko",
      "79": "speedzone"
    },
    "entity_by_crm_category_id": {
      "0": "eko",
      "2": "speedzone"
    },
    "incremental_overlap_seconds": 300,
    "recent_page_size": 50
  }
}
```

The example entity mappings are illustrative rather than production defaults.
The committed example configuration should include only mappings verified for
this repository's known entities. `entity_by_config_id` maps Open Lines
configuration IDs, while `entity_by_crm_category_id` maps Bitrix CRM deal
pipeline/category IDs. Every CRM deal category that HyperP ingests must have an
explicit CRM-category mapping; unmapped deals fail closed rather than creating
records without record-scoped ownership.

Supported channel-type values are:

- `whatsapp_business_api`;
- `whatsapp_device`;
- `facebook_direct`;
- `facebook_comments`;
- `instagram`;
- `telegram`;
- `carousell`;
- `bitrix_chat`;
- `other`.

If the block is omitted, the default included types are
`whatsapp_business_api`, `facebook_direct`, and `instagram`. All configuration
ID override lists and entity mappings default to empty.

Filtering precedence is:

1. An ID in `excluded_config_ids` is always excluded.
2. An ID in `included_config_ids` is eligible even when its channel type is not
   selected.
3. Other configurations are eligible only when their classified channel type
   is in `included_channel_types`.
4. Every eligible configuration still requires an `entity_by_config_id`
   mapping. Missing mappings are skipped with a warning.

This precedence lets operators opt a configuration into discovery without
bypassing entity isolation.

Channel classification uses the connector identifier carried by the dialog's
Bitrix `entity_link`. Known WhatsApp Business API identifiers such as
`WHATSAPP_BUSINESS_API_CONNECTOR_AIAPPS_PRO_1` classify as
`whatsapp_business_api`; device connectors such as `SPARKFN_WHATSAPP` classify
as `whatsapp_device`. Facebook messages and Facebook Comments remain distinct.
Unknown identifiers classify as `other`, are excluded by default, and produce a
warning for operational review.

The Bitrix REST base URL and credential, request timeout, retry settings, and
page limits that are service deployment concerns remain environment variables.
Secrets must not be stored in the ingestion JSON.

## Source records and provenance

Use source system key `bitrix_chat`. A conversation produces one source record
per extracted possible person, following the existing chat-ingestion model.
Stable IDs and conversation provenance remain compatible with the existing
Open Lines connector, for example `bitrix-openlines-chat-153291-person-1` with
platform `bitrix_openlines`. The distinct prefix prevents a portal `CHAT_ID`
from colliding with the independent MariaDB/dump `chats.id` namespace.

The shared `bitrix_chat` SourceSystem has no source-level `OPERATED_BY` owner.
Each conversation instead carries its configured entity in its record-scoped
`tenant` provenance and has a `SourceRecord-[:OWNED_BY]->Entity` relationship,
so one shared source is never attached to multiple entities. Only administrators
may dispatch ingestion for this multi-entity source.

Deployments that previously seeded `bitrix_openlines` rehome its records and
runs to `bitrix_chat`, establish `OWNED_BY` for every record, and only then
deactivate the legacy source and remove stale source-level ownership. The
migration is atomic and stops if any record cannot be mapped to a known entity.

The raw payload retains:

- numeric Bitrix chat ID;
- Open Channel configuration ID and line name;
- classified channel type and raw connector identifier;
- mapped HyperP entity key;
- CRM activity and entity references when available;
- discovery methods used for the chat;
- customer and agent participant metadata;
- ordered message transcript;
- first and last message timestamps;
- extracted summary, sentiment, identifiers, inquiries, and transactions.

Agents and configured internal identities remain provenance only and must not
become customer identity evidence. Existing exclusions and chat extraction
rules continue to apply.

## Error handling and rate limits

Use bounded retry with exponential backoff for transient Bitrix failures and
rate limits. Respect any upstream retry interval. Authentication, authorization,
invalid response schema, and configuration errors fail the ingestion run.

Dialogs that were deleted or became inaccessible after discovery are recorded
as processing errors with their chat IDs and discovery source. They must not be
silently treated as successful. Any processing error prevents watermark
advancement.

Requests remain sequential or conservatively rate-limited unless the live
Bitrix contract establishes a safe higher concurrency. Logs must contain IDs and
method names but never credentials, webhook URLs containing credentials, message
bodies, or sensitive customer identifiers.

## Dispatch and deployment

Register `bitrix_chat` for `api` and `backfill` modes in the existing connector
factory and Celery ingestion task path. Preserve the database connector for
`batch` and the SQL connector for `dump`. Do not invoke ingestion directly from
an API route; the authenticated route creates a run and enqueues the existing
ingestion task with that run ID.

Add environment settings for the Bitrix REST connection and retry behavior to
the ingestion service and deployment examples. Scheduling is disabled by
default. If a schedule is later enabled, it dispatches `mode="api"`; historical
`backfill` remains manual.

## Validation

Implement through behavior-first vertical test slices. Coverage includes:

- default and custom ingestion JSON parsing;
- strict rejection of malformed channel types, IDs, entity maps, and numeric
  settings;
- WhatsApp Business API versus WhatsApp Device classification;
- Facebook direct-message versus Facebook Comments classification;
- Instagram and unknown-connector classification;
- filter override precedence;
- unmapped entities being skipped;
- CRM activity and recent-dialog discovery;
- pagination and chat-ID deduplication;
- ordered message-history conversion;
- stable source-record IDs and raw provenance;
- incremental overlap and watermark commit behavior;
- bounded retry and failed-run behavior;
- connector registration and dispatch for `api` and `backfill`.

Run focused ingestion tests during development, followed by ingestion Ruff,
format checking, strict mypy, the full ingestion test suite, `git diff --check`,
and a hostile review for correctness, security, edge cases, duplication, brittle
tests, and compatibility.

No branch push or Woodpecker validation occurs without explicit authorization.
