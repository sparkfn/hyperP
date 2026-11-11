"""Additive schema for C1 identity assertions and skip ledgers."""

from __future__ import annotations

CREATE_IDENTITY_FOUNDATION_SCHEMA: tuple[str, ...] = (
    """CREATE CONSTRAINT source_record_assertion_id_unique IF NOT EXISTS
FOR ()-[assertion:ASSERTS]-() REQUIRE assertion.assertion_id IS UNIQUE""",
    """CREATE CONSTRAINT ingest_skip_id_unique IF NOT EXISTS
FOR (skip:IngestSkip) REQUIRE skip.skip_id IS UNIQUE""",
    """CREATE INDEX ingest_skip_run IF NOT EXISTS
FOR (skip:IngestSkip) ON (skip.ingest_run_id)""",
    """CREATE CONSTRAINT profile_fact_key_unique IF NOT EXISTS
FOR (fact:ProfileFact)
REQUIRE (fact.attribute_name, fact.attribute_value) IS UNIQUE""",
    """CREATE INDEX source_record_attribution_status IF NOT EXISTS
FOR (sr:SourceRecord) ON (sr.attribution_status)""",
    """CREATE INDEX source_record_reeval_pending IF NOT EXISTS
FOR (sr:SourceRecord) ON (sr.reeval_pending)""",
)
