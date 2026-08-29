# Profile Unifier - Deal Intelligence Architecture

## Status and scope

This document describes the broader staged Deal Intelligence architecture. Issue
#315 is limited to the disabled PostgreSQL 16 platform foundation: shared
source-instance/readiness/run/unit/checkpoint/lease-fence/terminal-accounting/
migration tables only. It does not implement or claim ownership of the later
identity, deal/stage, activity, historical-import, artifact, projection/outbox,
ownership-transfer, credential, ingress, deployment, or live-execution work
otherwise described as future architecture below.

Where this reviewed contract differs from the initial issue proposal, this
contract supersedes it. In particular, Deal Intelligence stores CRM identity
references rather than unrestricted contact profiles, preserves the existing
SQLite v1 dataset and safe JSON model formats, and does not introduce joblib or
pickle artifacts.

It does not authorize a live cutover, destructive graph repair, full Bitrix
backfill, or reuse of existing CRM deal links as identity authority. Those
operations require the explicit gates in [Operational acceptance gates](#operational-acceptance-gates).

The service owns CRM operational history, stage authority, analytical datasets,
and prediction evidence. HyperP remains the sole authority for Person identity,
matching, review, locks, and golden profiles.

## Implementation readiness

Architecture and disabled-foundation work is ready to begin. Standalone HyperP
contact/lead/company identity and the identity-link stream are also ready for
implementation without further architectural discovery.

The following are not ready for operational execution: treating existing deal
links as migration truth, authoritative identity seeding, CRM projection-read
cutover, disabling HyperP deal ingestion, transferring source ownership, and
destructive Neo4j cleanup. Those actions remain blocked by the measurable gates
in this document.

## Goals

- preserve immutable CRM deal, activity, and stage-history lineage in PostgreSQL;
- let analytical and prediction workloads leave Neo4j without changing identity
  ownership;
- preserve point-in-time reproducibility, authority corrections, and release
  lineage;
- retain the minimal Neo4j projections required by the existing person list,
  person detail, and CRM metrics experiences; and
- ensure any transfer of source ownership is observable, fenced, reversible,
  and single-writer.

## Non-goals

Deal Intelligence must not:

- resolve, merge, or unmerge Persons;
- retain raw CRM phone/email groups, review candidates, match scores, or
  `NO_MATCH_LOCK` topology in ordinary operational or analytical tables;
- infer a Person link from a deal, contact, lead, company, or historical graph
  edge;
- create canonical tenant entities from Bitrix companies; or
- replace HyperP before the operational acceptance gates have been met.

## Ownership boundary

| Capability or record | Authoritative owner | Deal Intelligence role |
| --- | --- | --- |
| Person, identifiers, source-record identity, golden profile | HyperP | Consume closed link status only |
| Match decisions, candidate lists, review cases, locks | HyperP | Never copy matching logic or candidates |
| Bitrix contact/lead standalone identity | HyperP | Reference source IDs and link revision |
| Bitrix company identity | HyperP | Non-Person source reference only |
| Current deal state and immutable deal versions | Deal Intelligence after cutover | HyperP receives minimal projections |
| Activity/call history | Deal Intelligence after cutover | HyperP receives minimal projections |
| Stage occurrences, conflict, authority, corrections, releases | Deal Intelligence after cutover | HyperP receives minimal projections |
| Dataset, evaluation, training, model metadata | Deal Intelligence | HyperP displays approved results only |

`review_candidate_person_ids`, blocked candidates, matching channels, heuristic
snapshots, and no-match locks are HyperP-only adjudication evidence. A resolved
link is a projection from HyperP, not a capability delegated to Deal
Intelligence.

Contacts and leads may be associated with multiple tenant `Entity` records.
Those associations are versioned and retain source instance, observation,
relationship kind, validity/lifecycle, and provenance. They must not inherit one
deal category as their tenant ownership. Bitrix contact-company and tenant
associations are likewise versioned. A Bitrix company remains a non-Person
source organization/reference and does not automatically become a canonical
HyperP `Entity`.

## CRM identity policy contracts

The existing deal policy is immutable:

```text
crm_deal_identity_v2
```

It is specific to identity hints carried by a CRM **deal**. A future policy
change must receive a new version string and a bounded impact report; it must
never silently change the meaning of `crm_deal_identity_v2`.

Standalone Bitrix identity uses separate contracts:

```text
crm_contact / crm_contact_identity_v1
crm_lead / crm_lead_identity_v1
crm_company / crm_company_reference_v1
```

The standalone policies may reuse normalization, invalid-email filtering, and
channel-cardinality helpers, but must not inherit deal-specific semantics by
accident. Contact and lead channel evidence remains governed by HyperP's
identity policy. A company is a source organization/reference and cannot create
or match a Person automatically.

### Source identity namespace

Every standalone identity record is unique across portal and source type:

```text
source_system
source_instance_id
source_entity_type
source_entity_id
```

Stable source-record IDs are:

```text
bitrix-crm-contact-{contact_id}
bitrix-crm-lead-{lead_id}
bitrix-crm-company-{company_id}
```

New records must use the portal-scoped identifier types:

```text
crm_contact_id
crm_lead_id
crm_company_id
```

`crm_company_id` is a source-reference identifier, not Person identity
evidence. It is never projected as a Person `Identifier` and never enters Person
candidate generation. Company source records use the `crm_company` provenance
class and a reference-only write path, so they are activated without invoking
Person matching or Person creation. Contact and lead identifiers may participate
only under their separately versioned HyperP policies.

Existing CRM deal records that use `external_customer_id` are legacy evidence.
They remain readable but must not be reinterpreted or emitted by a new lead
pipeline.

The source-record ID strings above are stable within a source instance. HyperP's
current SourceRecord identity is `(source_system, source_record_id)`, which is
not sufficient for multiple Bitrix portals. PR 250.2 therefore introduces a
backwards-compatible, immutable, non-secret `source_instance_id` on the source
context, SourceRecord envelope, lifecycle identity lock, parent references, and
relevant provenance relationships. The effective SourceRecord identity becomes:

```text
source_system + source_instance_id + source_record_id
```

Existing non-instance-aware records receive a deterministic legacy/default
instance during an idempotent migration; no existing identity may be left in
both pair-keyed and triple-keyed active state. Deal-v2 raw payloads and hashes
remain unchanged because the instance is source-envelope provenance rather than
deal payload content. For CRM identifier types, the canonical matching key
becomes:

```text
identifier_type + source_instance_id + normalized_value
```

The existing Bitrix portal receives one explicit configured instance ID before
standalone records are emitted. Normalization obtains it from the registered
source context, not from the credential-bearing webhook URL. Existing CRM
identifier evidence is migrated idempotently to that instance scope before any
standalone records are emitted. The bounded standalone CRM identity reader is
internal foundation only: `get_connector()`, `run_ingestion()`, and Celery task
dispatch reject `bitrix_crm_identity` until the durable census authority and
checkpointed child payload path are implemented. It is not available as an API-mode
full snapshot and is not added to Celery Beat.
Historical SourceRecord lifecycle identity remains in the deterministic legacy namespace;
only its Bitrix CRM identifier evidence is remapped to the registered portal scope,
avoiding a second collision-sensitive record rekey. A second Bitrix portal is rejected until its
distinct source instance is registered and the instance-scoped constraints and
parent-resolution queries are online. A numeric ID from one Bitrix portal must
never resolve a record or link from another portal.

## Link projection contract

Deal Intelligence receives only a closed identity projection:

```text
resolved
unresolved
pending_review
blocked
rejected
retired
```

For `resolved`, it receives the accepted HyperP Person UUID and the HyperP
resolution revision. It must not receive alternative candidates, raw identity
values, matching scores, or lock relationships.

The link key is the complete source identity tuple, not merely a CRM numeric ID.
Each projection includes:

```text
event_id
global_revision
source_system
source_instance_id
source_entity_type
source_entity_id
identity_policy_version
link_status
hyperp_person_id
resolution_kind
resolution_revision
effective_at
match_decision_id
review_case_id
supersedes_event_id
```

Only safe provenance identifiers are transferred. A missing Person UUID is
required for all non-`resolved` statuses.

Identity-link unavailability does not block Bitrix operational synchronization
or checkpoint advancement. A deal, activity, or stage record is committed with
an unresolved closed status and may be linked later by applying a higher HyperP
revision. That later resolution updates only the mutable link projection; it
does not rewrite immutable CRM history.

## HyperP identity-link revision stream

HyperP must publish one globally ordered identity-link revision whenever an
authoritative lifecycle mutation occurs:

- automatic activation;
- reviewed activation;
- review rejection or manual no-match;
- source-record supersession;
- Person merge or unmerge;
- Person retirement; and
- source-record retirement or deletion.

The event write must occur in the same repository-mediated transaction as the
authoritative lifecycle mutation, or under an equivalent atomic persistence
guarantee. A graph scan cannot replace this contract because it cannot preserve
the ordering of activation, merge, unmerge, and retirement transitions.

Consumers require:

- bounded, cursor-based OAuth event reads;
- duplicate-safe application by event ID and global revision;
- gap detection and bounded retry;
- a full identity-link snapshot fixed at a global revision; and
- snapshot-plus-tail recovery from that revision.

The machine endpoints are intentionally excluded from MCP because they are
transport-specific synchronization interfaces. The API-to-MCP parity test must
assert this explicit exclusion and its reason.

Before cutover, every pending CRM deal review must be accepted, rejected,
migrated as unresolved, or retained read-only with an approved disposition.
Blocked reviews are quarantine evidence, not actionable candidate-selection
cases, and cannot export a resolved link.

## Service foundation

`services/deal_intelligence` is an installable Python 3.12 package in the root
`uv` workspace. Its foundation runtime is PostgreSQL 16, SQLAlchemy 2, psycopg 3,
and Alembic, with separately runnable API, worker, scheduler, migration, and
health commands.

The disabled foundation contains only typed configuration and default-off controls;
source-instance registry records; schema-revision readiness; synchronization runs/
units, checkpoints, and terminal accounting; and generic leases with monotonically
increasing fence tokens. Default registries are empty. A process start does not
register a source, start a writer or schedule, call a CRM, or write later-domain
data.

This foundation does not own identity-link projections, CRM deal/activity/stage
schemas, historical imports, artifacts or analytics, projection outbox/dead-letter
state, or ownership-transfer workflows/tasks. Those existing architectural sections
remain proposals for later, separately scoped work; they are not #315 delivery
requirements.

Migrations are additive reviewed Alembic revisions. Future component lanes branch
independently from the platform lane by default; a real additional dependency belongs
to that future revision, not sibling serialization. Independent branches converge
through a reviewed merge revision before release. API, worker, scheduler, and health
startup never applies migrations; an operator must run the migration command
explicitly. Readiness fails closed when required revisions are absent or ambiguous.

## API and process topology

Deal Intelligence has no nginx route, browser/BFF path, public ingress, or MCP
surface. Its health/readiness interfaces are container-internal only: no host port
or external health route is part of this foundation. The
`deal-intelligence-health` command accepts `--component api|worker|scheduler`
and returns a secret-safe structured disabled readiness result only after schema
validation and a fresh component heartbeat. API, worker, and scheduler probes must
not activate a writer or schedule.
Worker and scheduler commands are long-lived, signal-aware disabled heartbeat loops;
their one-cycle seams exist only for source tests.

Deal Intelligence is not a new public browser ingress. Browser calls continue
through frontend2 BFF handlers to HyperP's authenticated `/app/v2` contract.
HyperP API repositories or typed internal services obtain Deal Intelligence CRM
views, while minimal CRM projections preserve indexed person-list operations.
Browser code never calls Deal Intelligence directly.

Business endpoints exposed through HyperP use typed envelopes, stable operation
IDs, route-catalog registration, and API-to-MCP parity. Transport-specific
identity event/snapshot and projection-consumer endpoints use machine OAuth,
bounded scopes, cursor/resource limits, and explicit tested MCP exclusions.
Service credentials, database passwords, artifact signing keys, backup keys, and
Bitrix webhook credentials remain runtime secrets and must not appear in events,
manifests, logs, source-instance IDs, or repository configuration.

Synchronization, training, and latency-sensitive prediction do not share one
worker concurrency pool. The first deployment may use one image/package, but it
provides separate queues/process entry points so training cannot starve source
synchronization or serving.

## Synchronization and point-in-time contract

Direct Bitrix synchronization is bounded by source instance and stream. Each run
freezes or records an upper source boundary, uses deterministic keyset/cursor
progress, leases and fence tokens, and accounts every unit as committed,
existing, excluded, quarantined, conflict, retryable, or permanently failed.
Checkpoint advancement and domain writes occur transactionally where they share
PostgreSQL, and redelivery is idempotent.

Immutable observations retain both the source event/effective timestamp and the
time HyperP or Deal Intelligence could first know the value. Dataset features at
`as_of_at` may include only evidence with an availability timestamp at or before
that cutoff. Later stage, close date, amount, assignment, contact relationship,
activity, correction, and identity-link changes cannot alter an earlier
snapshot. Reopen/revert, censoring, eligibility, mapping, and label behavior
remain governed by the accepted versioned #123/#125 contracts.

Identity linkage is orthogonal to CRM terminal accounting. An unresolved link
does not discard the CRM record or make a source page retry forever. Analytical
eligibility and Person-linked projection eligibility are recorded separately.

## Deal Intelligence data boundary

The PostgreSQL service stores source references needed for operations and
analytics:

```text
bitrix_contact_refs
bitrix_lead_refs
bitrix_company_refs
bitrix_deal_contacts
bitrix_deal_leads
bitrix_deal_companies
```

Those records may contain portal/source IDs, deal relationship roles, primary
flags, observation versions, operational timestamps, policy/suppression
metadata, and closed HyperP link status/revision. They do not constitute an
independent identity profile.

Raw CRM channel groups remain in the immutable HyperP source record for audit,
replay hashing, and retention handling. They are excluded from normal Deal
Intelligence operational tables, feature datasets, API responses, logs, model
metadata, and explanations. Any restricted legacy archive requires its own
retention and access decision.

## Artifact and analytics contract

The migration preserves the accepted issue #125 formats and versions:

- deterministic SQLite v1 datasets;
- canonical safe JSON rules/model artifacts;
- existing feature, dataset, selector, mapping, eligibility, label, evaluation,
  and model version constants; and
- authenticated restricted-artifact manifests, checksums, provenance, atomic
  publication, backup verification, and retention metadata.

Production must not deserialize pickle or joblib. Parquet is a possible future
v2 dataset format, not an implicit migration requirement. A future format must
use a new schema/version and demonstrate deterministic conversion and reader
compatibility; it cannot silently reinterpret SQLite v1.

Artifact migration inventories every deployed environment and separately
classifies ephemeral `/tmp` outputs. It copies before cleanup, verifies source
and destination checksums, registers metadata in PostgreSQL, load-tests every
accepted dataset/model, records degenerate or unloadable models as rejected, and
keeps legacy roots read-only through the rollback window. Cleanup is a later,
separately approved operation.

Every dataset manifest records the fixed HyperP identity snapshot revision used
for its Person links. Identity corrections create a new dataset/version; they do
not mutate an accepted artifact.

## Historical migration contract

Historical migration must preserve operational history even when identity is
pending or rejected. It imports active, pending-review, rejected, and
superseded deal versions; activity/call history; stage occurrences; conflict and
authority lineage; corrections; terminal accounting; checkpoints; and accepted
analytical releases.

When a parent CRM deal is pending review, its activities and stage occurrences
remain operationally present while its Person link is `pending_review` or
`unresolved`. They cannot contribute to Person-linked metrics until HyperP emits
a later resolved revision.

Existing `crm_deal -> Person` links cannot seed authority. They are
comparison-only and must be classified as one of:

```text
same_owner
changed_owner
ambiguous
blocked
unresolved
contaminated_legacy_link
```

### Deal policy migration lineage

Every exported deal version records a classification, without inferring a
policy version from timestamps when persisted provenance exists:

```text
pre_policy
policy_v2_active
policy_v2_pending_review
policy_v2_rejected
policy_v2_quarantined
policy_v2_adjudicated
```

The migration retains policy version, source-record lifecycle/version/hash,
match decision ID, review case/resolution where applicable, selected Person
where resolved, prior owners, automatic-versus-reviewed provenance, suppression
metadata, and ambiguity/ownership-change reason codes.

The migration also preserves the complete accepted stage contract: occurrence
identity, raw observation hash, conflict group and variants, parent-resolution
decision, append-only authority decision, correction/supersession, release,
mapping, policy, invalidation, and terminal accounting. An accepted analytical
release must be reproducible from imported lineage without consulting the old
Neo4j analytical readers.

## Operational acceptance gates

PR #249 satisfies the preventative code gate for new CRM deal ingestion. It
does not satisfy the remediation gate for historical contamination.

Before authoritative identity seeding, projection-read switch, source cutover,
or destructive cleanup, the remediation operation must:

1. deploy the policy-v2 guardrail build to the authoritative environment;
2. inventory all pre-policy and policy-v2 deal versions and active links;
3. repair invalid deal-to-Person relationships idempotently;
4. retire identifiers and facts introduced only by contaminated deals while
   preserving independent evidence;
5. rebuild affected golden profiles and recompute CRM counts/projections;
6. reconcile review cases, prior owners, and potentially contaminated locks;
7. resolve the stale staging run `e5deb1d6-7333-4660-be4f-c44fcf5af686` to an
   explicit terminal state;
8. run a bounded representative policy-v2 replay;
9. show zero unsupported multi-linked active CRM deals;
10. show no deal-origin phone/email Person projections; and
11. produce a human-accepted clean boundary containing source hash, policy
    version, graph revision, execution provenance, and zero unexplained
    remainder.

The cutover additionally requires recurring standalone contact/lead coverage,
company references that cannot create Persons, a clean fixed-revision identity
snapshot, snapshot-plus-tail convergence in Deal Intelligence, a final #221
activity-backfill boundary, an explicit #183 scope decision, API/projection
parity, and successful single-writer/rollback exercises.

Issue #221 must record a fixed final activity boundary with balanced terminal
coverage; a merged defect fix or partially started backfill is not acceptance.
Issue #183 must be retargeted to the PostgreSQL destination or explicitly
bounded to migration/rollback evidence so that it cannot create a competing
Neo4j analytical owner. Issues #123 and #125 must name Deal Intelligence as the
owner of dataset generation, training, evaluation, shadow scoring, and
prediction history while preserving their accepted label, dataset, and safe
artifact contracts.

## Ownership transfer and recovery

Source transfer uses a signed ownership manifest and a monotonically increasing
ownership epoch. Writers first prepare against an epoch, then commit only if
the epoch remains current. Queued tasks re-check the epoch immediately before
mutation. Manual/backfill paths fail closed if ownership is not explicit.

There must be one checkpoint owner per stream. The old writer cannot emit after
transfer; the new writer cannot emit before activation. A partial handoff enters
a writerless failure state rather than allowing dual writers. Rollback creates a
higher epoch and requires a reconciliation checkpoint; it never decrements an
epoch.

The PostgreSQL service uses a transactional projection outbox. HyperP applies
minimal projections through repository-mediated Neo4j writes and supports
dead-letter handling, idempotent replay, tombstones, and full rebuild.

The minimal projection carries accepted Person UUID, deal/current-stage and
lifecycle state, approved CRM counts/recency aggregates, source revision, and
freshness. Unresolved, pending, blocked, rejected, or retired links do not
contribute Person metrics. Existing Person list filters/sorting, Person detail
CRM sections, and CRM metrics must pass parity tests at a fixed boundary.
Staleness and source outages are visible states, never silently presented as
fresh empty data.

## Backup and rollback contract

PostgreSQL backups and associated restricted artifacts are a paired backup set
identified by `backup_set_id`. Each set has a custom encrypted `pg_dump`,
artifact checksums, and restore verification. "Custom encrypted `pg_dump`"
means a PostgreSQL custom-format dump encrypted after creation; PostgreSQL's
custom format alone is not encryption. Encryption keys are managed outside the
backup set and are never stored in manifests or repository configuration.
Retain fourteen daily and eight weekly sets. The operational objective is an RPO
of 24 hours and an RTO of four hours.

## Delivery sequence

### PR 250.1 - Contracts and policy

Land this contract, the ownership matrix, identity namespaces, event/snapshot
schemas, review dispositions, #246 acceptance boundary, related-issue ownership
decisions, cutover epoch, backup/rollback contract, and deployment policy.

Exit: every existing CRM identity/review object has an owner, destination, or
retention decision, and every operational gate is measurable.

### PR 250.2 - Standalone HyperP CRM identity

Implement independent contact, lead, and company records; portal-scoped IDs;
many-to-many tenant associations; company non-Person behavior; recurring
bounded synchronization; and connector/pipeline tests. Do not disable deal
ingestion or alter `crm_deal_identity_v2` hashes/semantics.

This slice also registers the immutable Bitrix source instance, makes CRM
identifier normalization/matching instance-aware, migrates existing CRM
identifier evidence into that scope, and fails closed if a portal is unregistered
or conflicts with another instance.

Exit: contact/lead records ingest without a deal, companies cannot create a
Person, and the new records do not write deal/activity/stage history.

### PR 250.3 - Issue #246 repair

Provide a read-only remediation plan first and a separately approved,
idempotent write execution. Repair the full pre-v2 contamination closure,
including affected facts, profiles, later match decisions/pair audits, reviews,
locks, counts, projections, and the stale staging run.

Exit: representative replay and signed clean-boundary evidence satisfy all
remediation gates with zero unexplained remainder.

### PR 250.4 - HyperP identity-link revision stream

Add ordered lifecycle events, bounded OAuth reads, fixed-revision snapshots,
snapshot-plus-tail recovery, idempotency/gap tests, and the documented MCP
exclusion. No raw identifiers or candidate topology leave HyperP.

Exit: activation, rejection, supersession, merge, unmerge, and retirement
produce convergent higher revisions.

### Issue #315 - Disabled Deal Intelligence foundation

Create the installable package, PostgreSQL/Alembic shared-control-plane foundation,
typed process entry points, configuration, and default-off controls. Add PostgreSQL
16 validation as a separate step/service in synchronized PR and MAIN workflows.

This issue adds the disabled Compose topology and shared Deal Intelligence app
image for the foundation. It does not change nginx or deployment workflows and
owns no writers, schedules, source credentials, or live data movement.

Exit: package-owned opt-in PostgreSQL tests exercise fresh and base-to-head
migrations, schema inventory, and internal structured disabled readiness while all
writers and schedules remain disabled.

### PR 250.6 - Deal and complete-stage synchronization

Implement bounded direct Bitrix reads, deal current/version state, reference
edges, complete stage occurrence/conflict/authority/correction/release lineage,
leases/fencing, terminal accounting, and fixed-boundary reconciliation. Do not
implement Person matching.

Exit: disabled or explicitly bounded shadow runs reconcile without Neo4j bulk
analytical writes.

### PR 250.7 - Activity synchronization

Implement activity versions, participants, assignments, call metadata,
approved communication metadata, retries/quarantine, point-in-time timestamps,
and fixed-boundary reconciliation. Retarget #221/#183 execution to this
destination. Exclude raw bodies by default.

Exit: every bounded unit reaches an explicit terminal disposition and future
activity cannot alter an earlier snapshot.

### PR 250.8 - Historical Neo4j migration

Export/import operational versions and terminal states, accepted stage lineage,
checkpoints/releases, and only identity links qualified at the #246 clean
boundary. Pending review history remains present but Person-unlinked.

Exit: every version and terminal state balances, the accepted release is
regenerable, and links match one fixed HyperP snapshot revision.

### PR 250.9 - Artifact and analytics migration

Extract/reuse the restricted-artifact library, migrate SQLite v1 and safe JSON
artifacts, inventory ephemeral files, register rejected artifacts explicitly,
and port dataset/evaluation/training/model metadata to PostgreSQL repositories.

Exit: accepted artifacts checksum/load-test successfully and reproduce material
metrics without Neo4j analytical repositories.

### PR 250.10 - CRM projection outbox and API parity

Implement the PostgreSQL transactional outbox and repository-mediated minimal
Neo4j projection, including tombstones, freshness, dead-letter/replay controls,
and full rebuild. Preserve stable operation IDs and API/MCP parity.

Exit: fixed-boundary UI/API metrics and filters match, stale behavior is tested,
and a full projection rebuild converges.

### PR 250.11 - Ownership cutover controls

Implement signed manifests, prepare/commit epoch transfer, old/new writer
fencing, queued-task rechecks, fail-closed manual/backfill paths, negative write
probes, rollback/recovery commands, and the writerless failure state.

Exit: exactly one checkpoint owner exists and rollback always advances the
epoch.

### Operational migration and later cleanup

Live migration, shadow comparison, source ownership activation, and rollback
rehearsal require separate approval. Only after acceptance and the rollback
window may a later cleanup remove old writers/readers, legacy mounts, obsolete
controls, or Neo4j analytical history.

## Acceptance tests

The implementation must cover at least:

- policy-version immutability and namespace non-collision;
- contact/lead standalone ingestion without a deal;
- a company record that cannot create or merge a Person;
- no raw channel groups or candidate IDs in normal PostgreSQL data;
- operational checkpoints advance during temporary HyperP identity
  unavailability;
- identity resolution updates that do not rewrite immutable CRM history;
- pending/rejected operational history present but Person-unlinked;
- repair idempotency and survival of valid independent evidence;
- review approval, rejection, merge, unmerge, and retirement event ordering;
- stale/out-of-order event rejection and snapshot-plus-tail convergence;
- complete stage release regeneration from lineage;
- deterministic SQLite v1 and safe JSON artifact compatibility, with no
  pickle/joblib loading;
- projection rebuild, tombstones, and stale-state behavior; and
- epoch fencing, writerless failure, and rollback to a higher epoch.
