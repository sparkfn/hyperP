"""Control protocol shared by the bounded runner and Neo4j implementation."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from src.bounded_ingestion_budget import BoundedIngestionBudget
from src.bounded_ingestion_models import (
    AttemptContext,
    BoundedUnit,
    BoundedUnitWriter,
    FailureCategory,
    PauseReason,
    UnitApplyResult,
    Usage,
)


class BoundedCommitStore(Protocol):
    def reserve_usage(
        self,
        context: AttemptContext,
        requested: Usage,
        budget: BoundedIngestionBudget,
    ) -> bool: ...

    def commit_unit(
        self,
        context: AttemptContext,
        unit: BoundedUnit,
        writer: BoundedUnitWriter,
    ) -> UnitApplyResult | None: ...

    def pause(
        self,
        context: AttemptContext,
        reason: PauseReason,
        next_eligible_at: datetime,
    ) -> bool: ...

    def fail(
        self,
        context: AttemptContext,
        category: FailureCategory,
        safe_message: str,
        next_eligible_at: datetime,
    ) -> bool: ...

    def finalize(self, context: AttemptContext) -> bool: ...
