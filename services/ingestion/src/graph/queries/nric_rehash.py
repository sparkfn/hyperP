"""Read-only census and verification queries for the NRIC rehash CLI."""

from __future__ import annotations

NRIC_IDENTIFIER_CENSUS = """
MATCH (i:Identifier {identifier_type: 'nric'})
RETURN count(i) AS total,
       coalesce(sum(CASE
         WHEN i.normalized_value =~ '^[0-9a-f]{64}$' THEN 1 ELSE 0
       END), 0) AS digest_shaped,
       coalesce(sum(CASE
         WHEN i.normalized_value =~ '^[0-9a-f]{64}$' THEN 0 ELSE 1
       END), 0) AS non_digest,
       coalesce(sum(CASE WHEN i.identifier_id IS NULL THEN 1 ELSE 0 END), 0)
         AS missing_ids,
       coalesce(sum(CASE WHEN i.identifier_scope IS NULL THEN 1 ELSE 0 END), 0)
         AS missing_scopes,
       coalesce(sum(CASE
         WHEN i.identifier_scope IS NOT NULL AND i.identifier_scope <> 'global'
           THEN 1 ELSE 0
       END), 0) AS unexpected_scopes
"""

NRIC_IDENTIFIER_PAGE = """
MATCH (i:Identifier {identifier_type: 'nric'})
WHERE $after_id IS NULL OR i.identifier_id > $after_id
RETURN i.identifier_id AS identifier_id,
       i.identifier_scope AS identifier_scope,
       i.normalized_value AS normalized_value,
       i.hashed_value AS hashed_value,
       i.nric_masked AS nric_masked,
       i.checksum_valid AS checksum_valid,
       i.nric_key_version AS nric_key_version,
       i.nric_encrypted AS nric_encrypted
ORDER BY identifier_id
LIMIT $batch_size
"""

NRIC_VERIFY_IDENTIFIERS = """
MATCH (i:Identifier {identifier_type: 'nric'})
RETURN count(i) AS identifier_count,
       coalesce(sum(CASE
         WHEN i.normalized_value =~ '^[0-9a-f]{64}$' THEN 0 ELSE 1
       END), 0) AS non_digest_count,
       coalesce(sum(CASE
         WHEN i.nric_masked IS NULL OR i.checksum_valid IS NULL
           OR i.nric_key_version IS NULL OR i.nric_encrypted IS NULL
           OR i.nric_key_version <> $key_version
         THEN 1 ELSE 0
       END), 0) AS missing_metadata_count
"""

NRIC_VERIFY_DUPLICATE_PERSON_KEYS = """
MATCH (p:Person)
WHERE p.nric_key IS NOT NULL
WITH p.nric_key AS key, count(*) AS owners
WHERE owners > 1
RETURN count(*) AS duplicate_key_groups,
       coalesce(sum(owners - 1), 0) AS duplicate_extra_owners
"""

NRIC_VERIFY_DUPLICATE_SCOPED_TARGETS = """
MATCH (i:Identifier {identifier_type: 'nric'})
WHERE i.identifier_scope = 'global' OR i.identifier_scope IS NULL
WITH i.normalized_value AS normalized_value, count(i) AS identifiers
WHERE identifiers > 1
RETURN count(*) AS duplicate_scoped_targets
"""
