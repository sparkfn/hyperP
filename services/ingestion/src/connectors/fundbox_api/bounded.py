"""Bounded, replay-safe Fundbox change-window adapter."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

import httpx
from neo4j import ManagedTransaction

from src.bounded_ingestion_models import (
    AttemptContext,
    BoundedUnit,
    RecordDisposition,
    RunScope,
    UnitApplyResult,
    Usage,
)
from src.config import get_settings
from src.connectors.fundbox_api.client import (
    FundboxApiClient,
    FundboxApiCredentials,
)
from src.connectors.fundbox_api.connectors import (
    FundboxApiConnector,
    FundboxContactsApiConnector,
    FundboxSalesApiConnector,
    FundboxUsersApiConnector,
)
from src.connectors.fundbox_api.models import (
    MAX_CURSOR_LENGTH,
    MAX_SNAPSHOT_ID_LENGTH,
    BoundedIngestionPage,
)
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
from src.resumable import CheckpointCompatibility, CheckpointDescriptor, IngestionUnit
from src.retirement import retire_source_evidence_in_transaction

_CONTRACT_VERSION = "fundbox-change-feed-v1"
_CAPABILITY_EVIDENCE = "verified_upstream_contract"
_MIN_CURSOR_RETENTION_DAYS = 30
_MAX_NESTED_CHILDREN = 100


class FundboxBoundedClientProtocol(Protocol):
    def fetch_bounded_page(
        self,
        resource: str,
        *,
        snapshot_id: str,
        lower_change_version: int,
        upper_change_version: int,
        cursor: str | None,
        context: AttemptContext,
        max_bytes: int,
    ) -> BoundedIngestionPage: ...

    def iter_source(
        self,
        resource: str,
        *,
        updated_since: str | None = None,
    ) -> Iterator[dict[str, JsonValue]]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class _FrozenWindow:
    snapshot_id: str
    lower_change_version: int
    upper_change_version: int


@dataclass(frozen=True)
class _CursorState:
    """Decoded durable continuation position of one change-feed window."""

    continuation: str | None
    last_position: tuple[int, int] | None
    terminal: bool
    expires_at: datetime | None


class FundboxBoundedConnector:
    """Translate exactly one source page into one atomic bounded unit."""

    def __init__(
        self,
        *,
        source_key: str,
        resource: str,
        mapper_type: type[FundboxApiConnector],
        client: FundboxBoundedClientProtocol,
        max_records: int,
        max_bytes: int,
    ) -> None:
        self._source_key = source_key
        self._resource = resource
        self._client = client
        self._mapper = mapper_type(client)
        self._max_records = max_records
        self._max_bytes = max_bytes
        self._closed = False

    def validate_checkpoint(self, checkpoint: CheckpointDescriptor) -> CheckpointCompatibility:
        if checkpoint.connector_version != FundboxBoundedDescriptor.connector_version:
            return "incompatible"
        if checkpoint.schema_version != FundboxBoundedDescriptor.checkpoint_schema_version:
            return "incompatible"
        try:
            self._frozen_window(checkpoint.source_window)
            cursor = self._cursor(checkpoint)
        except ValueError:
            return "corrupted"
        if cursor.expires_at is not None and cursor.expires_at <= datetime.now(UTC):
            return "expired"
        evidence = checkpoint.source_window.get("capability_evidence")
        if evidence != _CAPABILITY_EVIDENCE:
            return "rejected"
        return "compatible"

    def fetch_one_unit(
        self,
        checkpoint: CheckpointDescriptor,
        context: AttemptContext,
    ) -> BoundedUnit:
        context.require_operation_budget(datetime.now(UTC), 0.001)
        window = self._frozen_window(checkpoint.source_window)
        cursor = self._cursor(checkpoint)
        page = self._client.fetch_bounded_page(
            self._resource,
            snapshot_id=window.snapshot_id,
            lower_change_version=window.lower_change_version,
            upper_change_version=window.upper_change_version,
            cursor=cursor.continuation,
            context=context,
            max_bytes=self._max_bytes,
        )
        if len(page.data) > self._max_records:
            raise ValueError("Fundbox page record cap exceeded")
        self._validate_page_progress(page, cursor)
        records, last_record_id = self._records(page)
        position = self._last_position(page, cursor.last_position)
        checkpoint_after = CheckpointDescriptor(
            phase=checkpoint.phase,
            cursor={
                "continuation": page.meta.next_cursor,
                "last_position": (
                    None if position is None else cast(list[JsonValue], list(position))
                ),
                "cursor_expires_at": page.meta.cursor_expires_at.astimezone(UTC).isoformat(),
                "terminal": page.meta.terminal,
            },
            source_window=checkpoint.source_window,
            last_committed_record_id=last_record_id or checkpoint.last_committed_record_id,
            connector_version=checkpoint.connector_version,
            schema_version=checkpoint.schema_version,
            replay_boundary=checkpoint.replay_boundary,
        )
        replay_id = _replay_id(self._source_key, checkpoint, window)
        return BoundedUnit(
            unit=IngestionUnit(
                checkpoint_before=checkpoint,
                checkpoint_after=checkpoint_after,
                records=tuple(records),
            ),
            replay_id=replay_id,
            usage=Usage(
                records=len(records),
                source_requests=1,
                pages=1,
                bytes_read=page.response_bytes or 0,
            ),
            terminal=page.meta.terminal,
        )

    def cancel(self) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._mapper.close()

    def _frozen_window(self, source_window: Mapping[str, JsonValue]) -> _FrozenWindow:
        contract_version = source_window.get("contract_version")
        resource = source_window.get("resource")
        snapshot_id = source_window.get("snapshot_id")
        lower = _source_int(source_window.get("lower_change_version"))
        upper = _source_int(source_window.get("upper_change_version"))
        retention = _source_int(source_window.get("cursor_retention_days"))
        if contract_version != _CONTRACT_VERSION or resource != self._resource:
            raise ValueError("Fundbox bounded source contract does not match descriptor")
        if (
            not isinstance(snapshot_id, str)
            or not snapshot_id.strip()
            or len(snapshot_id) > MAX_SNAPSHOT_ID_LENGTH
        ):
            raise ValueError("Fundbox snapshot_id is invalid")
        if lower is None or upper is None or upper < lower:
            raise ValueError("Fundbox change window is invalid")
        if retention is None or retention < _MIN_CURSOR_RETENTION_DAYS:
            raise ValueError("Fundbox cursor retention is insufficient")
        return _FrozenWindow(snapshot_id, lower, upper)

    @staticmethod
    def _cursor(checkpoint: CheckpointDescriptor) -> _CursorState:
        cursor = checkpoint.cursor
        continuation: str | None = None
        if cursor.get("continuation") is not None:
            raw_continuation = cursor["continuation"]
            if (
                not isinstance(raw_continuation, str)
                or not raw_continuation.strip()
                or len(raw_continuation) > MAX_CURSOR_LENGTH
            ):
                raise ValueError("Fundbox continuation cursor is invalid")
            continuation = raw_continuation
        position: tuple[int, int] | None = None
        if cursor.get("last_position") is not None:
            raw_position = cursor["last_position"]
            if not isinstance(raw_position, list) or len(raw_position) != 2:
                raise ValueError("Fundbox cursor position is invalid")
            change_version = _source_int(raw_position[0])
            root_id = _source_int(raw_position[1])
            if change_version is None or root_id is None:
                raise ValueError("Fundbox cursor position is invalid")
            position = (change_version, root_id)
        terminal = cursor.get("terminal")
        if not isinstance(terminal, bool):
            raise ValueError("Fundbox cursor terminal state is invalid")
        return _CursorState(
            continuation=continuation,
            last_position=position,
            terminal=terminal,
            expires_at=_utc_expiry(cursor.get("cursor_expires_at")),
        )

    def _validate_page_progress(
        self,
        page: BoundedIngestionPage,
        cursor: _CursorState,
    ) -> None:
        if page.meta.next_cursor == cursor.continuation and not page.meta.terminal:
            raise ValueError("Fundbox continuation cursor did not progress")
        prior = cursor.last_position
        if prior is None:
            return
        for change in page.data:
            if (change.change_version, change.root_id) <= prior:
                raise ValueError("Fundbox source order did not progress")

    def _records(self, page: BoundedIngestionPage) -> tuple[list[dict[str, JsonValue]], str | None]:
        records: list[dict[str, JsonValue]] = []
        last_record_id: str | None = None
        for change in page.data:
            if change.kind == "tombstone":
                source_record_id = self._mapper.source_record_id(change.root_id)
                tombstone: dict[str, JsonValue] = {
                    "source_record_id": source_record_id,
                    "retired_at": change.effective_updated_at.astimezone(UTC).isoformat(),
                    "snapshot_id": page.meta.snapshot_id,
                    "reason": change.tombstone_reason,
                }
                records.append({"_fundbox_tombstone": tombstone})
                last_record_id = source_record_id
                continue
            composite = change.composite
            if composite is None:
                raise ValueError("Fundbox upsert change has no composite")
            self._validate_nested_children(composite)
            records.append(self._mapper.build_record(composite))
            last_record_id = self._mapper.source_record_id(change.root_id)
        return records, last_record_id

    @staticmethod
    def _last_position(
        page: BoundedIngestionPage,
        previous: tuple[int, int] | None,
    ) -> tuple[int, int] | None:
        if page.data:
            change = page.data[-1]
            return change.change_version, change.root_id
        return previous

    @staticmethod
    def _validate_nested_children(composite: Mapping[str, JsonValue]) -> None:
        for field in ("addresses", "social_accounts", "device_ids", "items"):
            children = composite.get(field)
            if children is not None and (
                not isinstance(children, list) or len(children) > _MAX_NESTED_CHILDREN
            ):
                raise ValueError(f"Fundbox {field} exceeds bounded child limit")


class FundboxBoundedWriter:
    """Apply authoritative Fundbox upserts and tombstones in the receipt transaction."""

    def apply(
        self,
        tx: ManagedTransaction,
        context: AttemptContext,
        unit: BoundedUnit,
    ) -> UnitApplyResult:
        pipeline = IngestPipeline(None, control_instance_id=context.scope.control_instance_id)
        exclusions = _exclusions()
        dispositions: list[RecordDisposition] = []
        for record in unit.unit.records:
            tombstone = record.get("_fundbox_tombstone")
            if isinstance(tombstone, dict):
                retired = self._retire(tx, context, tombstone)
                dispositions.append("committed" if retired else "duplicate")
                continue
            envelope = SourceRecordEnvelope.model_validate(
                {"source_system": context.scope.source_key, **record}
            )
            if _is_excluded(envelope, exclusions):
                dispositions.append("excluded")
                continue
            if envelope.record_type == RecordType.SALES:
                result = ingest_sales_record_in_transaction(
                    tx,
                    envelope,
                    ingest_run_id=context.ingest_run_id,
                    exclusion_context=exclusions,
                    control_instance_id=context.scope.control_instance_id,
                )
            else:
                result = pipeline.ingest_in_transaction(
                    tx,
                    envelope,
                    ingest_run_id=context.ingest_run_id,
                    exclusion_context=exclusions,
                )
            if result.skipped_duplicate:
                dispositions.append("duplicate")
            elif result.dropped:
                dispositions.append("policy_dropped")
            elif result.retry_pending:
                dispositions.append("durable_retry")
            else:
                dispositions.append("committed")
        return UnitApplyResult(tuple(dispositions))

    @staticmethod
    def _retire(
        tx: ManagedTransaction,
        context: AttemptContext,
        tombstone: Mapping[str, JsonValue],
    ) -> int:
        source_record_id = tombstone.get("source_record_id")
        retired_at = tombstone.get("retired_at")
        snapshot_id = tombstone.get("snapshot_id")
        if not isinstance(source_record_id, str) or not source_record_id:
            raise ValueError("Fundbox tombstone is missing its source record ID")
        if not isinstance(retired_at, str) or not retired_at:
            raise ValueError("Fundbox tombstone is missing its retirement time")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise ValueError("Fundbox tombstone is missing its snapshot")
        return retire_source_evidence_in_transaction(
            tx,
            context.scope.source_key,
            source_record_id,
            retired_at,
            snapshot_id,
        )


class FundboxBoundedDescriptor:
    """One trusted descriptor shape shared by Fundbox users, contacts, and sales."""

    connector_version = "fundbox-bounded-v1"
    configuration_version = "fundbox-change-feed-v1"
    checkpoint_schema_version = 1
    supports_bootstrap = True
    supports_delta = True
    supports_one_time = False
    max_records_per_unit = 500
    max_source_requests_per_unit = 1
    max_bytes_per_unit = 2_000_000
    max_extraction_calls_per_unit = 1
    max_close_seconds = 5.0
    max_retry_backoff_seconds = 60.0
    supports_deadline = True
    supports_cancellation = True
    writer = FundboxBoundedWriter()

    def __init__(
        self,
        source_key: str,
        resource: str,
        mapper_type: type[FundboxApiConnector],
    ) -> None:
        self.source_key = source_key
        self._resource = resource
        self._mapper_type = mapper_type

    def initial_checkpoint(
        self,
        scope: RunScope,
        _occurrence: object,
    ) -> CheckpointDescriptor:
        """Build a side-effect-free position; admission owns any source call."""
        if scope.source_key != self.source_key:
            raise ValueError("Fundbox descriptor cannot initialize another source")
        return CheckpointDescriptor(
            phase="change_window",
            cursor={
                "continuation": None,
                "last_position": None,
                "cursor_expires_at": None,
                "terminal": False,
            },
            source_window=scope.source_window,
            last_committed_record_id=None,
            connector_version=self.connector_version,
            schema_version=self.checkpoint_schema_version,
            replay_boundary=(
                f"fundbox:{self.source_key}:{scope.source_window.get('snapshot_id', '-')}"
            ),
        )

    def create(self, _context: AttemptContext) -> FundboxBoundedConnector:
        settings = get_settings()
        client = FundboxApiClient(
            FundboxApiCredentials(
                base_url=settings.fundbox_api_base_url.strip(),
                username=settings.fundbox_api_username.strip(),
                password=settings.fundbox_api_password.get_secret_value(),
                page_size=settings.fundbox_api_page_size,
            ),
            http=httpx.Client(timeout=settings.fundbox_api_timeout_seconds),
            max_attempts=settings.fundbox_api_max_attempts,
        )
        return FundboxBoundedConnector(
            source_key=self.source_key,
            resource=self._resource,
            mapper_type=self._mapper_type,
            client=client,
            max_records=self.max_records_per_unit,
            max_bytes=self.max_bytes_per_unit,
        )


def _replay_id(
    source_key: str,
    checkpoint: CheckpointDescriptor,
    window: _FrozenWindow,
) -> str:
    payload = {
        "source_key": source_key,
        "snapshot_id": window.snapshot_id,
        "lower_change_version": window.lower_change_version,
        "upper_change_version": window.upper_change_version,
        "cursor": checkpoint.cursor,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "fundbox:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _exclusions() -> ExclusionContext:
    settings = get_settings()
    return build_exclusion_context(
        company_mobile_numbers=settings.company_mobile_numbers,
        company_email_addresses=settings.company_email_addresses,
        internal_person_names=settings.internal_person_names,
        file_exclusions=get_ingestion_config().exclusions,
    )


def _source_int(value: JsonValue) -> int | None:
    """Return an exact non-negative integer, rejecting bools and non-integers."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _utc_expiry(value: JsonValue) -> datetime | None:
    """Parse a persisted cursor expiry into an aware UTC instant."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Fundbox cursor expiry is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Fundbox cursor expiry is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Fundbox cursor expiry is invalid")
    return parsed.astimezone(UTC)


def _is_excluded(envelope: SourceRecordEnvelope, context: ExclusionContext) -> bool:
    if is_excluded_source_id(envelope.source_record_id, context):
        return True
    return any(
        (identifier.type == "phone" and is_excluded_phone(identifier.value, context))
        or (identifier.type == "email" and is_excluded_email(identifier.value, context))
        for identifier in envelope.identifiers
    )


DESCRIPTORS = (
    FundboxBoundedDescriptor("fundbox", "users", FundboxUsersApiConnector),
    FundboxBoundedDescriptor("fundbox:contacts", "contacts", FundboxContactsApiConnector),
    FundboxBoundedDescriptor("fundbox:sales", "sales", FundboxSalesApiConnector),
)
