# HyperP

Planning workspace for a customer profile unification and relationship
intelligence platform that resolves the same real-world person across systems
such as POS, Bitrix CRM, and third-party applications. Designed to support
complex relationship use cases including contact tracing. The initial use case
is sales. Built on Neo4j for native graph traversal.

## Document Map

- [Glossary](./docs/profile-unifier-glossary.md)
- [Architecture](./docs/profile-unifier-architecture.md)
- [Matching Spec](./docs/profile-unifier-matching-spec.md)
- [Policy Decisions](./docs/profile-unifier-policy-decisions.md)
- [Graph Schema](./docs/profile-unifier-graph-schema.md)
- [API Spec](./docs/profile-unifier-api-spec.md)
- [OpenAPI 3.1](./docs/profile-unifier-openapi-3.1.yaml)
- [Reviewer Workflow](./docs/profile-unifier-reviewer-workflow.md)
- [Sequence Diagrams](./docs/profile-unifier-sequence-diagrams.md)
- [PRD](./docs/profile-unifier-prd.md)
- [Roadmap](./docs/profile-unifier-roadmap.md)
- [Graph Model Diagram](./docs/profile-unifier-graph-model-diagram.md)
- [Scaffold Architecture](./docs/profile-unifier-scaffold.md)

## Recommended Reading Order

1. PRD
2. Glossary
3. Architecture
4. Matching Spec
5. Policy Decisions
6. Graph Schema
7. Graph Model Diagram
8. API Spec
9. OpenAPI 3.1
10. Reviewer Workflow
11. Sequence Diagrams
12. Roadmap
13. Scaffold Architecture

## Current Scope

The document set covers:

- centralized identity resolution
- deterministic and probabilistic matching
- heuristic and LLM-based adjudication paths
- manual review and unmerge workflows
- golden profile generation
- contact tracing and complex relationship queries
- phased rollout planning

## Principles

- optimize for low false-merge rates
- keep source facts immutable and auditable
- make every decision explainable
- treat NRIC and Singpass-linked data as highly sensitive
- prefer controlled rollout over aggressive automation
