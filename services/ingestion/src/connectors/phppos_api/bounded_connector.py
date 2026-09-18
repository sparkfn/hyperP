"""Bounded PHPPOS connector: one frozen source page slice per unit.

The traversal keeps a single fixed phase per stream. Continuation is expressed
only by the typed cursor: an in-page record offset while a fetched frozen page
still has unconverted records, then the page cursor of the next frozen page,
then a terminal marker. A replayed unit re-fetches the same frozen page and
re-slices it at the same offset, so a replay identity is deterministic.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from src.bounded_ingestion_models import AttemptContext, BoundedUnit, Usage
from src.connectors.phppos_api.bounded_checkpoint import (
    PhpposCheckpointError,
    PhpposCursor,
    PhpposSourceWindow,
    Resource,
    next_checkpoint,
    parse_checkpoint,
)
from src.connectors.phppos_api.client import (
    BoundedRequestBudget,
    PhpposApiClient,
    PhpposBoundedTransportError,
)
from src.connectors.phppos_api.connectors import (
    build_customer_envelope,
    build_sales_envelope,
)
from src.connectors.phppos_api.models import (
    BoundedChange,
    BoundedPage,
    BoundedTombstone,
    CustomerRow,
    SaleRow,
)
from src.models import JsonValue
from src.resumable import CheckpointCompatibility, CheckpointDescriptor, IngestionUnit

MAX_SALE_LINES = 200
BoundedRecord = BoundedChange | BoundedTombstone


class PhpposBoundedConnector:
    """Fetch exactly one bounded page slice and never fall back to legacy traversal."""

    def __init__(
        self,
        *,
        source_key: str,
        resource: Resource,
        expected_tenant_id: str,
        configuration_fingerprint: str,
        client: PhpposApiClient,
        max_records_per_unit: int,
        max_source_requests_per_unit: int,
        max_bytes_per_unit: int,
        observed_at: Callable[[], datetime] | None = None,
    ) -> None:
        if not expected_tenant_id.strip():
            raise PhpposBoundedTransportError("bounded PHPPOS tenant is not configured")
        self._source_key = source_key
        self._resource = resource
        self._expected_tenant_id = expected_tenant_id
        self._configuration_fingerprint = configuration_fingerprint
        self._client = client
        self._max_records = max_records_per_unit
        self._max_source_requests = max_source_requests_per_unit
        self._max_bytes = max_bytes_per_unit
        self._observed_at = observed_at or (lambda: datetime.now(UTC))

    def validate_checkpoint(self, checkpoint: CheckpointDescriptor) -> CheckpointCompatibility:
        try:
            self._parse(checkpoint)
        except PhpposCheckpointError as exc:
            return exc.compatibility
        return "compatible"

    def fetch_one_unit(
        self,
        checkpoint: CheckpointDescriptor,
        context: AttemptContext,
    ) -> BoundedUnit:
        window, cursor = self._parse(checkpoint)
        if cursor.terminal:
            raise PhpposBoundedTransportError("bounded PHPPOS checkpoint is already complete")
        budget = BoundedRequestBudget(
            max_requests=self._max_source_requests,
            # One legal source page is admitted here; the unit still commits at
            # most ``max_records`` by slicing the page it just read.
            max_rows=self._client.page_size,
            max_bytes=self._max_bytes,
        )
        result = self._client.fetch_bounded_page(
            self._resource,
            cursor=cursor.page_cursor,
            window=window.window,
            context=context,
            budget=budget,
        )
        page = result.page
        if page.tenant_id != self._expected_tenant_id:
            raise PhpposBoundedTransportError("bounded PHPPOS page crossed its tenant")
        if cursor.record_offset > len(page.data):
            raise PhpposBoundedTransportError("bounded PHPPOS page shrank below its offset")
        page_slice = page.data[cursor.record_offset : cursor.record_offset + self._max_records]
        observed_at = self._observed_at().isoformat()
        records = tuple(
            _record_for(self._source_key, self._resource, change, observed_at)
            for change in page_slice
        )
        after, terminal = _next_checkpoint_state(checkpoint, window, cursor, page, page_slice)
        return BoundedUnit(
            unit=IngestionUnit(checkpoint, after, records),
            replay_id=cursor.page_replay_id,
            usage=Usage(
                records=len(records),
                source_requests=result.usage.source_requests,
                pages=result.usage.pages,
                bytes_read=result.usage.bytes_read,
            ),
            terminal=terminal,
        )

    def cancel(self) -> None:
        """Abort any in-flight source read by closing the transport."""
        self._client.close()

    def close(self) -> None:
        self._client.close()

    def _parse(
        self,
        checkpoint: CheckpointDescriptor,
    ) -> tuple[PhpposSourceWindow, PhpposCursor]:
        return parse_checkpoint(
            checkpoint,
            source_key=self._source_key,
            configuration_fingerprint=self._configuration_fingerprint,
        )


def _next_checkpoint_state(
    checkpoint: CheckpointDescriptor,
    window: PhpposSourceWindow,
    cursor: PhpposCursor,
    page: BoundedPage,
    page_slice: list[BoundedRecord],
) -> tuple[CheckpointDescriptor, bool]:
    """Continue in page, continue at the next page, or close the stream."""
    consumed = cursor.record_offset + len(page_slice)
    last_record_id = page_slice[-1].source_id if page_slice else None
    if consumed < len(page.data):
        return (
            next_checkpoint(
                checkpoint,
                window,
                next_page_cursor=cursor.page_cursor,
                next_record_offset=consumed,
                last_committed_record_id=last_record_id,
                terminal=False,
            ),
            False,
        )
    if page.pagination.has_more:
        return (
            next_checkpoint(
                checkpoint,
                window,
                next_page_cursor=page.pagination.next_cursor,
                last_committed_record_id=last_record_id,
                terminal=False,
            ),
            False,
        )
    return (
        next_checkpoint(
            checkpoint,
            window,
            next_page_cursor=None,
            last_committed_record_id=last_record_id,
            terminal=True,
        ),
        True,
    )


def _record_for(
    source_key: str,
    resource: Resource,
    change: BoundedRecord,
    observed_at: str,
) -> dict[str, JsonValue]:
    """Map one discriminated change into a bounded unit record.

    The mappers and the HyperP source record ids are keyed on the tenant's base
    source key (``eko_phppos``), exactly as the legacy connectors call them, while
    the run itself keeps the scope key (``eko_phppos:sales``) for its envelope and
    retirement provenance.
    """
    tenant_key = base_source_key(source_key)
    if change.kind == "tombstone":
        return {
            "_retire_source_record_id": source_record_id(tenant_key, resource, change.source_id),
            "_retired_at": observed_at,
            "_reconciliation_snapshot_at": observed_at,
        }
    if resource == "customers":
        customer = CustomerRow.model_validate(change.record)
        _require_identity(change.source_id, customer.person_id)
        return build_customer_envelope(tenant_key, customer.model_dump())
    sale = SaleRow.model_validate(change.record)
    _require_identity(change.source_id, sale.sale_id)
    if len(sale.lines) > MAX_SALE_LINES:
        raise PhpposBoundedTransportError("bounded PHPPOS sale aggregate is too large")
    if any(line.sale_id != sale.sale_id for line in sale.lines):
        raise PhpposBoundedTransportError("bounded PHPPOS sale aggregate is incomplete")
    return build_sales_envelope(tenant_key, sale)


def _require_identity(source_id: str, row_id: int) -> None:
    if source_id != str(row_id):
        raise PhpposBoundedTransportError("bounded PHPPOS change identity is inconsistent")


def base_source_key(source_key: str) -> str:
    """Return the tenant key the canonical mappers and source record ids use."""
    return source_key.split(":", maxsplit=1)[0]


def source_record_id(source_key: str, resource: Resource, source_id: str) -> str:
    """Derive the canonical HyperP source record id for one source row identity."""
    suffix = "customer" if resource == "customers" else "sale"
    return f"{source_key}-{suffix}-{source_id}"
