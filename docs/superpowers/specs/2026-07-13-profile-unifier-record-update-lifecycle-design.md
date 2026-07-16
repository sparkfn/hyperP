# Profile Unifier Record Update Lifecycle Design

Date: 2026-07-13

## Purpose

Make changed upstream records safe and consistent across every ingestion path.
Source facts remain immutable, while only an accepted record version contributes
to active graph projections and golden profiles.

The design covers identity, conversation, address, sales, relationship, and
bankruptcy records.

## Existing Correctness Gaps

The current implementation has record-type-specific update behavior:

- Main identity ingestion re-evaluates a changed record without preferring its
  previously linked Person.
- It immediately supersedes the old version and retires its identity evidence,
  including when the replacement requires review.
- A reassignment does not reliably recompute the Person losing evidence.
- Address ingestion skips exact duplicates but does not consistently allocate
  versions or supersede changed records.
- Sales ingestion removes old purchase and vehicle links before confirming that
  the replacement can resolve its customer.
- Relationship materialization can scan historical records and does not provide
  a consistent retirement operation for superseded assertions.
- Bankruptcy and conversation-derived vehicle projections do not share a
  consistent supersession lifecycle.
- Sales customer resolution does not deliberately select the active accepted
  identity record.
- Latest-version reads and version allocation are not protected by the same
  write transaction, allowing concurrent writers to produce competing state.

## Decisions

1. A changed record prefers continuity with its previously linked Person, but
   continuity is evidence rather than an unconditional match.
2. Strong contradictory evidence may reassign a record automatically. Weaker
   evidence creates a review case.
3. A pending replacement does not retire the accepted version or alter active
   projections.
4. Every ingestion path uses one lifecycle coordinator.
5. Projection activation, prior-projection retirement, lifecycle transitions,
   and golden-profile recomputation are atomic.
6. Only one unresolved replacement may exist for a source identity. A newer,
   different replacement rejects the older pending candidate with reason
   `rejected_by_newer_version`; the accepted version remains active.

## Architecture

Introduce a `RecordLifecycleCoordinator` shared by all ingestion paths:

```text
Incoming envelope
       |
       v
Lock source identity and load accepted/pending versions
       |
       +-- known hash ----------------------> return existing result
       |
       v
Create immutable staged version
       |
       v
Resolve using prior Person as continuity evidence
       |
       +-- accepted --> activate replacement atomically
       |                retire prior projections
       |                recompute affected Persons
       |
       +-- review ----> keep prior version active
                        retain replacement as pending
```

The coordinator owns:

- idempotency and immutable version creation;
- source-identity locking and version allocation;
- lifecycle transitions;
- continuity context;
- activation and retirement orchestration;
- the complete set of Persons affected by a transition.

Identity matching remains responsible for deciding whether evidence supports
the prior Person, another Person, multiple Persons, or review. Specialized
projectors remain responsible for record-type-specific graph structures.

## Source Identity and Version Model

A source identity is the tuple `(source_system, source_record_id)`. A source
version is uniquely identified by
`(source_system, source_record_id, source_record_version)`.

Each `SourceRecord` has one explicit `lifecycle_status`:

- `active`: accepted and currently contributing projections;
- `pending_review`: immutable candidate replacement with no active projections;
- `superseded`: previously active and retained for audit;
- `rejected`: reviewed replacement that must not contribute projections;
- `link_failed`: malformed or permanently unresolved specialized record.

The model enforces at most one `active` version for each source identity.
Queries must select `lifecycle_status = 'active'` deliberately. The existing
`is_latest` property must not remain the authority because "latest received"
and "latest accepted" are different concepts.

Versions form a linear history through an explicit relationship such as
`PREVIOUS_VERSION_OF`. A staged version also records continuity with the prior
Person so the decision remains explainable. Historical `LINKED_TO` and
provenance edges remain available for audit, but inactive versions do not
contribute to matching, active domain reads, or golden profiles.

## Lifecycle Flow

### Duplicate

Under the source-identity lock, compare the incoming hash with active and
pending versions. If the hash exists, return the existing result without
creating another version, decision, projection, or review case.

### Accepted update

1. Create the immutable replacement version.
2. Resolve it with the prior accepted Person included as continuity evidence.
3. Build the replacement projections.
4. Retire projections sourced by the prior active version.
5. Mark the prior version `superseded` and the replacement `active`.
6. Recompute every Person that gained or lost evidence.

Steps 3 through 6 occur in one Neo4j write transaction. Any failure rolls back
the complete transition.

### Review update

Create the replacement as `pending_review` and persist its decision and review
case. Do not activate its projections, retire the prior version, or recompute a
golden profile.

Review outcomes are:

- approve same Person: activate the replacement and recompute that Person;
- approve reassignment: activate against the new Person and recompute both the
  previous and new Persons;
- reject: mark the replacement `rejected` and leave the active version intact;
- defer: leave the replacement `pending_review` without active side effects.

### Newer update while review is pending

Only one unresolved candidate is allowed. A different newer payload creates the
next version and marks the older pending candidate `rejected` with reason
`rejected_by_newer_version`. An identical payload returns the existing pending
result. The accepted version remains active until a candidate is accepted.

## Matching Continuity

For an update, the matching engine receives the prior accepted Person as a
distinguished candidate. Ordinary matching evidence should preserve continuity.
Automatic reassignment requires stronger evidence than initial attachment,
including no hard sensitive-identifier conflict with the proposed destination.
If that stronger threshold is not met, the update enters review.

The exact numeric reassignment threshold remains an implementation-policy
constant tested independently from the lifecycle coordinator. The required
behavior is deterministic: continuity wins below the reassignment threshold,
and ambiguous or conflicting cases never silently move evidence between Persons.

## Projector Contract

Every record-type projector implements two logical operations:

```text
activate(new_version)
retire(previous_version)
```

These operations execute within the coordinator's activation transaction. They
must affect only graph structures carrying the relevant `source_record_pk`.
Retirement must be idempotent so transaction retries are safe.

### Identity and conversation

Activation writes accepted identifiers, addresses, facts, bankruptcy
materialization where applicable, and vehicle mentions. Retirement deactivates
only evidence sourced from the superseded record. Pending conversation evidence
does not affect matching or the golden profile.

### Address

Address inventory records use the same version allocation and lifecycle states.
Activation creates the current source-to-address assertion; retirement
deactivates the prior assertion without deleting the shared Address node.

### Sales

The prior purchase remains active while a replacement is unresolved. Activation
updates the durable Order projection, removes line relationships absent from the
accepted replacement, and replaces purchase and vehicle evidence atomically.
Customer resolution selects the active accepted identity version.

### Relationship

Relationship materialization scans active source records only. Activation
creates or refreshes the sourced `KNOWS` assertion. Retirement deactivates the
assertion whose `source_record_pk` belongs to the superseded version.

### Bankruptcy

Immutable versions remain available for audit. Only the accepted version updates
the durable bankruptcy case projection and its active Person association.

### Multi-person evidence

An accepted record may project evidence to multiple Persons under the existing
multi-match policy without merging those Persons. The coordinator records and
recomputes the full set of Persons gaining or losing evidence.

## Concurrency and Atomicity

The coordinator acquires a Neo4j lock scoped to the source identity before it
reads current state or allocates a version. It re-reads active and pending
versions inside the write transaction.

Activation uses a compare-and-transition condition: it succeeds only if the
expected prior version remains active. Retriable transaction conflicts retry the
whole operation. Invalid payloads become explicit `rejected` or `link_failed`
results and must not leave partial projections.

Database constraints and transaction logic enforce:

- unique source-version tuples;
- a linear version chain;
- no duplicate decision or review case for a known hash;
- at most one accepted active version per source identity.

Because Neo4j cannot express every conditional uniqueness rule as a schema
constraint, the source-identity lock and compare-and-transition write are part
of the invariant, not optional optimizations.

## API and Query Compatibility

Reads must distinguish active, pending, and historical source versions.
Person-facing and matching queries use active versions only. Audit and review
queries may expose the full version chain and lifecycle state.

During migration, existing records with `is_latest = true` become `active` and
older records become `superseded`. Records already carrying unresolved review
state require a migration rule that preserves the last accepted version as
active and marks the reviewed replacement `pending_review`.

The API contract should expose `lifecycle_status` wherever callers currently
need to interpret `is_latest` or `link_status`. `link_status` may remain for
domain linking progress, such as sales customer resolution, but it must not be
used as the record-version lifecycle state.

## Testing

Shared lifecycle contract tests run against every projector:

- identical hashes create no new state;
- accepted changes create one version and leave exactly one active version;
- review changes preserve prior evidence and golden-profile values;
- rejected changes leave the prior version authoritative;
- approved reassignment recomputes both affected Persons;
- concurrent changes produce a linear chain with unique version numbers;
- projector failures roll back lifecycle and domain changes.

Specialized integration tests verify:

- address changes retire the prior assertion;
- sales replacements preserve purchases until activation and remove deleted
  line relationships afterward;
- relationship materialization ignores superseded records;
- bankruptcy projections use only accepted versions;
- superseded conversations stop contributing vehicle mentions;
- sales customer resolution selects the active accepted identity record;
- multi-person activation recomputes every affected profile.

## Acceptance Criteria

- Every ingestion path uses the lifecycle coordinator.
- Historical source facts remain immutable and auditable.
- Exactly one accepted active version exists per source identity.
- Pending updates do not alter active projections or golden profiles.
- Accepted activation leaves no stale projections from the superseded version.
- Reassignment prefers continuity and requires stronger evidence.
- All affected Persons are recomputed after activation.
- Concurrent ingestion cannot create competing active versions.
- API and graph reads explicitly select the lifecycle states they require.

## Out of Scope

- Replacing the graph model with full event sourcing.
- Changing existing identity confidence bands unrelated to reassignment.
- Merging Persons as a side effect of a record update.
- Deleting historical SourceRecord nodes or provenance.
