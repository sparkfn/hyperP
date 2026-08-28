"""Fail-closed authority boundary for standalone CRM census control."""

from __future__ import annotations

from typing import Protocol

from src.standalone_crm_census_models import (
    StandaloneCrmCensusAuthorityError,
    StandaloneCrmCensusRequest,
)


class StandaloneCrmCensusAuthority(Protocol):
    """Verifies the immutable authority heads captured by a census request."""

    def verify(self, request: StandaloneCrmCensusRequest) -> None: ...


class UnavailableStandaloneCrmCensusAuthority:
    """Production-safe default until #274/#275 provide authoritative readers."""

    def verify(self, request: StandaloneCrmCensusRequest) -> None:
        del request
        raise StandaloneCrmCensusAuthorityError(
            "standalone CRM census authority is unavailable; refusing mutation"
        )
