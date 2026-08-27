"""Durable, non-secret registration and admission for Bitrix instances."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from neo4j import ManagedTransaction

from src.config import Settings
from src.graph.client import Neo4jClient
from src.graph.queries.bitrix_source_instances import (
    ADMIT_BITRIX_CONTROL_INSTANCE,
    DISABLE_BITRIX_SOURCE_INSTANCE,
    REGISTER_BITRIX_SOURCE_INSTANCE,
    REQUIRE_ACTIVE_BITRIX_SOURCE_INSTANCE,
)
from src.source_instances import canonical_source_instance_id

BITRIX_SOURCE_KEY = "bitrix_chat"


class BitrixSourceInstanceError(RuntimeError):
    """Base class for bounded source-instance control-plane failures."""


class BitrixSourceInstanceMissingError(BitrixSourceInstanceError):
    """The requested durable registration does not exist."""


class BitrixSourceInstanceDisabledError(BitrixSourceInstanceError):
    """The requested durable registration has been disabled."""


class BitrixSourceInstanceConflictError(BitrixSourceInstanceError):
    """Registration state is ambiguous or immutable data conflicts."""


class BitrixControlAdmissionError(BitrixSourceInstanceError):
    """Control migration, registration, or dispatch state is not ready."""


@dataclass(frozen=True)
class BitrixSourceInstance:
    source_key: str
    source_instance_id: str
    status: str
    created: bool = False


class BitrixSourceInstanceRepository:
    """Repository restricted to the canonical Bitrix source family."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def register(self, source_key: str, source_instance_id: str) -> BitrixSourceInstance:
        self._validate_source_key(source_key)
        slug = canonical_source_instance_id(source_instance_id, allow_legacy_default=True)

        def _work(tx: ManagedTransaction) -> BitrixSourceInstance:
            record = tx.run(
                REGISTER_BITRIX_SOURCE_INSTANCE,
                source_key=BITRIX_SOURCE_KEY,
                source_instance_id=slug,
                creation_token=uuid.uuid4().hex,
            ).single()
            if record is None:
                raise BitrixSourceInstanceConflictError("Bitrix source registration conflicts")
            return BitrixSourceInstance(
                BITRIX_SOURCE_KEY, slug, "active", record["created"] is True
            )

        return self._client.execute_write(_work)

    def require_active(self, source_key: str, source_instance_id: str) -> BitrixSourceInstance:
        self._validate_source_key(source_key)
        slug = canonical_source_instance_id(source_instance_id, allow_legacy_default=True)

        def _work(tx: ManagedTransaction) -> BitrixSourceInstance:
            record = tx.run(
                REQUIRE_ACTIVE_BITRIX_SOURCE_INSTANCE,
                source_key=BITRIX_SOURCE_KEY,
                source_instance_id=slug,
            ).single()
            if record is None:
                raise BitrixSourceInstanceMissingError("Bitrix source registration is missing")
            matches = record["matches"]
            statuses = record["statuses"]
            source_matches = record["source_matches"]
            relationship_count = record["relationship_count"]
            source_keys = record["source_keys"]
            source_active = record["source_active"]
            values = (matches, source_matches, relationship_count)
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in values
            ):
                raise BitrixSourceInstanceConflictError("Bitrix source registration is invalid")
            if not isinstance(statuses, list) or not all(
                isinstance(status, str) for status in statuses
            ):
                raise BitrixSourceInstanceConflictError("Bitrix source registration is invalid")
            if not isinstance(source_keys, list) or not all(
                isinstance(source_key, str) for source_key in source_keys
            ):
                raise BitrixSourceInstanceConflictError("Bitrix source registration is invalid")
            if not isinstance(source_active, list) or not all(
                isinstance(is_active, bool) for is_active in source_active
            ):
                raise BitrixSourceInstanceConflictError("Bitrix source registration is invalid")
            if matches == 0:
                raise BitrixSourceInstanceMissingError("Bitrix source registration is missing")
            if matches != 1 or len(statuses) != 1:
                raise BitrixSourceInstanceConflictError("Bitrix source registration is ambiguous")
            status = statuses[0]
            if status == "disabled":
                raise BitrixSourceInstanceDisabledError("Bitrix source registration is disabled")
            if status != "active":
                raise BitrixSourceInstanceConflictError("Bitrix source registration is invalid")
            if (
                source_matches != 1
                or relationship_count != 1
                or source_keys != [BITRIX_SOURCE_KEY]
                or source_active != [True]
            ):
                raise BitrixSourceInstanceConflictError("Bitrix source registration is ambiguous")
            return BitrixSourceInstance(BITRIX_SOURCE_KEY, slug, status)

        return self._client.execute_read(_work)

    def disable(self, source_key: str, source_instance_id: str, actor: str, reason: str) -> None:
        self._validate_source_key(source_key)
        slug = canonical_source_instance_id(source_instance_id, allow_legacy_default=True)
        if slug == "legacy-default":
            raise ValueError("legacy-default Bitrix registration cannot be disabled")
        if not actor.strip() or not reason.strip():
            raise ValueError("disable actor and reason must be non-empty")
        # Classify registration absence, lifecycle state, and topology ambiguity before
        # attempting the guarded mutation. A race after this read fails closed below.
        self.require_active(BITRIX_SOURCE_KEY, slug)

        def _work(tx: ManagedTransaction) -> None:
            record = tx.run(
                DISABLE_BITRIX_SOURCE_INSTANCE,
                source_key=BITRIX_SOURCE_KEY,
                source_instance_id=slug,
                actor=actor[:200],
                reason=reason[:1000],
            ).single()
            if record is None:
                raise BitrixSourceInstanceConflictError(
                    "Bitrix source registration cannot be disabled while active controls exist"
                )

        self._client.execute_write(_work)

    def admit(self, *, control_instance_id: str, source_instance_id: str) -> None:
        # Do not let a completed marker mask dropped or malformed replacement
        # constraints. This check is deliberately read-only and happens before
        # the registration/dispatch query, client construction, or publication.
        from src.graph.ingestion_control_instance_migration import (
            assert_ingestion_control_ready,
        )

        control = canonical_source_instance_id(control_instance_id, allow_legacy_default=True)
        source = canonical_source_instance_id(source_instance_id, allow_legacy_default=True)
        assert_ingestion_control_ready(self._client)

        def _work(tx: ManagedTransaction) -> None:
            record = tx.run(
                ADMIT_BITRIX_CONTROL_INSTANCE,
                control_instance_id=control,
                source_instance_id=source,
            ).single()
            if record is None:
                raise BitrixControlAdmissionError("Bitrix control admission is not ready")

        self._client.execute_write(_work)

    @staticmethod
    def _validate_source_key(source_key: str) -> None:
        if source_key != BITRIX_SOURCE_KEY:
            raise ValueError("Bitrix source-instance repository requires source_key='bitrix_chat'")


def admit_configured_bitrix_control(
    settings: Settings,
    control_instance_id: str = "legacy-default",
) -> str:
    """Fail closed before a Bitrix client, source call, or Celery publication."""
    from src.ingestion_config import get_ingestion_config
    from src.source_instances import effective_control_instance_id, effective_source_instance_id

    control = effective_control_instance_id(control_instance_id)
    source = effective_source_instance_id(
        get_ingestion_config().bitrix_openlines.source_instance_id
    )
    client = Neo4jClient(settings)
    try:
        BitrixSourceInstanceRepository(client).admit(
            control_instance_id=control,
            source_instance_id=source,
        )
    finally:
        client.close()
    return control
