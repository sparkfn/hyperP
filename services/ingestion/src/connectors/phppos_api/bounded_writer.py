"""Deterministic graph writes for one bounded PHPPOS unit.

Every record in a unit is written inside the caller-owned transaction the
bounded control store opens for that unit, so the unit's output and its
checkpoint commit atomically. Nothing here reads the source, calls an LLM, or
opens a session of its own.
"""

from __future__ import annotations

from typing import cast

from neo4j import ManagedTransaction

from src.bounded_ingestion_models import (
    AttemptContext,
    BoundedUnit,
    RecordDisposition,
    UnitApplyResult,
)
from src.config import get_settings
from src.connectors.phppos_api.bounded_checkpoint import resource_for_source
from src.exclusions import (
    ExclusionContext,
    build_exclusion_context,
    is_excluded_email,
    is_excluded_phone,
    is_excluded_source_id,
)
from src.ingestion_config import get_ingestion_config
from src.models import JsonValue, RecordType, SourceRecordEnvelope
from src.pipeline import IngestPipeline
from src.pipeline_sales import ingest_sales_record_in_transaction
from src.retirement import retire_source_evidence_in_transaction

_RETIREMENT_KEYS = ("_retire_source_record_id", "_retired_at", "_reconciliation_snapshot_at")


class PhpposBoundedWriteError(RuntimeError):
    """A bounded PHPPOS unit could not be written deterministically."""


class PhpposBoundedWriter:
    """Apply bounded PHPPOS units through the canonical identity/sales pipelines."""

    def __init__(self, source_key: str) -> None:
        resource_for_source(source_key)
        self._source_key = source_key

    def apply(
        self,
        tx: ManagedTransaction,
        context: AttemptContext,
        unit: BoundedUnit,
    ) -> UnitApplyResult:
        """Write every record of one unit inside the bounded transaction."""
        pipeline = IngestPipeline(control_instance_id=context.scope.control_instance_id)
        exclusion_context = _load_exclusion_context()
        dispositions = tuple(
            self._apply_record(
                tx,
                pipeline,
                exclusion_context,
                record,
                context.ingest_run_id,
            )
            for record in unit.unit.records
        )
        return UnitApplyResult(dispositions)

    def _apply_record(
        self,
        tx: ManagedTransaction,
        pipeline: IngestPipeline,
        exclusion_context: ExclusionContext,
        record: dict[str, JsonValue],
        ingest_run_id: str,
    ) -> RecordDisposition:
        retirement = _retirement_record(record)
        if retirement is not None:
            source_record_id, retired_at, snapshot_at = retirement
            retired = retire_source_evidence_in_transaction(
                tx,
                self._source_key,
                source_record_id,
                retired_at,
                snapshot_at,
            )
            return "committed" if retired > 0 else "duplicate"
        envelope = SourceRecordEnvelope.model_validate(
            {"source_system": self._source_key, **record}
        )
        if _record_is_excluded(envelope, exclusion_context):
            return "excluded"
        if envelope.record_type == RecordType.SALES:
            result = ingest_sales_record_in_transaction(
                tx,
                envelope,
                ingest_run_id=ingest_run_id,
                exclusion_context=exclusion_context,
            )
        elif envelope.record_type == RecordType.IDENTITY:
            result = pipeline.ingest_in_transaction(
                tx,
                envelope,
                ingest_run_id=ingest_run_id,
                exclusion_context=exclusion_context,
            )
        else:
            raise PhpposBoundedWriteError("bounded PHPPOS unit carried an unsupported record")
        if result.errors:
            raise PhpposBoundedWriteError("bounded PHPPOS record failed to write")
        if result.skipped_duplicate:
            return "duplicate"
        if result.dropped:
            return "policy_dropped"
        return "committed"


def _retirement_record(record: dict[str, JsonValue]) -> tuple[str, str, str] | None:
    """Return the retirement triple, or ``None`` when the record is an envelope."""
    if not any(key in record for key in _RETIREMENT_KEYS):
        return None
    values = tuple(record.get(key) for key in _RETIREMENT_KEYS)
    if not all(isinstance(value, str) and value for value in values):
        raise PhpposBoundedWriteError("bounded PHPPOS retirement marker is malformed")
    return cast(tuple[str, str, str], values)


def _record_is_excluded(envelope: SourceRecordEnvelope, context: ExclusionContext) -> bool:
    """Apply the run-level exclusion policy the unbounded ingestion loop applies."""
    if is_excluded_source_id(envelope.source_record_id, context):
        return True
    for identifier in envelope.identifiers:
        if identifier.type == "phone" and is_excluded_phone(identifier.value, context):
            return True
        if identifier.type == "email" and is_excluded_email(identifier.value, context):
            return True
    return False


def _load_exclusion_context() -> ExclusionContext:
    settings = get_settings()
    return build_exclusion_context(
        company_mobile_numbers=settings.company_mobile_numbers,
        company_email_addresses=settings.company_email_addresses,
        internal_person_names=settings.internal_person_names,
        file_exclusions=get_ingestion_config().exclusions,
    )
