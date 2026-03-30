# Profile Unifier Sequence Diagrams

## Purpose

Illustrate the main runtime flows of the profile unifier platform so the
relationship between ingestion, matching, review, and audit side effects is
clear across teams.

## Diagram Conventions

- diagrams use Mermaid sequence syntax
- `PU API` represents the application API layer
- `Resolver` represents deterministic and probabilistic matching logic
- `Store` represents the OLTP persistence layer
- `Reviewer UI` represents the internal review tool

## Flow 1: Source Record Ingestion and Candidate Evaluation

```mermaid
sequenceDiagram
    participant Source as Source System
    participant API as PU API
    participant Norm as Normalizer
    participant Resolver as Resolver
    participant Store as Person Graph Store

    Source->>API: POST /v1/ingest/{source}/records
    API->>Store: Persist source_record and ingest metadata
    API->>Norm: Normalize identifiers and attributes
    Norm-->>API: Normalized payload + quality flags
    API->>Resolver: Generate candidates and evaluate hard rules
    Resolver->>Store: Read identifiers, facts, locks, and trust config
    Resolver-->>API: Decision = merge | review | no_match
    API->>Store: Persist match_decision

    alt decision = merge
        API->>Store: Link source_record to person
        API->>Store: Persist identifiers and attribute facts
        API->>Store: Recompute golden_profile
    else decision = review
        API->>Store: Create review_case
        API->>Store: Mark source_record link_status = pending_review
    else decision = no_match
        API->>Store: Create new person or preserve separation
        API->>Store: Persist identifiers and attribute facts
    end

    API-->>Source: Accepted response
```

## Flow 2: Deterministic or High-Confidence Auto-Merge

```mermaid
sequenceDiagram
    participant API as PU API
    participant Resolver as Resolver
    participant Store as Person Graph Store
    participant Events as Domain Events

    API->>Resolver: Evaluate candidate pair
    Resolver->>Store: Read candidate evidence
    Resolver-->>API: Decision = merge
    API->>Store: Persist match_decision
    API->>Store: Create merge_event
    API->>Store: Relink source_records to target person
    API->>Store: Update absorbed person status if needed
    API->>Store: Recompute affected golden_profile rows
    API->>Events: Emit merge-related downstream event
```

## Flow 3: Review Case Merge

```mermaid
sequenceDiagram
    participant Reviewer as Reviewer UI
    participant API as PU API
    participant Store as Person Graph Store
    participant Events as Domain Events

    Reviewer->>API: POST /v1/review-cases/{id}/assign
    API->>Store: Update review_case queue_state = assigned
    API->>Store: Create review_action(assign)

    Reviewer->>API: POST /v1/review-cases/{id}/actions { action_type: merge }
    API->>Store: Verify no hard blockers and no active no-match lock
    API->>Store: Create review_action(merge)
    API->>Store: Create merge_event(manual_merge or reviewer-approved merge)
    API->>Store: Relink affected source_records and persons
    API->>Store: Resolve review_case with resolution = merge
    API->>Store: Recompute golden_profile for affected persons
    API->>Events: Emit merge event
    API-->>Reviewer: Merge completed
```

## Flow 4: Review Case Manual No-Match

```mermaid
sequenceDiagram
    participant Reviewer as Reviewer UI
    participant API as PU API
    participant Store as Person Graph Store

    Reviewer->>API: POST /v1/review-cases/{id}/actions { action_type: manual_no_match }
    API->>Store: Create review_action(manual_no_match)
    API->>Store: Create person_pair_lock
    API->>Store: Create merge_event(manual_no_match)
    API->>Store: Resolve review_case with resolution = manual_no_match
    API-->>Reviewer: Lock created and case resolved
```

## Flow 5: Review Case Defer and Reopen

```mermaid
sequenceDiagram
    participant Reviewer as Reviewer UI
    participant API as PU API
    participant Store as Person Graph Store
    participant Scheduler as Job or Trigger

    Reviewer->>API: POST /v1/review-cases/{id}/actions { action_type: defer }
    API->>Store: Create review_action(defer)
    API->>Store: Set queue_state = deferred
    API->>Store: Set follow_up_at if provided
    API-->>Reviewer: Case deferred

    Scheduler->>Store: Find deferred cases ready for follow-up
    Scheduler->>Store: Reopen or reassign case
```

## Flow 6: Admin Unmerge

```mermaid
sequenceDiagram
    participant Admin as Admin UI
    participant API as PU API
    participant Store as Person Graph Store
    participant Events as Domain Events

    Admin->>API: POST /v1/persons/unmerge
    API->>Store: Validate referenced merge_event and current lineage
    API->>Store: Create merge_event(unmerge)
    API->>Store: Restore or create affected person rows
    API->>Store: Relink source_records, identifiers, and facts
    API->>Store: Recompute affected golden_profile rows
    API->>Events: Emit unmerge event
    API-->>Admin: Unmerge completed
```

## Flow 7: Assignment Concurrency Conflict

```mermaid
sequenceDiagram
    participant ReviewerA as Reviewer A
    participant ReviewerB as Reviewer B
    participant API as PU API
    participant Store as Person Graph Store

    ReviewerA->>API: POST /v1/review-cases/{id}/assign
    API->>Store: Assign case to Reviewer A
    API-->>ReviewerA: Success

    ReviewerB->>API: POST /v1/review-cases/{id}/assign
    API->>Store: Check current version or updated_at
    Store-->>API: Ownership changed
    API-->>ReviewerB: 409 Conflict
```

## Flow 8: Review Merge Blocked by New Lock

```mermaid
sequenceDiagram
    participant Reviewer as Reviewer UI
    participant API as PU API
    participant Store as Person Graph Store

    Reviewer->>API: POST /v1/review-cases/{id}/actions { action_type: merge }
    API->>Store: Re-read locks and hard blockers
    Store-->>API: Active manual_no_match lock exists
    API-->>Reviewer: 409 merge_blocked
```

## Recommended Reading Order

For implementation discussions, read these together:

1. [profile-unifier-architecture.md](./profile-unifier-architecture.md)
2. [profile-unifier-sql-schema.md](./profile-unifier-sql-schema.md)
3. [profile-unifier-api-spec.md](./profile-unifier-api-spec.md)
4. [profile-unifier-reviewer-workflow.md](./profile-unifier-reviewer-workflow.md)
5. this sequence-diagram document

## Recommendation

Use these diagrams to validate application boundaries and side effects before
writing migrations or service code. If implementation diverges from a diagram,
update the diagram together with the affected API, schema, or workflow doc.
