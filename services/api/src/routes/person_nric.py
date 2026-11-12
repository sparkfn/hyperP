"""Routes for audited NRIC reveal operations."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from src.auth.deps import require_human_admin
from src.auth.models import AuthUser
from src.http_utils import ApiResponse, envelope, http_error, request_id
from src.nric import NricKeyUnavailableError
from src.repositories.deps import get_nric_repo
from src.repositories.protocols.nric import (
    NricRevealRepository,
    NricUnavailableError,
)
from src.types import NricReveal, NricRevealRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/persons", tags=["Persons"])


@router.post(
    "/{person_id}/nric/reveal",
    response_model=ApiResponse[NricReveal],
    operation_id="reveal_person_nric",
)
async def reveal_person_nric(
    person_id: str,
    body: NricRevealRequest,
    request: Request,
    admin: AuthUser = Depends(require_human_admin),
    repo: NricRevealRepository = Depends(get_nric_repo),
) -> ApiResponse[NricReveal]:
    """Perform an audited reveal of active NRIC identifiers for an active person."""
    try:
        result = await repo.reveal(
            person_id=person_id,
            actor_email=admin.email,
            reason=body.reason,
            request_id=request_id(request),
        )
    except NricKeyUnavailableError as exc:
        raise http_error(503, "nric_key_unavailable", str(exc), request) from exc
    except NricUnavailableError as exc:
        raise http_error(503, "nric_unavailable", str(exc), request) from exc
    except Exception as exc:
        logger.exception("Failed to reveal NRIC for person %s", person_id)
        raise http_error(500, "nric_reveal_failed", "Failed to reveal NRIC.", request) from exc

    if result is None:
        raise http_error(404, "person_not_found", "Person not found.", request)

    return envelope(
        NricReveal(
            person_id=result.person_id,
            reveal_id=result.reveal_id,
            revealed_at=result.revealed_at,
            values=result.values,
            unavailable_count=result.unavailable_count,
        ),
        request,
    )
