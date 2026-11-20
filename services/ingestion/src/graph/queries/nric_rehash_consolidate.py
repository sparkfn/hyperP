"""Parameterized Cypher for NRIC collision consolidation."""

from __future__ import annotations

NRIC_REHASH_EVIDENCE_COUNTS = """
CALL {
    MATCH (:Identifier {identifier_type: 'nric'})-[rel:ASSERTS]-()
    RETURN count(DISTINCT rel) AS asserts
}
CALL {
    MATCH (:Identifier {identifier_type: 'nric'})-[rel:IDENTIFIED_BY]-()
    RETURN count(DISTINCT rel) AS identified_by
}
CALL {
    MATCH (:Identifier {identifier_type: 'nric'})-[rel:ANCHORED_BY]-()
    RETURN count(DISTINCT rel) AS anchored_by
}
CALL {
    MATCH (:Identifier {identifier_type: 'nric'})-[rel:MOVED_RELATIONSHIP]-()
    RETURN count(DISTINCT rel) AS moved
}
CALL {
    MATCH (:Identifier {identifier_type: 'nric'})-[rel:HAS_IDENTIFIER]-()
    RETURN count(DISTINCT rel) AS has_identifier
}
CALL {
    MATCH (:SourceRecord)-[link:LINKED_TO]->(:Person)
    WHERE coalesce(link.is_active, true) = true
    RETURN count(DISTINCT link) AS active_links
}
RETURN asserts, identified_by, anchored_by, moved, has_identifier, active_links
"""

NRIC_REHASH_LOCK_CONSOLIDATION_IDENTIFIERS = """
MATCH (identifier:Identifier)
WHERE elementId(identifier) IN $element_ids
WITH identifier
ORDER BY elementId(identifier)
SET identifier.identifier_id = identifier.identifier_id
RETURN elementId(identifier) AS element_id,
       identifier.identifier_type AS identifier_type,
       identifier.identifier_scope AS identifier_scope,
       identifier.normalized_value AS normalized_value,
       identifier.hashed_value AS hashed_value,
       identifier.nric_masked AS nric_masked,
       identifier.checksum_valid AS checksum_valid,
       identifier.nric_key_version AS nric_key_version,
       identifier.nric_encrypted AS nric_encrypted
"""

NRIC_REHASH_INVENTORY_CONSOLIDATION_RELATIONSHIPS = """
MATCH (loser:Identifier)
WHERE elementId(loser) = $loser
OPTIONAL MATCH (loser)-[rel]-(other)
RETURN type(rel) AS relationship_type,
       startNode(rel) = loser AS loser_is_start,
       labels(other) AS other_labels,
       count(DISTINCT rel) AS relationship_count
"""

NRIC_REHASH_CONSOLIDATION_PARTICIPANTS = """
MATCH (participant)-[:HAS_IDENTIFIER]->(identifier:Identifier)
WHERE elementId(identifier) IN [$loser, $survivor]
RETURN DISTINCT labels(participant) AS participant_labels,
       CASE
         WHEN participant:Person THEN 'person'
         WHEN participant:Profile THEN 'profile'
         ELSE null
       END AS participant_kind,
       CASE
         WHEN participant:Person THEN participant.person_id
         WHEN participant:Profile THEN participant.profile_id
         ELSE null
       END AS participant_id
ORDER BY participant_kind, participant_id
"""

NRIC_REHASH_MOVE_ASSERTS = """
MATCH (source:SourceRecord)-[old:ASSERTS]->(loser:Identifier)
WHERE elementId(loser) = $loser
MATCH (survivor:Identifier)
WHERE elementId(survivor) = $survivor
WITH source, old, survivor, properties(old) AS saved
DELETE old
CREATE (source)-[replacement:ASSERTS]->(survivor)
SET replacement = saved
RETURN count(replacement) AS moved
"""

NRIC_REHASH_MOVE_IDENTIFIED_BY = """
MATCH (person:Person)-[old:IDENTIFIED_BY]->(loser:Identifier)
WHERE elementId(loser) = $loser
MATCH (survivor:Identifier)
WHERE elementId(survivor) = $survivor
WITH person, old, survivor, properties(old) AS saved
DELETE old
CREATE (person)-[replacement:IDENTIFIED_BY]->(survivor)
SET replacement = saved
RETURN count(replacement) AS moved
"""

NRIC_REHASH_MOVE_ANCHORED_BY = """
MATCH (person:Person)-[old:ANCHORED_BY]->(loser:Identifier)
WHERE elementId(loser) = $loser
MATCH (survivor:Identifier)
WHERE elementId(survivor) = $survivor
WITH person, old, survivor, properties(old) AS saved
DELETE old
CREATE (person)-[replacement:ANCHORED_BY]->(survivor)
SET replacement = saved
RETURN count(replacement) AS moved
"""

NRIC_REHASH_MOVE_MOVED_RELATIONSHIP = """
MATCH (event:MergeEvent)-[old:MOVED_RELATIONSHIP]->(loser:Identifier)
WHERE elementId(loser) = $loser
MATCH (survivor:Identifier)
WHERE elementId(survivor) = $survivor
WITH event, old, survivor, properties(old) AS saved
DELETE old
CREATE (event)-[replacement:MOVED_RELATIONSHIP]->(survivor)
SET replacement = saved
RETURN count(replacement) AS moved
"""

NRIC_REHASH_DELETE_CONSOLIDATED_IDENTIFIER = """
MATCH (loser:Identifier)
WHERE elementId(loser) = $loser
  AND loser.identifier_type = 'nric'
  AND NOT EXISTS { MATCH (loser)--() }
DELETE loser
RETURN count(*) AS deleted
"""

NRIC_REHASH_VERIFY_IDENTITY_LINK_HEAD_TAILS = """
MATCH (head:IdentityLinkHead)
OPTIONAL MATCH (tail:IdentityLinkRevision {link_key: head.link_key})
WITH head, tail
ORDER BY tail.global_revision DESC
WITH head, collect(tail.event_id)[0] AS tail_event_id
RETURN count(head) AS head_count,
       coalesce(sum(CASE
         WHEN tail_event_id IS NULL OR head.latest_event_id <> tail_event_id THEN 1
         ELSE 0
       END), 0) AS mismatch_count
"""

NRIC_REHASH_UPDATE_SURVIVOR_METADATA = """
MATCH (survivor:Identifier)
WHERE elementId(survivor) = $survivor
  AND survivor.identifier_type = 'nric'
  AND survivor.identifier_scope = 'global'
  AND survivor.normalized_value = $digest
SET survivor.hashed_value = $hashed_value,
    survivor.nric_key_version = $key_version,
    survivor.nric_masked = $masked,
    survivor.checksum_valid = $checksum_valid,
    survivor.nric_encrypted = $ciphertext
RETURN count(survivor) AS updated
"""
