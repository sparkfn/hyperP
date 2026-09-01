"""Fail-closed authority boundary for standalone CRM census control."""

from __future__ import annotations

from typing import Protocol

from src.standalone_crm_census_models import (
    MappingPrepareCensusRequest,
    MappingRollbackCensusRequest,
    SourceSyncCensusRequest,
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


class StandaloneCrmMappingFreshnessAuthority(Protocol):
    """Exact mapping side of #307 production census authority."""

    def validate_source_sync(self, request: SourceSyncCensusRequest) -> None: ...

    def validate_mapping_prepare(self, request: MappingPrepareCensusRequest) -> None: ...

    def validate_mapping_rollback(self, request: MappingRollbackCensusRequest) -> None: ...


class StandaloneCrmProjectionFreshnessAuthority(Protocol):
    """Exact projection side of #307 production census authority."""

    def validate_source_sync(self, request: SourceSyncCensusRequest) -> None: ...

    def validate_mapping_activation(
        self, request: MappingPrepareCensusRequest | MappingRollbackCensusRequest
    ) -> None: ...


class ProductionStandaloneCrmCensusAuthority:
    """Compose strict mapping and projection readers without any latest fallback."""

    def __init__(
        self,
        mapping: StandaloneCrmMappingFreshnessAuthority,
        projection: StandaloneCrmProjectionFreshnessAuthority,
    ) -> None:
        self._mapping = mapping
        self._projection = projection

    def verify(self, request: StandaloneCrmCensusRequest) -> None:
        if isinstance(request, SourceSyncCensusRequest):
            self._mapping.validate_source_sync(request)
            self._projection.validate_source_sync(request)
            return
        if isinstance(request, MappingPrepareCensusRequest):
            self._mapping.validate_mapping_prepare(request)
        else:
            self._mapping.validate_mapping_rollback(request)
        self._projection.validate_mapping_activation(request)
