"""Bounded descriptors for the Eko and SpeedZone PHPPOS deltas.

Discovery is convention-based: ``connectors.registry`` imports this module
because its name ends in ``bounded_descriptor`` and registers every entry of
``DESCRIPTORS``. Each tenant scope owns an independent descriptor, writer, and
tenant-dedicated transport, so a run never reuses another tenant's cursor,
records, or credentials.
"""

from __future__ import annotations

import httpx

from src.bounded_ingestion_models import (
    AttemptContext,
    BoundedConnector,
    BoundedUnitWriter,
    OccurrenceContext,
    RunScope,
)
from src.config import Settings, get_settings
from src.connectors.phppos_api import bounded_checkpoint
from src.connectors.phppos_api.bounded_checkpoint import Resource
from src.connectors.phppos_api.bounded_connector import PhpposBoundedConnector
from src.connectors.phppos_api.bounded_writer import PhpposBoundedWriter
from src.connectors.phppos_api.client import ApiCredentials, PhpposApiClient
from src.resumable import CheckpointDescriptor

CONFIGURATION_VERSION = "phppos-bounded-config-v1"
CUSTOMER_SCOPES = ("pos.customers.read",)
SALES_SCOPES = ("pos.sales.read", "pos.items.read", "pos.customers.read")
SUPPORTED_SOURCES = (
    "eko_phppos",
    "eko_phppos:sales",
    "speedzone_phppos",
    "speedzone_phppos:sales",
)


class PhpposBoundedConfigurationError(RuntimeError):
    """The bounded PHPPOS transport is not configured for this source."""


class PhpposBoundedDescriptor:
    """One bounded PHPPOS source scope with adapter-local limits."""

    connector_version = bounded_checkpoint.CONNECTOR_VERSION
    configuration_version = CONFIGURATION_VERSION
    checkpoint_schema_version = bounded_checkpoint.CHECKPOINT_SCHEMA_VERSION
    supports_bootstrap = True
    supports_delta = True
    supports_one_time = False
    max_records_per_unit = 500
    # Bounds the full retry policy of one unit: 3 OAuth attempts, 3 page
    # attempts, and the single re-authorization pair after an HTTP 401.
    max_source_requests_per_unit = 8
    max_bytes_per_unit = 2_000_000
    max_extraction_calls_per_unit = 1
    max_close_seconds = 10.0
    max_retry_backoff_seconds = 600.0
    supports_deadline = True
    supports_cancellation = True

    def __init__(self, source_key: str) -> None:
        if source_key not in SUPPORTED_SOURCES:
            raise ValueError("bounded PHPPOS source is unsupported")
        self.source_key = source_key
        self.resource: Resource = bounded_checkpoint.resource_for_source(source_key)
        self.writer: BoundedUnitWriter = PhpposBoundedWriter(source_key)

    def initial_checkpoint(
        self,
        scope: RunScope,
        occurrence: OccurrenceContext | None,
    ) -> CheckpointDescriptor:
        """Open the stream at the first page of the caller-supplied frozen window."""
        window = bounded_checkpoint.PhpposSourceWindow.from_mapping(
            scope.source_window,
            source_key=scope.source_key,
            configuration_fingerprint=scope.configuration_fingerprint,
        )
        return bounded_checkpoint.initial_checkpoint(scope.source_key, window)

    def create(self, context: AttemptContext) -> BoundedConnector:
        client, tenant_id = create_bounded_client(self.source_key, self.resource)
        return PhpposBoundedConnector(
            source_key=self.source_key,
            resource=self.resource,
            expected_tenant_id=tenant_id,
            configuration_fingerprint=context.scope.configuration_fingerprint,
            client=client,
            max_records_per_unit=self.max_records_per_unit,
            max_source_requests_per_unit=self.max_source_requests_per_unit,
            max_bytes_per_unit=self.max_bytes_per_unit,
        )


def create_bounded_client(
    source_key: str,
    resource: Resource,
) -> tuple[PhpposApiClient, str]:
    """Build the tenant-dedicated transport for one bounded PHPPOS source.

    ``principal_tenant_id`` repeats the tenant the credential set serves: the
    bounded transport refuses to read a tenant with a principal that is not that
    tenant, so one tenant's credentials can never serve another tenant's run.
    """
    settings = get_settings()
    tenant_id = _tenant_id(source_key, settings)
    credentials = ApiCredentials(
        base_url=settings.phppos_api_base_url,
        client_id=settings.phppos_api_client_id,
        client_secret=settings.phppos_api_client_secret.get_secret_value(),
        tenant_id=tenant_id,
        page_size=settings.phppos_api_page_size,
        scopes=SALES_SCOPES if resource == "sales" else CUSTOMER_SCOPES,
        principal_tenant_id=tenant_id,
    )
    if not all(
        (
            credentials.base_url.strip(),
            credentials.client_id.strip(),
            credentials.client_secret.strip(),
            credentials.tenant_id.strip(),
        )
    ):
        raise PhpposBoundedConfigurationError("bounded PHPPOS transport is not configured")
    client = PhpposApiClient(
        credentials,
        http=httpx.Client(timeout=settings.phppos_api_timeout_seconds),
        max_attempts=settings.phppos_api_max_attempts,
    )
    return client, tenant_id


def _tenant_id(source_key: str, settings: Settings) -> str:
    if source_key.startswith("eko_phppos"):
        return settings.eko_phppos_api_tenant_id
    return settings.speedzone_phppos_api_tenant_id


DESCRIPTORS: tuple[PhpposBoundedDescriptor, ...] = tuple(
    PhpposBoundedDescriptor(source_key) for source_key in SUPPORTED_SOURCES
)
