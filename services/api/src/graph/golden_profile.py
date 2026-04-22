"""Golden profile recompute transaction — shared between survivorship and review routes."""

from __future__ import annotations

from datetime import UTC, datetime

from dataclasses import dataclass

from neo4j import AsyncManagedTransaction

from src.graph.converters import to_optional_str, to_str
from src.graph.queries import (
    CHECK_PERSON_ACTIVE,
    CREATE_RECOMPUTE_AUDIT,
    GET_BEST_ADDRESS,
    GET_BEST_IDENTIFIER,
    GET_PERSON_FACTS,
    GET_PERSON_OVERRIDES,
    UPDATE_GOLDEN_PROFILE,
)

TRUST_RANK: dict[str, int] = {"tier_1": 1, "tier_2": 2, "tier_3": 3, "tier_4": 4}
INVALID_QUALITY_FLAGS: frozenset[str] = frozenset({"invalid_format", "placeholder_value"})
GOLDEN_FACT_FIELDS: tuple[str, ...] = ("full_name", "phone", "email", "dob")


@dataclass
class _BestFact:
    value: str | None
    trust_rank: int
    observed_at: str


def _select_best_fact(current: _BestFact | None, candidate: _BestFact) -> _BestFact:
    if current is None:
        return candidate
    if candidate.trust_rank < current.trust_rank:
        return candidate
    if candidate.trust_rank == current.trust_rank and candidate.observed_at > current.observed_at:
        return candidate
    return current


def _fact_value_to_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _empty_fact() -> _BestFact:
    return _BestFact(value=None, trust_rank=99, observed_at="")


def _completeness_score(best_by_field: dict[str, _BestFact], has_address: bool) -> float:
    filled = sum(
        1 for f in GOLDEN_FACT_FIELDS
        if best_by_field.get(f) and best_by_field[f].value is not None
    )
    bonus = 1 if has_address else 0
    return (filled + bonus) / (len(GOLDEN_FACT_FIELDS) + 1)


async def _gather_best_facts(
    tx: AsyncManagedTransaction, person_id: str
) -> dict[str, _BestFact]:
    facts_result = await tx.run(GET_PERSON_FACTS, person_id=person_id)
    overrides_result = await tx.run(GET_PERSON_OVERRIDES, person_id=person_id)
    overrides_record = await overrides_result.single()
    overrides_raw = overrides_record["overrides"] if overrides_record else None
    overrides: dict[str, dict[str, str]] = {}
    if isinstance(overrides_raw, dict):
        for k, v in overrides_raw.items():
            if isinstance(v, dict):
                overrides[to_str(k)] = {to_str(ik): to_str(iv) for ik, iv in v.items()}

    best: dict[str, _BestFact] = {}
    async for record in facts_result:
        attr_name = to_str(record["attribute_name"])
        attr_value = _fact_value_to_str(record["attribute_value"])
        quality_flag = to_str(record["quality_flag"], "valid")
        trust_tier = to_str(record["trust_tier"], "tier_4")
        source_pk = to_str(record["source_record_pk"])
        observed_at = to_str(record["observed_at"], "")

        if quality_flag in INVALID_QUALITY_FLAGS:
            continue

        override = overrides.get(f"preferred_{attr_name}")
        if override is not None and override.get("source_record_pk") == source_pk:
            best[attr_name] = _BestFact(value=attr_value, trust_rank=0, observed_at=observed_at)
            continue

        rank = TRUST_RANK.get(trust_tier, 4)
        best[attr_name] = _select_best_fact(
            best.get(attr_name),
            _BestFact(value=attr_value, trust_rank=rank, observed_at=observed_at),
        )
    return best


async def _resolve_best_address(tx: AsyncManagedTransaction, person_id: str) -> str | None:
    address_result = await tx.run(GET_BEST_ADDRESS, person_id=person_id)
    record = await address_result.single()
    if record is None:
        return None
    return to_optional_str(record["address_id"])


async def _resolve_best_identifier(
    tx: AsyncManagedTransaction, person_id: str, identifier_type: str
) -> str | None:
    result = await tx.run(
        GET_BEST_IDENTIFIER, person_id=person_id, identifier_type=identifier_type
    )
    record = await result.single()
    if record is None:
        return None
    return to_optional_str(record["normalized_value"])


async def recompute_golden_profile_tx(
    tx: AsyncManagedTransaction, person_id: str
) -> float | None:
    """Recompute golden profile for person_id within a write transaction.

    Returns completeness score, or None if the person is not found / not active.
    """
    person_check = await tx.run(CHECK_PERSON_ACTIVE, person_id=person_id)
    if await person_check.single() is None:
        return None

    best_by_field = await _gather_best_facts(tx, person_id)
    preferred_address_id = await _resolve_best_address(tx, person_id)
    preferred_nric = await _resolve_best_identifier(tx, person_id, "nric")

    completeness = _completeness_score(best_by_field, preferred_address_id is not None)
    version = f"computed-{datetime.now(UTC).isoformat()}"
    await tx.run(
        UPDATE_GOLDEN_PROFILE,
        person_id=person_id,
        full_name=best_by_field.get("full_name", _empty_fact()).value,
        phone=best_by_field.get("phone", _empty_fact()).value,
        email=best_by_field.get("email", _empty_fact()).value,
        dob=best_by_field.get("dob", _empty_fact()).value,
        address_id=preferred_address_id,
        nric=preferred_nric,
        completeness=completeness,
        version=version,
    )
    await tx.run(CREATE_RECOMPUTE_AUDIT, person_id=person_id)
    return completeness
