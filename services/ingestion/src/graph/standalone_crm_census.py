"""Compatibility facade for standalone CRM census Neo4j repository operations."""

from __future__ import annotations

from src.graph.client import Neo4jClient
from src.graph.standalone_crm_census_admission import StandaloneCrmCensusAdmissionRepository
from src.graph.standalone_crm_census_reconciliation import (
    StandaloneCrmCensusReconciliationRepository,
)
from src.graph.standalone_crm_census_records import (
    StandaloneCrmAttemptTakeover,
    StandaloneCrmCensusAdmission,
    StandaloneCrmCensusStatus,
    StandaloneCrmPublicationRepair,
    StandaloneCrmRuntimeSnapshot,
)
from src.graph.standalone_crm_census_work import StandaloneCrmCensusWorkRepository


class StandaloneCrmCensusRepository(
    StandaloneCrmCensusAdmissionRepository,
    StandaloneCrmCensusWorkRepository,
    StandaloneCrmCensusReconciliationRepository,
):
    def __init__(self, client: Neo4jClient) -> None:
        self._client = client


__all__ = [
    "StandaloneCrmAttemptTakeover",
    "StandaloneCrmCensusAdmission",
    "StandaloneCrmCensusRepository",
    "StandaloneCrmCensusStatus",
    "StandaloneCrmPublicationRepair",
    "StandaloneCrmRuntimeSnapshot",
]
