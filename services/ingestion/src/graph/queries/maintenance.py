"""One-off maintenance / migration Cypher run at ingestion startup.

These statements are idempotent so they can be applied on every run (after the
schema and entity/source bootstrap) without harm.
"""

from __future__ import annotations

#: Reclassify legacy ``record_type = 'system'`` and intermediate
#: ``record_type = 'public_record'`` SourceRecords into the current subtypes
#: (identity / bankruptcy / rental_flat / relationship). Keyed on
#: ``source_system`` so the mapping matches what the connectors now emit.
#: Idempotent: after the first pass no ``'system'`` / ``'public_record'`` rows
#: remain, so a re-run updates 0.
BACKFILL_RECORD_TYPE_SUBTYPES = """
MATCH (sr:SourceRecord)
WHERE sr.record_type IN ['system', 'public_record']
SET sr.record_type = CASE
    WHEN sr.source_system ENDS WITH ':contacts' THEN 'relationship'
    WHEN sr.source_system = 'sgbankruptcy'  THEN 'bankruptcy'
    WHEN sr.source_system = 'sgrentalflats' THEN 'rental_flat'
    ELSE 'identity'
END
RETURN count(sr) AS updated
"""
