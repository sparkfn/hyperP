"""Split graph deal and bounded live Bitrix activity metrics endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from src.auth.deps import require_scope
from src.config import config
from src.http_utils import envelope, http_error
from src.repositories.deps import get_crm_activity_metrics_repo, get_crm_deal_metrics_repo
from src.repositories.protocols.crm import CrmDealMetricsRepository
from src.repositories.protocols.crm_activity import CrmActivityMetricsRepository
from src.types import ApiResponse
from src.types_crm import PersonCrmActivityMetrics, PersonCrmDealMetrics

router = APIRouter(
    prefix="/v1/persons", tags=["Persons"], dependencies=[Depends(require_scope("persons:read"))]
)


@router.get(
    "/{person_id}/crm/deal-metrics",
    response_model=ApiResponse[PersonCrmDealMetrics],
    operation_id="get_person_crm_deal_metrics",
)
async def get_person_crm_deal_metrics(
    person_id: str,
    request: Request,
    repo: CrmDealMetricsRepository = Depends(get_crm_deal_metrics_repo),
) -> ApiResponse[PersonCrmDealMetrics]:
    metrics = await repo.get_person_crm_deal_metrics(person_id)
    if metrics is None:
        raise http_error(404, "person_not_found", "Person not found.", request)
    return envelope(metrics, request)


@router.get(
    "/{person_id}/crm/activity-metrics",
    response_model=ApiResponse[PersonCrmActivityMetrics],
    operation_id="get_person_crm_activity_metrics",
)
async def get_person_crm_activity_metrics(
    person_id: str,
    request: Request,
    deal_repo: CrmDealMetricsRepository = Depends(get_crm_deal_metrics_repo),
    activity_repo: CrmActivityMetricsRepository = Depends(get_crm_activity_metrics_repo),
) -> ApiResponse[PersonCrmActivityMetrics]:
    scope = await deal_repo.resolve_bitrix_deal_scope(
        person_id, config.bitrix_activity_source_instance, config.bitrix_activity_deal_limit
    )
    if scope is None:
        raise http_error(404, "person_not_found", "Person not found.", request)
    return envelope(await activity_repo.get_person_crm_activity_metrics(scope), request)
