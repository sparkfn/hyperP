-- Read projection schema for the #491 Rust read spike.
--
-- This schema is owned by the projection writer (`POSTGRES_USER=spike_writer`).
-- It is created by the Postgres init sequence; neither the Celery projection
-- task nor the Rust reader creates or migrates objects here.
--
-- Contract notes for readers:
--   * `person.list_payload` carries the person-list row (counters, entity
--     membership, display fields) and `person.detail_payload` carries the
--     person detail view. Both are JSON documents owned by the writer.
--   * `source_record` is keyed by (source_record_pk, person_id) because one
--     source record may be linked to several persons.
--   * `identity_link_rev.revision` holds only the public identity-link
--     revision fields; raw or normalized source evidence is never projected.
--   * `person.canonical_id` is a soft reference: merged rows are retained for
--     one-hop canonical detail lookup and may reference a canonical person
--     that has not been written yet, so it carries no foreign key.

CREATE TABLE IF NOT EXISTS public.person (
    person_id text PRIMARY KEY,
    canonical_id text,
    status text NOT NULL,
    preferred_full_name text,
    preferred_phone text,
    preferred_email text,
    preferred_dob text,
    preferred_nric text,
    preferred_address_id text,
    updated_at timestamptz,
    profile_completeness_score double precision,
    is_high_value boolean,
    is_high_risk boolean,
    source_record_count bigint NOT NULL DEFAULT 0,
    connection_count bigint NOT NULL DEFAULT 0,
    entity_count bigint NOT NULL DEFAULT 0,
    identifier_count bigint NOT NULL DEFAULT 0,
    possible_match_count bigint NOT NULL DEFAULT 0,
    system_match_count bigint NOT NULL DEFAULT 0,
    order_count bigint NOT NULL DEFAULT 0,
    crm_deal_count bigint NOT NULL DEFAULT 0,
    bankruptcy_case_count bigint NOT NULL DEFAULT 0,
    phone_confidence double precision,
    list_payload jsonb NOT NULL,
    detail_payload jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS public.person_identifier (
    person_id text NOT NULL REFERENCES public.person (person_id) ON DELETE CASCADE,
    identifier_type text NOT NULL,
    normalized_value text NOT NULL,
    is_active boolean,
    is_verified boolean,
    payload jsonb,
    PRIMARY KEY (person_id, identifier_type, normalized_value)
);

CREATE TABLE IF NOT EXISTS public.person_address (
    person_id text NOT NULL REFERENCES public.person (person_id) ON DELETE CASCADE,
    address_id text NOT NULL,
    is_active boolean,
    unit_number text,
    street_name text,
    city text,
    postal_code text,
    country_code text,
    payload jsonb,
    PRIMARY KEY (person_id, address_id)
);

CREATE TABLE IF NOT EXISTS public.person_connection (
    person_id text NOT NULL REFERENCES public.person (person_id) ON DELETE CASCADE,
    connected_person_id text NOT NULL,
    hops integer,
    connection_type text,
    payload jsonb,
    PRIMARY KEY (person_id, connected_person_id)
);

CREATE TABLE IF NOT EXISTS public.source_record (
    source_record_pk text NOT NULL,
    person_id text NOT NULL REFERENCES public.person (person_id) ON DELETE CASCADE,
    entity_key text,
    source_key text,
    record_type text,
    lifecycle_status text,
    is_active boolean,
    observed_at timestamptz,
    payload jsonb,
    PRIMARY KEY (source_record_pk, person_id)
);

CREATE TABLE IF NOT EXISTS public.identity_link_rev (
    event_id text PRIMARY KEY,
    global_revision bigint NOT NULL,
    link_key text NOT NULL,
    revision jsonb NOT NULL,
    CONSTRAINT identity_link_rev_revision_is_object CHECK (jsonb_typeof(revision) = 'object'),
    CONSTRAINT identity_link_rev_revision_allowlisted CHECK (
        (
            revision - ARRAY[
                'event_id',
                'global_revision',
                'source_system',
                'source_instance_id',
                'source_entity_type',
                'source_entity_id',
                'identity_policy_version',
                'link_status',
                'hyperp_person_id',
                'resolution_kind',
                'resolution_revision',
                'effective_at',
                'match_decision_id',
                'review_case_id',
                'supersedes_event_id'
            ]::text[]
        ) = '{}'::jsonb
    )
);

CREATE TABLE IF NOT EXISTS public.summary_counter (
    id text PRIMARY KEY,
    high_water timestamptz,
    last_success timestamptz,
    current_revision bigint NOT NULL DEFAULT 0,
    identity_stream_ready boolean NOT NULL DEFAULT false,
    summary jsonb,
    CONSTRAINT summary_counter_singleton_global CHECK (id = 'global')
);

-- Person filters and sorts.
CREATE INDEX IF NOT EXISTS idx_person_status ON public.person (status);
CREATE INDEX IF NOT EXISTS idx_person_updated_at ON public.person (updated_at);
CREATE INDEX IF NOT EXISTS idx_person_is_high_value ON public.person (is_high_value);
CREATE INDEX IF NOT EXISTS idx_person_is_high_risk ON public.person (is_high_risk);
CREATE INDEX IF NOT EXISTS idx_person_preferred_full_name ON public.person (preferred_full_name);
CREATE INDEX IF NOT EXISTS idx_person_preferred_phone ON public.person (preferred_phone);
CREATE INDEX IF NOT EXISTS idx_person_preferred_email ON public.person (preferred_email);
CREATE INDEX IF NOT EXISTS idx_person_preferred_dob ON public.person (preferred_dob);
CREATE INDEX IF NOT EXISTS idx_person_preferred_nric ON public.person (preferred_nric);
CREATE INDEX IF NOT EXISTS idx_person_profile_completeness
    ON public.person (profile_completeness_score DESC, person_id ASC);
CREATE INDEX IF NOT EXISTS idx_person_source_record_count
    ON public.person (source_record_count DESC, person_id ASC);
CREATE INDEX IF NOT EXISTS idx_person_connection_count
    ON public.person (connection_count DESC, person_id ASC);
CREATE INDEX IF NOT EXISTS idx_person_entity_count
    ON public.person (entity_count DESC, person_id ASC);
CREATE INDEX IF NOT EXISTS idx_person_identifier_count
    ON public.person (identifier_count DESC, person_id ASC);
CREATE INDEX IF NOT EXISTS idx_person_possible_match_count
    ON public.person (possible_match_count DESC, person_id ASC);
CREATE INDEX IF NOT EXISTS idx_person_system_match_count
    ON public.person (system_match_count DESC, person_id ASC);
CREATE INDEX IF NOT EXISTS idx_person_order_count
    ON public.person (order_count DESC, person_id ASC);
CREATE INDEX IF NOT EXISTS idx_person_crm_deal_count
    ON public.person (crm_deal_count DESC, person_id ASC);
CREATE INDEX IF NOT EXISTS idx_person_bankruptcy_case_count
    ON public.person (bankruptcy_case_count DESC, person_id ASC);
CREATE INDEX IF NOT EXISTS idx_person_phone_confidence
    ON public.person (phone_confidence DESC, person_id ASC);

-- Identifier lookup by type and value.
CREATE INDEX IF NOT EXISTS idx_person_identifier_lookup
    ON public.person_identifier (identifier_type, normalized_value);

-- Reverse connection lookup.
CREATE INDEX IF NOT EXISTS idx_person_connection_connected
    ON public.person_connection (connected_person_id);

-- Source-record listing, filtering and CRM-deal summary reads.
CREATE INDEX IF NOT EXISTS idx_source_record_person_observed
    ON public.source_record (person_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_record_entity_key ON public.source_record (entity_key);
CREATE INDEX IF NOT EXISTS idx_source_record_source_key ON public.source_record (source_key);
CREATE INDEX IF NOT EXISTS idx_source_record_type_observed
    ON public.source_record (record_type, observed_at DESC);

-- Identity-link stream ordering and snapshot keyset paging.
CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_link_rev_global_revision
    ON public.identity_link_rev (global_revision);
CREATE INDEX IF NOT EXISTS idx_identity_link_rev_link_global_revision
    ON public.identity_link_rev (link_key, global_revision DESC);

-- Singleton projection state. The row starts uninitialized: high_water and
-- last_success stay NULL and the summary stays NULL until the writer has
-- produced real values.
INSERT INTO public.summary_counter (
    id,
    high_water,
    last_success,
    current_revision,
    identity_stream_ready,
    summary
)
VALUES ('global', NULL, NULL, 0, false, NULL)
ON CONFLICT (id) DO NOTHING;
