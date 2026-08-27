"""Neo4j bootstrap required before repositories accept traffic."""

from src.graph.client import get_session
from src.graph.queries.identity_link_revisions import CREATE_IDENTITY_LINK_SCHEMA
from src.graph.queries.source_records import CREATE_SOURCE_RECORD_IDENTITY_LOCK_CONSTRAINT


async def ensure_source_record_identity_lock_constraint() -> None:
    """Install review serialization and identity-link stream schema prerequisites."""
    async with get_session(write=True) as session:
        await session.run(CREATE_SOURCE_RECORD_IDENTITY_LOCK_CONSTRAINT)
        for statement in CREATE_IDENTITY_LINK_SCHEMA:
            await session.run(statement)
