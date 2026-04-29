"""Golden profile recompute and survivorship override endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from src.auth.deps import require_admin
from src.auth.models import AuthUser
from src.http_utils import envelope, http_error
from src.repositories.deps import get_survivorship_repo
from src.repositories.protocols.survivorship import SurvivorshipRepository
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
    repo: SurvivorshipRepository = Depends(get_survivorship_repo),
) -> ApiResponse[RecomputeResponse]:
    """Recompute a person's golden profile from HAS_FACT, IDENTIFIED_BY, and LIVES_AT."""
    completeness = await repo.recompute_golden_profile(person_id)
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
    repo: SurvivorshipRepository = Depends(get_survivorship_repo),
) -> ApiResponse[OverrideResponse]:
    """Pin a golden-profile field value to a specific source record."""
    outcome = await repo.create_override(
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
