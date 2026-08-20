"""CRM metrics endpoint for the persons resource."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from src.auth.deps import require_scope
from src.http_utils import envelope, http_error
from src.repositories.deps import get_crm_metrics_repo
from src.repositories.protocols.crm import CrmMetricsRepository
from src.types import ApiResponse
from src.types_crm import PersonCrmMetrics

router = APIRouter(
    prefix="/v1/persons",
    tags=["Persons"],
    dependencies=[Depends(require_scope("persons:read"))],
)


@router.get("/{person_id}/crm/metrics", response_model=ApiResponse[PersonCrmMetrics])
async def get_person_crm_metrics(
    person_id: str,
    request: Request,
    repo: CrmMetricsRepository = Depends(get_crm_metrics_repo),
) -> ApiResponse[PersonCrmMetrics]:
    """Return aggregate CRM engagement metrics for a person."""
    metrics = await repo.get_person_crm_metrics(person_id)
    if metrics is None:
        raise http_error(404, "person_not_found", "Person not found.", request)
    return envelope(metrics, request)
