# Profile Unifier Graph Model Diagram

## Purpose

Visual reference for how Neo4j nodes and relationships connect. Use alongside
the [graph schema](./profile-unifier-graph-schema.md) for property-level
detail.

## Full Graph Model

```mermaid
graph LR
    subgraph Identity Core
        P[Person]
        ID[Identifier]
        ADDR[Address]

        P -->|IDENTIFIED_BY| ID
        P -->|LIVES_AT| ADDR
    end

    subgraph Source Data
        SR[SourceRecord]
        SS[SourceSystem]
        IR[IngestRun]

        SR -->|LINKED_TO| P
        SR -->|FROM_SOURCE| SS
        IR -->|FROM_SOURCE| SS
        SR -->|PART_OF_RUN| IR
        P -->|HAS_FACT| SR
    end

    subgraph Merge Lineage
        P2[Person absorbed]
        ME[MergeEvent]

        P2 -->|MERGED_INTO| P
        ME -->|ABSORBED| P2
        ME -->|SURVIVOR| P
        ME -->|TRIGGERED_BY| MD
        ME -->|AFFECTED_RECORD| SR
    end

    subgraph Matching & Review
        MD[MatchDecision]
        RC[ReviewCase]

        MD -->|ABOUT_LEFT| P
        MD -->|ABOUT_RIGHT| SR
        RC -->|FOR_DECISION| MD
    end

    subgraph Locks
        P3[Person A]
        P4[Person B]

        P3 -->|NO_MATCH_LOCK| P4
    end
```

## Identity Subgraph (Detail)

How persons connect through shared Identifier and Address nodes — the
foundation for contact tracing.

```mermaid
graph TD
    PA[Person A: Alice] -->|IDENTIFIED_BY| PH1[Identifier: +6591234567 phone]
    PB[Person B: Bob] -->|IDENTIFIED_BY| PH1

    PA -->|IDENTIFIED_BY| EM1[Identifier: alice@example.com email]
    PC[Person C: Charlie] -->|IDENTIFIED_BY| EM1

    PA -->|LIVES_AT| AD1[Address: 10 Example St, SG 123456]
    PD[Person D: Dana] -->|LIVES_AT| AD1

    PA -->|HAS_FACT| SR1[SourceRecord: bitrix:12345]
    SR1 -->|FROM_SOURCE| SS1[SourceSystem: Bitrix CRM]

    style PA fill:#4a9eff,color:#fff
    style PB fill:#4a9eff,color:#fff
    style PC fill:#4a9eff,color:#fff
    style PD fill:#4a9eff,color:#fff
    style PH1 fill:#ff9f43,color:#fff
    style EM1 fill:#ff9f43,color:#fff
    style AD1 fill:#2ecc71,color:#fff
    style SR1 fill:#a29bfe,color:#fff
    style SS1 fill:#636e72,color:#fff
```

Reading this diagram:

- **Alice and Bob** share a phone — connected through the same Identifier node
- **Alice and Charlie** share an email — connected through another Identifier
- **Alice and Dana** share an address — connected through the same Address node
- Contact tracing from Alice reaches Bob (1 hop via phone), Charlie (1 hop
  via email), and Dana (1 hop via address)

## Merge Lineage Subgraph

How merge history and path compression work.

```mermaid
graph LR
    subgraph Before Path Compression
        A1[Person A] -->|MERGED_INTO| B1[Person B]
        B1 -->|MERGED_INTO| C1[Person C]
    end

    subgraph After Path Compression
        A2[Person A] -->|MERGED_INTO| C2[Person C]
        B2[Person B] -->|MERGED_INTO| C2
    end
```

Path compression rewires all `MERGED_INTO` relationships to point directly to
the final survivor. Max 1 hop to resolve any person to its canonical form.

## Match and Review Subgraph

How decisions, review cases, and merge events connect.

```mermaid
graph TD
    SR[SourceRecord: new record] --> MD[MatchDecision]
    P[Person: existing] --> MD

    MD -->|decision=review| RC[ReviewCase]

    RC -->|reviewer merges| ME[MergeEvent: manual_merge]
    ME -->|ABSORBED| P2[Person: absorbed]
    ME -->|SURVIVOR| P
    ME -->|AFFECTED_RECORD| SR

    style MD fill:#fdcb6e,color:#000
    style RC fill:#e17055,color:#fff
    style ME fill:#d63031,color:#fff
```

## Ingestion Write Flow

Order of operations when a new source record is ingested.

```mermaid
sequenceDiagram
    participant Ingest as Ingestion Service
    participant Neo as Neo4j

    Ingest->>Neo: MATCH SourceSystem
    Ingest->>Neo: CREATE SourceRecord + FROM_SOURCE
    Ingest->>Neo: MERGE Identifier nodes (upsert)
    Ingest->>Neo: MERGE Address node (upsert)
    Ingest->>Neo: Find candidates via Identifier/Address traversal
    Note over Ingest: Match engine evaluates candidates
    alt New person
        Ingest->>Neo: CREATE Person
        Ingest->>Neo: CREATE MergeEvent(person_created)
    end
    Ingest->>Neo: CREATE IDENTIFIED_BY relationships
    Ingest->>Neo: CREATE LIVES_AT relationship
    Ingest->>Neo: CREATE HAS_FACT relationships (name, DOB, etc.)
    Ingest->>Neo: CREATE LINKED_TO (SourceRecord → Person)
    Ingest->>Neo: UPDATE Person golden profile properties
```

## Merge Write Flow

Order of operations when two persons are merged.

```mermaid
sequenceDiagram
    participant Svc as API Service
    participant Neo as Neo4j

    Note over Svc,Neo: Single ACID transaction
    Svc->>Neo: MATCH absorbed Person + survivor Person
    Svc->>Neo: CREATE MergeEvent + ABSORBED/SURVIVOR
    Svc->>Neo: Rewire LINKED_TO (SourceRecords → survivor)
    Svc->>Neo: Rewire IDENTIFIED_BY (→ survivor)
    Svc->>Neo: Rewire LIVES_AT (→ survivor)
    Svc->>Neo: SET absorbed.status = 'merged'
    Svc->>Neo: CREATE MERGED_INTO (absorbed → survivor)
    Svc->>Neo: Path compress: rewire prior MERGED_INTO → survivor
    Svc->>Neo: UPDATE survivor golden profile
```

## Node Relationship Summary

| From | Relationship | To | Cardinality | Notes |
| --- | --- | --- | --- | --- |
| Person | IDENTIFIED_BY | Identifier | many-to-many | shared identifiers are the graph backbone |
| Person | LIVES_AT | Address | many-to-many | shared addresses enable household detection |
| Person | HAS_FACT | SourceRecord | many-to-many | attribute observations (name, DOB, etc.) |
| Person | MERGED_INTO | Person | many-to-one | max 1 hop after path compression |
| Person | NO_MATCH_LOCK | Person | many-to-many | always left_id < right_id |
| SourceRecord | LINKED_TO | Person | many-to-one | one person per source record |
| SourceRecord | FROM_SOURCE | SourceSystem | many-to-one | provenance |
| SourceRecord | PART_OF_RUN | IngestRun | many-to-one | batch grouping |
| IngestRun | FROM_SOURCE | SourceSystem | many-to-one | provenance |
| MatchDecision | ABOUT_LEFT | Person or SR | one-to-one | left side of compared pair |
| MatchDecision | ABOUT_RIGHT | Person or SR | one-to-one | right side of compared pair |
| ReviewCase | FOR_DECISION | MatchDecision | one-to-one | links review to decision |
| MergeEvent | ABSORBED | Person | one-to-one | the person that was absorbed |
| MergeEvent | SURVIVOR | Person | one-to-one | the person that survived |
| MergeEvent | TRIGGERED_BY | MatchDecision | one-to-one | optional |
| MergeEvent | AFFECTED_RECORD | SourceRecord | one-to-many | for unmerge replay |
