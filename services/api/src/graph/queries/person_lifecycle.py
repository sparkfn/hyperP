"""Cypher for internal Person retirement."""

RETIRE_PERSON = """
MATCH (person:Person {person_id: $person_id, status: 'active'})
SET person.status = 'retired', person.retired_at = datetime(), person.updated_at = datetime()
CREATE (event:MergeEvent {merge_event_id: randomUUID(), event_type: 'person_retired',
  actor_type: 'system', actor_id: $actor_id, reason: $reason, created_at: datetime()})
CREATE (event)-[:AFFECTED_PERSON]->(person)
RETURN event.merge_event_id AS lifecycle_event_id, toString(person.retired_at) AS retired_at
"""
