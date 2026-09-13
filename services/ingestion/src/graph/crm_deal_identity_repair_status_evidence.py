"""Incremental canonical evidence encoders for CRM-deal repair status."""

from __future__ import annotations

from contextlib import ExitStack

from neo4j import ManagedTransaction

from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.bounded import CanonicalByteSorter
from src.graph.crm_deal_identity_repair_boundary_evidence import (
    canonical_boundary_evidence,
    record_json_dict,
)


def spool_evidence(
    stack: ExitStack,
    tx: ManagedTransaction,
    query: str,
    *,
    stale_run_id: str | None = None,
    control_instance_id: str | None = None,
) -> tuple[CanonicalByteSorter, int]:
    """Fully consume, canonicalize, and disk-sort one unordered evidence family."""
    if (stale_run_id is None) == (control_instance_id is None):
        raise ValueError("status evidence requires exactly one supported query parameter")
    sorter = stack.enter_context(CanonicalByteSorter())
    if stale_run_id is not None:
        result = tx.run(query, stale_run_id=stale_run_id)
    else:
        if control_instance_id is None:
            raise RuntimeError("status evidence control parameter is missing")
        result = tx.run(query, control_instance_id=control_instance_id)
    count = 0
    for record in result:
        value = canonical_boundary_evidence(record_json_dict(record))
        if not isinstance(value, dict):
            raise RuntimeError("repair boundary evidence rows must be JSON objects")
        sorter.add(canonical_json_bytes({"value": value}), canonical_json_bytes(value))
        count += 1
    result.consume()
    return sorter, count
