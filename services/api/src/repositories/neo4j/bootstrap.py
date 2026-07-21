"""Neo4j bootstrap required before repositories accept traffic."""

from src.graph.client import get_session
from src.graph.queries.ingestion import CREATE_INGEST_RUN_IDEMPOTENCY_CONSTRAINT
from src.graph.queries.source_records import CREATE_SOURCE_RECORD_IDENTITY_LOCK_CONSTRAINT


async def ensure_ingest_run_idempotency_constraint() -> None:
    """Install the tuple constraint that makes run creation idempotent."""
    async with get_session(write=True) as session:
        await session.run(CREATE_INGEST_RUN_IDEMPOTENCY_CONSTRAINT)


async def ensure_source_record_identity_lock_constraint() -> None:
    """Install the tuple constraint used to serialize review activation."""
    async with get_session(write=True) as session:
        await session.run(CREATE_SOURCE_RECORD_IDENTITY_LOCK_CONSTRAINT)
