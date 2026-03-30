# Profile Unifier SQL Schema

## Purpose

Define a concrete PostgreSQL reference schema for the profile unifier platform.
This schema is designed for implementation planning and can be translated into
application migrations with minimal interpretation.

## Scope

The schema covers:

- source-system registration
- ingestion runs and raw source records
- canonical persons
- identifiers and attribute facts
- golden profile storage
- match decisions and review cases
- merge and unmerge audit events
- manual locks and survivorship overrides

## Design Notes

- PostgreSQL is the reference database.
- UUID primary keys are used for internal entities.
- Raw source identifiers remain source-scoped string keys.
- JSONB is used where the shape is flexible and audit-oriented.
- Sensitive identifiers should be encrypted or tokenized before application
  write where policy requires it.
- The schema is normalized first. Read models or materialized views can be
  added later for performance.

## PostgreSQL Assumptions

Recommended extensions:

```sql
create extension if not exists pgcrypto;
create extension if not exists citext;
```

Recommended schema namespace:

```sql
create schema if not exists profile_unifier;
set search_path = profile_unifier, public;
```

## Enumerated Types

```sql
create type person_status as enum (
  'active',
  'under_review',
  'merged',
  'suppressed'
);

create type record_link_status as enum (
  'linked',
  'pending_review',
  'rejected',
  'suppressed'
);

create type identifier_type as enum (
  'government_id_hash',
  'phone',
  'email',
  'external_customer_id',
  'membership_id',
  'crm_contact_id',
  'loyalty_id',
  'custom'
);

create type trust_tier as enum (
  'tier_1',
  'tier_2',
  'tier_3',
  'tier_4'
);

create type quality_flag as enum (
  'valid',
  'invalid_format',
  'placeholder_value',
  'shared_identifier_suspected',
  'stale',
  'source_untrusted'
);

create type match_engine_type as enum (
  'deterministic',
  'heuristic',
  'llm',
  'manual'
);

create type match_decision_type as enum (
  'merge',
  'review',
  'no_match'
);

create type review_queue_state as enum (
  'open',
  'assigned',
  'deferred',
  'resolved',
  'cancelled'
);

create type review_resolution_type as enum (
  'merge',
  'reject',
  'manual_no_match',
  'cancelled_superseded'
);

create type merge_event_type as enum (
  'auto_merge',
  'manual_merge',
  'review_reject',
  'manual_no_match',
  'unmerge',
  'person_split',
  'survivorship_override'
);

create type actor_type as enum (
  'system',
  'reviewer',
  'admin',
  'service'
);

create type lock_type as enum (
  'manual_no_match',
  'manual_merge_hint',
  'person_suppression'
);

create type review_action_type as enum (
  'assign',
  'unassign',
  'merge',
  'reject',
  'manual_no_match',
  'defer',
  'escalate',
  'cancel',
  'reopen'
);
```

## Core Reference Tables

### source_system

Registers upstream systems and broad source metadata.

```sql
create table source_system (
  source_system_id uuid primary key default gen_random_uuid(),
  source_key text not null unique,
  display_name text not null,
  system_type text not null,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
```

### source_field_trust

Stores field-level trust policy per source system.

```sql
create table source_field_trust (
  source_field_trust_id uuid primary key default gen_random_uuid(),
  source_system_id uuid not null references source_system(source_system_id),
  field_name text not null,
  trust_tier trust_tier not null,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (source_system_id, field_name)
);
```

### ingest_run

Groups a sync or backfill execution for observability and replay.

```sql
create table ingest_run (
  ingest_run_id uuid primary key default gen_random_uuid(),
  source_system_id uuid not null references source_system(source_system_id),
  run_type text not null,
  status text not null,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  metadata jsonb not null default '{}'::jsonb
);
```

## Canonical Person Tables

### person

```sql
create table person (
  person_id uuid primary key default gen_random_uuid(),
  status person_status not null default 'active',
  primary_source_system_id uuid references source_system(source_system_id),
  merged_into_person_id uuid references person(person_id),
  is_high_value boolean not null default false,
  is_high_risk boolean not null default false,
  suppression_reason text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (
    (status = 'merged' and merged_into_person_id is not null)
    or (status <> 'merged' and merged_into_person_id is null)
  )
);
```

### person_alias

Stores deprecated or external canonical IDs if needed during migration.

```sql
create table person_alias (
  person_alias_id uuid primary key default gen_random_uuid(),
  person_id uuid not null references person(person_id),
  alias_namespace text not null,
  alias_value text not null,
  created_at timestamptz not null default now(),
  unique (alias_namespace, alias_value)
);
```

## Source Record Tables

### source_record

Stores immutable raw input records.

```sql
create table source_record (
  source_record_pk uuid primary key default gen_random_uuid(),
  source_system_id uuid not null references source_system(source_system_id),
  source_record_id text not null,
  source_record_version text,
  ingest_run_id uuid references ingest_run(ingest_run_id),
  linked_person_id uuid references person(person_id),
  link_status record_link_status not null default 'pending_review',
  observed_at timestamptz,
  ingested_at timestamptz not null default now(),
  record_hash text not null,
  raw_payload jsonb not null,
  normalized_payload jsonb not null default '{}'::jsonb,
  metadata jsonb not null default '{}'::jsonb,
  unique (source_system_id, source_record_id, record_hash)
);
```

### source_record_rejection

Captures normalization or ingestion failures without dropping evidence.

```sql
create table source_record_rejection (
  source_record_rejection_id uuid primary key default gen_random_uuid(),
  source_system_id uuid not null references source_system(source_system_id),
  source_record_id text,
  ingest_run_id uuid references ingest_run(ingest_run_id),
  rejection_reason text not null,
  raw_payload jsonb,
  created_at timestamptz not null default now()
);
```

## Identifier and Attribute Tables

### person_identifier

Stores identifiers associated with a canonical person and their lineage.

```sql
create table person_identifier (
  person_identifier_id uuid primary key default gen_random_uuid(),
  person_id uuid not null references person(person_id),
  source_record_pk uuid references source_record(source_record_pk),
  source_system_id uuid not null references source_system(source_system_id),
  identifier_type identifier_type not null,
  raw_value text,
  normalized_value text,
  hashed_value text,
  is_verified boolean not null default false,
  verification_method text,
  is_active boolean not null default true,
  quality_flag quality_flag not null default 'valid',
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  metadata jsonb not null default '{}'::jsonb,
  check (
    normalized_value is not null or hashed_value is not null
  )
);
```

Recommended uniqueness policy:

- do not globally unique phone or email
- keep namespace-based uniqueness for source-owned IDs only through application
  policy or partial indexes where safe

Example partial indexes:

```sql
create unique index person_identifier_unique_source_external
  on person_identifier (source_system_id, identifier_type, normalized_value)
  where identifier_type in ('external_customer_id', 'membership_id', 'crm_contact_id', 'loyalty_id')
    and normalized_value is not null
    and is_active = true;
```

### person_attribute_fact

Stores source-specific attribute observations.

```sql
create table person_attribute_fact (
  person_attribute_fact_id uuid primary key default gen_random_uuid(),
  person_id uuid not null references person(person_id),
  source_record_pk uuid references source_record(source_record_pk),
  source_system_id uuid not null references source_system(source_system_id),
  attribute_name text not null,
  attribute_value jsonb not null,
  source_trust_tier trust_tier not null,
  confidence numeric(5,4) not null default 1.0,
  quality_flag quality_flag not null default 'valid',
  is_current_hint boolean not null default false,
  observed_at timestamptz not null,
  created_at timestamptz not null default now()
);
```

### survivorship_override

Stores manual override for preferred field selection.

```sql
create table survivorship_override (
  survivorship_override_id uuid primary key default gen_random_uuid(),
  person_id uuid not null references person(person_id),
  attribute_name text not null,
  selected_person_attribute_fact_id uuid not null references person_attribute_fact(person_attribute_fact_id),
  reason text not null,
  actor_type actor_type not null,
  actor_id text not null,
  created_at timestamptz not null default now(),
  unique (person_id, attribute_name)
);
```

## Golden Profile Tables

### golden_profile

One row per canonical person for commonly accessed preferred fields.

```sql
create table golden_profile (
  person_id uuid primary key references person(person_id),
  preferred_full_name text,
  preferred_phone text,
  preferred_email citext,
  preferred_dob date,
  preferred_address jsonb,
  profile_completeness_score numeric(5,4) not null default 0,
  computed_at timestamptz not null default now(),
  computation_version text not null
);
```

### golden_profile_lineage

Tracks which facts fed each preferred field.

```sql
create table golden_profile_lineage (
  golden_profile_lineage_id uuid primary key default gen_random_uuid(),
  person_id uuid not null references person(person_id),
  field_name text not null,
  person_attribute_fact_id uuid references person_attribute_fact(person_attribute_fact_id),
  person_identifier_id uuid references person_identifier(person_identifier_id),
  created_at timestamptz not null default now(),
  unique (person_id, field_name)
);
```

## Matching Tables

### candidate_pair

Optional persisted table for debugging and replay of candidate generation.

```sql
create table candidate_pair (
  candidate_pair_id uuid primary key default gen_random_uuid(),
  left_entity_type text not null,
  left_entity_id text not null,
  right_entity_type text not null,
  right_entity_id text not null,
  blocking_reason text not null,
  created_at timestamptz not null default now(),
  unique (left_entity_type, left_entity_id, right_entity_type, right_entity_id, blocking_reason)
);
```

### match_decision

Stores outputs from deterministic, heuristic, LLM, or manual paths.

```sql
create table match_decision (
  match_decision_id uuid primary key default gen_random_uuid(),
  left_entity_type text not null,
  left_entity_id text not null,
  right_entity_type text not null,
  right_entity_id text not null,
  candidate_pair_id uuid references candidate_pair(candidate_pair_id),
  engine_type match_engine_type not null,
  engine_version text not null,
  decision match_decision_type not null,
  confidence numeric(5,4),
  reasons jsonb not null default '[]'::jsonb,
  blocking_conflicts jsonb not null default '[]'::jsonb,
  feature_snapshot jsonb not null default '{}'::jsonb,
  prompt_snapshot jsonb,
  policy_version text not null,
  created_at timestamptz not null default now()
);
```

### person_pair_lock

Stores manual no-match or suppression-style locks that must survive reprocessing.

```sql
create table person_pair_lock (
  person_pair_lock_id uuid primary key default gen_random_uuid(),
  left_person_id uuid references person(person_id),
  right_person_id uuid references person(person_id),
  left_source_record_pk uuid references source_record(source_record_pk),
  right_source_record_pk uuid references source_record(source_record_pk),
  lock_type lock_type not null,
  reason text not null,
  expires_at timestamptz,
  actor_type actor_type not null,
  actor_id text not null,
  created_at timestamptz not null default now(),
  check (
    left_person_id is not null
    or left_source_record_pk is not null
  ),
  check (
    right_person_id is not null
    or right_source_record_pk is not null
  )
);
```

## Review Workflow Tables

### review_case

```sql
create table review_case (
  review_case_id uuid primary key default gen_random_uuid(),
  match_decision_id uuid not null references match_decision(match_decision_id),
  priority integer not null default 100,
  queue_state review_queue_state not null default 'open',
  assigned_to text,
  follow_up_at timestamptz,
  sla_due_at timestamptz,
  resolution review_resolution_type,
  resolved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
```

### review_action

```sql
create table review_action (
  review_action_id uuid primary key default gen_random_uuid(),
  review_case_id uuid not null references review_case(review_case_id),
  action_type review_action_type not null,
  actor_type actor_type not null,
  actor_id text not null,
  notes text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
```

## Merge Audit Tables

### merge_event

```sql
create table merge_event (
  merge_event_id uuid primary key default gen_random_uuid(),
  event_type merge_event_type not null,
  from_person_id uuid references person(person_id),
  to_person_id uuid references person(person_id),
  match_decision_id uuid references match_decision(match_decision_id),
  actor_type actor_type not null,
  actor_id text not null,
  reason text not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
```

### merge_event_source_record

Maps affected source records to merge events for replay support.

```sql
create table merge_event_source_record (
  merge_event_source_record_id uuid primary key default gen_random_uuid(),
  merge_event_id uuid not null references merge_event(merge_event_id),
  source_record_pk uuid not null references source_record(source_record_pk),
  created_at timestamptz not null default now(),
  unique (merge_event_id, source_record_pk)
);
```

## Suggested Indexes

```sql
create index idx_source_record_source_key
  on source_record (source_system_id, source_record_id);

create index idx_source_record_linked_person
  on source_record (linked_person_id);

create index idx_person_identifier_person
  on person_identifier (person_id, identifier_type, is_active);

create index idx_person_identifier_lookup_norm
  on person_identifier (identifier_type, normalized_value)
  where normalized_value is not null;

create index idx_person_identifier_lookup_hash
  on person_identifier (identifier_type, hashed_value)
  where hashed_value is not null;

create index idx_attribute_fact_person_name
  on person_attribute_fact (person_id, attribute_name, observed_at desc);

create index idx_match_decision_created
  on match_decision (created_at desc, engine_type, decision);

create index idx_review_case_queue
  on review_case (queue_state, priority, sla_due_at);

create index idx_merge_event_persons
  on merge_event (from_person_id, to_person_id, created_at desc);
```

## Example Read Patterns

### Find a Person by Phone

```sql
select p.person_id, gp.preferred_full_name, gp.preferred_phone, gp.preferred_email
from person_identifier pi
join person p on p.person_id = pi.person_id
left join golden_profile gp on gp.person_id = p.person_id
where pi.identifier_type = 'phone'
  and pi.normalized_value = '+6591234567'
  and pi.is_active = true
  and p.status = 'active';
```

### Show All Source Records for a Person

```sql
select sr.source_system_id, sr.source_record_id, sr.link_status, sr.observed_at, sr.ingested_at
from source_record sr
where sr.linked_person_id = $1
order by sr.observed_at desc nulls last, sr.ingested_at desc;
```

### Fetch Pending Review Cases

```sql
select rc.review_case_id, rc.priority, rc.sla_due_at, md.decision, md.confidence, md.engine_type
from review_case rc
join match_decision md on md.match_decision_id = rc.match_decision_id
where rc.queue_state in ('open', 'assigned')
order by rc.priority asc, rc.sla_due_at asc nulls last, rc.created_at asc;
```

## Example Write Flows

### Ingest a New Source Record

1. insert `ingest_run` if needed
2. insert `source_record`
3. insert normalized `person_identifier` rows after person linkage is known
4. insert `person_attribute_fact` rows
5. insert `match_decision`
6. update `source_record.linked_person_id` and `link_status`
7. recompute `golden_profile`

### Manual No-Match Lock

1. create `review_action`
2. insert `person_pair_lock`
3. insert `merge_event` of type `manual_no_match`
4. close `review_case`

### Unmerge

1. insert `merge_event` of type `unmerge`
2. restore or create affected `person` rows
3. relink `source_record`
4. rebuild `person_identifier` and `person_attribute_fact` associations if needed
5. recompute impacted `golden_profile` rows

## Data Integrity Notes

- `person.status = 'merged'` must always imply `merged_into_person_id` is set
- hard uniqueness should be conservative to avoid blocking legitimate shared
  phone and email cases
- all manual locks and overrides must be durable across replay
- raw payload and decision history should never be overwritten in place

## Scaling Notes

- large installs may partition `source_record`, `person_attribute_fact`, and
  `match_decision` by time
- search-heavy workloads may justify a separate search index for name and
  address lookup
- analytical reporting should be moved to warehouse tables or replicas rather
  than burdening OLTP paths

## Migration Strategy

Recommended migration order:

1. create enums
2. create reference tables
3. create person and source tables
4. create identifier and fact tables
5. create matching and review tables
6. create audit tables
7. create indexes
8. seed source systems and trust settings

## Recommendation

Use this schema as the OLTP reference model and keep application-level matching
logic outside the database. The database should enforce identity lineage,
review durability, and audit integrity, not embed heuristic business logic in
triggers.
