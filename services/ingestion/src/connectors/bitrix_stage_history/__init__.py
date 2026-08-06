"""Read-only Bitrix CRM stage-history capability assessment primitives."""

from src.connectors.bitrix_stage_history.canonical import (
    canonical_stage_hash_v1,
    encode_stage_source_record_id,
    normalize_source_contract_id,
)
from src.connectors.bitrix_stage_history.models import (
    ProbeLimits,
    StageHistoryItem,
    StageHistoryPage,
    TraversalOutcome,
    parse_stage_history_page,
)

__all__ = [
    "ProbeLimits",
    "StageHistoryItem",
    "StageHistoryPage",
    "parse_stage_history_page",
    "TraversalOutcome",
    "canonical_stage_hash_v1",
    "encode_stage_source_record_id",
    "normalize_source_contract_id",
]
