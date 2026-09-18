"""Bounded PHPPOS writer: dispositions, tombstones, and fail-closed behaviour."""

from __future__ import annotations

import pytest
from _phppos_bounded_fixture import attempt_context, rows
from src import pipeline as pipeline_module
from src.bounded_ingestion_models import BoundedUnit, Usage
from src.connectors.phppos_api.bounded_checkpoint import PhpposCheckpointError
from src.connectors.phppos_api.bounded_connector import source_record_id
from src.connectors.phppos_api.bounded_writer import (
    PhpposBoundedWriteError,
    PhpposBoundedWriter,
)
from src.connectors.phppos_api.connectors import (
    build_customer_envelope,
    build_sales_envelope,
)
from src.connectors.phppos_api.models import SaleRow
from src.exclusions import ExclusionContext, normalized_phone_set
from src.models import IngestResult, JsonValue
from src.resumable import IngestionUnit

EKO = "eko_phppos"


def _unit(records: list[dict[str, JsonValue]]) -> BoundedUnit:
    context = attempt_context()
    return BoundedUnit(
        unit=IngestionUnit(context.checkpoint, context.checkpoint, tuple(records)),
        replay_id="replay-1",
        usage=Usage(records=len(records)),
        terminal=True,
    )


def _identity_record(row: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return build_customer_envelope(EKO, dict(row))


def _sales_record(row: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return build_sales_envelope(EKO, SaleRow.model_validate(row))


def _empty_exclusions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.connectors.phppos_api.bounded_writer._load_exclusion_context",
        lambda: ExclusionContext(),
    )


def _patch_ingest(
    monkeypatch: pytest.MonkeyPatch,
    outcome: IngestResult | None = None,
    seen: list[str] | None = None,
) -> None:
    def fake(
        self: object,
        tx: object,
        envelope: object,
        ingest_run_id: str | None = None,
        exclusion_context: ExclusionContext | None = None,
    ) -> IngestResult:
        identity = str(getattr(envelope, "source_record_id"))
        if seen is not None:
            seen.append(identity)
        if outcome is not None:
            return outcome.model_copy(update={"source_record_id": identity})
        return IngestResult(source_record_id=identity, ingest_run_id=ingest_run_id)

    monkeypatch.setattr(pipeline_module.IngestPipeline, "ingest_in_transaction", fake)


def test_writer_refuses_an_unsupported_source_scope() -> None:
    with pytest.raises(PhpposCheckpointError, match="unsupported"):
        PhpposBoundedWriter("fundbox")


def test_identity_and_sales_records_route_to_the_canonical_pipelines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    controls: list[object] = []

    def fake_identity(
        self: object,
        tx: object,
        envelope: object,
        ingest_run_id: str | None = None,
        exclusion_context: ExclusionContext | None = None,
    ) -> IngestResult:
        controls.append(getattr(self, "_control_instance_id"))
        return IngestResult(source_record_id=str(getattr(envelope, "source_record_id")))

    def fake_sales(
        tx: object,
        envelope: object,
        *,
        ingest_run_id: str | None,
        exclusion_context: ExclusionContext | None = None,
    ) -> IngestResult:
        seen.append(str(getattr(envelope, "source_record_id")))
        return IngestResult(source_record_id=str(getattr(envelope, "source_record_id")))

    monkeypatch.setattr(pipeline_module.IngestPipeline, "ingest_in_transaction", fake_identity)
    monkeypatch.setattr(
        "src.connectors.phppos_api.bounded_writer.ingest_sales_record_in_transaction",
        fake_sales,
    )
    _empty_exclusions(monkeypatch)

    writer = PhpposBoundedWriter(EKO)
    records = [
        _identity_record(rows("customer_rows.json")[0]),
        _sales_record(rows("sale_rows.json")[0]),
    ]

    result = writer.apply(object(), attempt_context(), _unit(records))

    assert result.dispositions == ("committed", "committed")
    assert seen == ["eko_phppos-sale-90211"]
    assert controls == ["test-control"]


def test_tombstones_retire_through_the_shared_retirement_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retired: list[tuple[str, str, str]] = []

    def fake_retire(
        tx: object,
        source_system: str,
        retired_source_record_id: str,
        retired_at: str,
        reconciliation_snapshot_at: str,
        **kwargs: object,
    ) -> int:
        retired.append((source_system, retired_source_record_id, retired_at))
        return 1 if len(retired) == 1 else 0

    monkeypatch.setattr(
        "src.connectors.phppos_api.bounded_writer.retire_source_evidence_in_transaction",
        fake_retire,
    )

    writer = PhpposBoundedWriter(EKO)
    context = attempt_context()
    removal = {
        "_retire_source_record_id": source_record_id(EKO, "customers", "4099"),
        "_retired_at": "2026-09-17T02:00:00+00:00",
        "_reconciliation_snapshot_at": "2026-09-17T02:00:00+00:00",
    }

    first = writer.apply(object(), context, _unit([removal]))
    replay = writer.apply(object(), context, _unit([removal]))

    assert first.dispositions == ("committed",)
    assert replay.dispositions == ("duplicate",)
    assert retired == [
        (EKO, "eko_phppos-customer-4099", "2026-09-17T02:00:00+00:00"),
        (EKO, "eko_phppos-customer-4099", "2026-09-17T02:00:00+00:00"),
    ]


def test_excluded_records_are_not_written_and_only_their_disposition_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[str] = []
    _patch_ingest(monkeypatch, seen=writes)
    monkeypatch.setattr(
        "src.connectors.phppos_api.bounded_writer._load_exclusion_context",
        lambda: ExclusionContext(phones=normalized_phone_set(["+65 9777 1234"])),
    )

    records = [
        _identity_record(rows("customer_rows.json")[2]),
        _identity_record(rows("customer_rows.json")[0]),
    ]
    result = PhpposBoundedWriter(EKO).apply(object(), attempt_context(), _unit(records))

    assert result.dispositions == ("excluded", "committed")
    assert writes == ["eko_phppos-customer-4021"]


def test_malformed_retirement_markers_and_unsupported_records_fail_the_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _empty_exclusions(monkeypatch)
    writer = PhpposBoundedWriter(EKO)
    context = attempt_context()

    malformed = {
        "_retire_source_record_id": "eko_phppos-customer-4099",
        "_retired_at": None,
        "_reconciliation_snapshot_at": None,
    }
    with pytest.raises(PhpposBoundedWriteError, match="retirement marker is malformed"):
        writer.apply(object(), context, _unit([malformed]))

    unsupported = {
        "source_record_id": "eko_phppos-customer-4021",
        "observed_at": "2026-09-01T04:15:00+00:00",
        "record_type": "bankruptcy",
        "identifiers": [],
        "attributes": {},
        "raw_payload": {},
        "record_hash": "hash",
    }
    with pytest.raises(PhpposBoundedWriteError, match="unsupported record"):
        writer.apply(object(), context, _unit([unsupported]))


def test_a_failed_ingest_write_fails_the_whole_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    _empty_exclusions(monkeypatch)
    _patch_ingest(
        monkeypatch,
        outcome=IngestResult(source_record_id="unused", errors=["deterministic write failed"]),
    )

    with pytest.raises(PhpposBoundedWriteError, match="failed to write"):
        PhpposBoundedWriter(EKO).apply(
            object(),
            attempt_context(),
            _unit([_identity_record(rows("customer_rows.json")[0])]),
        )


def test_replayed_and_dropped_records_report_durable_terminal_dispositions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _empty_exclusions(monkeypatch)
    writer = PhpposBoundedWriter(EKO)
    context = attempt_context()
    record = _identity_record(rows("customer_rows.json")[0])

    _patch_ingest(monkeypatch, outcome=IngestResult(source_record_id="x", skipped_duplicate=True))
    assert writer.apply(object(), context, _unit([record])).dispositions == ("duplicate",)

    _patch_ingest(monkeypatch, outcome=IngestResult(source_record_id="x", dropped=True))
    assert writer.apply(object(), context, _unit([record])).dispositions == ("policy_dropped",)


def test_every_record_receives_exactly_one_disposition(monkeypatch: pytest.MonkeyPatch) -> None:
    _empty_exclusions(monkeypatch)
    _patch_ingest(monkeypatch)

    records = [_identity_record(row) for row in rows("customer_rows.json")]
    result = PhpposBoundedWriter(EKO).apply(object(), attempt_context(), _unit(records))

    assert len(result.dispositions) == len(records)
    assert set(result.dispositions) == {"committed"}
