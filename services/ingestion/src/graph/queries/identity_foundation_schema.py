"""Additive schema for C1 identity assertions, C3 identity links, and skip ledgers."""

from __future__ import annotations

CREATE_IDENTITY_FOUNDATION_SCHEMA: tuple[str, ...] = (
    """CREATE CONSTRAINT source_record_assertion_id_unique IF NOT EXISTS
FOR ()-[assertion:ASSERTS]-() REQUIRE assertion.assertion_id IS UNIQUE""",
    """CREATE CONSTRAINT ingest_skip_id_unique IF NOT EXISTS
FOR (skip:IngestSkip) REQUIRE skip.skip_id IS UNIQUE""",
    """CREATE CONSTRAINT ingest_run_attempt_key_unique IF NOT EXISTS
FOR (attempt:IngestRunAttempt)
REQUIRE (attempt.ingest_run_id, attempt.attempt_key) IS UNIQUE""",
    """CREATE INDEX ingest_skip_run IF NOT EXISTS
FOR (skip:IngestSkip) ON (skip.ingest_run_id)""",
    """CREATE CONSTRAINT profile_fact_key_unique IF NOT EXISTS
FOR (fact:ProfileFact)
REQUIRE (fact.attribute_name, fact.attribute_value) IS UNIQUE""",
    """CREATE INDEX source_record_attribution_status IF NOT EXISTS
FOR (sr:SourceRecord) ON (sr.attribution_status)""",
    """CREATE INDEX source_record_reeval_pending IF NOT EXISTS
FOR (sr:SourceRecord) ON (sr.reeval_pending)""",
    """CREATE CONSTRAINT profile_id_unique IF NOT EXISTS
FOR (profile:Profile) REQUIRE profile.profile_id IS UNIQUE""",
    """CREATE CONSTRAINT review_case_case_key_unique IF NOT EXISTS
FOR (rc:ReviewCase) REQUIRE rc.case_key IS UNIQUE""",
    """CREATE CONSTRAINT person_nric_key_unique IF NOT EXISTS
FOR (person:Person) REQUIRE person.nric_key IS UNIQUE""",
    """CREATE CONSTRAINT has_identifier_link_id_unique IF NOT EXISTS
FOR ()-[link:HAS_IDENTIFIER]-() REQUIRE link.link_id IS UNIQUE""",
    """CREATE INDEX profile_status_materialized_at IF NOT EXISTS
FOR (profile:Profile) ON (profile.status, profile.materialized_at)""",
)
