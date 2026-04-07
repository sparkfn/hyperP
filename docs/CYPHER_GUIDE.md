# Cypher Guide for Developers

A practical reference for working with the Neo4j graph in this project.
No prior Cypher knowledge assumed.

## Cypher in 5 minutes

Cypher is Neo4j's query language. It reads like ASCII art:

```
(node)-[:RELATIONSHIP]->(other_node)
```

| Cypher | Meaning |
|---|---|
| `(p:Person)` | A node with label `Person`, aliased as `p` |
| `-[:IDENTIFIED_BY]->` | A relationship of type `IDENTIFIED_BY`, directed |
| `{person_id: $pid}` | Property match — `$pid` is a parameter |
| `MATCH` | Find existing data |
| `CREATE` | Create new data |
| `MERGE` | Match or create (upsert) |
| `SET` | Update properties |
| `DELETE` | Delete a relationship or node |
| `RETURN` | What to send back |
| `WITH` | Pipe results between clauses (like a subquery) |
| `WHERE` | Filter |
| `OPTIONAL MATCH` | Like LEFT JOIN — returns null if no match |

## Our graph model at a glance

```
(Identifier)<-[:IDENTIFIED_BY]-(Person)-[:LIVES_AT]->(Address)
                                  ^
(SourceRecord)-[:LINKED_TO]-------+
(SourceRecord)-[:FROM_SOURCE]->(SourceSystem)

(Person)-[:MERGED_INTO]->(Person)
(Person)-[:NO_MATCH_LOCK]->(Person)
(Person)-[:HAS_FACT {props}]->(SourceRecord)

(MatchDecision)-[:ABOUT_LEFT]->(SourceRecord)
(MatchDecision)-[:ABOUT_RIGHT]->(Person)
(ReviewCase)-[:FOR_DECISION]->(MatchDecision)
(MergeEvent)-[:ABSORBED]->(Person)
(MergeEvent)-[:SURVIVOR]->(Person)
(SourceRecord)-[:PART_OF_RUN]->(IngestRun)
```

## Where queries live

| File | Language | Purpose |
|---|---|---|
| `services/ingestion/src/graph/queries.py` | Python | All ingestion write queries |
| `services/api/src/graph/queries.ts` | TypeScript | All API read queries |
| `services/api/src/routes/*.ts` | TypeScript | Inline write queries for merge, review, locks |
| `infra/neo4j/init.cypher` | Cypher | Constraints and indexes (run once) |

## Read patterns

### Find a person by phone number

```cypher
MATCH (id:Identifier {identifier_type: 'phone', normalized_value: '+6591234567'})
  <-[:IDENTIFIED_BY]-(p:Person {status: 'active'})
RETURN p.preferred_full_name, p.preferred_phone
```

**How it works:** Start at the Identifier node, follow the `IDENTIFIED_BY`
relationship backwards to find the Person.

```
(Identifier: +6591234567) <--[IDENTIFIED_BY]-- (Person: Alice Tan)
```

### Get a person with merge chain resolution

```cypher
MATCH (p:Person {person_id: $pid})
OPTIONAL MATCH (p)-[:MERGED_INTO]->(canonical:Person)
WITH coalesce(canonical, p) AS person
RETURN person.person_id, person.preferred_full_name
```

**How it works:** If the person was merged into someone else, follow the
`MERGED_INTO` relationship (max 1 hop due to path compression). `coalesce`
picks the canonical person or falls back to the original if not merged.

```
Before: lookup "Bob" -> (Bob)-[:MERGED_INTO]->(Alice) -> return Alice
After:  lookup "Alice" -> no MERGED_INTO -> return Alice
```

### Resolve preferred address

```cypher
MATCH (p:Person {person_id: $pid})
OPTIONAL MATCH (addr:Address {address_id: p.preferred_address_id})
RETURN p.preferred_full_name, addr.street_name, addr.postal_code
```

**How it works:** The Person node stores `preferred_address_id` as a UUID.
We look up the Address node by that ID. If the Address was deleted, `addr`
is null.

### Contact tracing — who shares an identifier?

```cypher
MATCH (p:Person {person_id: $pid})
  -[:IDENTIFIED_BY]->(id:Identifier)
  <-[:IDENTIFIED_BY]-(other:Person {status: 'active'})
WHERE other.person_id <> p.person_id
RETURN other.preferred_full_name, id.identifier_type, id.normalized_value
```

**How it works:** Two hops through a shared Identifier node. This is the
query that justifies the graph database — no value comparison needed, pure
traversal.

```
(Alice) --[IDENTIFIED_BY]--> (Phone: +6591234567) <--[IDENTIFIED_BY]-- (Alice T.)
```

### Contact tracing — who shares an address?

```cypher
MATCH (p:Person {person_id: $pid})
  -[:LIVES_AT]->(addr:Address)
  <-[:LIVES_AT]-(other:Person {status: 'active'})
WHERE other.person_id <> p.person_id
RETURN other.preferred_full_name, addr.normalized_full
```

Same pattern as identifiers, but through Address nodes.

### Multi-hop contact tracing (2 degrees)

```cypher
MATCH (start:Person {person_id: $pid})
  -[:IDENTIFIED_BY]->(:Identifier)<-[:IDENTIFIED_BY]-
  (hop1:Person)-[:IDENTIFIED_BY]->(:Identifier)<-[:IDENTIFIED_BY]-
  (hop2:Person)
WHERE hop1.person_id <> start.person_id
  AND hop2.person_id <> start.person_id
  AND hop2.person_id <> hop1.person_id
RETURN DISTINCT hop2.preferred_full_name, hop1.preferred_full_name AS via
```

**How it works:** Four hops — start → identifier → person → identifier → end.
Finds people connected through intermediaries.

### Get all source records for a person

```cypher
MATCH (sr:SourceRecord)-[:LINKED_TO]->(p:Person {person_id: $pid})
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
RETURN sr.source_record_id, ss.source_key, sr.link_status
ORDER BY sr.observed_at DESC
```

### Get attribute facts

```cypher
MATCH (p:Person {person_id: $pid})-[f:HAS_FACT]->(sr:SourceRecord)
RETURN f.attribute_name, f.attribute_value, f.source_trust_tier, sr.source_record_id
```

**Note:** `HAS_FACT` is a relationship with properties — the attribute data
lives on the edge, not on a separate node. Direction is always
`Person → SourceRecord`.

### List review cases

```cypher
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.queue_state IN ['open', 'assigned']
RETURN rc.review_case_id, rc.priority, md.decision, md.confidence
ORDER BY rc.priority, rc.sla_due_at
```

## Write patterns

### Create a person

```cypher
CREATE (p:Person {
  person_id: randomUUID(),
  status: 'active',
  created_at: datetime(),
  updated_at: datetime()
})
RETURN p.person_id
```

### Upsert an identifier (MERGE = find or create)

```cypher
MERGE (id:Identifier {identifier_type: 'phone', normalized_value: '+6591234567'})
ON CREATE SET id.identifier_id = randomUUID(), id.created_at = datetime()
RETURN id.identifier_id
```

**`MERGE` vs `CREATE`:** Use `MERGE` when the node might already exist
(identifiers, addresses). Use `CREATE` when it's always new (source records,
merge events).

### Link a person to an identifier (MERGE to prevent duplicates)

```cypher
MATCH (p:Person {person_id: $pid})
MATCH (id:Identifier {identifier_type: $type, normalized_value: $value})
MERGE (p)-[rel:IDENTIFIED_BY]->(id)
ON CREATE SET
    rel.is_verified = $verified,
    rel.is_active = true,
    rel.first_seen_at = datetime(),
    rel.last_seen_at = datetime()
ON MATCH SET
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime()
```

**Why MERGE on the relationship:** If Alice's phone is confirmed by two
different source records, we don't want two `IDENTIFIED_BY` edges. `MERGE`
creates the relationship on first encounter and updates timestamps on
subsequent encounters.

### Create an attribute fact (always CREATE — facts are append-only)

```cypher
MATCH (p:Person {person_id: $pid})
MATCH (sr:SourceRecord {source_record_pk: $srpk})
CREATE (p)-[:HAS_FACT {
    attribute_name: 'full_name',
    attribute_value: 'Alice Tan',
    source_trust_tier: 'tier_2',
    observed_at: datetime()
}]->(sr)
```

**Direction:** Person → SourceRecord. NOT `(p)-[:HAS_FACT]->(p)` — that
would be a self-loop and is wrong.

### Check for a no-match lock

```cypher
MATCH (a:Person {person_id: $left})-[lock:NO_MATCH_LOCK]-(b:Person {person_id: $right})
WHERE lock.expires_at IS NULL OR lock.expires_at > datetime()
RETURN count(lock) > 0 AS is_locked
```

**Note the undirected pattern** `(a)-[lock]-(b)` — locks are always created
with `left_id < right_id`, but we check both directions to be safe.

## Merge operation (the complex one)

This is the hardest write in the system. It runs in a single ACID transaction.

### What happens when Person A is merged into Person B

```
BEFORE                              AFTER

(SR1)-[:LINKED_TO]->(A)            (SR1)-[:LINKED_TO]->(B)
(SR2)-[:LINKED_TO]->(A)            (SR2)-[:LINKED_TO]->(B)
(A)-[:IDENTIFIED_BY]->(Phone)      (B)-[:IDENTIFIED_BY]->(Phone)
(A)-[:LIVES_AT]->(Addr)            (B)-[:LIVES_AT]->(Addr)
(A)-[:HAS_FACT]->(SR1)             (B)-[:HAS_FACT]->(SR1)
(A) status='active'                (A) status='merged'
                                   (A)-[:MERGED_INTO]->(B)
```

### The Cypher (simplified)

```cypher
// 1. Rewire source records
MATCH (sr:SourceRecord)-[old:LINKED_TO]->(absorbed:Person {person_id: $from})
DELETE old
CREATE (sr)-[:LINKED_TO]->(survivor:Person {person_id: $to})

// 2. Rewire identifiers (MERGE to avoid duplicates if survivor already has it)
MATCH (absorbed)-[old:IDENTIFIED_BY]->(id:Identifier)
DELETE old
MERGE (survivor)-[:IDENTIFIED_BY]->(id)

// 3. Same for LIVES_AT, HAS_FACT...

// 4. Mark absorbed
SET absorbed.status = 'merged'
CREATE (absorbed)-[:MERGED_INTO]->(survivor)

// 5. Path compression — anyone who previously merged into absorbed now points to survivor
MATCH (prev:Person)-[old:MERGED_INTO]->(absorbed)
DELETE old
CREATE (prev)-[:MERGED_INTO]->(survivor)
```

### Path compression explained

```
BEFORE compression:    A -[:MERGED_INTO]-> B -[:MERGED_INTO]-> C
AFTER compression:     A -[:MERGED_INTO]-> C
                       B -[:MERGED_INTO]-> C
```

This guarantees any person lookup is max 1 hop to find the canonical person.

## Common mistakes to avoid

### 1. Map literals in Cypher

Neo4j Community does NOT support nested maps as property values:

```cypher
// WRONG — will crash
CREATE (n:Node {metadata: {key: 'value'}})

// CORRECT — serialize to JSON string
CREATE (n:Node {metadata: '{"key": "value"}'})
```

### 2. HAS_FACT direction

```cypher
// WRONG — self-loop
CREATE (p)-[:HAS_FACT {attribute_name: 'name'}]->(p)

// CORRECT — Person to SourceRecord
CREATE (p)-[:HAS_FACT {attribute_name: 'name'}]->(sr)
```

### 3. CREATE vs MERGE for relationships

```cypher
// WRONG — creates duplicates on re-ingestion
CREATE (p)-[:IDENTIFIED_BY]->(id)

// CORRECT — upserts
MERGE (p)-[rel:IDENTIFIED_BY]->(id)
ON CREATE SET rel.first_seen_at = datetime()
ON MATCH SET rel.last_seen_at = datetime()
```

Use `CREATE` only for things that are always new: SourceRecord, MergeEvent,
MatchDecision. Use `MERGE` for things that might already exist: Identifier
nodes, Address nodes, IDENTIFIED_BY relationships, LIVES_AT relationships.

### 4. Forgetting to resolve merge chains

```cypher
// WRONG — returns merged person shell
MATCH (p:Person {person_id: $pid})
RETURN p

// CORRECT — follows merge chain
MATCH (p:Person {person_id: $pid})
OPTIONAL MATCH (p)-[:MERGED_INTO]->(canonical:Person)
RETURN coalesce(canonical, p) AS person
```

### 5. Lock ordering

```cypher
// WRONG — might create duplicate locks
CREATE (a)-[:NO_MATCH_LOCK]->(b)
CREATE (b)-[:NO_MATCH_LOCK]->(a)

// CORRECT — always left_id < right_id
WHERE a.person_id < b.person_id
CREATE (a)-[:NO_MATCH_LOCK]->(b)
```

## How to add a new query

1. Write the Cypher in the appropriate query file:
   - Reads → `services/api/src/graph/queries.ts`
   - Ingestion writes → `services/ingestion/src/graph/queries.py`
   - API writes → inline in the route file under `session.executeWrite()`

2. Use `$parameter` syntax for all dynamic values — never concatenate strings

3. Test in Neo4j Browser first (`http://localhost:7474`), replacing `$params`
   with literal values

4. For writes, always use explicit transactions:
   - TypeScript: `session.executeWrite(async (tx) => tx.run(...))`
   - Python: `session.execute_write(lambda tx: tx.run(...))`

## Neo4j Browser tips

Open `http://localhost:7474`, login with `neo4j` / your password.

| Action | How |
|---|---|
| See the full graph | `MATCH (n)-[r]->(m) RETURN n, r, m` |
| Count nodes by label | `MATCH (n) RETURN labels(n)[0] AS label, count(*)` |
| Count relationships | `MATCH ()-[r]->() RETURN type(r), count(*)` |
| Check constraints | `SHOW CONSTRAINTS` |
| Check indexes | `SHOW INDEXES` |
| Profile a query | Prefix with `PROFILE` to see the execution plan |
| Explain without running | Prefix with `EXPLAIN` |
