# Product Roadmap

## Roadmap Goals

- deliver a safe canonical identity platform in phases
- establish a measurable baseline before adding LLM complexity
- keep legal, operational, and engineering readiness aligned

## Workstreams

- source integration
- identity resolution engine
- golden profile API
- review operations
- security and compliance
- analytics and observability

## Phase 0: Discovery and Data Audit

Target: 2 to 3 weeks

### Objectives

- understand real source-system data quality
- define the initial source scope
- establish benchmark labeling

### Deliverables

- source inventory and field map
- identifier taxonomy
- trust ranking by source and field
- sample labeled dataset of matches and non-matches
- initial compliance notes for sensitive identifiers

### Exit Criteria

- at least 2 to 3 initial systems are selected
- source schemas are documented
- benchmark examples exist for major edge cases

## Phase 1: Data Foundation

Target: 3 to 5 weeks

### Objectives

- build the canonical data model
- ingest raw records reliably
- normalize identifiers consistently

### Deliverables

- canonical schema
- ingestion framework for batch and incremental loads
- normalization library
- raw source record persistence
- identifier and attribute fact persistence
- person lookup API v1

### Dependencies

- source credentials or exports
- infrastructure for storage and secrets

### Exit Criteria

- ingestion is idempotent
- normalized identifiers are searchable
- raw and normalized lineage is queryable

## Phase 2: Deterministic Matching and Basic Golden Profile

Target: 3 to 4 weeks

### Objectives

- implement safe hard rules first
- establish auditability for automated decisions
- expose a basic golden profile so stakeholders see visible output early

### Deliverables

- hard-merge rules
- hard-conflict rules
- person linkage workflow
- merge event logging
- no-match lock support
- basic golden profile computation
- basic person lookup and golden profile API
- minimal review UI for deterministic edge cases

### Exit Criteria

- deterministic merges are explainable
- hard conflicts block unsafe merges
- audit history exists for every deterministic decision
- support teams can retrieve a basic unified person view
- golden profile API returns preferred fields for resolved persons

## Phase 3: Heuristic Matching v1

Target: 3 to 4 weeks

### Objectives

- add scalable candidate generation
- implement a measurable probabilistic baseline

### Deliverables

- candidate generation and blocking service
- feature extraction library
- weighted scoring engine
- threshold configuration
- benchmark evaluation harness
- confusion-matrix reporting by source and confidence band

### Exit Criteria

- auto-merge precision meets target on labeled benchmark
- review volume is within expected operating capacity
- poor-quality identifiers are penalized correctly

## Phase 4: Full Review Operations and Unmerge

Target: 2 to 4 weeks

### Objectives

- operationalize full ambiguous-case handling
- implement unmerge and advanced review workflows

### Deliverables

- full reviewer queue and decision workflow
- merge, reject, defer, escalate, and unmerge actions
- manual no-match locks
- linked source-record timeline
- review SLA and prioritization rules
- unmerge with post-merge record flagging for review

### Exit Criteria

- reviewers can safely resolve ambiguous cases
- unmerge works without manual database fixes
- post-merge source records are flagged for review after unmerge

## Phase 5: LLM Shadow Evaluation

Target: 3 to 4 weeks

### Objectives

- test whether the LLM adds value without taking production control
- measure quality, cost, and privacy impact

### Deliverables

- structured prompt contract
- redaction or tokenization policy for model input
- shadow-run pipeline for review-band cases
- benchmark comparison against reviewer outcomes
- cost and latency report

### Guardrails

- no autonomous production merges
- no override of hard conflict rules
- model and prompt versions logged for every output

### Exit Criteria

- privacy controls are approved
- LLM performance is at least complementary to heuristic review decisions
- cost and latency fit the intended workflow

## Phase 6: Controlled LLM Assist

Target: 2 to 3 weeks

### Objectives

- reduce reviewer effort without increasing merge risk

### Deliverables

- reviewer summary generation
- recommendation and triage support
- ongoing shadow comparison against heuristic path
- rollback switch for model disablement

### Exit Criteria

- reviewer productivity improves measurably
- no increase in false-merge incidents
- model rollout can be disabled quickly if quality regresses

## Phase 7: Operational Hardening

Target: ongoing

### Objectives

- make the platform resilient and governable

### Deliverables

- monitoring and alerting
- source drift detection
- threshold and source-trust tuning workflow
- merge quality dashboard
- runbooks for incident response and rollback
- optional upstream synchronization strategy

## Milestones

- M1: schema and ingestion in place
- M2: deterministic matches live
- M3: heuristic engine live with review queue
- M4: golden profile API available to consumers
- M5: LLM path evaluated in shadow mode
- M6: controlled LLM assist adopted if metrics justify it

## Cross-Phase Dependencies

- benchmark labeling must start early and continue throughout tuning
- review operations must exist before probabilistic rollout scales
- security approval must precede any LLM testing with sensitive data
- downstream API consumers should be onboarded before broad rollout
- source trust ranking and hard-rule policy must be versioned before heuristic
  rollout

## Critical Path

1. source inventory and benchmark creation
2. ingestion and normalization
3. deterministic rules and audit
4. heuristic engine and review queue
5. golden profile API
6. LLM shadow evaluation

## Suggested Team Shape

- 1 product owner
- 1 to 2 backend or platform engineers
- 1 data engineer or analytics engineer
- 1 reviewer or identity steward
- part-time security and compliance support

## Risks by Phase

### Early Phases

- source contract instability
- underestimated data cleanup complexity

### Matching Phases

- threshold misconfiguration
- shared identifiers causing bad candidate expansion

### LLM Phases

- privacy policy friction
- weak incremental value relative to heuristic engine

### Operational Phases

- review backlog growth
- downstream misuse of low-confidence identity links

## Exit Criteria by Stage

### Foundation Exit

- first source systems ingest successfully
- normalized identifiers are searchable
- canonical person graph can be queried
- default policy decisions for trust, retention, and hard blockers are approved

### Matching Exit

- false merges remain below target
- review queue volume is manageable
- unmerge flow is operational

### Consumer Exit

- golden profile API is stable
- support teams can find persons by major identifiers
- lineage and audit data are available

### LLM Exit

- privacy controls are approved
- LLM quality exceeds or complements heuristic performance on ambiguous cases
- cost and latency are acceptable for the intended review workflow

## Post-Roadmap Backlog

- upstream write-back where justified
- household or relationship graphing
- self-service quality dashboards for source-system owners
- advanced rule simulation tooling
