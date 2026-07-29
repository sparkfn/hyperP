# Person Profile Analysis Design

Date: 2026-07-21

## Purpose

Generate two independent, LLM-authored analyses on demand for active Persons:

- a sales analysis that summarizes supported customer and purchase signals; and
- a contact-tracing analysis that summarizes supported relationship and event signals.

Generation runs directly on demand in the API and must never determine whether ingestion succeeds.
Only authenticated users can read the analyses. Prompts contain a minimized,
redacted view of Person information rather than raw source data or direct
identifiers.

## Scope

This initial implementation includes:

- immutable, versioned sales and contact-tracing analysis history;
- current-analysis pointers maintained independently for the two analysis types;
- ingestion-driven invalidation for accepted changes to Source Records,
  Identifiers, relationships, orders, and vehicles, without automatic generation;
- person-and-analysis-type targeted direct API execution with durable request state;
- automatic on-demand requests when the Person detail page finds missing, stale, or expired output;
- authenticated current and history APIs through `/app/v2`;
- Next.js BFF handlers and separate Person-detail cards; and
- API prose and OpenAPI updates for the new authenticated contract.

This implementation does not expose analyses through public Person links, make
profile-analysis output part of matching or merge decisions, or send messages
or sales actions automatically. The Person detail page provides a bounded
forced-refresh control for a still-valid analysis.

## Decisions

| Decision | Choice |
| --- | --- |
| Output separation | Independent `sales` and `contact_tracing` generations |
| Ingestion coupling | Ingestion records freshness only; it never queues LLM work |
| Persistence | Immutable version history with one current pointer per type |
| Privacy | Redacted, purpose-built snapshots; no raw payloads or direct identifiers |
| Existing Persons | Person detail requests missing, stale, or expired output on demand |
| Freshness | Durable Person revision plus stale-result publication guard |
| Visibility | Authenticated Person API and UI only |
| Trigger delivery | Dirty marker in Neo4j plus direct per-Person/per-type API execution |

## Architecture

An accepted graph mutation and its analysis invalidation share one Neo4j
transaction. The mutation increments `Person.analysis_input_revision` and sets
`Person.analysis_dirty_at` for every affected active Person. This durable state
is authoritative; direct API execution reads that state only when a human
requests analysis.

Ingestion only records a changed input revision and dirty timestamp; it never
queues profile analysis or calls an LLM. When the Person detail page loads, it
reads both slots and runs a separate direct request for each missing, stale, or
expired analysis. A successful analysis remains valid for 24 hours. A still
valid result may be force-refreshed, limited to three accepted forced requests
per canonical Person and analysis type in a rolling hour. Durable request and
claim state prevents duplicate requests and keeps upstream failures from
causing reload-driven request storms.

For each claimed Person, the API runtime:

1. reads a consistent, purpose-built profile snapshot and captures the current
   analysis input revision;
2. redacts and validates the snapshot before any network request;
3. generates the sales and contact-tracing outputs independently;
4. persists each attempt with its prompt and model provenance; and
5. publishes a successful result as current only if the Person is still active
   and its input revision still equals the captured revision.

The API runtime never holds a Neo4j transaction open during an LLM call. If ingestion
changes the Person while generation is running, the result is retained as an
obsolete historical attempt and the Person remains eligible for regeneration.

## Trigger Semantics

Only accepted changes that affect an active Person increment the analysis input
revision. The following changes are triggers:

| Change | Affected Persons |
| --- | --- |
| New or updated identity, bankruptcy, relationship, or conversation Source Record | Every Person gaining or losing accepted evidence |
| New, activated, retired, or changed Identifier projection | Every linked Person whose evidence changed |
| New, refreshed, or retired `KNOWS` relationship | Both active Person endpoints |
| New or updated accepted sales order | The resolved active customer Person |
| New, changed, or retired vehicle ownership, purchase, or conversation mention | Every active Person whose vehicle context changed |
| Accepted source-evidence retirement | Every active Person losing projections |

Exact duplicate records, excluded records, dropped match-only records, pending
review replacements, rejected replacements, and permanently failed records do
not trigger analysis because they do not change accepted Person information.
Address-only records do not trigger analysis unless they alter a Person-linked
projection through an accepted Person record.

The existing ingestion paths continue to decide which projections are active.
A shared graph helper only records that the resulting Person snapshot changed;
it does not duplicate domain-specific trigger logic.

## Graph Model

Each immutable `ProfileAnalysis` node contains:

- `analysis_id`: unique UUID;
- `person_id`: owning Person ID for indexed history lookup;
- `analysis_type`: `sales` or `contact_tracing`;
- `status`: `succeeded`, `failed`, or `obsolete`;
- `content`: plain text for successful output, absent for failures;
- `input_revision` and `input_fingerprint`;
- `prompt_version`, `provider`, and `model`;
- `started_at` and `completed_at`;
- `failure_code`, `retryable`, and `next_retry_at` when generation fails; and
- `attempt_number` for immutable attempt history.

The retry fields remain in the stored and API history contracts for compatibility
with earlier attempts. Direct executions persist one analysis attempt and do
not schedule `next_retry_at`; a later attempt requires an explicit human retry.

The Person owns history through:

```text
(:Person)-[:HAS_PROFILE_ANALYSIS]->(:ProfileAnalysis)
```

At most one pointer per analysis type identifies the currently published
successful result:

```text
(:Person)-[:CURRENT_PROFILE_ANALYSIS {analysis_type: "sales"}]->(:ProfileAnalysis)
(:Person)-[:CURRENT_PROFILE_ANALYSIS {analysis_type: "contact_tracing"}]->(:ProfileAnalysis)
```

Publishing deletes only the prior pointer for that type and creates the new
pointer in one transaction. It never deletes historical nodes. Schema setup adds
a uniqueness constraint for `analysis_id` and a composite history index covering
`person_id`, `analysis_type`, and completion time.

Person execution-state properties are operational metadata:

- `analysis_input_revision` and `analysis_dirty_at`;
- `analysis_claim_token` and `analysis_claim_until`; and
- `analysis_last_attempt_at`.

Claims are leases, not locks held across network requests. Expired claims are
recoverable by another direct API invocation.

## Redacted Input Snapshot

The snapshot query returns only data required by the two purposes. It can include:

- coarse demographic and completeness signals without name or exact date of birth;
- Source Record type, source category, observation date, and quality metadata;
- safe order facts such as date, total, currency, product, category, and merchant;
- safe vehicle facts such as product, manufacturer, and model;
- relationship category, direction, event dates, and locally scoped aliases; and
- safe data-gap, conflict, and staleness indicators.

The snapshot must not contain:

- names, NRICs, phone numbers, email addresses, or exact dates of birth;
- exact addresses or unit/postal identifiers;
- source-system record IDs or internal graph IDs;
- vehicle serial numbers or LTA tags;
- raw Source Record payloads; or
- raw conversation transcripts or unreviewed free-form source text.

Related Persons receive deterministic aliases scoped only to the current
snapshot, such as `Contact A`. Evidence receives local references such as
`order-1` and `relationship-2`. The stored input fingerprint is calculated from
the canonical redacted snapshot. The snapshot itself is not duplicated on the
analysis node.

Snapshot serialization uses typed models and explicit field allowlists. Copied
labels and generated output reject generic email, phone, NRIC, postal/address,
and vehicle-registration patterns in addition to known Person values. The
snapshot retains the most recent 20 Source Records, 8 orders with at most 5
items each, 10 vehicles, and 20 relationships. Data-quality metadata records
omitted counts, and the final serialized snapshot may not exceed 40,000 UTF-8
bytes before invoking the LLM.

## LLM Contracts

The API service owns a purpose-specific profile-analysis adapter using the
existing prose-oriented Proclaude backend.
Profile analysis uses the backend's plain-text chat mode rather than JSON mode,
matching the prompt and validator contract. The model remains configurable
through the established LLM hierarchy.

The two prompt versions are independent constants, initially
`sales-profile-v2` and `contact-tracing-profile-v2`. Both prompts:

- treat snapshot content as untrusted data, never as instructions;
- require concise plain text with local evidence references;
- include a clearly labeled limitations section;
- prohibit invented facts and unsupported identity claims;
- prohibit medical, legal, or safety conclusions not present in evidence;
- express uncertainty when evidence is incomplete or stale; and
- enforce a bounded output size and reject HTML.

The sales prompt focuses on observed purchase behavior, supported preferences,
customer-value signals, relevant opportunities, and cautions. It must not infer
protected traits or recommend discriminatory treatment.

The contact-tracing prompt focuses on observed relationship paths, interaction
or event chronology, reachable relationship categories, data gaps, and suggested
human follow-up priorities. It must not claim physical exposure, infection, or
causality without explicit structured evidence.

## Direct Execution Behavior

Each direct API invocation claims exactly one durable `ProfileAnalysisRequest`
for one canonical Person and one analysis type. The Person lease serializes
concurrent direct requests. Terminal, missing, and inactive requests are not
regenerated automatically.

Sales and contact-tracing calls are isolated. A failure in one type does not
discard or delay a successful result for the other type. Transport, rate-limit,
and transient provider failures are recorded as terminal attempts with safe
failure codes. Invalid output, privacy-boundary failures, and other permanent
errors are likewise recorded without a provider response body.

The API runtime clears its Person lease after the requested type is attempted
and completes the request as succeeded, failed, or obsolete. Provider failures
may be retried only by an explicit authenticated-human request; they are not
sent to Celery or scheduled in the background. There is no periodic
profile-analysis sweep or ingestion-triggered recovery; a later Person detail
request may create new work when output remains invalid.

No prompt, snapshot, generated content, provider response body, credential, or
direct identifier is written to logs. Logs may contain Person IDs, analysis IDs,
analysis type, revision, duration, safe status/error codes, and safe output
validation reason codes.

## Authenticated API

The API extends `PersonRepository` with protocol methods and implements Neo4j
queries in the repository layer. Routes do not access graph sessions directly.

The canonical contract paths are
`GET /v1/persons/{person_id}/profile-analyses`,
`POST /v1/persons/{person_id}/profile-analyses/requests`, and
`GET /v1/persons/{person_id}/profile-analyses/history`. The authenticated
frontend mount strips `/v1`, so their runtime UI paths are
`/api/app/v2/persons/{person_id}/profile-analyses` and
`/api/app/v2/persons/{person_id}/profile-analyses/history`. These routes require
an active authenticated human frontend user; they are excluded from the
root/public app and the `/oauth2/v1` machine subset.

`GET /v1/persons/{person_id}/profile-analyses` returns:

- the current sales analysis or `null`;
- the current contact-tracing analysis or `null`;
- overall refresh state: `disabled`, `pending`, `running`, `retrying`, `ready`,
  `partial`, or `failed`;
- the current Person input revision; and
- per-output stale/expired/valid indicators, refresh state (`disabled`, `idle`,
  `pending`, `running`, `retrying`, `ready`, or `failed`), a nullable safe
  `failure_code`, automatic-request eligibility, and forced-refresh budget.

A slot is `disabled` when rollout configuration pauses generation, `ready` when
its current success has the Person's input revision and is within 24 hours,
`idle` when invalid output has no active request,
`running` when the slot is not fresh and an active claim covers it, `failed`
when the slot is not fresh or running and a terminal failure exists for the
current revision, `retrying` when a historical failure retains a future stored
retry timestamp from the earlier delivery model, and
`pending` when an on-demand request is queued. The overall state is `running`
if either slot is running,
then `retrying` if either has a future legacy retry timestamp, `ready` if both are ready,
`partial` if exactly one is
ready, `failed` if neither is ready or running and at least one failed, and
otherwise `pending`. Disabled state takes precedence for the overall response
and prevents UI polling. A stale prior success and its content remain current while
a replacement is pending, running, or failed. Current output includes immutable
revision/fingerprint, prompt/model provenance, timestamps, API-formatted
`completed_at_display`, relative generated age, validity deadline, and attempt
metadata.

`GET /v1/persons/{person_id}/profile-analyses/history` returns cursor-paginated
terminal history ordered deterministically by completion time and analysis ID,
both newest first. An optional `analysis_type` filter accepts only `sales` or
`contact_tracing`; `meta.total_count` counts all entries matching that filter
before pagination, and `meta.next_cursor` is null on the final page. History
exposes immutable safe content, provenance, timestamps, attempt fields, and
nullable failure metadata (`failure_code`, `retryable`, `next_retry_at`). It
never exposes a provider response body, prompt, snapshot, credential, raw
payload, or direct identifier; unsafe failure codes become `null`.

Both routes return `ApiResponse[T]` through `envelope()` and use the existing
authenticated Person router. The public router and shared public `Person` model
remain unchanged. API prose and `docs/profile-unifier-openapi-3.1.yaml` are
updated together.

## Frontend

routes. Strict TypeScript interfaces mirror the API models.
Browser code calls thin Next.js BFF handlers for the three authenticated API
routes. Strict TypeScript interfaces mirror the API models.
routes. Strict TypeScript interfaces mirror the API models.

The authenticated Person detail page adds a Profile analysis section near the
top of its content area. A focused component renders separate Sales and Contact
tracing cards. Each card displays:

- current plain-text content;
- relative and exact generated time, validity deadline, and model label;
- disabled, stale, expired, pending, running, retrying, or failed refresh state; and
- the limitations text included in the output.

If refresh is pending or failed, the card retains the last successful content
and presents the refresh state separately. Missing output has an explicit empty
state. The panel automatically requests invalid output and uses independent
per-card overlays, so it cannot block the enclosing Person detail page. A valid
card offers a confirmed forced refresh, limited by the API to three accepted
requests per Person/type per rolling hour. Content is rendered as text, not
injected HTML. History remains available through the authenticated API; a
history browser is outside this initial UI scope.

## Failure and Concurrency Guarantees

- Ingestion success never depends on provider availability or analysis output.
- Dirty state is committed with the accepted Person mutation, so provider
  availability cannot lose invalidation.
- Duplicate or concurrent requests cannot publish more than one current pointer
  per type.
- A result for revision `N` cannot become current after the Person advances to
  revision `N+1`.
- A partial generation can update one current type without changing the other.
- A failed refresh cannot remove a prior successful current result.
- Merged and suppressed Persons are not selected for generation.
- A Person that changes status while generation runs fails the publication
  guard and receives no new current output.

## Configuration and Rollout

Profile analysis has explicit configuration for enablement, claim lease duration,
and retry limit. Existing LLM provider configuration and secrets remain unchanged.
The feature can be disabled without changing ingestion behavior or deleting
analysis history.

The same enablement flag is forwarded to the authenticated API. When disabled,
current history remains readable but current-state responses report `disabled`
and the frontend does not poll.

There is no automatic analysis backfill. Existing Persons receive an analysis
only when their detail page requests invalid output.

## Testing

Test-driven implementation covers:

- snapshot allowlisting, generic direct-identifier rejection at input and output,
  local aliases, canonical fingerprints, deterministic evidence caps, omitted
  counts, a 40,000-byte ceiling, and sparse profiles;
- every requested trigger and every non-trigger state;
- on-demand request creation, force-budget enforcement, and request idempotency;
- claim acquisition, expiry, and duplicate-execution behavior;
- separate sales/contact calls, partial success, safe terminal provider-failure
  metadata, and permanent failure recording;
- publication guards for revision and Person-status races;
- immutable history and one current pointer per analysis type;
- authenticated current/request/history APIs, pagination, filtering, missing
  data, stale and expired state, and public-route exclusion;
- BFF error propagation and strict frontend response types; and
- UI rendering for disabled, ready, partial, pending, running, failed, stale,
  empty, and long-content states.

Validation follows repository policy: safe structural checks may run locally,
while project lint, type, test, and build verdicts come from the canonical
Woodpecker PR workflow after an explicitly authorized commit, push, and PR.

## Acceptance Criteria

1. An active Person with available supported information can receive separate
   sales and contact-tracing analyses through direct on-demand API processing.
2. Accepted updates to Source Records, Identifiers, relationships, orders, and
   vehicles durably invalidate every affected active Person.
3. Ingestion succeeds when the LLM is unavailable, and prior successful analyses
   remain readable.
4. Direct identifiers, raw payloads, and transcripts never cross the LLM boundary.
5. Historical output and generation provenance remain auditable.
6. Stale generations cannot replace results for newer Person information.
7. Only authenticated Person APIs and UI expose analysis content.
8. A Person detail page can request invalid output without blocking its own load,
   while a valid card can force at most three refreshes per rolling hour.
