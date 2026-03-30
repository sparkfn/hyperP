# Product Requirements Document

## Product Name

Unified Customer Profile Platform

## Problem Statement

Customer records are fragmented across POS, Bitrix CRM, and multiple
third-party systems. The same person may appear with different emails,
shared or reused phone numbers, inconsistent naming, and missing or stale
attributes. This causes duplicate profiles, poor service outcomes, inaccurate
analytics, and operational inefficiency.

## Product Goal

Create a centralized identity platform that resolves fragmented source records
into a canonical person profile with explainable, reversible, and auditable
merge decisions.

## Primary Users

- customer support and operations teams
- CRM administrators
- data operations reviewers
- downstream applications consuming unified profiles

## Success Metrics

- auto-merge precision above 99%
- false merge rate near zero
- duplicate rate reduced by an agreed target
- review queue rate within operating capacity
- median person lookup latency under target SLA
- reviewer turnaround time under target SLA

## User Stories

- As an agent, I can search a person by phone, email, or external ID and see a
  unified view of linked records.
- As a reviewer, I can approve or reject a suggested merge with clear evidence.
- As an admin, I can unmerge an incorrect match and restore history.
- As an integration consumer, I can fetch a golden profile and linked source
  records by canonical person ID.
- As a product owner, I can compare heuristic and LLM matching performance on
  ambiguous cases.

## In Scope

- multi-source record ingestion
- identifier normalization
- deterministic matching
- heuristic probabilistic matching
- optional LLM adjudication path for experiments
- manual review queue
- merge and unmerge audit trail
- golden profile generation
- search and profile retrieval APIs

## Out of Scope

- immediate write-back to every upstream system
- household and family graph resolution
- marketing segmentation workflows
- recommendation engines
- unrestricted real-time LLM-based autonomous merging

## Functional Requirements

- The system must ingest records from multiple source systems.
- The system must normalize identifiers and preserve raw input data.
- The system must link source records to canonical persons.
- The system must support deterministic and probabilistic match flows.
- The system must record reasons for all merge decisions.
- The system must support manual merge, reject, defer, and unmerge actions.
- The system must compute a golden profile using survivorship rules.
- The system must support configurable source trust and threshold settings.
- The system must expose APIs for search and profile retrieval.
- The system must support side-by-side evaluation of heuristic and LLM engines.

## Non-Functional Requirements

- strong PII protection
- full auditability
- idempotent ingestion
- scalable candidate generation
- explainable decisions
- reversible merges
- operational observability

## Constraints

- source systems may have weak or inconsistent identifiers
- phone numbers and emails may not be unique per person
- government identifiers require strict handling
- reviewer capacity is limited and should not become the default resolution path

## Assumptions

- a small set of trusted identifiers exists for deterministic matches
- labeled examples can be collected for calibration
- business owners can rank source-system trust levels
- sensitive data can be handled under approved governance controls

## Release Strategy

### MVP

- deterministic matching
- heuristic scoring
- review queue
- golden profile API

### Post-MVP

- LLM shadow evaluation
- LLM reviewer assist
- operational dashboards
- optional upstream write-back

## Risks

- high impact of false merges
- privacy exposure during LLM testing
- poor data quality from third-party systems
- drift in source formats and identifier quality
- insufficient review coverage for difficult cases

## Open Decisions

- which identifiers qualify as hard blockers or hard merges
- which source systems are trusted per field
- whether the LLM path may ever auto-merge or remain review-only
- whether the first reviewer tool is internal UI or ops-driven workflow

## Launch Criteria

- ingestion is stable for the first selected source systems
- deterministic and heuristic logic pass benchmark quality targets
- audit trail and unmerge flow are operational
- reviewer workflow is functioning with acceptable turnaround time
- security controls for sensitive identifiers are in place
