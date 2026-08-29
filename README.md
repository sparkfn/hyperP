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
- [Relationship Match Thresholds](./docs/profile-unifier-relationship-match-thresholds.md)
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
- [Eko and SpeedZone API Ingestion Design](./docs/superpowers/specs/profile-unifier-eko-speedzone-api-ingestion-design.md)
- [SG Bankruptcy API Ingestion](./docs/profile-unifier-sg-bankruptcy-api-ingestion.md)
- [WhatsAdmin API Ingestion Design](./docs/superpowers/specs/profile-unifier-whatsadmin-api-ingestion-design.md)
- [Fundbox API Ingestion Design](./docs/superpowers/specs/profile-unifier-fundbox-api-ingestion-design.md)
- [Bitrix Chat Open Lines API Ingestion Design](./docs/superpowers/specs/profile-unifier-bitrix-openlines-api-ingestion-design.md)
- [Ingestion Operations](./docs/profile-unifier-ingestion-operations.md)
- [Person Profile Analysis Design](./docs/superpowers/specs/profile-unifier-person-profile-analysis-design.md)
- [Sales Prediction Approach and PRD](./docs/profile-unifier-sales-prediction-prd.md)
- [Sales Prediction Feasibility Discovery](./docs/profile-unifier-sales-prediction-discovery.md)
- [CRM History Authority Contract](./docs/profile-unifier-crm-history-authority.md)
- [Person CRM Metrics Design](./docs/profile-unifier-person-crm-metrics.md)
- [Deal Intelligence Architecture](./docs/profile-unifier-deal-intelligence-architecture.md)
- [Deal Intelligence Platform Operations](./docs/profile-unifier-deal-intelligence-platform-operations.md)
- [CRM-deal Identity Remediation Inventory](./docs/profile-unifier-crm-deal-identity-remediation.md)

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
14. Person Profile Analysis Design
15. Sales Prediction Approach and PRD
16. Sales Prediction Feasibility Discovery
17. CRM History Authority Contract
18. Person CRM Metrics Design
19. Deal Intelligence Architecture
20. Deal Intelligence Platform Operations
21. CRM-deal Identity Remediation Inventory

## Current Scope

The document set covers:

- centralized identity resolution
- deterministic and probabilistic matching
- heuristic and LLM-based adjudication paths
- manual review and unmerge workflows
- golden profile generation
- contact tracing and complex relationship queries
- authenticated, LLM-generated sales and contact-tracing profile analysis
- explainable sales conversion prediction and opportunity prioritization
- phased rollout planning

## Principles

- optimize for low false-merge rates
- keep source facts immutable and auditable
- make every decision explainable
- treat NRIC and Singpass-linked data as highly sensitive
- prefer controlled rollout over aggressive automation

## Agent-triggered all-source ingestion

Agents and server operators can queue one complete, two-phase ingestion without
using the API or frontend. Identity sources run as the first Celery group; the
orchestration task only queues dependent sources after every identity task returns
`completed`.

Submit a validated inline JSON payload from the ingestion worker container.
`$PAYLOAD` must contain the complete `identity` and `dependent` arrays:

```bash
docker compose exec -T ingestion-worker python -m src.ingestion_orchestrator trigger \
  --payload "$PAYLOAD"
```

When addressing the container directly instead of through Compose:

```bash
docker exec <ingestion-worker-container> python -m src.ingestion_orchestrator trigger \
  --payload "$PAYLOAD"
```

For larger payloads, pass JSON on standard input to avoid shell-escaping
problems:

```bash
printf '%s' "$PAYLOAD" | docker compose exec -T ingestion-worker \
  python -m src.ingestion_orchestrator trigger --payload-stdin
```

Use `validate` in place of `trigger` for a no-dispatch check. A payload must
include every registered source exactly once, put identity sources in
`identity`, and provide each source's `mode` plus a relative `dump_path` when
`mode` is `dump`. The command prints one compact JSON result for automation.
