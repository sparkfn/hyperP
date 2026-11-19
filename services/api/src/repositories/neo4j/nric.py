"""Neo4j implementation of NricRevealRepository."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from neo4j import AsyncManagedTransaction

from src.graph.client import get_session
from src.graph.converters import to_optional_str, to_str
from src.graph.queries.nric import CREATE_NRIC_REVEAL_EVENT, GET_PERSON_NRIC_IDENTIFIERS
from src.nric import decrypt_nric, is_nric_digest, scrub_nric_text
from src.repositories.protocols.nric import (
    NricRevealRepository,
    NricRevealResult,
    NricUnavailableError,
)


class Neo4jNricRevealRepository(NricRevealRepository):
    """Neo4j repository for audited NRIC reveals."""

    async def reveal(
        self,
        person_id: str,
        actor_email: str,
        reason: str,
        request_id: str,
    ) -> NricRevealResult | None:
        async with get_session(write=True) as session:
            return await session.execute_write(
                _reveal_tx, person_id, actor_email, reason, request_id
            )


async def _reveal_tx(
    tx: AsyncManagedTransaction,
    person_id: str,
    actor_email: str,
    reason: str,
    request_id: str,
) -> NricRevealResult | None:
    result = await tx.run(GET_PERSON_NRIC_IDENTIFIERS, person_id=person_id)
    record = await result.single()
    if record is None:
        return None

    raw_identifiers = record.get("identifiers")
    raw_values: list[str] = []
    unavailable_digests: set[str] = set()

    if isinstance(raw_identifiers, list):
        for raw in raw_identifiers:
            if not isinstance(raw, dict):
                continue
            norm_val = to_str(raw.get("normalized_value"))
            ciphertext = to_optional_str(raw.get("nric_encrypted"))
            if is_nric_digest(norm_val):
                if ciphertext:
                    decrypted = decrypt_nric(ciphertext)
                    raw_values.append(decrypted)
                else:
                    unavailable_digests.add(norm_val)
            else:
                raw_values.append(norm_val)

    # Deduplicate resolved values (sorted, unique)
    values = sorted(list(dict.fromkeys(raw_values)))
    value_count = len(values)
    unavailable_count = len(unavailable_digests)

    if value_count == 0 and unavailable_count > 0:
        raise NricUnavailableError("All NRIC values are unavailable.")

    reveal_id = str(uuid4())
    revealed_at = datetime.now(UTC).isoformat()
    scrubbed_reason = scrub_nric_text(reason)

    await (
        await tx.run(
            CREATE_NRIC_REVEAL_EVENT,
            reveal_id=reveal_id,
            person_id=person_id,
            actor_email=actor_email,
            reason=scrubbed_reason,
            request_id=request_id,
            revealed_at=revealed_at,
            value_count=value_count,
            unavailable_count=unavailable_count,
        )
    ).consume()

    return NricRevealResult(
        person_id=person_id,
        reveal_id=reveal_id,
        revealed_at=revealed_at,
        values=values,
        unavailable_count=unavailable_count,
    )
