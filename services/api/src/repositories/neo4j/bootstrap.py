"""Neo4j bootstrap required before repositories accept traffic."""

from src.graph.client import get_session
from src.graph.queries.identity_link_revisions import CREATE_IDENTITY_LINK_SCHEMA
from src.graph.queries.person_list_summary import (
    CREATE_PERSON_LIST_SUMMARY_COUNTER_CONSTRAINT,
    ENSURE_PERSON_LIST_SUMMARY_COUNTER,
    PERSON_LIST_SUMMARY_COUNTER_KEY,
)
from src.graph.queries.source_records import CREATE_SOURCE_RECORD_IDENTITY_LOCK_CONSTRAINT


async def ensure_source_record_identity_lock_constraint() -> None:
    """Install review serialization and identity-link stream schema prerequisites."""
    async with get_session(write=True) as session:
        await session.run(CREATE_SOURCE_RECORD_IDENTITY_LOCK_CONSTRAINT)
        for statement in CREATE_IDENTITY_LINK_SCHEMA:
            await session.run(statement)


async def ensure_person_list_summary_counter() -> None:
    """Install the person-list summary counter schema and its global singleton.

    The singleton is created unready; publication of the six counts happens
    only through the explicit backfill/reconciliation path, so neither this
    bootstrap nor an ordinary restart can promote an unready counter.
    """
    async with get_session(write=True) as session:
        await session.run(CREATE_PERSON_LIST_SUMMARY_COUNTER_CONSTRAINT)
        await session.run(
            ENSURE_PERSON_LIST_SUMMARY_COUNTER,
            key=PERSON_LIST_SUMMARY_COUNTER_KEY,
        )
