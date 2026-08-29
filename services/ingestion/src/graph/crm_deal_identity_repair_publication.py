"""Fail-closed #310 publication reservation authority; no broker calls occur here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from neo4j import ManagedTransaction

from src.graph.client import Neo4jClient
from src.graph.queries.crm_deal_identity_repair_ledger import (
    BEGIN_REPAIR_PUBLICATION,
    PUBLISH_REPAIR_PUBLICATION,
    READ_REPAIR_PUBLICATION_RESERVATION_BY_IDENTITY,
    RESERVE_REPAIR_PUBLICATION,
)

RepairPublicationStatus = Literal["pending", "publishing", "published"]


@dataclass(frozen=True)
class RepairPublicationReservation:
    """One immutable admission token for a deal/activity/Open Lines publication."""

    control_instance_id: str
    stream_scope: str
    routing_identity_digest: str
    occurrence_generation_identity: str
    reservation_token: str
    status: RepairPublicationStatus
    publication_id: str | None
    is_exact_replay: bool


class CrmDealRepairPublicationRepository:
    """Neo4j is the publication authority; Redis markers remain convenience-only."""

    def __init__(self, client: Neo4jClient, control_instance_id: str) -> None:
        self._client = client
        self._control_instance_id = control_instance_id

    def reserve(
        self,
        *,
        stream_scope: str,
        routing_identity_digest: str,
        occurrence_generation_identity: str,
    ) -> RepairPublicationReservation:
        token = uuid4().hex

        def work(tx: ManagedTransaction) -> RepairPublicationReservation:
            record = tx.run(
                RESERVE_REPAIR_PUBLICATION,
                control_instance_id=self._control_instance_id,
                stream_scope=stream_scope,
                routing_identity_digest=routing_identity_digest,
                occurrence_generation_identity=occurrence_generation_identity,
                reservation_token=token,
            ).single()
            if record is None:
                raise RuntimeError(
                    "repair dispatch block or incompatible publication "
                    "reservation rejected admission"
                )
            return RepairPublicationReservation(
                self._control_instance_id,
                stream_scope,
                routing_identity_digest,
                occurrence_generation_identity,
                str(record["reservation_token"]),
                _reservation_status(record["status"]),
                _optional_text(record["publication_id"]),
                _strict_bool(record["is_exact_replay"]),
            )

        return self._client.execute_write(work)

    def get_by_identity(
        self,
        *,
        stream_scope: str,
        routing_identity_digest: str,
        occurrence_generation_identity: str,
    ) -> RepairPublicationReservation | None:
        """Read an existing immutable reservation without creating one."""

        def work(tx: ManagedTransaction) -> RepairPublicationReservation | None:
            record = tx.run(
                READ_REPAIR_PUBLICATION_RESERVATION_BY_IDENTITY,
                control_instance_id=self._control_instance_id,
                routing_identity_digest=routing_identity_digest,
                occurrence_generation_identity=occurrence_generation_identity,
            ).single()
            if record is None:
                return None
            actual_scope = str(record["stream_scope"])
            if actual_scope != stream_scope:
                raise RuntimeError(
                    "existing publication reservation has an incompatible stream scope"
                )
            return RepairPublicationReservation(
                self._control_instance_id,
                actual_scope,
                routing_identity_digest,
                occurrence_generation_identity,
                str(record["reservation_token"]),
                _reservation_status(record["status"]),
                _optional_text(record["publication_id"]),
                True,
            )

        return self._client.execute_read(work)

    def begin_publishing(self, reservation: RepairPublicationReservation) -> None:
        self._assert_control(reservation)

        def work(tx: ManagedTransaction) -> None:
            record = tx.run(
                BEGIN_REPAIR_PUBLICATION,
                control_instance_id=self._control_instance_id,
                reservation_token=reservation.reservation_token,
                routing_identity_digest=reservation.routing_identity_digest,
                occurrence_generation_identity=reservation.occurrence_generation_identity,
                stream_scope=reservation.stream_scope,
            ).single()
            if record is None:
                raise RuntimeError(
                    "repair publication reservation is stale or repair dispatch is blocked"
                )

        self._client.execute_write(work)

    def mark_published(
        self,
        reservation: RepairPublicationReservation,
        publication_id: str,
    ) -> None:
        self._assert_control(reservation)
        if not publication_id:
            raise ValueError("repair publication ID is required")

        def work(tx: ManagedTransaction) -> None:
            record = tx.run(
                PUBLISH_REPAIR_PUBLICATION,
                control_instance_id=self._control_instance_id,
                reservation_token=reservation.reservation_token,
                publication_id=publication_id,
            ).single()
            if record is None:
                raise RuntimeError("repair publication reservation cannot be confirmed")

        self._client.execute_write(work)

    def _assert_control(self, reservation: RepairPublicationReservation) -> None:
        if reservation.control_instance_id != self._control_instance_id:
            raise ValueError("repair publication reservation belongs to another control instance")


def _reservation_status(value: object) -> RepairPublicationStatus:
    if value == "pending":
        return "pending"
    if value == "publishing":
        return "publishing"
    if value == "published":
        return "published"
    raise RuntimeError("repair publication reservation has an invalid status")


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value:
        return value
    raise RuntimeError("repair publication reservation has an invalid publication ID")


def _strict_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raise RuntimeError("repair publication reservation replay flag is invalid")


def reserve_repair_publication(
    client: Neo4jClient,
    *,
    control_instance_id: str,
    stream_scope: str,
    routing_identity_digest: str,
    occurrence_generation_identity: str,
) -> RepairPublicationReservation:
    """Reserve before a marker, source-window freeze, canvas, or broker publication."""
    return CrmDealRepairPublicationRepository(client, control_instance_id).reserve(
        stream_scope=stream_scope,
        routing_identity_digest=routing_identity_digest,
        occurrence_generation_identity=occurrence_generation_identity,
    )
