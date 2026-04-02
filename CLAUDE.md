# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

HyperP is a **planning workspace** for a customer profile unification and relationship intelligence platform. It resolves the same real-world person across systems (POS, Bitrix CRM, third-party apps) and supports complex relationship use cases such as contact tracing. The initial use case is sales. The repository currently contains **only design documentation** — no implementation code exists yet.

## Repository Structure

All documents live in `docs/` and follow the naming convention `profile-unifier-*.md` (plus one `.yaml`).

**Recommended reading order**: PRD → Glossary → Architecture → Matching Spec → Policy Decisions → Graph Schema → Graph Model Diagram → API Spec → OpenAPI 3.1 → Reviewer Workflow → Sequence Diagrams → Roadmap → Scaffold Architecture

## Key Design Decisions

- **Database**: Neo4j (decided). The platform must support contact tracing and complex multi-hop relationship queries beyond simple identity resolution. Neo4j's native graph storage and Cypher query language are the right fit. ACID transactions are available in Neo4j 4+.
- **Precision over recall**: optimize for low false-merge rates; false merges have high operational cost.
- **Immutable source facts**: source records are never modified after ingestion — all changes create new records.
- **Explainable decisions**: every merge/no-match must have traceable reasons.
- **4-layer matching**: Deterministic rules → Heuristic scoring → LLM adjudication (shadow-only in MVP) → Human review.
- **Confidence bands**: ≥0.90 auto-merge, 0.60–0.89 human review, <0.60 no-match (thresholds to be calibrated).
- **Sensitive data**: NRIC and Singpass-linked data require special handling; govt IDs stored as salted hashes.
- **Controlled rollout**: LLM starts in shadow/assist mode only — no autonomous production merges in MVP.
- **Merge lineage**: merge history is stored as native graph relationships (`MERGED_INTO`) between Person nodes. Path compression via relationship rewiring guarantees max 1 hop for canonical person lookups.
- **Unmerge**: post-merge source records stay with the surviving person but are flagged for review.
- **Concurrency**: ingestion partitioned by blocking key to prevent race conditions.
- **Cardinality caps**: blocking keys with too many matches are skipped; configurable per identifier type.
- **Pair ordering**: lock relationships (`NO_MATCH_LOCK`) enforce `left.person_id < right.person_id` to prevent duplicates.
- **Golden profile recomputation**: synchronous within the merge transaction (Neo4j ACID transactions).
- **Downstream events**: polling endpoint (`GET /v1/events?since=`) for now, designed for future push migration.
- **Identifier aging**: time-based deactivation via `last_confirmed_at`, configurable per type.
- **Retention**: `retention_expires_at` property on relevant nodes (SourceRecord, MatchDecision, MergeEvent); NULL for legal holds.
- **Scoring model**: must use conditional weighting or capping, not simple additive weights.
- **Graph-native candidate generation**: candidate persons are found by traversing shared Identifier nodes in Neo4j, not by index-based blocking-key lookups. Composite blocking (DOB + name) falls back to index queries.
- **Explicit relationships** (post-MVP): typed Person-to-Person relationships (`REFERRED_BY`, `WORKS_WITH`, `FAMILY_OF`) are planned but do not affect identity resolution.
- **Interaction model** (post-MVP): Interaction nodes for contact tracing will connect to Person nodes in the same Neo4j graph.
- **Data deletion**: graph deletion requires detaching all relationships before removing a Person node. Shared Identifier nodes survive individual person deletion.

## Architecture Summary

Ingestion → Normalization → Candidate Generation (graph traversal through shared Identifier and Address nodes) → Match Engine → Person Graph (Neo4j) → Golden Profile → Review Operations → APIs

Core graph nodes: `Person` (with golden profile properties inline), `Identifier` (shared across persons — the graph backbone for contact tracing), `Address` (shared across persons — enables "who else lives here?" traversal), `SourceRecord`, `MatchDecision`, `MergeEvent`, `ReviewCase`, `SourceSystem`, `IngestRun`. Many relational concepts are modeled as relationships: `IDENTIFIED_BY`, `LIVES_AT`, `LINKED_TO`, `MERGED_INTO`, `NO_MATCH_LOCK`, `HAS_FACT`, `FOR_DECISION`.

Person statuses: `active`, `merged`, `suppressed` (no `under_review` — review state is tracked on `review_case`).

## Implementation Roadmap

- Phase 0: Source inventory, benchmark labeling
- Phase 1: Schema, ingestion framework, normalization library
- Phase 2: Deterministic matching, basic golden profile, basic review UI
- Phase 3: Heuristic matching, candidate generation, scoring engine
- Phase 4: Full review operations, unmerge
- Phase 5: LLM shadow evaluation
- Phase 6: LLM assist mode
- Phase 7: Monitoring, alerting, observability

## Working with This Repo

When editing or adding documentation:
- Follow the existing `profile-unifier-*.md` naming convention.
- Use the glossary terms consistently (Person, Source Record, Identifier, Address, Match Decision, Golden Profile, Merge Lineage, etc.).
- Keep the README document map and reading order in sync with any new files.
- Sequence diagrams use Mermaid syntax.
- The API contract is defined in both prose (`api-spec.md`) and machine-readable (`openapi-3.1.yaml`) — keep them consistent.
- The `review_action_type` enum includes both API-submitted actions and system-recorded actions — the API layer exposes only the API-submitted subset.
