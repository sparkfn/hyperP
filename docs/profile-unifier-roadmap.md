# Product Roadmap

## Phase 0: Discovery and Data Audit

Target: 2 to 3 weeks

- inventory all source systems
- document identifier fields and data quality
- rank source trust by field
- define sensitive-field handling rules
- collect labeled examples of true matches and non-matches

## Phase 1: Data Foundation

Target: 3 to 5 weeks

- implement canonical schema
- build raw ingestion pipeline
- build normalization library
- persist source records, identifiers, and attribute facts
- add basic search and person retrieval API

## Phase 2: Deterministic Matching

Target: 2 to 3 weeks

- implement trusted hard-match rules
- implement hard conflict rules
- link source records to person entities
- store merge decisions and audit events

## Phase 3: Heuristic Matching v1

Target: 3 to 4 weeks

- implement candidate generation and blocking
- implement feature extraction
- implement weighted scoring engine
- define thresholds for merge, review, and no-match
- benchmark against labeled dataset

## Phase 4: Golden Profile and Review Operations

Target: 2 to 4 weeks

- implement survivorship logic
- add reviewer queue and decision workflow
- support manual override and unmerge
- expose linked source record views

## Phase 5: LLM Shadow Evaluation

Target: 3 to 4 weeks

- design structured prompt contract
- redact or tokenize sensitive fields
- run the LLM on review-band cases only
- compare outputs against reviewer outcomes
- measure precision, review acceptance, latency, and cost

## Phase 6: Controlled LLM Assist

Target: 2 to 3 weeks

- use LLM to summarize ambiguous cases for reviewers
- use LLM to recommend review priority
- keep hard blockers outside the model
- maintain shadow comparison against heuristic path

## Phase 7: Operational Hardening

Target: ongoing

- add monitoring and alerting
- add source drift detection
- tune thresholds and source trust settings
- build analyst dashboard for merge quality
- prepare optional upstream synchronization

## Milestones

- M1: schema and ingestion in place
- M2: deterministic matches live
- M3: heuristic engine live with review queue
- M4: golden profile API available to consumers
- M5: LLM path evaluated in shadow mode
- M6: controlled LLM assist adopted if metrics justify it

## Exit Criteria by Stage

### Foundation Exit

- first source systems ingest successfully
- normalized identifiers are searchable
- person graph can be queried

### Matching Exit

- false merges remain below target
- reviewer queue volume is manageable
- unmerge flow is operational

### LLM Exit

- privacy controls are approved
- LLM quality exceeds or complements heuristic performance on ambiguous cases
- cost and latency are acceptable for the intended review workflow
