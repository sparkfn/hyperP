"""Golden profile recompute and survivorship override endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from neo4j import AsyncManagedTransaction
from pydantic import BaseModel

from src.graph.client import get_session
from src.graph.converters import GraphValue, to_optional_str, to_str
from src.graph.queries import (
    CHECK_PERSON_ACTIVE,
    CHECK_SOURCE_RECORD_LINKED,
    CREATE_RECOMPUTE_AUDIT,
    GET_BEST_ADDRESS,
    GET_FACT_VALUE,
    GET_PERSON_FACTS,
    GET_PERSON_OVERRIDES,
    GET_PERSON_OVERRIDES_FULL,
    UPDATE_GOLDEN_FIELD,
    UPDATE_GOLDEN_PROFILE,
    UPDATE_OVERRIDES,
)
from src.http_utils import envelope, http_error
from src.types import ApiResponse, SurvivorshipOverrideRequest

router = APIRouter()

GOLDEN_FIELDS: tuple[str, ...] = ("full_name", "phone", "email", "dob")
TRUST_RANK: dict[str, int] = {"tier_1": 1, "tier_2": 2, "tier_3": 3, "tier_4": 4}
INVALID_QUALITY_FLAGS: frozenset[str] = frozenset({"invalid_format", "placeholder_value"})


class _BestFact(BaseModel):
    value: str | None
    trust_rank: int
    observed_at: str


class RecomputeResponse(BaseModel):
    person_id: str
    status: str
    profile_completeness_score: float


class OverrideResponse(BaseModel):
    person_id: str
    attribute_name: str
    selected_source_record_pk: str
    status: str


def _select_best_fact(current: _BestFact | None, candidate: _BestFact) -> _BestFact:
    if current is None:
        return candidate
    if candidate.trust_rank < current.trust_rank:
        return candidate
    if candidate.trust_rank == current.trust_rank and candidate.observed_at > current.observed_at:
        return candidate
    return current


def _fact_value_to_str(value: GraphValue) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _empty_fact() -> _BestFact:
    return _BestFact(value=None, trust_rank=99, observed_at="")


@router.post(
    "/v1/persons/{person_id}/golden-profile/recompute",
    response_model=ApiResponse[RecomputeResponse],
)
async def recompute_golden_profile(
    person_id: str, request: Request
) -> ApiResponse[RecomputeResponse]:
    """Recompute a person's golden profile from HAS_FACT and LIVES_AT relationships."""
    async with get_session(write=True) as session:
        completeness = await session.execute_write(_recompute_tx, person_id)
    if completeness is None:
        raise http_error(404, "person_not_found", "Person not found or not active.", request)
    return envelope(
        RecomputeResponse(
            person_id=person_id, status="recomputed", profile_completeness_score=completeness
        ),
        request,
    )


async def _recompute_tx(tx: AsyncManagedTransaction, person_id: str) -> float | None:
    person_check = await tx.run(CHECK_PERSON_ACTIVE, person_id=person_id)
    if await person_check.single() is None:
        return None

    best_by_field = await _gather_best_facts(tx, person_id)
    preferred_address_id = await _resolve_best_address(tx, person_id)

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
        completeness=completeness,
        version=version,
    )
    await tx.run(CREATE_RECOMPUTE_AUDIT, person_id=person_id)
    return completeness


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


def _completeness_score(best_by_field: dict[str, _BestFact], has_address: bool) -> float:
    filled = sum(
        1 for f in GOLDEN_FIELDS if best_by_field.get(f) and best_by_field[f].value is not None
    )
    bonus = 1 if has_address else 0
    return (filled + bonus) / (len(GOLDEN_FIELDS) + 1)


@router.post(
    "/v1/persons/{person_id}/survivorship-overrides",
    response_model=ApiResponse[OverrideResponse],
)
async def create_survivorship_override(
    person_id: str, body: SurvivorshipOverrideRequest, request: Request
) -> ApiResponse[OverrideResponse]:
    """Pin a golden-profile field value to a specific source record."""
    async with get_session(write=True) as session:
        outcome = await session.execute_write(
            _override_tx,
            person_id,
            body.attribute_name,
            body.selected_source_record_pk,
            body.reason,
        )

    if outcome == "person_not_found":
        raise http_error(404, "person_not_found", "Person not found or not active.", request)
    if outcome == "sr_not_found":
        raise http_error(
            404, "not_found", "Source record not found or not linked to this person.", request
        )
    if outcome == "fact_not_found":
        raise http_error(
            422,
            "unprocessable_entity",
            "No attribute fact found for the given attribute_name on the selected source record.",
            request,
        )

    return envelope(
        OverrideResponse(
            person_id=person_id,
            attribute_name=body.attribute_name,
            selected_source_record_pk=body.selected_source_record_pk,
            status="applied",
        ),
        request,
    )


def _parse_overrides(raw: object) -> dict[str, dict[str, str]]:
    """Parse existing overrides map from a Neo4j property."""
    if not isinstance(raw, dict):
        return {}
    return {
        to_str(k): {to_str(ik): to_str(iv) for ik, iv in v.items()}
        for k, v in raw.items()
        if isinstance(v, dict)
    }


async def _override_tx(
    tx: AsyncManagedTransaction,
    person_id: str,
    attribute_name: str,
    source_record_pk: str,
    reason: str,
) -> str:
    person_record = await (await tx.run(GET_PERSON_OVERRIDES_FULL, person_id=person_id)).single()
    if person_record is None:
        return "person_not_found"

    if await (await tx.run(
        CHECK_SOURCE_RECORD_LINKED, source_record_pk=source_record_pk, person_id=person_id,
    )).single() is None:
        return "sr_not_found"

    bare_attr = attribute_name.removeprefix("preferred_")
    fact_record = await (await tx.run(
        GET_FACT_VALUE, person_id=person_id,
        attribute_name=bare_attr, source_record_pk=source_record_pk,
    )).single()
    if fact_record is None:
        return "fact_not_found"

    overrides = _parse_overrides(person_record["overrides"])
    overrides[attribute_name] = {
        "source_record_pk": source_record_pk, "reason": reason,
        "actor_type": "reviewer", "actor_id": "current_user",
        "created_at": datetime.now(UTC).isoformat(),
    }
    await tx.run(UPDATE_OVERRIDES, person_id=person_id, overrides=overrides)

    field_name = (
        attribute_name if attribute_name.startswith("preferred_")
        else f"preferred_{attribute_name}"
    )
    selected_value = _fact_value_to_str(fact_record["value"])
    await tx.run(
        UPDATE_GOLDEN_FIELD, person_id=person_id,
        field_name=field_name, value=selected_value,
    )
    return "ok"
