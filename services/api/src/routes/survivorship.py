"""Golden profile recompute and survivorship override endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from src.auth.deps import require_admin
from src.auth.models import AuthUser
from src.display_format import format_display_date, format_display_datetime
from src.http_utils import envelope, http_error
from src.repositories.deps import get_survivorship_repo
from src.repositories.protocols.survivorship import FieldOptionsData, SurvivorshipRepository
from src.types import (
    ApiResponse,
    EditableFieldOptions,
    FieldOption,
    GoldenFieldName,
    GoldenSourceKind,
    PersonFieldOptions,
)
from src.types_requests import SurvivorshipOverrideBatchRequest, SurvivorshipOverrideRequest

router = APIRouter(tags=["Persons"])

#: Editable fields in display order: (field_name, label, source_kind).
_FIELD_META: tuple[tuple[GoldenFieldName, str, GoldenSourceKind], ...] = (
    ("preferred_full_name", "Full name", "source_record_fact"),
    ("preferred_phone", "Phone", "identifier"),
    ("preferred_email", "Email", "identifier"),
    ("preferred_dob", "Date of birth", "source_record_fact"),
    ("preferred_nric", "NRIC", "identifier"),
    ("preferred_address", "Address", "address"),
)


class RecomputeResponse(BaseModel):
    person_id: str
    status: str
    profile_completeness_score: float


class OverrideResponse(BaseModel):
    person_id: str
    field_name: str
    source_record_pk: str
    status: str


class BatchOverrideResponse(BaseModel):
    person_id: str
    applied: list[str]
    status: str


def _value_display(field_name: str, value: str) -> str:
    if field_name == "preferred_dob":
        return format_display_date(value) or value
    return value


def _current_value(data: FieldOptionsData, field_name: str) -> str | None:
    return {
        "preferred_full_name": data.preferred_full_name,
        "preferred_phone": data.preferred_phone,
        "preferred_email": data.preferred_email,
        "preferred_dob": data.preferred_dob,
        "preferred_nric": data.preferred_nric,
    }.get(field_name)


def _build_field_options(data: FieldOptionsData) -> PersonFieldOptions:
    fields: list[EditableFieldOptions] = []
    for field_name, label, source_kind in _FIELD_META:
        current_value = _current_value(data, field_name)
        seen: set[tuple[str, str]] = set()
        options: list[FieldOption] = []
        for row in data.options:
            if row.field_name != field_name:
                continue
            dedup_key = (row.source_record_pk, row.value)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            if field_name == "preferred_address":
                is_current = (
                    data.preferred_address_id is not None
                    and row.address_id == data.preferred_address_id
                )
            else:
                is_current = current_value is not None and row.value == current_value
            observed_display = format_display_datetime(row.observed_at) if row.observed_at else None
            options.append(
                FieldOption(
                    # source_kind is produced as a fixed literal by GET_FIELD_OPTIONS,
                    # but flows through the repo dataclass as a plain str.
                    source_record_pk=row.source_record_pk,
                    source_kind=row.source_kind,  # type: ignore[arg-type]
                    identifier_type=row.identifier_type,
                    value=row.value,
                    value_display=_value_display(field_name, row.value),
                    source_system=row.source_system,
                    entity_display_name=row.entity_display_name,
                    observed_at_display=observed_display,
                    is_current=is_current,
                )
            )
        options.sort(key=lambda o: (not o.is_current, o.source_system, o.value))

        if field_name == "preferred_address":
            current_display = next((o.value_display for o in options if o.is_current), None)
        else:
            current_display = (
                _value_display(field_name, current_value) if current_value is not None else None
            )

        fields.append(
            EditableFieldOptions(
                field_name=field_name,
                label=label,
                source_kind=source_kind,
                current_value_display=current_display,
                is_overridden=field_name in data.overrides,
                options=options,
            )
        )
    return PersonFieldOptions(person_id=data.person_id, fields=fields)


@router.get(
    "/v1/persons/{person_id}/field-options",
    response_model=ApiResponse[PersonFieldOptions],
)
async def get_person_field_options(
    person_id: str,
    request: Request,
    repo: SurvivorshipRepository = Depends(get_survivorship_repo),
) -> ApiResponse[PersonFieldOptions]:
    """Editable golden-profile fields and the source values selectable for each."""
    data = await repo.get_field_options(person_id)
    if data is None:
        raise http_error(404, "person_not_found", "Person not found or not active.", request)
    return envelope(_build_field_options(data), request)


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


def _override_error(outcome: str, request: Request, field_name: str | None = None) -> None:
    suffix = f" ({field_name})" if field_name else ""
    if outcome == "person_not_found":
        raise http_error(404, "person_not_found", "Person not found or not active.", request)
    if outcome == "invalid_field":
        raise http_error(
            422, "unprocessable_entity", f"Unknown golden-profile field{suffix}.", request
        )
    if outcome == "sr_not_found":
        raise http_error(
            404,
            "not_found",
            f"Source record not found or not linked to this person{suffix}.",
            request,
        )
    if outcome == "value_not_found":
        raise http_error(
            422,
            "unprocessable_entity",
            f"The selected source record has no value for this field{suffix}.",
            request,
        )
    raise http_error(500, "internal_error", f"Unexpected override outcome: {outcome}.", request)


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
        body.field_name,
        body.source_record_pk,
        body.reason,
        user.email,
    )
    if outcome != "ok":
        _override_error(outcome, request)

    return envelope(
        OverrideResponse(
            person_id=person_id,
            field_name=body.field_name,
            source_record_pk=body.source_record_pk,
            status="applied",
        ),
        request,
    )


@router.post(
    "/v1/persons/{person_id}/survivorship-overrides/batch",
    response_model=ApiResponse[BatchOverrideResponse],
)
async def create_survivorship_override_batch(
    person_id: str,
    body: SurvivorshipOverrideBatchRequest,
    request: Request,
    user: AuthUser = Depends(require_admin),
    repo: SurvivorshipRepository = Depends(get_survivorship_repo),
) -> ApiResponse[BatchOverrideResponse]:
    """Pin multiple golden-profile fields to specific source records in one atomic transaction."""
    if not body.overrides:
        raise http_error(422, "unprocessable_entity", "overrides must not be empty.", request)

    items: list[tuple[str, str]] = [
        (item.field_name, item.source_record_pk) for item in body.overrides
    ]
    result = await repo.create_batch_overrides(person_id, items, body.reason, user.email)
    if result.outcome != "ok":
        _override_error(result.outcome, request, result.failed_field)

    return envelope(
        BatchOverrideResponse(
            person_id=person_id,
            applied=[item.field_name for item in body.overrides],
            status="applied",
        ),
        request,
    )
