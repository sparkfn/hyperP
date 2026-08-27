"""Child fences, outbox publications, and checkpoint operations for census persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from neo4j import ManagedTransaction, Record

from src.graph.queries import standalone_crm_census as queries
from src.graph.standalone_crm_census_checkpoint_ops import _fenced_checkpoint_params
from src.graph.standalone_crm_census_types import (
    StandaloneCrmCensusAdmission,
    StandaloneCrmCensusConflictError,
    StandaloneCrmCensusStaleError,
    StandaloneCrmPublication,
)
from src.standalone_crm_census_models import (
    StandaloneCrmAttempt,
    StandaloneCrmCheckpoint,
    StandaloneCrmChildEnvelope,
    StandaloneCrmPublicationObservation,
    StandaloneCrmPublicationState,
    StandaloneCrmUnitKind,
)

if TYPE_CHECKING:
    from src.graph.client import Neo4jClient


class StandaloneCrmChildOperations:
    """Mixin; host supplies ``_client`` and a fenced mutation helper."""

    _client: Neo4jClient

    def _require_mutation(self, query: str, params: dict[str, object], message: str) -> Record:
        raise NotImplementedError

    def reserve_publication(
        self,
        *,
        admission: StandaloneCrmCensusAdmission,
        attempt: StandaloneCrmAttempt,
        unit_kind: StandaloneCrmUnitKind,
        sequence: int,
        publication_id: str | None = None,
        task_id: str,
        task_name: str,
        queue: str,
        payload_json: str,
        payload_digest: str,
    ) -> StandaloneCrmPublication:
        if publication_id is None:
            publication_id = f"{admission.census_id}:{attempt.generation}:{unit_kind}:{sequence}"
        if not publication_id.strip():
            raise ValueError("publication_id must be non-empty")

        def work(tx: ManagedTransaction) -> StandaloneCrmPublication:
            params = _guard(admission) | {
                "generation": attempt.generation,
                "parent_fence_token": attempt.parent_fence_token,
                "unit_kind": unit_kind,
                "sequence": sequence,
                "publication_id": publication_id,
                "task_id": task_id,
                "task_name": task_name,
                "queue": queue,
                "payload_json": payload_json,
                "payload_digest": payload_digest,
            }
            record = tx.run(queries.RESERVE_PUBLICATION, **params).single()  # type: ignore[arg-type]
            if record is None:
                raise StandaloneCrmCensusStaleError("publication reservation rejected")
            if _bool(record, "payload_conflict"):
                raise StandaloneCrmCensusConflictError("publication immutable payload conflicts")
            publication = _publication_from_record(record)
            if (
                publication.publication_id != publication_id
                or publication.task_id != task_id
                or publication.task_name != task_name
                or publication.queue != queue
                or publication.payload_json != payload_json
                or publication.payload_digest != payload_digest
            ):
                raise StandaloneCrmCensusConflictError("publication immutable record conflicts")
            return publication

        return self._client.execute_write(work)

    def mark_publication_publishing(
        self,
        admission: StandaloneCrmCensusAdmission,
        attempt: StandaloneCrmAttempt,
        publication_id: str,
    ) -> None:
        self._publication_mutation(
            queries.MARK_PUBLICATION_PUBLISHING, admission, attempt, publication_id
        )

    def authorize_publication_broker(
        self,
        admission: StandaloneCrmCensusAdmission,
        attempt: StandaloneCrmAttempt,
        publication_id: str,
    ) -> None:
        """Perform the final durable lease/fence CAS immediately before broker I/O."""
        self._publication_mutation(
            queries.AUTHORIZE_PUBLICATION_BROKER, admission, attempt, publication_id
        )

    def mark_publication_ambiguous(
        self,
        admission: StandaloneCrmCensusAdmission,
        attempt: StandaloneCrmAttempt,
        publication_id: str,
    ) -> None:
        self._publication_mutation(
            queries.MARK_PUBLICATION_AMBIGUOUS, admission, attempt, publication_id
        )

    def confirm_publication(
        self,
        admission: StandaloneCrmCensusAdmission,
        attempt: StandaloneCrmAttempt,
        publication_id: str,
    ) -> None:
        self._publication_mutation(
            queries.MARK_PUBLICATION_PUBLISHED, admission, attempt, publication_id
        )

    def _publication_mutation(
        self,
        query: str,
        admission: StandaloneCrmCensusAdmission,
        attempt: StandaloneCrmAttempt,
        publication_id: str,
    ) -> None:
        self._require_mutation(
            query,
            _guard(admission)
            | {
                "generation": attempt.generation,
                "parent_fence_token": attempt.parent_fence_token,
                "publication_id": publication_id,
            },
            "publication mutation rejected",
        )

    def publication_recovery(
        self, publication_id: str
    ) -> tuple[
        StandaloneCrmCensusAdmission, StandaloneCrmPublication, StandaloneCrmPublicationObservation
    ]:
        """Load immutable payload plus child evidence that cannot be a freeze allocation."""

        def work(
            tx: ManagedTransaction,
        ) -> tuple[
            StandaloneCrmCensusAdmission,
            StandaloneCrmPublication,
            StandaloneCrmPublicationObservation,
        ]:
            record = tx.run(
                queries.GET_PUBLICATION_RECOVERY, publication_id=publication_id
            ).single()
            if record is None:
                raise StandaloneCrmCensusStaleError("publication is missing")
            census = _mapping(record, "census")
            observation = _observation(record["observation"])
            return (
                StandaloneCrmCensusAdmission(
                    _value_text(census.get("census_id"), "census_id"),
                    _value_text(census.get("state"), "state"),
                    _value_text(census.get("fingerprint"), "fingerprint"),
                    _value_text(census.get("authority_digest"), "authority_digest"),
                    _value_text(census.get("source_instance_id"), "source_instance_id"),
                    _value_text(census.get("control_instance_id"), "control_instance_id"),
                    False,
                ),
                _publication_from_mapping(_mapping(record, "publication")),
                observation,
            )

        return self._client.execute_read(work)

    def confirm_observed_publication(
        self,
        admission: StandaloneCrmCensusAdmission,
        attempt: StandaloneCrmAttempt,
        publication_id: str,
    ) -> None:
        self._publication_mutation(
            queries.CONFIRM_OBSERVED_PUBLICATION, admission, attempt, publication_id
        )

    def renew_child_fence(
        self,
        admission: StandaloneCrmCensusAdmission,
        checkpoint: StandaloneCrmCheckpoint,
        *,
        lease_seconds: int,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._require_mutation(
            queries.RENEW_UNIT_FENCE,
            _fenced_checkpoint_params(admission, checkpoint) | {"lease_seconds": lease_seconds},
            "child fence renewal rejected",
        )

    def claim_child_fence(
        self,
        admission: StandaloneCrmCensusAdmission,
        envelope: StandaloneCrmChildEnvelope,
        *,
        worker_task_id: str,
        lease_seconds: int,
        recovery: bool = False,
    ) -> int:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        record = self._require_mutation(
            queries.CLAIM_UNIT_FENCE,
            _guard(admission)
            | {
                "generation": envelope.generation,
                "parent_fence_token": envelope.parent_fence_token,
                "unit_kind": envelope.unit_kind,
                "publication_id": envelope.publication_id,
                "task_id": worker_task_id,
                "lease_seconds": lease_seconds,
                "recovery": recovery,
            },
            "child fence claim rejected",
        )
        return _positive(record, "child_fence_token")


def _guard(admission: StandaloneCrmCensusAdmission) -> dict[str, object]:
    return {
        "census_id": admission.census_id,
        "authority_digest": admission.authority_digest,
        "source_instance_id": admission.source_instance_id,
        "control_instance_id": admission.control_instance_id,
    }


def _publication_from_record(record: Record) -> StandaloneCrmPublication:
    return StandaloneCrmPublication(
        _text(record, "publication_id"),
        _text(record, "task_id"),
        _text(record, "payload_json"),
        _text(record, "payload_digest"),
        _text(record, "task_name"),
        _text(record, "queue"),
        _publication_state(record["status"]),
    )


def _publication_from_mapping(value: dict[str, object]) -> StandaloneCrmPublication:
    return StandaloneCrmPublication(
        _value_text(value.get("publication_id"), "publication_id"),
        _value_text(value.get("task_id"), "task_id"),
        _value_text(value.get("payload_json"), "payload_json"),
        _value_text(value.get("payload_digest"), "payload_digest"),
        _value_text(value.get("task_name"), "task_name"),
        _value_text(value.get("queue"), "queue"),
        _publication_state(value.get("status")),
    )


def _publication_state(value: object) -> StandaloneCrmPublicationState:
    if value not in {"reserved", "publishing", "published", "ambiguous", "retired"}:
        raise RuntimeError("standalone CRM census publication state is invalid")
    return cast(StandaloneCrmPublicationState, value)


def _observation(value: object) -> StandaloneCrmPublicationObservation:
    if value not in {"none", "fence_claim", "checkpoint_advanced"}:
        raise RuntimeError("publication observation is invalid")
    return cast(StandaloneCrmPublicationObservation, value)


def _text(record: Record, key: str) -> str:
    return _value_text(record[key], key)


def _value_text(value: object, key: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"standalone CRM census returned invalid {key}")
    return value


def _positive(record: Record, key: str) -> int:
    value = record[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError(f"standalone CRM census returned invalid {key}")
    return int(value)


def _bool(record: Record, key: str) -> bool:
    value = record[key]
    if not isinstance(value, bool):
        raise RuntimeError(f"standalone CRM census returned invalid {key}")
    return value


def _mapping(record: Record, key: str) -> dict[str, object]:
    value = record[key]
    if not isinstance(value, dict):
        raise RuntimeError(f"standalone CRM census returned invalid {key}")
    return value
