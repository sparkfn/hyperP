"""One-off maintenance / migration Cypher run at ingestion startup.

These statements are idempotent so they can be applied on every run (after the
schema and entity/source bootstrap) without harm.
"""

from __future__ import annotations

#: Reclassify legacy ``record_type = 'system'`` SourceRecords into the three
#: subtypes that replaced ``system`` (identity / public_record / relationship).
#: Keyed on ``source_system`` so the mapping matches what the connectors now
#: emit. Idempotent: after the first pass no ``'system'`` rows remain.
BACKFILL_RECORD_TYPE_SUBTYPES = """
MATCH (sr:SourceRecord)
WHERE sr.record_type = 'system'
SET sr.record_type = CASE
    WHEN sr.source_system ENDS WITH ':contacts' THEN 'relationship'
    WHEN sr.source_system IN ['sgbankruptcy', 'sgrentalflats'] THEN 'public_record'
    ELSE 'identity'
END
RETURN count(sr) AS updated
"""
