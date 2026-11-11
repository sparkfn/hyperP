"""Skip ledger recording and reconciliation for ingestion runs."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from neo4j import ManagedTransaction

from src.graph.client import Neo4jClient
from src.graph.queries.ingest_skips import COUNT_INGEST_SKIPS, WRITE_INGEST_SKIPS
from src.models import JsonValue, SourceRecordEnvelope
from src.source_instances import LEGACY_DEFAULT_CONTROL_INSTANCE_ID

logger = logging.getLogger(__name__)

_FLUSH_BATCH_SIZE = 500

SkipStage = Literal["connector", "pipeline"]


@dataclass(frozen=True)
class ConnectorSkip:
    """A record-level skip observed within a source connector."""

    reason: str
    source_record_id: str | None
    locator: str


class SkipRecorder:
    """Mixin for recording connector-stage record skips.

    Does not define ``__init__`` so connector subclasses can preserve their own
    constructors and inheritance chains without cooperative super() calls.
    """

    _recorded_skips: list[ConnectorSkip] | None = None

    def record_skip(
        self,
        reason_or_skip: str | ConnectorSkip,
        *,
        source_record_id: str | None = None,
        locator: str = "",
    ) -> None:
        """Buffer one skip; accepts a ready ``ConnectorSkip`` so it can be an ``on_skip`` hook."""
        if self._recorded_skips is None:
            self._recorded_skips = []
        if isinstance(reason_or_skip, ConnectorSkip):
            self._recorded_skips.append(reason_or_skip)
        else:
            self._recorded_skips.append(
                ConnectorSkip(
                    reason=reason_or_skip,
                    source_record_id=source_record_id,
                    locator=locator,
                )
            )

    def drain_skips(self) -> list[ConnectorSkip]:
        """Return and clear the buffered skips."""
        skips = self._recorded_skips or []
        self._recorded_skips = None
        return skips


@runtime_checkable
class SkipReporter(Protocol):
    """Duck-typed protocol for connectors that buffer record skips."""

    def drain_skips(self) -> list[ConnectorSkip]: ...


def skip_row(
    ingest_run_id: str,
    stage: SkipStage,
    source_system: str,
    reason: str,
    source_record_id: str | None,
    locator: str,
    occurrence: int = 0,
) -> dict[str, JsonValue]:
    """Build one ``WRITE_INGEST_SKIPS`` row; ``skip_id`` makes replays idempotent.

    ``occurrence`` numbers repeats of an identical skip within one run (0 for the first) so each
    repeat keeps its own row instead of merging into one.
    """
    seed = f"{ingest_run_id}|{stage}|{source_system}|{reason}|{source_record_id or locator}"
    if occurrence:
        seed = f"{seed}|{occurrence}"
    return {
        "skip_id": hashlib.sha1(seed.encode("utf-8")).hexdigest(),
        "ingest_run_id": ingest_run_id,
        "source_system": source_system,
        "stage": stage,
        "reason": reason,
        "source_record_id": source_record_id,
        "locator": locator,
    }


class SkipLedger:
    """Tracks and flushes pipeline and connector skips for an IngestRun."""

    def __init__(
        self,
        client: Neo4jClient,
        ingest_run_id: str,
        source_key: str,
        control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
    ) -> None:
        self._client = client
        self._ingest_run_id = ingest_run_id
        self._source_key = source_key
        self._control_instance_id = control_instance_id

        self.emitted: int = 0
        self.persisted: int = 0
        self.duplicates: int = 0
        self.pipeline_skips: int = 0
        self.connector_skips: int = 0
        self.record_errors: int = 0
        self.reconciled: bool | None = None

        self._buffer: list[dict[str, JsonValue]] = []
        self._occurrences: dict[tuple[str, str, str, str], int] = {}

    def add_pipeline_skip(
        self,
        reason: str,
        envelope: SourceRecordEnvelope | None = None,
        *,
        source_record_id: str | None = None,
        locator: str = "",
    ) -> None:
        self.pipeline_skips += 1
        self._buffer_skip(
            "pipeline",
            envelope.source_system if envelope is not None else self._source_key,
            reason,
            envelope.source_record_id if envelope is not None else source_record_id,
            locator,
        )

    def add_connector_skips(self, skips: list[ConnectorSkip]) -> None:
        for skip in skips:
            self.connector_skips += 1
            self._buffer_skip(
                "connector", self._source_key, skip.reason, skip.source_record_id, skip.locator
            )

    def _buffer_skip(
        self,
        stage: SkipStage,
        source_system: str,
        reason: str,
        source_record_id: str | None,
        locator: str,
    ) -> None:
        key = (stage, source_system, reason, source_record_id or locator)
        occurrence = self._occurrences.get(key, 0)
        self._occurrences[key] = occurrence + 1
        row = skip_row(
            self._ingest_run_id, stage, source_system, reason, source_record_id, locator, occurrence
        )
        self._buffer.append(row)
        if len(self._buffer) >= _FLUSH_BATCH_SIZE:
            self.flush()

    def flush(self) -> None:
        """Write buffered rows; they stay buffered if the write fails (the MERGE is idempotent)."""
        if not self._buffer:
            return
        rows = list(self._buffer)

        def _tx(tx: ManagedTransaction) -> None:
            tx.run(
                WRITE_INGEST_SKIPS,
                rows=rows,
                control_instance_id=self._control_instance_id,
            )

        with self._client.session() as session:
            session.execute_write(_tx)
        del self._buffer[: len(rows)]

    def flush_quietly(self) -> None:
        """Best-effort flush for failure/teardown paths that must not mask the real outcome."""
        try:
            self.flush()
        except Exception:
            logger.exception("Failed to flush skip ledger for run %s", self._ingest_run_id)

    def reconcile(self) -> bool:
        """Compare stored skip rows and run totals; log (never raise) on a mismatch."""
        try:
            self.flush()

            def _tx(tx: ManagedTransaction) -> int:
                record = tx.run(
                    COUNT_INGEST_SKIPS,
                    ingest_run_id=self._ingest_run_id,
                ).single()
                return int(record["skip_count"]) if record is not None else 0

            with self._client.session() as session:
                stored_skips = session.execute_read(_tx)
        except Exception:
            logger.exception("Skip ledger count failed for run %s", self._ingest_run_id)
            self.reconciled = False
            return False

        accounted = self.persisted + self.duplicates + self.pipeline_skips + self.record_errors
        expected_skips = self.pipeline_skips + self.connector_skips
        self.reconciled = stored_skips == expected_skips and self.emitted == accounted
        if not self.reconciled:
            logger.error(
                "Skip ledger reconciliation failed for run %s: stored_skips=%d expected_skips=%d "
                "emitted=%d accounted=%d (persisted=%d duplicates=%d pipeline_skips=%d "
                "connector_skips=%d record_errors=%d)",
                self._ingest_run_id,
                stored_skips,
                expected_skips,
                self.emitted,
                accounted,
                self.persisted,
                self.duplicates,
                self.pipeline_skips,
                self.connector_skips,
                self.record_errors,
            )
        return self.reconciled

    def counters(self) -> dict[str, int | bool]:
        """IngestRun counter properties for ``UPDATE_INGEST_RUN``."""
        return {
            "emitted_count": self.emitted,
            "persisted_count": self.persisted,
            "duplicate_count": self.duplicates,
            "pipeline_skip_count": self.pipeline_skips,
            "connector_skip_count": self.connector_skips,
            "record_error_count": self.record_errors,
            "skip_ledger_reconciled": bool(self.reconciled),
        }
