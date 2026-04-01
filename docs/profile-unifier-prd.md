# Product Requirements Document

## Product Name

Unified Customer Profile Platform

## Executive Summary

Customer identity data is fragmented across POS, Bitrix CRM, and multiple
third-party systems. The same real-world person may appear under different
phones, emails, external IDs, or partially conflicting profile fields. The
platform will create a centralized person graph (Neo4j) that resolves those
records into a canonical profile, supports human review, and exposes a reliable
golden profile to downstream systems. Beyond identity resolution, the graph
database enables complex relationship use cases such as contact tracing and
multi-hop network analysis. The initial use case is sales.

## Problem Statement

Current customer data is distributed across systems that were not designed to
share a universal person identifier. As a result:

- the same person appears multiple times
- support and sales teams cannot see a complete customer picture
- downstream analytics are distorted by duplicates and fragmented history
- source systems disagree on key contact details
- operations teams resolve identity conflicts manually and inconsistently

## Product Vision

Create a durable identity foundation that becomes the system of record for
customer identity linkage and relationship intelligence, while allowing source
systems to continue owning their original records. The graph-native storage
(Neo4j) enables future use cases such as contact tracing, referral network
analysis, and multi-hop relationship queries.

## Product Goals

- resolve fragmented source records into canonical persons
- support deterministic and probabilistic matching
- provide a trusted golden profile for downstream use
- minimize false merges
- make every decision explainable and reversible
- establish a safe path to evaluate heuristic and LLM-based matching

## Non-Goals

- replacing all CRMs or POS systems
- becoming the full customer engagement platform
- supporting unrestricted autonomous LLM merging in MVP
- solving family, household, or company-account graphing in MVP (planned for later phases)

## Primary Users

### Sales Teams

Need deduplicated leads, a complete view of a prospect's history across
systems, and visibility into relationship networks (referrals, shared
accounts, overlapping contacts) to prioritize outreach and avoid duplicate
pitches.

### Customer Support and Operations

Need a reliable view of a customer's linked records and contact details.

### CRM Administrators

Need better deduplication and profile quality across operational tools.

### Data Operations Reviewers

Need a queue for ambiguous matches with enough evidence to make safe decisions.

### Downstream System Consumers

Need a canonical person ID and golden profile API for integrations.

### Product and Data Owners

Need clear metrics on match quality, duplicate reduction, and review workload.

## User Problems to Solve

- sales reps waste time pursuing duplicate leads or prospects already owned by another rep
- sales cannot see a prospect's full interaction history across POS, CRM, and third-party apps
- sales has no visibility into relationship networks (who referred whom, shared contacts)
- agents cannot confidently identify a person across systems
- duplicate records create inconsistent support outcomes
- identifiers such as phone or email are reused, stale, or partially missing
- there is no central audit trail for why records were merged
- there is no safe mechanism to test LLM-based identity adjudication

## Target Outcomes

- a single canonical person ID is available for resolved identities
- linked source records are visible from one place
- ambiguous cases are queued rather than guessed
- false merges are rare and recoverable
- downstream teams can rely on the golden profile API
- sales teams have a deduplicated prospect view from day one
- the graph structure supports future relationship and contact-tracing queries
  without re-architecture

## Core User Stories

### Sales (MVP)

- As a sales rep, I can search for a prospect and see a deduplicated profile
  with full contact details and linked source records across all systems.
- As a sales rep, I can see which other persons share identifiers with my
  prospect (e.g. same phone, same email) to understand potential relationships.
- As a sales manager, I can trust that two reps are not independently pursuing
  the same person under different source IDs.

### Support and Operations

- As an agent, I can search a person by phone, email, or external ID and see a
  unified view of linked records.

### Review and Admin

- As a reviewer, I can approve or reject a suggested merge with clear evidence.
- As an admin, I can unmerge an incorrect match and restore history.

### Integration and Analytics

- As an integration consumer, I can fetch a golden profile and linked source
  records by canonical person ID.
- As a product owner, I can compare heuristic and LLM matching performance on
  ambiguous cases.

### Relationship Intelligence (Post-MVP)

- As a sales rep, I can view the relationship network around a person — who
  referred them, who they share contacts or identifiers with, and how they are
  connected to other known persons.
- As an ops analyst, I can trace the contact chain from one person to another
  through shared identifiers, interactions, or explicit relationships.
- As a compliance officer, I can query the N-degree contact graph around a
  flagged person for risk assessment.

## Scope

### In Scope

- multi-source record ingestion
- identifier normalization and validation
- deterministic matching
- heuristic probabilistic matching
- optional LLM adjudication path for controlled experiments
- manual review queue
- merge and unmerge audit trail
- golden profile generation
- profile search and retrieval APIs
- benchmarking and shadow evaluation of match engines

### Out of Scope

- immediate write-back to every upstream system
- household and family graph resolution in MVP
- full customer 360 marketing workflows
- recommendation engines
- unrestricted real-time LLM-based autonomous merging

## Functional Requirements

### Ingestion and Normalization

- The system must ingest records from multiple source systems.
- The system must support historical backfills and incremental syncs.
- The system must normalize identifiers and preserve raw input data.
- The system must be idempotent across retries and duplicate deliveries.
- The system must attach source-specific quality indicators to normalized data.

### Identity Resolution

- The system must link source records to canonical persons.
- The system must support deterministic rules that run before probabilistic
  matching.
- The system must support probabilistic match flows using heuristic scoring.
- The system must support an optional LLM path behind the same decision
  contract.
- The system must support explicit no-match locks to prevent repeated bad
  suggestions.

### Golden Profile

- The system must compute a golden profile using survivorship rules.
- The system must retain alternate phones, emails, and addresses rather than
  only the winning value.
- The system must track provenance for each preferred profile field.

### Review and Operations

- The system must provide a manual review queue.
- The system must record reasons for all merge decisions.
- The system must support manual merge, reject, defer, and unmerge actions.
- The system must support field-level and person-level audit history.
- The system must expose review priority and SLA metadata.

### APIs and Integrations

- The system must expose APIs for search and profile retrieval.
- The system must expose linked source records and audit history.
- The system must provide a stable canonical person ID to consumers.
- The system must support side-by-side evaluation of heuristic and LLM engines.

## Non-Functional Requirements

- strong PII protection
- full auditability
- explainable decisions
- reversible merges
- idempotent ingestion
- scalable candidate generation
- operational observability
- controlled access to sensitive identifiers

## Data and Compliance Requirements

- NRIC and Singpass-linked identifiers must be handled as highly sensitive data.
- Sensitive identifiers must be encrypted or tokenized where possible.
- Access to sensitive fields must be role-restricted and audited.
- LLM experiments must minimize raw PII exposure and follow approved policy.
- Data retention and deletion rules must align with business and legal policy.

## Product Principles

- prioritize precision over recall for auto-merges
- preserve evidence rather than collapsing data too early
- prefer review over speculative merges
- make bad decisions reversible
- tune the system from labeled evidence, not intuition alone

## Constraints

- source systems may have weak or inconsistent identifiers
- phone numbers and emails may not be unique per person
- government identifiers require strict handling
- reviewer capacity is limited and must be managed
- some systems may only provide batch exports rather than real-time APIs

## Assumptions

- a small set of trusted identifiers exists for deterministic matches
- labeled examples can be collected for calibration
- business owners can rank source-system trust levels
- sensitive data can be handled under approved governance controls
- downstream systems can adopt canonical person IDs incrementally

## Success Metrics

### Identity Quality

- auto-merge precision above 99%
- false merge rate near zero
- duplicate rate reduced by target percentage after rollout
- manual-review acceptance rate tracked by confidence band

### Operational Efficiency

- review queue volume within operating capacity
- reviewer turnaround time under target SLA
- median person lookup latency under target SLA
- ingestion pipeline success rate above target

### Experimentation

- heuristic and LLM outputs are benchmarked on the same labeled dataset
- shadow evaluation is available before any LLM promotion
- model or ruleset regressions are detectable through versioned metrics

## Acceptance Criteria for MVP

- first selected source systems ingest successfully
- deterministic and heuristic logic meet benchmark precision targets
- manual review and unmerge workflows are operational
- golden profile API returns canonical person and source links
- sales teams can search, view deduplicated profiles, and see shared-identifier
  connections between persons
- audit trail exists for every merge and review action
- security controls for sensitive identifiers are in place

## Release Strategy

### MVP

- deterministic matching
- heuristic scoring
- review queue
- golden profile API
- audit trail and unmerge

### Post-MVP

- LLM shadow evaluation
- LLM reviewer assist
- contact tracing and relationship graph queries
- operational dashboards
- optional upstream write-back
- source trust and rule tuning workflows

## Dependencies

- source-system data contracts
- engineering ownership for ingestion and APIs
- review operations ownership
- legal or compliance guidance for sensitive identifiers
- benchmark labeling process

## Risks

- false merges causing high operational impact
- privacy exposure during LLM testing
- poor data quality from third-party systems
- drift in source formats and identifier quality
- insufficient reviewer capacity for ambiguous cases
- downstream misuse of canonical IDs without lineage awareness

## Risk Mitigations

- keep hard conflict rules outside the probabilistic engine
- require review for uncertain cases
- maintain explicit unmerge and replay capability
- instrument source drift and review backlog
- keep LLM initially in shadow or assist-only mode

## Operating Model

### Product Owner

Owns scope, success metrics, and rollout priorities.

### Platform Engineering

Owns ingestion, APIs, storage, and auditability.

### Data or Identity Steward

Owns threshold tuning, source trust settings, and review policy.

### Review Operations

Owns ambiguous-case handling and quality feedback loops.

### Security and Compliance

Owns policy for sensitive identifiers and LLM usage boundaries.

## Proposed Default Policy Decisions

Until business or legal review overrides them, the default policy is:

- use field-level source trust ranking rather than one trust level per system
- allow deterministic auto-merge only for verified government ID, trusted
  migration-map, or explicit manual merge cases
- treat conflicting verified government IDs and manual no-match locks as hard
  blockers
- retain raw payloads for 12 months by default, with longer retention for
  decision and audit history
- keep the LLM in assist-only and review-band mode during MVP

Detailed defaults are defined in
[profile-unifier-policy-decisions.md](./profile-unifier-policy-decisions.md).

## Open Decisions

- whether the first reviewer tool is an internal UI or ops-driven workflow
- the exact legal-hold and deletion workflows for each deployment environment
- whether manual no-match locks should expire by default or persist indefinitely
- whether high-value profile policies require custom review routing

## Launch Criteria

- operational owners are assigned
- benchmark targets are met
- review workflow is staffed and working
- rollback procedures are tested
- downstream consumers can retrieve canonical person profiles safely
