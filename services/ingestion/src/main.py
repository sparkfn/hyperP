"""CLI entry point and reusable runner for the ingestion service."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from typing import Protocol, TypedDict, runtime_checkable

import httpx
from neo4j import ManagedTransaction
from redis import Redis

from src.config import get_settings
from src.connectors.base import SourceConnector
from src.connectors.bitrix import BitrixChatConnector
from src.connectors.bitrix_openlines.client import BitrixOpenLinesClient
from src.connectors.bitrix_openlines.connector import BitrixOpenLinesConnector
from src.connectors.bitrix_openlines.dialog_cache import RedisDialogConfigCache
from src.connectors.bitrix_openlines.watermark import (
    RedisWatermarkStore as BitrixOpenLinesWatermarkStore,
)
from src.connectors.dumps.connectors import get_dump_connector
from src.connectors.dumps.reader import resolve_dump_path
from src.connectors.eko import EkoConnector, EkoSalesConnector
from src.connectors.fundbox import (
    FundboxConnector,
    FundboxContactsConnector,
    FundboxLegacyConnector,
    FundboxMergedUsersConnector,
    FundboxSalesConnector,
)
from src.connectors.fundbox_api import (
    FundboxApiClient,
    FundboxApiCredentials,
    FundboxContactsApiConnector,
    FundboxSalesApiConnector,
    FundboxUsersApiConnector,
)
from src.connectors.fundbox_api.checkpoints import (
    load_source_ids,
    load_watermark,
    save_reconciliation_state,
)
from src.connectors.fundbox_api.connectors import FundboxApiConnector
from src.connectors.phppos_api import (
    EkoApiConnector,
    EkoSalesApiConnector,
    SpeedZoneApiConnector,
    SpeedZoneSalesApiConnector,
)
from src.connectors.phppos_api.client import ApiCredentials, PhpposApiClient
from src.connectors.sggov.bankruptcy_api import SGGovernmentBankruptcyApiConnector
from src.connectors.sggov.rental_flats_api import (
    SGGovernmentRentalFlatsApiClient,
    SGGovernmentRentalFlatsApiConnector,
)
from src.connectors.speedzone import SpeedZoneConnector, SpeedZoneSalesConnector
from src.connectors.whatsadmin_api.client import WhatsAdminApiClient
from src.connectors.whatsadmin_api.connector import WhatsAdminChatApiConnector
from src.connectors.whatsadmin_api.credentials import WhatsAdminCredentialResolver
from src.connectors.whatsadmin_api.watermark import RedisWatermarkStore
from src.connectors.whatsapp import WhatsAppChatConnector
from src.exclusions import (
    ExclusionContext,
    build_exclusion_context,
    is_excluded_email,
    is_excluded_phone,
    is_excluded_source_id,
)
from src.graph import queries
from src.graph.bootstrap import bootstrap_entities_and_sources
from src.graph.client import Neo4jClient
from src.graph.incremental_checkpoints import Neo4jCheckpointRedis
from src.graph.migrations import apply_data_migrations
from src.graph.schema_init import apply_deferred_source_record_constraints, apply_schema
from src.ingestion_config import get_ingestion_config
from src.llm import validate_ingestion_llm_readiness
from src.models import IngestResult, JsonValue, RecordType, SourceRecordEnvelope
from src.pipeline import IngestPipeline
from src.pipeline_addresses import ingest_address_record
from src.pipeline_crm import (
    ingest_call_record,
    ingest_crm_history_record,
    link_conversation_to_crm_history,
    link_crm_history_to_existing_conversations,
)
from src.pipeline_sales import (
    drain_pending_customer_sales,
    ingest_sales_record,
    propose_vehicle_matches_for_pending_sales,
)
from src.retirement import retire_source_evidence

logger = logging.getLogger(__name__)

_URL_IN_FAILURE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_SECRET_IN_FAILURE = re.compile(
    r"\b(api[_-]?key|token|password|secret)\b([\"']?)(\s*[:=]\s*)"
    r"([\"']?)([^\s,;}\]]+)",
    re.IGNORECASE,
)
_BEARER_IN_FAILURE = re.compile(
    r"\b(Authorization\s*:\s*)?Bearer\s+[^\s,;}\]]+",
    re.IGNORECASE,
)


@runtime_checkable
class _ClosableConnector(Protocol):
    def close(self) -> None: ...


@runtime_checkable
class _WatermarkCommitter(Protocol):
    def commit_watermark(self) -> None: ...


@runtime_checkable
class _FailureCheckpointReporter(Protocol):
    def failure_checkpoint(self) -> dict[str, JsonValue]: ...


@runtime_checkable
class _RecordOutcomeReporter(Protocol):
    def record_processed(self, *, succeeded: bool) -> None: ...


@runtime_checkable
class _ConnectorErrorReporter(Protocol):
    def connector_error_count(self) -> int: ...


@runtime_checkable
class _PartialFailureProgressCommitter(Protocol):
    def commit_progress_with_errors(self) -> bool: ...


class FailureSummary(TypedDict):
    category: str
    exception_class: str
    message: str
    source: str
    mode: str
    task_id: str | None
    checkpoint: dict[str, JsonValue]


def _build_failure_summary(
    exc: Exception,
    *,
    source_key: str,
    mode: str,
    task_id: str | None,
    checkpoint: dict[str, JsonValue],
) -> FailureSummary:
    message = _safe_failure_message(exc)
    exception_class = type(exc).__name__
    if "entity_key" in message and "parameter" in message.lower():
        category = "sales_entity_key"
    elif "DeadlockDetected" in message or "deadlock" in message.lower():
        category = "neo4j_deadlock"
    elif isinstance(exc, httpx.TimeoutException):
        category = "upstream_timeout"
    else:
        category = "distinct"
    return {
        "category": category,
        "exception_class": exception_class,
        "message": message,
        "source": source_key,
        "mode": mode,
        "task_id": task_id,
        "checkpoint": checkpoint,
    }


def _safe_failure_message(exc: Exception) -> str:
    """Return bounded diagnostic text without URLs or credential-like values."""
    without_urls = _URL_IN_FAILURE.sub("[redacted-url]", str(exc))
    without_secrets = _SECRET_IN_FAILURE.sub(r"\1\3[redacted]", without_urls)
    redacted = _BEARER_IN_FAILURE.sub(r"\1Bearer [redacted]", without_secrets)
    return redacted[:1000]


# Registry of available connectors keyed by source_key. New sources only need
# to add an entry here; the CLI and the Celery task share the same registry.
_CONNECTOR_REGISTRY: dict[str, type[SourceConnector]] = {
    "fundbox": FundboxConnector,
    "fundbox:contacts": FundboxContactsConnector,
    "fundbox:legacy": FundboxLegacyConnector,
    "fundbox:merged": FundboxMergedUsersConnector,
    "fundbox:sales": FundboxSalesConnector,
    "speedzone_phppos": SpeedZoneConnector,
    "speedzone_phppos:sales": SpeedZoneSalesConnector,
    "eko_phppos": EkoConnector,
    "eko_phppos:sales": EkoSalesConnector,
    "whatsapp_chat": WhatsAppChatConnector,
    "bitrix_chat": BitrixChatConnector,
    # SG government registers are not registered for database batch mode.
    # Dump mode uses get_dump_connector; sgrentalflats additionally supports
    # its dedicated HTTP extraction connector through mode="api".
}

_ADDRESS_ONLY_SOURCES = frozenset({"sgrentalflats"})

_PHPPOS_CUSTOMER_SCOPES = ("pos.customers.read",)
_PHPPOS_SALES_SCOPES = (
    "pos.sales.read",
    "pos.items.read",
    "pos.customers.read",
)
_PHPPOS_SCOPES_BY_SOURCE = {
    "eko_phppos": _PHPPOS_CUSTOMER_SCOPES,
    "eko_phppos:sales": _PHPPOS_SALES_SCOPES,
    "speedzone_phppos": _PHPPOS_CUSTOMER_SCOPES,
    "speedzone_phppos:sales": _PHPPOS_SALES_SCOPES,
}


def _is_address_only_source(source_key: str) -> bool:
    return source_key in _ADDRESS_ONLY_SOURCES


def _mark_run_failed(
    client: Neo4jClient,
    ingest_run_id: str,
    record_count: int,
    rejected_count: int,
    summary: FailureSummary,
) -> None:
    """Best-effort finaliser that records a structured terminal failure.

    Swallows any secondary failure so the original exception propagates to
    the Celery task handler.
    """
    try:

        def _work(tx: ManagedTransaction) -> None:
            tx.run(
                queries.MARK_INGEST_RUN_FAILED,
                ingest_run_id=ingest_run_id,
                record_count=record_count,
                rejected_count=rejected_count,
                failure_category=summary["category"],
                failure_exception_class=summary["exception_class"],
                failure_message=summary["message"],
                failure_source=summary["source"],
                failure_mode=summary["mode"],
                failure_task_id=summary["task_id"],
                failure_checkpoint=json.dumps(summary["checkpoint"], default=str),
            )

        with client.session() as session:
            session.execute_write(_work)
        logger.warning("Marked IngestRun %s -> failed (%s)", ingest_run_id, summary["category"])
    except Exception:
        logger.exception("Failed to mark IngestRun %s as failed", ingest_run_id)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)


def _finalize_connector_progress(connector: object, *, error_count: int) -> None:
    if error_count == 0 and isinstance(connector, _WatermarkCommitter):
        connector.commit_watermark()
    elif isinstance(connector, _PartialFailureProgressCommitter):
        if connector.commit_progress_with_errors() and isinstance(connector, _WatermarkCommitter):
            connector.commit_watermark()


def _report_record_outcome(connector: object, *, succeeded: bool) -> None:
    if isinstance(connector, _RecordOutcomeReporter):
        connector.record_processed(succeeded=succeeded)


def create_phppos_api_client(source_key: str) -> PhpposApiClient:
    settings = get_settings()
    tenant_id = (
        settings.eko_phppos_api_tenant_id
        if source_key.startswith("eko_phppos")
        else settings.speedzone_phppos_api_tenant_id
    )
    credentials = ApiCredentials(
        base_url=settings.phppos_api_base_url,
        client_id=settings.phppos_api_client_id,
        client_secret=settings.phppos_api_client_secret.get_secret_value(),
        tenant_id=tenant_id,
        page_size=settings.phppos_api_page_size,
        scopes=_PHPPOS_SCOPES_BY_SOURCE[source_key],
    )
    return PhpposApiClient(
        credentials,
        http=httpx.Client(timeout=settings.phppos_api_timeout_seconds),
        max_attempts=settings.phppos_api_max_attempts,
    )


def create_sgbankruptcy_api_connector() -> SGGovernmentBankruptcyApiConnector:
    """Build the authenticated SG bankruptcy API connector from settings."""
    settings = get_settings()
    return SGGovernmentBankruptcyApiConnector(
        settings.sgbankruptcy_api_base_url,
        settings.sgbankruptcy_api_key.get_secret_value(),
        page_size=settings.sgbankruptcy_api_page_size,
        http=httpx.Client(timeout=settings.sgbankruptcy_api_timeout_seconds),
        max_attempts=settings.sgbankruptcy_api_max_attempts,
    )


def create_sgrentalflats_api_client() -> SGGovernmentRentalFlatsApiClient:
    settings = get_settings()
    base_url = settings.sgrentalflats_api_base_url
    api_key = settings.sgrentalflats_api_key.get_secret_value()
    page_size = settings.sgrentalflats_api_page_size
    SGGovernmentRentalFlatsApiClient.validate_config(
        base_url=base_url,
        api_key=api_key,
        page_size=page_size,
    )
    return SGGovernmentRentalFlatsApiClient(
        base_url=base_url,
        api_key=api_key,
        page_size=page_size,
        http=httpx.Client(timeout=settings.sgrentalflats_api_timeout_seconds),
    )


def create_whatsadmin_api_connector(
    entity_key: str | None = None,
    *,
    incremental: bool = True,
    checkpoint_store: Neo4jCheckpointRedis | None = None,
) -> WhatsAdminChatApiConnector:
    settings = get_settings()
    resolver = WhatsAdminCredentialResolver(
        base_url=settings.whatsadmin_api_base_url,
        eko_api_key=settings.whatsadmin_eko_api_key,
        speedzone_api_key=settings.whatsadmin_speedzone_api_key,
    )
    clients = tuple(
        WhatsAdminApiClient(
            credential=credential,
            page_size=settings.whatsadmin_api_page_size,
            timeout_seconds=settings.whatsadmin_api_timeout_seconds,
            max_attempts=settings.whatsadmin_api_max_attempts,
            retry_base_delay_seconds=settings.whatsadmin_api_retry_base_delay_seconds,
        )
        for credential in resolver.resolve_job(entity_key)
    )
    redis = checkpoint_store or Redis.from_url(settings.celery_broker_url, decode_responses=True)
    legacy_entity = settings.whatsadmin_legacy_entity
    return WhatsAdminChatApiConnector(
        clients,
        RedisWatermarkStore(redis, legacy_entity=legacy_entity),
        legacy_entity=legacy_entity,
        incremental=incremental,
    )


def create_bitrix_openlines_connector(
    mode: str,
    *,
    incremental: bool = True,
    checkpoint_store: Neo4jCheckpointRedis | None = None,
) -> BitrixOpenLinesConnector:
    settings = get_settings()
    ingestion_config = get_ingestion_config()
    client = BitrixOpenLinesClient(
        base_url=settings.bitrix_openlines_api_base_url.get_secret_value(),
        timeout_seconds=settings.bitrix_openlines_api_timeout_seconds,
        max_attempts=settings.bitrix_openlines_api_max_attempts,
        request_delay_seconds=settings.bitrix_openlines_api_request_delay_seconds,
    )
    redis = checkpoint_store or Redis.from_url(settings.celery_broker_url, decode_responses=True)
    dialog_redis = Redis.from_url(settings.celery_broker_url, decode_responses=True)
    return BitrixOpenLinesConnector(
        client,
        BitrixOpenLinesWatermarkStore(redis),
        ingestion_config.bitrix_openlines,
        mode=mode,
        company_mobile_numbers=settings.company_mobile_numbers,
        company_email_addresses=settings.company_email_addresses,
        internal_person_names=settings.internal_person_names,
        file_exclusions=ingestion_config.exclusions,
        dialog_cache=RedisDialogConfigCache(dialog_redis),
        incremental=incremental,
    )


def create_fundbox_api_client() -> FundboxApiClient:
    settings = get_settings()
    # Strip surrounding whitespace so a padded env value (e.g. " https://x ") is
    # tolerated end-to-end rather than tripping a misleading "must use HTTPS"
    # check or failing at request time.
    return FundboxApiClient(
        FundboxApiCredentials(
            base_url=settings.fundbox_api_base_url.strip(),
            username=settings.fundbox_api_username.strip(),
            password=settings.fundbox_api_password.get_secret_value(),
            page_size=settings.fundbox_api_page_size,
        ),
        http=httpx.Client(timeout=settings.fundbox_api_timeout_seconds),
        max_attempts=settings.fundbox_api_max_attempts,
    )


def get_connector(
    source_key: str,
    dump_path: str | None = None,
    *,
    mode: str = "batch",
    entity_key: str | None = None,
    incremental: bool = True,
    checkpoint_store: Neo4jCheckpointRedis | None = None,
) -> SourceConnector:
    """Return the appropriate connector for the given source key."""
    if entity_key is not None and (source_key != "whatsapp_chat" or mode != "api"):
        raise ValueError("entity_key is only valid for whatsapp_chat API ingestion")
    if dump_path is not None:
        settings = get_settings()
        resolved_dump_path = resolve_dump_path(dump_path, settings.dumps_root)
        return get_dump_connector(source_key, resolved_dump_path)
    if source_key == "bitrix_chat" and mode in {"api", "backfill"}:
        if checkpoint_store is None:
            return create_bitrix_openlines_connector(mode, incremental=incremental)
        return create_bitrix_openlines_connector(
            mode,
            incremental=incremental,
            checkpoint_store=checkpoint_store,
        )
    if mode == "backfill":
        raise ValueError(f"Backfill mode is not supported for source {source_key!r}")
    if mode == "api":
        if source_key == "sgbankruptcy":
            return create_sgbankruptcy_api_connector()
        if source_key == "sgrentalflats":
            return SGGovernmentRentalFlatsApiConnector(create_sgrentalflats_api_client())
        if source_key == "whatsapp_chat":
            if entity_key is None:
                if checkpoint_store is None:
                    return create_whatsadmin_api_connector(incremental=incremental)
                return create_whatsadmin_api_connector(
                    incremental=incremental,
                    checkpoint_store=checkpoint_store,
                )
            if checkpoint_store is None:
                return create_whatsadmin_api_connector(entity_key, incremental=incremental)
            return create_whatsadmin_api_connector(
                entity_key,
                incremental=incremental,
                checkpoint_store=checkpoint_store,
            )
        fundbox_types: dict[str, type[FundboxApiConnector]] = {
            "fundbox": FundboxUsersApiConnector,
            "fundbox:contacts": FundboxContactsApiConnector,
            "fundbox:sales": FundboxSalesApiConnector,
        }
        fundbox_type = fundbox_types.get(source_key)
        if fundbox_type is not None:
            settings = get_settings()
            api_client = create_fundbox_api_client()
            if incremental:
                store = checkpoint_store or Redis.from_url(settings.celery_broker_url)
                with store:
                    updated_since = load_watermark(
                        store,
                        source_key,
                        settings.fundbox_api_overlap_seconds,
                    )
                    previous_source_ids = load_source_ids(store, source_key)
            else:
                updated_since = None
                previous_source_ids = None
            return fundbox_type(
                api_client,
                updated_since=updated_since,
                previous_source_ids=previous_source_ids,
            )
        api_types: dict[
            str,
            type[
                EkoApiConnector
                | EkoSalesApiConnector
                | SpeedZoneApiConnector
                | SpeedZoneSalesApiConnector
            ],
        ] = {
            "eko_phppos": EkoApiConnector,
            "eko_phppos:sales": EkoSalesApiConnector,
            "speedzone_phppos": SpeedZoneApiConnector,
            "speedzone_phppos:sales": SpeedZoneSalesApiConnector,
        }
        try:
            connector_type = api_types[source_key]
        except KeyError as exc:
            raise ValueError(f"API mode is not supported for source {source_key!r}") from exc
        updated_since = None
        if incremental and checkpoint_store is not None:
            updated_since = checkpoint_store.get(
                f"profile_unifier:phppos_api:watermark:{source_key}"
            )
        return connector_type(
            create_phppos_api_client(source_key),
            updated_since=updated_since,
            watermark_store=checkpoint_store,
        )
    try:
        return _CONNECTOR_REGISTRY[source_key]()
    except KeyError as exc:
        available = ", ".join(sorted(_CONNECTOR_REGISTRY))
        raise ValueError(f"Unknown source key: {source_key!r}. Available: {available}") from exc


class IngestionSummary(TypedDict):
    """Summary returned by :func:`run_ingestion`. Celery serializes this as JSON."""

    ingest_run_id: str
    status: str
    succeeded: int
    errors: int
    skipped: int
    source_key: str
    mode: str
    dump_path: str | None
    entity_key: str | None


def _create_ingest_run(client: Neo4jClient, source_key: str, mode: str) -> str:
    """Create an IngestRun node and return its ID."""

    def _tx(tx: ManagedTransaction) -> str:
        result = tx.run(
            queries.CREATE_INGEST_RUN,
            source_key=source_key,
            run_type=mode,
            mode=mode,
        )
        record = result.single()
        assert record is not None, "CREATE_INGEST_RUN must return a row"
        run_id_value = record["ingest_run_id"]
        assert isinstance(run_id_value, str)
        return run_id_value

    with client.session() as session:
        return session.execute_write(_tx)


def finalize_ingest_run(
    client: Neo4jClient,
    ingest_run_id: str,
    status: str,
    record_count: int,
    rejected_count: int,
    checkpoint_store: Neo4jCheckpointRedis | None = None,
) -> None:
    """Update the IngestRun with final status and counts."""

    def _tx(tx: ManagedTransaction) -> None:
        tx.run(
            queries.UPDATE_INGEST_RUN,
            ingest_run_id=ingest_run_id,
            status=status,
            record_count=record_count,
            rejected_count=rejected_count,
        )
        if checkpoint_store is not None and status in {"completed", "completed_with_errors"}:
            checkpoint_store.flush(tx, ingest_run_id, status)

    with client.session() as session:
        session.execute_write(_tx)
    if checkpoint_store is not None:
        checkpoint_store.clear_staged()


def _process_record(
    client: Neo4jClient,
    pipeline: IngestPipeline,
    envelope: SourceRecordEnvelope,
    ingest_run_id: str,
    exclusion_context: ExclusionContext,
) -> IngestResult:
    """Route a single envelope to the correct ingestion pipeline."""
    if envelope.record_type == RecordType.CRM_HISTORY:
        result = ingest_crm_history_record(client, envelope, ingest_run_id=ingest_run_id)
        if result.source_record_pk is not None and not result.dropped:
            link_crm_history_to_existing_conversations(client, envelope, result.source_record_pk)
        return result
    if envelope.record_type == RecordType.CALL:
        return ingest_call_record(client, envelope, ingest_run_id=ingest_run_id)
    if envelope.record_type == RecordType.SALES:
        return ingest_sales_record(
            client,
            envelope,
            ingest_run_id=ingest_run_id,
            exclusion_context=exclusion_context,
        )
    if _is_address_only_source(envelope.source_system):
        return ingest_address_record(client, envelope, ingest_run_id=ingest_run_id)
    result = pipeline.ingest(
        envelope,
        ingest_run_id=ingest_run_id,
        exclusion_context=exclusion_context,
    )
    if (
        envelope.record_type == RecordType.CONVERSATION
        and result.source_record_pk is not None
        and not result.dropped
        and _has_crm_activity_references(envelope)
    ):
        linked = link_conversation_to_crm_history(client, envelope, result.source_record_pk)
        if not linked:
            logger.warning(
                "Conversation %s was persisted without a matching CRM history item",
                envelope.source_record_id,
            )
    return result


def _has_crm_activity_references(envelope: SourceRecordEnvelope) -> bool:
    """Return whether a conversation carries one or more CRM activity IDs."""
    activity_ids = envelope.raw_payload.get("crm_activity_ids")
    return isinstance(activity_ids, list) and any(
        isinstance(activity_id, str) and activity_id for activity_id in activity_ids
    )


def _record_is_excluded(envelope: SourceRecordEnvelope, context: ExclusionContext) -> bool:
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


def _ingest_all_records(
    client: Neo4jClient,
    pipeline: IngestPipeline,
    connector: SourceConnector,
    ingest_run_id: str,
    exclusion_context: ExclusionContext | None = None,
) -> tuple[int, int, int]:
    """Process all connector records; the run owner releases connector resources."""
    return _ingest_all_records_open(
        client,
        pipeline,
        connector,
        ingest_run_id,
        exclusion_context,
    )


def _ingest_all_records_open(
    client: Neo4jClient,
    pipeline: IngestPipeline,
    connector: SourceConnector,
    ingest_run_id: str,
    exclusion_context: ExclusionContext | None = None,
) -> tuple[int, int, int]:
    """Process every record from the connector. Returns (success, errors, skipped)."""
    success = errors = skipped = 0
    active_exclusion_context = (
        exclusion_context if exclusion_context is not None else _load_exclusion_context()
    )
    for raw_record in connector.fetch_records():
        retirement_id = raw_record.get("_retire_source_record_id")
        retired_at = raw_record.get("_retired_at")
        if isinstance(retirement_id, str) and isinstance(retired_at, str):
            retire_source_evidence(
                client,
                connector.get_source_key(),
                retirement_id,
                retired_at,
            )
            success += 1
            _report_record_outcome(connector, succeeded=True)
            continue
        envelope = SourceRecordEnvelope.model_validate(
            {"source_system": connector.get_source_key(), **raw_record},
        )
        if _record_is_excluded(envelope, active_exclusion_context):
            skipped += 1
            logger.info("  %s -> excluded", envelope.source_record_id)
            _report_record_outcome(connector, succeeded=True)
            continue
        result = _process_record(
            client,
            pipeline,
            envelope,
            ingest_run_id,
            active_exclusion_context,
        )
        if result.skipped_duplicate:
            skipped += 1
        elif result.errors:
            errors += 1
        elif result.dropped:
            skipped += 1
        else:
            success += 1
        _report_record_outcome(connector, succeeded=not bool(result.errors))
        if result.dropped:
            logger.info(
                "  %s -> dropped (no match — match-only source)",
                result.source_record_id,
            )
        elif result.person_id is None:
            logger.info(
                "  %s -> address-only%s",
                result.source_record_id,
                " (DUPLICATE)" if result.skipped_duplicate else "",
            )
        else:
            logger.info(
                "  %s -> person=%s new=%s decision=%s candidates=%d%s",
                result.source_record_id,
                result.person_id,
                result.is_new_person,
                result.match_decision,
                result.candidate_count,
                " (DUPLICATE)" if result.skipped_duplicate else "",
            )
    return success, errors, skipped


def initialize_ingestion_graph() -> None:
    settings = get_settings()
    client = Neo4jClient(settings)
    try:
        client.verify_connectivity()
        apply_schema(client)
        bootstrap_entities_and_sources(client)
        apply_data_migrations(client)
        apply_deferred_source_record_constraints(client)
    finally:
        client.close()


def run_ingestion(
    source_key: str,
    mode: str = "batch",
    dump_path: str | None = None,
    *,
    entity_key: str | None = None,
    initialize_graph: bool = True,
    existing_ingest_run_id: str | None = None,
    task_id: str | None = None,
    incremental: bool = True,
) -> IngestionSummary:
    """Execute one ingestion run end-to-end."""
    settings = get_settings()
    if mode == "dump" and dump_path is None:
        raise ValueError("dump_path is required when mode='dump'")
    if mode != "dump" and dump_path is not None:
        raise ValueError("dump_path is only valid when mode='dump'")
    if entity_key is not None and (source_key != "whatsapp_chat" or mode != "api"):
        raise ValueError("entity_key is only valid for whatsapp_chat API ingestion")
    logger.info(
        "Starting ingestion: source=%s mode=%s entity=%s incremental=%s",
        source_key,
        mode,
        entity_key or "all",
        incremental,
    )

    if initialize_graph:
        initialize_ingestion_graph()

    client = Neo4jClient(settings)
    connector: SourceConnector | None = None
    checkpoint_store: Neo4jCheckpointRedis | None = None
    try:
        if not initialize_graph:
            client.verify_connectivity()

        pipeline = IngestPipeline(client)
        # Create the IngestRun before building the connector so a
        # connector-construction failure (e.g. a source dispatched before its
        # env is provisioned) is recorded as a failed run instead of vanishing
        # from the runs UI.
        ingest_run_id = existing_ingest_run_id or _create_ingest_run(client, source_key, mode)
        logger.info(
            "IngestRun %s %s",
            ingest_run_id,
            "reused" if existing_ingest_run_id is not None else "created",
        )

        success = errors = skipped = 0
        try:
            if (
                incremental
                and mode == "api"
                and source_key
                in {
                    "fundbox",
                    "fundbox:contacts",
                    "fundbox:sales",
                    "bitrix_chat",
                    "whatsapp_chat",
                    "eko_phppos",
                    "eko_phppos:sales",
                    "speedzone_phppos",
                    "speedzone_phppos:sales",
                }
            ):
                broker_url = getattr(settings, "celery_broker_url", None)
                legacy = Redis.from_url(broker_url) if isinstance(broker_url, str) else None
                checkpoint_store = Neo4jCheckpointRedis(
                    client,
                    source_key,
                    legacy=legacy,
                    active_ingest_run_id=ingest_run_id,
                )
            connector = get_connector(
                source_key,
                dump_path if mode == "dump" else None,
                mode=mode,
                entity_key=entity_key,
                incremental=incremental,
                checkpoint_store=checkpoint_store,
            )
            logger.info("Connector=%s", type(connector).__name__)
            if source_key in {"bitrix_chat", "whatsapp_chat"}:
                validate_ingestion_llm_readiness()
            exclusion_context = _load_exclusion_context()
            success, errors, skipped = _ingest_all_records(
                client,
                pipeline,
                connector,
                ingest_run_id,
                exclusion_context,
            )
            if isinstance(connector, _ConnectorErrorReporter):
                errors += connector.connector_error_count()
            drained = drain_pending_customer_sales(
                client,
                exclusion_context=exclusion_context,
            )
            if drained:
                logger.info("Drained %d pending sales records", drained)
            proposed = propose_vehicle_matches_for_pending_sales(client)
            if proposed:
                logger.info("Proposed %d vehicle matches for pending sales", proposed)
            _finalize_connector_progress(connector, error_count=errors)
        except Exception as exc:
            checkpoint: dict[str, JsonValue] = {
                "dump_path": dump_path,
                "entity_key": entity_key,
            }
            if isinstance(connector, _FailureCheckpointReporter):
                checkpoint.update(connector.failure_checkpoint())
            summary = _build_failure_summary(
                exc,
                source_key=source_key,
                mode=mode,
                task_id=task_id,
                checkpoint=checkpoint,
            )
            _mark_run_failed(
                client,
                ingest_run_id,
                success + errors + skipped,
                errors,
                summary,
            )
            raise

        final_status = "completed" if errors == 0 else "completed_with_errors"
        if (
            final_status == "completed"
            and incremental
            and isinstance(connector, FundboxApiConnector)
            and connector.reconciliation_completed
            and connector.current_source_ids is not None
        ):
            assert checkpoint_store is not None
            save_reconciliation_state(
                checkpoint_store,
                source_key,
                connector.current_source_ids,
                connector.latest_effective_updated_at,
            )
        finalize_ingest_run(
            client,
            ingest_run_id,
            final_status,
            success + errors + skipped,
            errors,
            checkpoint_store,
        )
        logger.info(
            "Ingestion complete: %d succeeded, %d errors, %d skipped",
            success,
            errors,
            skipped,
        )
        return {
            "ingest_run_id": ingest_run_id,
            "status": final_status,
            "succeeded": success,
            "errors": errors,
            "skipped": skipped,
            "source_key": source_key,
            "mode": mode,
            "dump_path": dump_path,
            "entity_key": entity_key,
        }
    finally:
        if isinstance(connector, _ClosableConnector):
            connector.close()
        client.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="profile-unifier-ingestion",
        description="Ingestion service for the profile unification platform",
    )
    parser.add_argument("--source-key", required=True)
    parser.add_argument("--mode", choices=["batch", "backfill", "dump", "api"], default="batch")
    parser.add_argument("--dump-path", default=None)
    parser.add_argument("--entity-key", choices=["eko", "speedzone"], default=None)
    args = parser.parse_args(argv)

    setup_logging(get_settings().log_level)
    try:
        run_ingestion(args.source_key, args.mode, args.dump_path, entity_key=args.entity_key)
    except Exception:
        logger.exception("Fatal error during ingestion")
        sys.exit(1)


if __name__ == "__main__":
    main()
