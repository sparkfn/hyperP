"""Golden profile recompute and survivorship override endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from neo4j import AsyncManagedTransaction
from pydantic import BaseModel

from src.auth.deps import require_admin
from src.auth.models import AuthUser
from src.graph.client import get_session
from src.graph.converters import to_str
from src.graph.golden_profile import recompute_golden_profile_tx
from src.graph.queries import (
    CHECK_SOURCE_RECORD_LINKED,
    GET_FACT_VALUE,
    GET_PERSON_OVERRIDES_FULL,
    UPDATE_GOLDEN_FIELD,
    UPDATE_OVERRIDES,
)
from src.http_utils import envelope, http_error
from src.types import ApiResponse
from src.types_requests import SurvivorshipOverrideRequest

router = APIRouter()


class RecomputeResponse(BaseModel):
    person_id: str
    status: str
    profile_completeness_score: float


class OverrideResponse(BaseModel):
    person_id: str
    attribute_name: str
    selected_source_record_pk: str
    status: str


@router.post(
    "/v1/persons/{person_id}/golden-profile/recompute",
    response_model=ApiResponse[RecomputeResponse],
)
async def recompute_golden_profile(
    person_id: str,
    request: Request,
    _user: AuthUser = Depends(require_admin),
) -> ApiResponse[RecomputeResponse]:
    """Recompute a person's golden profile from HAS_FACT, IDENTIFIED_BY, and LIVES_AT."""
    async with get_session(write=True) as session:
        completeness = await session.execute_write(recompute_golden_profile_tx, person_id)
    if completeness is None:
        raise http_error(404, "person_not_found", "Person not found or not active.", request)
    return envelope(
        RecomputeResponse(
            person_id=person_id, status="recomputed", profile_completeness_score=completeness
        ),
        request,
    )


@router.post(
    "/v1/persons/{person_id}/survivorship-overrides",
    response_model=ApiResponse[OverrideResponse],
)
async def create_survivorship_override(
    person_id: str,
    body: SurvivorshipOverrideRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
) -> ApiResponse[OverrideResponse]:
    """Pin a golden-profile field value to a specific source record."""
    async with get_session(write=True) as session:
        outcome = await session.execute_write(
            _override_tx,
            person_id,
            body.attribute_name,
            body.selected_source_record_pk,
            body.reason,
            user.email,
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


def _fact_value_to_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


async def _override_tx(
    tx: AsyncManagedTransaction,
    person_id: str,
    attribute_name: str,
    source_record_pk: str,
    reason: str,
    actor_id: str,
) -> str:
    person_record = await (await tx.run(GET_PERSON_OVERRIDES_FULL, person_id=person_id)).single()
    if person_record is None:
        return "person_not_found"

    if (
        await (
            await tx.run(
                CHECK_SOURCE_RECORD_LINKED,
                source_record_pk=source_record_pk,
                person_id=person_id,
            )
        ).single()
        is None
    ):
        return "sr_not_found"

    bare_attr = attribute_name.removeprefix("preferred_")
    fact_record = await (
        await tx.run(
            GET_FACT_VALUE,
            person_id=person_id,
            attribute_name=bare_attr,
            source_record_pk=source_record_pk,
        )
    ).single()
    if fact_record is None:
        return "fact_not_found"

    overrides = _parse_overrides(person_record["overrides"])
    overrides[attribute_name] = {
        "source_record_pk": source_record_pk,
        "reason": reason,
        "actor_type": "admin",
        "actor_id": actor_id,
        "created_at": datetime.now(UTC).isoformat(),
    }
    await tx.run(UPDATE_OVERRIDES, person_id=person_id, overrides=overrides)

    field_name = (
        attribute_name if attribute_name.startswith("preferred_") else f"preferred_{attribute_name}"
    )
    selected_value = _fact_value_to_str(fact_record["value"])
    await tx.run(
        UPDATE_GOLDEN_FIELD,
        person_id=person_id,
        field_name=field_name,
        value=selected_value,
    )
    return "ok"
