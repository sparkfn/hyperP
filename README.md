# HyperP

Planning workspace for a customer profile unification platform that resolves
the same real-world person across systems such as POS, Bitrix CRM, and
third-party applications.

## Document Map

- [Architecture](./docs/profile-unifier-architecture.md)
- [Matching Spec](./docs/profile-unifier-matching-spec.md)
- [Policy Decisions](./docs/profile-unifier-policy-decisions.md)
- [SQL Schema](./docs/profile-unifier-sql-schema.md)
- [API Spec](./docs/profile-unifier-api-spec.md)
- [Reviewer Workflow](./docs/profile-unifier-reviewer-workflow.md)
- [PRD](./docs/profile-unifier-prd.md)
- [Roadmap](./docs/profile-unifier-roadmap.md)

## Recommended Reading Order

1. PRD
2. Architecture
3. Matching Spec
4. Policy Decisions
5. SQL Schema
6. API Spec
7. Reviewer Workflow
8. Roadmap

## Current Scope

The document set covers:

- centralized identity resolution
- deterministic and probabilistic matching
- heuristic and LLM-based adjudication paths
- manual review and unmerge workflows
- golden profile generation
- phased rollout planning

## Principles

- optimize for low false-merge rates
- keep source facts immutable and auditable
- make every decision explainable
- treat NRIC and Singpass-linked data as highly sensitive
- prefer controlled rollout over aggressive automation
