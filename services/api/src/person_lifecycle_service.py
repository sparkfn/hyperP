"""Trusted internal boundary for Person retirement; no HTTP route is exposed."""

from __future__ import annotations

from src.repositories.neo4j.person_lifecycle import Neo4jPersonLifecycleRepository


async def retire_person(person_id: str, reason: str, actor_id: str) -> bool:
    """Atomically retire one active Person and all current exported links."""
    return await Neo4jPersonLifecycleRepository().retire_person(person_id, reason, actor_id)
