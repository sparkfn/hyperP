"""Neo4j-backed repository for standalone CRM census control state."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from neo4j import ManagedTransaction, Record

from src.graph.queries.standalone_crm_census import (
    ADMIT_STANDALONE_CRM_CENSUS,
    ADVANCE_CENSUS_CHECKPOINT,
    ALLOCATE_SOURCE_UNITS,
    CLAIM_CENSUS_ATTEMPT,
    CLAIM_CENSUS_CHILD,
    COMMIT_NO_SOURCE_WINDOW,
    COMMIT_SOURCE_WINDOW,
    CONFIRM_CHILD_PUBLICATION,
    CONTINUE_CENSUS,
    CREATE_STANDALONE_CRM_CENSUS_SCHEMA,
    FINALIZE_CENSUS,
    GET_CENSUS_STATUS,
    LIST_UNRESOLVED_PUBLICATIONS,
    MARK_CHILD_TERMINAL,
    RECORD_CENSUS_HTTP_OUTCOME,
    REQUEST_CENSUS_CANCELLATION,
    REQUEST_CENSUS_PAUSE,
    RESERVE_CENSUS_HTTP_CALL,
    RESERVE_CHILD_PUBLICATION,
    START_CENSUS_FREEZING,
    SUPERSEDE_STALE_ATTEMPT,
)
from src.standalone_crm_census_models import (
    TERMINAL_PARENT_STATES,
    CensusBudgetError,
    CensusConflictError,
    CensusError,
    CensusKind,
    HttpCallState,
    ParentState,
)

if TYPE_CHECKING:
    from src.graph.client import Neo4jClient


class CensusRepositoryError(CensusError):
    """The graph refused a census transition."""


def assert_standalone_crm_census_ready(client: Neo4jClient) -> None:
    """Fail closed unless #272 readiness and the census schema are both present."""

    from src.graph.ingestion_control_instance_migration import assert_ingestion_control_ready

    assert_ingestion_control_ready(client)

    def _read(tx: ManagedTransaction) -> set[str]:
        return {
            str(record["name"])
            for record in tx.run("SHOW CONSTRAINTS").data()
            if isinstance(record.get("name"), str)
        }

    installed = client.execute_read(_read)
    required = {
        statement.split("CREATE CONSTRAINT ", 1)[1].split(" IF NOT EXISTS", 1)[0]
        for statement in CREATE_STANDALONE_CRM_CENSUS_SCHEMA
        if "CREATE CONSTRAINT" in statement
    }
    missing = sorted(required.difference(installed))
    if missing:
        raise CensusRepositoryError(f"standalone CRM census schema is missing: {missing}")


def _text(record: Record, key: str) -> str:
    value: object = record[key]
    if not isinstance(value, str):
        raise CensusRepositoryError(f"census query returned invalid {key}")
    return value


def _boolean(record: Record, key: str) -> bool:
    value: object = record[key]
    if not isinstance(value, bool):
        raise CensusRepositoryError(f"census query returned invalid {key}")
    return value


def _optional_text(record: Record, key: str) -> str | None:
    value: object = record[key]
    return value if isinstance(value, str) else None


@dataclass(frozen=True)
class CensusAdmission:
    census_id: str
    fingerprint: str
    state: str
    generation: int
    created: bool


class StandaloneCrmCensusRepository:
    """Repository boundary for durable census state and CAS transitions."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def assert_ready(self) -> None:
        assert_standalone_crm_census_ready(self._client)

    def admit(
        self,
        *,
        source_key: str = "bitrix_chat",
        source_instance_id: str,
        control_instance_id: str,
        census_kind: CensusKind,
        occurrence_key: str,
        fingerprint: str,
        request_json: str,
        budget_json: str,
        heads_json: str,
        occurrence_deadline: datetime,
        occurrence_calls: int,
        occurrence_rows: int,
        attempt_calls: int,
        attempt_rows: int,
        attempt_runtime_seconds: float,
        max_attempts: int,
    ) -> tuple[str, bool]:
        census_id = occurrence_key[:32] + datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")

        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                ADMIT_STANDALONE_CRM_CENSUS,
                census_id=census_id,
                source_key=source_key,
                source_instance_id=source_instance_id,
                control_instance_id=control_instance_id,
                census_kind=census_kind.value,
                occurrence_key=occurrence_key,
                fingerprint=fingerprint,
                request_json=request_json,
                budget_json=budget_json,
                heads_json=heads_json,
                occurrence_deadline=occurrence_deadline,
                occurrence_calls=occurrence_calls,
                occurrence_rows=occurrence_rows,
                attempt_calls=attempt_calls,
                attempt_rows=attempt_rows,
                attempt_runtime_seconds=attempt_runtime_seconds,
                max_attempts=max_attempts,
            ).single()
            if record is None:
                raise CensusRepositoryError("census admission was rejected")
            return record

        record = self._client.execute_write(_work)
        if _boolean(record, "fingerprint_conflict"):
            raise CensusConflictError("census occurrence key has a different fingerprint")
        if _boolean(record, "active_conflict"):
            raise CensusConflictError("another active census owns this control scope")
        return _text(record, "census_id"), record["created"] is True

    def claim_attempt(
        self,
        *,
        census_id: str,
        fingerprint: str,
        attempt_deadline: datetime,
    ) -> tuple[int, str]:
        fence_token = uuid.uuid4().hex

        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                CLAIM_CENSUS_ATTEMPT,
                census_id=census_id,
                fingerprint=fingerprint,
                attempt_deadline=attempt_deadline,
                lease_until=attempt_deadline,
                fence_token=fence_token,
            ).single()
            if record is None:
                raise CensusRepositoryError("census attempt could not be claimed")
            return record

        record = self._client.execute_write(_work)
        return int(record["generation"]), _text(record, "fence_token")

    def start_freezing(self, *, census_id: str, fingerprint: str) -> None:
        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                START_CENSUS_FREEZING,
                census_id=census_id,
                fingerprint=fingerprint,
            ).single()
            if record is None:
                raise CensusRepositoryError("census could not enter freezing")
            return record

        self._client.execute_write(_work)

    def commit_source_window(
        self, *, census_id: str, fingerprint: str, bounds_json: str, selected_kinds: list[str]
    ) -> None:
        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                COMMIT_SOURCE_WINDOW,
                census_id=census_id,
                fingerprint=fingerprint,
                bounds_json=bounds_json,
                selected_kinds=selected_kinds,
            ).single()
            if record is None:
                raise CensusRepositoryError("source window could not be committed")
            return record

        self._client.execute_write(_work)

    def commit_no_source_window(self, *, census_id: str, fingerprint: str) -> None:
        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                COMMIT_NO_SOURCE_WINDOW,
                census_id=census_id,
                fingerprint=fingerprint,
            ).single()
            if record is None:
                raise CensusRepositoryError("no-source window could not be committed")
            return record

        self._client.execute_write(_work)

    def reserve_http_call(
        self,
        *,
        census_id: str,
        fingerprint: str,
        fence_token: str,
        intent_id: str,
        call_kind: str,
        unit_kind: str,
        frozen_upper_id: int | None,
        cursor: int | None,
        retry_ordinal: int,
        deadline: datetime,
    ) -> bool:
        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                RESERVE_CENSUS_HTTP_CALL,
                census_id=census_id,
                fingerprint=fingerprint,
                fence_token=fence_token,
                intent_id=intent_id,
                call_kind=call_kind,
                unit_kind=unit_kind,
                frozen_upper_id=frozen_upper_id,
                cursor=cursor,
                retry_ordinal=retry_ordinal,
                deadline=deadline,
            ).single()
            if record is None:
                raise CensusBudgetError("HTTP call reservation was rejected")
            return record

        record = self._client.execute_write(_work)
        return _boolean(record, "reserved")

    def record_http_outcome(
        self,
        *,
        census_id: str,
        intent_id: str,
        outcome: HttpCallState,
        outcome_detail: str,
    ) -> None:
        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                RECORD_CENSUS_HTTP_OUTCOME,
                census_id=census_id,
                intent_id=intent_id,
                outcome=outcome.value,
                outcome_detail=outcome_detail[:1000],
            ).single()
            if record is None:
                raise CensusRepositoryError("HTTP call reservation was not found")
            return record

        self._client.execute_write(_work)

    def pause(self, *, census_id: str, fingerprint: str, reason: str) -> None:
        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                REQUEST_CENSUS_PAUSE,
                census_id=census_id,
                fingerprint=fingerprint,
                reason=reason[:200],
            ).single()
            if record is None:
                raise CensusRepositoryError("census pause was rejected")
            return record

        self._client.execute_write(_work)

    def cancel(self, *, census_id: str, fingerprint: str, actor: str) -> ParentState:
        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                REQUEST_CENSUS_CANCELLATION,
                census_id=census_id,
                fingerprint=fingerprint,
                actor=actor[:200],
            ).single()
            if record is None:
                raise CensusRepositoryError("census cancellation was rejected or terminal")
            return record

        record = self._client.execute_write(_work)
        return ParentState(_text(record, "state"))

    def continue_census(self, *, census_id: str, fingerprint: str) -> float:
        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                CONTINUE_CENSUS,
                census_id=census_id,
                fingerprint=fingerprint,
            ).single()
            if record is None:
                raise CensusRepositoryError("census continuation was rejected")
            return record

        record = self._client.execute_write(_work)
        value: object = record["attempt_runtime_seconds"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise CensusRepositoryError("census continuation returned invalid runtime budget")
        return float(value)

    def finalize(
        self,
        *,
        census_id: str,
        fingerprint: str,
        terminal_state: ParentState,
        reason: str,
        allow_paused: bool,
    ) -> None:
        if terminal_state not in TERMINAL_PARENT_STATES:
            raise ValueError("only terminal parent states may be persisted")

        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                FINALIZE_CENSUS,
                census_id=census_id,
                fingerprint=fingerprint,
                terminal_state=terminal_state.value,
                reason=reason[:200],
                allow_paused=allow_paused,
            ).single()
            if record is None:
                raise CensusRepositoryError("census terminal settlement was rejected")
            return record

        self._client.execute_write(_work)

    def allocate_source_units(
        self,
        *,
        census_id: str,
        fingerprint: str,
        units: list[dict[str, object]],
    ) -> list[str]:
        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                ALLOCATE_SOURCE_UNITS,
                census_id=census_id,
                fingerprint=fingerprint,
                units=units,
            ).single()
            if record is None:
                raise CensusRepositoryError("source child allocation was rejected")
            return record

        record = self._client.execute_write(_work)
        return [str(value) for value in record["unit_kinds"]]

    def reserve_publication(
        self,
        *,
        census_id: str,
        fingerprint: str,
        fence_token: str,
        unit_kind: str,
        publication_sequence: int,
        task_name: str,
        task_id: str,
        queue: str,
        payload_version: str,
        payload_digest: str,
        payload_json: str,
    ) -> str:
        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                RESERVE_CHILD_PUBLICATION,
                census_id=census_id,
                fingerprint=fingerprint,
                fence_token=fence_token,
                unit_kind=unit_kind,
                publication_sequence=publication_sequence,
                task_name=task_name,
                task_id=task_id,
                queue=queue,
                payload_version=payload_version,
                payload_digest=payload_digest,
                payload_json=payload_json,
            ).single()
            if record is None:
                raise CensusRepositoryError("child publication reservation was rejected")
            return record

        record = self._client.execute_write(_work)
        if not _boolean(record, "payload_matches"):
            raise CensusConflictError("child publication payload differs")
        return _text(record, "task_id")

    def confirm_publication(self, *, census_id: str, task_id: str) -> None:
        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                CONFIRM_CHILD_PUBLICATION,
                census_id=census_id,
                task_id=task_id,
            ).single()
            if record is None:
                raise CensusRepositoryError("child publication was not found")
            return record

        self._client.execute_write(_work)

    def mark_child_terminal(
        self,
        *,
        census_id: str,
        fingerprint: str,
        fence_token: str,
        unit_kind: str,
        terminal_state: str,
        reason: str,
    ) -> None:
        if terminal_state not in {"completed", "failed", "cancelled", "superseded"}:
            raise ValueError("invalid terminal child state")

        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                MARK_CHILD_TERMINAL,
                census_id=census_id,
                fingerprint=fingerprint,
                fence_token=fence_token,
                unit_kind=unit_kind,
                terminal_state=terminal_state,
                reason=reason[:200],
            ).single()
            if record is None:
                raise CensusRepositoryError("child terminal transition was rejected")
            return record

        self._client.execute_write(_work)

    def claim_child(
        self,
        *,
        census_id: str,
        fingerprint: str,
        fence_token: str,
        unit_kind: str,
    ) -> tuple[int, str]:
        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                CLAIM_CENSUS_CHILD,
                census_id=census_id,
                fingerprint=fingerprint,
                fence_token=fence_token,
                unit_kind=unit_kind,
            ).single()
            if record is None:
                raise CensusRepositoryError("child claim was rejected")
            return record

        record = self._client.execute_write(_work)
        return int(record["frozen_upper_id"]), _text(record, "revision_id")

    def status(self, census_id: str) -> dict[str, object] | None:
        def _work(tx: ManagedTransaction) -> Record | None:
            return tx.run(GET_CENSUS_STATUS, census_id=census_id).single()

        record = self._client.execute_read(_work)
        if record is None:
            return None
        return {
            "census_id": _text(record, "census_id"),
            "fingerprint": _text(record, "fingerprint"),
            "state": _text(record, "state"),
            "census_kind": _text(record, "census_kind"),
            "generation": int(record["generation"]),
            "calls_used": int(record["calls_used"]),
            "rows_processed": int(record["rows_processed"]),
            "attempt_generation": record["attempt_generation"],
            "attempt_state": record["attempt_state"],
        }

    def supersede_stale_attempt(self, *, census_id: str, fingerprint: str) -> tuple[int, str]:
        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                SUPERSEDE_STALE_ATTEMPT,
                census_id=census_id,
                fingerprint=fingerprint,
            ).single()
            if record is None:
                raise CensusRepositoryError("stale attempt could not be superseded")
            return record

        record = self._client.execute_write(_work)
        return int(record["generation"]), _text(record, "fence_token")

    def unresolved_publications(self, census_id: str) -> list[dict[str, object]]:
        def _read(tx: ManagedTransaction) -> list[Record]:
            return list(tx.run(LIST_UNRESOLVED_PUBLICATIONS, census_id=census_id))

        return [dict(record) for record in self._client.execute_read(_read)]

    def advance_checkpoint(
        self,
        *,
        census_id: str,
        fingerprint: str,
        fence_token: str,
        unit_kind: str,
        last_id: int,
        rows_processed: int,
        binding_position: int,
    ) -> tuple[int, int]:
        def _work(tx: ManagedTransaction) -> Record:
            record = tx.run(
                ADVANCE_CENSUS_CHECKPOINT,
                census_id=census_id,
                fingerprint=fingerprint,
                fence_token=fence_token,
                unit_kind=unit_kind,
                last_id=last_id,
                rows_processed=rows_processed,
                binding_position=binding_position,
            ).single()
            if record is None:
                raise CensusRepositoryError("checkpoint advance was rejected")
            return record

        record = self._client.execute_write(_work)
        return int(record["last_id"]), int(record["rows_processed"])
