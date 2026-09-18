"""Bounded WhatsAdmin descriptor, activation-gated on proven source capability.

The production descriptor stays non-ready until WhatsAdmin's deployed contract
proves the snapshot, session, retention, tombstone, and boundedness guarantees
the adapter depends on. Synthetic fixtures exercise the required contract; they
are not upstream capability evidence.
"""

from __future__ import annotations

from src.bounded_ingestion_models import AttemptContext, OccurrenceContext, RunScope
from src.config import get_settings
from src.connectors.whatsadmin_api.bounded_connector import (
    WhatsAdminBoundedConnector,
    initial_checkpoint,
)
from src.connectors.whatsadmin_api.bounded_state import (
    WhatsAdminBoundedState,
    bounded_graph_client,
    window_from_source_window,
)
from src.connectors.whatsadmin_api.bounded_writer import WhatsAdminBoundedWriter
from src.connectors.whatsadmin_api.client import WhatsAdminApiClient
from src.connectors.whatsadmin_api.credentials import (
    WHATSADMIN_ENTITIES,
    WhatsAdminCredential,
    WhatsAdminCredentialResolver,
    WhatsAdminEntity,
)
from src.connectors.whatsadmin_api.models import (
    REQUIRED_CAPABILITIES,
    CapabilityGuarantee,
    UpstreamCapability,
)
from src.graph.incremental_checkpoints import Neo4jCheckpointRedis
from src.resumable import CheckpointDescriptor

SOURCE_KEY = "whatsapp_chat"
CONNECTOR_VERSION = "whatsadmin-bounded-v1"
CONFIGURATION_VERSION = "whatsadmin-bounded-config-v1"
CHECKPOINT_SCHEMA_VERSION = 1
CONTRACT_VERSION = "whatsadmin-hyperp-extraction-v1"

#: Observed upstream capability. Every guarantee stays unproven until the
#: deployed WhatsAdmin contract is verified against the required behaviour.
UPSTREAM_CAPABILITY = UpstreamCapability(
    contract_version=CONTRACT_VERSION,
    cursor_retention_days=0,
    guarantees=tuple(
        CapabilityGuarantee(name=name, proven=False) for name in REQUIRED_CAPABILITIES
    ),
)


class WhatsAdminBoundedDescriptor:
    """Descriptor for the bounded WhatsAdmin chat adapter."""

    source_key = SOURCE_KEY
    connector_version = CONNECTOR_VERSION
    configuration_version = CONFIGURATION_VERSION
    checkpoint_schema_version = CHECKPOINT_SCHEMA_VERSION
    supports_bootstrap = True
    supports_delta = True
    supports_one_time = False
    max_records_per_unit = 50
    max_source_requests_per_unit = 1
    max_bytes_per_unit = 2_000_000
    max_extraction_calls_per_unit = 40
    max_close_seconds = 10.0
    max_retry_backoff_seconds = 3600.0
    supports_deadline = True
    supports_cancellation = True
    writer = WhatsAdminBoundedWriter()

    def readiness_block(self) -> str | None:
        """Return the upstream guarantees that deployed evidence has not proven."""
        if UPSTREAM_CAPABILITY.contract_version != CONTRACT_VERSION:
            return "whatsadmin_bounded_contract_version_mismatch"
        missing = UPSTREAM_CAPABILITY.missing()
        if missing:
            return "whatsadmin_bounded_capability_unproven:" + ",".join(missing)
        return None

    def initial_checkpoint(
        self,
        scope: RunScope,
        occurrence: OccurrenceContext | None,
    ) -> CheckpointDescriptor:
        """Build the first checkpoint from the immutable scope; no upstream I/O."""
        return initial_checkpoint(
            scope_window=scope.source_window,
            credential=self._credential(scope.entity_key, scope).api_key.get_secret_value(),
            connector_version=self.connector_version,
            schema_version=self.checkpoint_schema_version,
        )

    def create(self, context: AttemptContext) -> WhatsAdminBoundedConnector:
        """Build the bounded connector for one admitted attempt."""
        scope = context.scope
        credential = self._credential(scope.entity_key, scope)
        settings = get_settings()
        window = window_from_source_window(scope.source_window)
        client = WhatsAdminApiClient(
            credential=credential,
            page_size=settings.whatsadmin_api_page_size,
            timeout_seconds=settings.whatsadmin_api_timeout_seconds,
            max_attempts=settings.whatsadmin_api_max_attempts,
            retry_base_delay_seconds=settings.whatsadmin_api_retry_base_delay_seconds,
        )
        state = WhatsAdminBoundedState(
            Neo4jCheckpointRedis(
                bounded_graph_client(),
                scope.source_key,
                control_instance_id=scope.control_instance_id,
                reset_generation=scope.reset_generation,
            )
        )
        return WhatsAdminBoundedConnector(
            entity_key=_entity_key(scope.entity_key),
            client=client,
            credential=credential.api_key.get_secret_value(),
            state=state,
            window=window,
            legacy_entity=settings.whatsadmin_legacy_entity,
            max_records_per_unit=self.max_records_per_unit,
            max_bytes_per_unit=self.max_bytes_per_unit,
            max_extraction_calls_per_unit=self.max_extraction_calls_per_unit,
        )

    @staticmethod
    def _credential(entity_key: str | None, scope: RunScope) -> WhatsAdminCredential:
        """Resolve this entity's own credential; never fall back to the other."""
        settings = get_settings()
        resolver = WhatsAdminCredentialResolver(
            base_url=settings.whatsadmin_api_base_url,
            eko_api_key=settings.whatsadmin_eko_api_key,
            speedzone_api_key=settings.whatsadmin_speedzone_api_key,
        )
        return resolver.resolve(_entity_key(entity_key))


def _entity_key(value: str | None) -> WhatsAdminEntity:
    if value not in WHATSADMIN_ENTITIES:
        raise ValueError("bounded WhatsAdmin run requires an eko or speedzone entity")
    entity: WhatsAdminEntity = value
    return entity


DESCRIPTOR = WhatsAdminBoundedDescriptor()
