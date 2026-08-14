"""Focused tests for bounded live stage-history evidence capture."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from _bitrix_artifact_store_support import key_provider, new_store
from src.connectors.bitrix_stage_history import artifact_connector as artifact_connector_module
from src.connectors.bitrix_stage_history import connector as connector_module
from src.connectors.bitrix_stage_history.artifact_connector import (
    StageExpectedRow,
    StageQualificationEvidence,
    StageSmokePlan,
)
from src.connectors.bitrix_stage_history.artifact_provenance import ArtifactProvenanceInput
from src.connectors.bitrix_stage_history.artifact_store import LocalRestrictedArtifactStore
from src.connectors.bitrix_stage_history.canonical import (
    canonical_stage_hash_v1,
    encode_stage_source_record_id,
)
from src.connectors.bitrix_stage_history.connector import (
    StageCaptureAuthorization,
    StageCaptureLimits,
    StageCaptureResult,
    collect_stage_history_smoke,
    stage_capture_limits_digest,
)
from src.connectors.bitrix_stage_history.ingestion_spool import (
    SealedStageHistoryIngestionSpool,
)
from src.connectors.bitrix_stage_history.models import (
    DecodedStageHistoryRow,
    StageHistoryRawPage,
    decode_stage_history_item,
)
from src.models import JsonValue

_SOURCE_CONTRACT = "12345678-1234-5678-9234-567812345678"
_IMAGE_DIGEST = f"sha256:{'b' * 64}"
_CONFIG_DIGEST = f"sha256:{'c' * 64}"
_QUALIFICATION_DIGEST = f"sha256:{'e' * 64}"
_NOW = datetime(2026, 8, 14, 4, 0, tzinfo=UTC)
_EVIDENCE_BY_STAGE_ID: dict[str, StageQualificationEvidence] = {}


@pytest.fixture(autouse=True)
def _stub_authenticated_evidence_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    def load(
        store: object,
        *,
        owner_artifact_id: str,
        stage_artifact_id: str,
        expected_qualification_evidence_digest: str,
        expected_source_contract_uuid: str,
        expected_configuration_digest: str,
        entity_type_id: int,
    ) -> StageQualificationEvidence:
        _ = store
        evidence = _EVIDENCE_BY_STAGE_ID[stage_artifact_id]
        assert owner_artifact_id == evidence.owner_manifest.artifact_id
        assert expected_qualification_evidence_digest == evidence.qualification_evidence_digest
        assert expected_source_contract_uuid == _SOURCE_CONTRACT
        assert expected_configuration_digest == _CONFIG_DIGEST
        assert entity_type_id == evidence.entity_type_id
        return evidence

    monkeypatch.setattr(connector_module, "load_qualification_evidence", load)


class _RawClient:
    def __init__(self, pages: tuple[StageHistoryRawPage | BaseException, ...]) -> None:
        self._pages = list(pages)
        self.calls: list[tuple[int, dict[str, JsonValue], str, int]] = []
        self._request_count = 0
        self.budget: tuple[int, float] | None = None

    @property
    def request_count(self) -> int:
        return self._request_count

    def constrain_request_budget(
        self, *, max_request_count: int, deadline_monotonic: float
    ) -> None:
        self.budget = (max_request_count, deadline_monotonic)

    def list_stage_history_raw_page(
        self,
        *,
        entity_type_id: int,
        filters: Mapping[str, JsonValue] | None = None,
        order_direction: str = "ASC",
        start: int = -1,
    ) -> StageHistoryRawPage:
        self._request_count += 1
        self.calls.append((entity_type_id, dict(filters or {}), order_direction, start))
        if not self._pages:
            raise AssertionError("capture requested an unexpected source page")
        page = self._pages.pop(0)
        if isinstance(page, BaseException):
            raise page
        return page


@dataclass(frozen=True)
class _CaptureFixture:
    store: LocalRestrictedArtifactStore
    evidence: StageQualificationEvidence


def _raw(history_id: int, *, owner_id: str = "501") -> dict[str, JsonValue]:
    return {
        "ID": str(history_id),
        "OWNER_ID": owner_id,
        "TYPE_ID": "1",
        "CREATED_TIME": "2026-08-14T03:30:00Z",
        "CATEGORY_ID": "2",
        "STAGE_SEMANTIC_ID": "P",
        "STAGE_ID": "C2:NEW",
    }


def _expected(raw: JsonValue) -> StageExpectedRow:
    decoded = decode_stage_history_item(raw, entity_type_id="2")
    assert isinstance(decoded, DecodedStageHistoryRow)
    event_identity = encode_stage_source_record_id(
        _SOURCE_CONTRACT,
        "2",
        decoded.item.history_id,
    )
    return StageExpectedRow(
        history_id=int(decoded.item.history_id),
        event_identity=event_identity,
        canonical_hash=canonical_stage_hash_v1(_SOURCE_CONTRACT, decoded.item),
    )


def _capture_fixture(
    tmp_path: Path,
    expected_rows: tuple[StageExpectedRow, ...],
) -> _CaptureFixture:
    store = new_store(tmp_path / "primary", tmp_path / "backup", key_provider())
    provenance = _provenance()
    with store.begin(artifact_kind="owner-export") as artifact:
        artifact.write_json("owner-summary.json", {"rows": 1})
        owner = artifact.seal(
            metadata={
                "owner_manifest_digest": "hmac-sha256:owner-digest",
                "owner_manifest_file": "owner-summary.json",
            },
            provenance=provenance,
            retention_expires_at=_NOW + timedelta(days=30),
        )
    with store.begin(artifact_kind="stage-capability") as artifact:
        artifact.write_json("stage-summary.json", {"rows": len(expected_rows)})
        stage = artifact.seal(
            metadata={
                "owner_artifact_id": owner.artifact_id,
                "recommendation": "bounded_spool_reconcile",
            },
            provenance=provenance,
            retention_expires_at=_NOW + timedelta(days=30),
        )
    fixture = _CaptureFixture(
        store=store,
        evidence=StageQualificationEvidence(
            owner_manifest=owner,
            stage_manifest=stage,
            qualification_evidence_digest=_QUALIFICATION_DIGEST,
            entity_type_id=2,
            owner_ids=frozenset({"501"}),
            expected_rows=expected_rows,
        ),
    )
    _EVIDENCE_BY_STAGE_ID[stage.artifact_id] = fixture.evidence
    return fixture


def _provenance() -> ArtifactProvenanceInput:
    return ArtifactProvenanceInput.create(
        source_contract_uuid=_SOURCE_CONTRACT,
        repository_sha="a" * 40,
        image_digest=_IMAGE_DIGEST,
        configuration_digest=_CONFIG_DIGEST,
        restricted_boundaries={"accepted_boundary": "restricted"},
        counts={"rows": 1},
    )


def _page(*rows: JsonValue) -> StageHistoryRawPage:
    return StageHistoryRawPage(tuple(rows), None, len(rows), None, None)


def _plan(expected: tuple[StageExpectedRow, ...]) -> StageSmokePlan:
    return StageSmokePlan(
        lower_history_id=0,
        upper_history_id=expected[-1].history_id,
        expected_rows=expected,
        maximum_calls=max(1, (len(expected) + 49) // 50),
    )


def _collect(
    fixture: _CaptureFixture,
    client: _RawClient,
    plan: StageSmokePlan,
    *,
    limits: StageCaptureLimits | None = None,
    monotonic: Callable[[], float] | None = None,
    now: Callable[[], datetime] = lambda: _NOW,
    authorization: StageCaptureAuthorization | None = None,
) -> StageCaptureResult:
    selected_monotonic = monotonic or time.monotonic
    selected_limits = limits or StageCaptureLimits(4, 100, 10_000_000, 30)
    selected_authorization = authorization or StageCaptureAuthorization(
        enabled=True,
        reference="authorization-147-smoke",
        expires_at=_NOW + timedelta(days=1),
        owner_artifact_id=fixture.evidence.owner_manifest.artifact_id,
        owner_manifest_hmac=fixture.evidence.owner_manifest.manifest_hmac,
        stage_artifact_id=fixture.evidence.stage_manifest.artifact_id,
        stage_manifest_hmac=fixture.evidence.stage_manifest.manifest_hmac,
        qualification_evidence_digest=(fixture.evidence.qualification_evidence_digest),
        source_contract_uuid=_SOURCE_CONTRACT,
        entity_type_id=2,
        configuration_digest=_CONFIG_DIGEST,
        limits_digest=stage_capture_limits_digest(selected_limits),
    )
    return collect_stage_history_smoke(
        client,
        fixture.store,
        evidence=fixture.evidence,
        plan=plan,
        authorization=selected_authorization,
        limits=selected_limits,
        image_digest=_IMAGE_DIGEST,
        configuration_digest=_CONFIG_DIGEST,
        limits_digest=stage_capture_limits_digest(selected_limits),
        retention_days=30,
        now=now,
        monotonic=selected_monotonic,
    )


def test_exact_capture_seals_authenticated_stage_ingestion_artifact(tmp_path: Path) -> None:
    raw_rows = (_raw(1), _raw(2))
    expected = tuple(_expected(row) for row in raw_rows)
    fixture = _capture_fixture(tmp_path, expected)
    client = _RawClient((_page(*raw_rows),))
    try:
        result = _collect(fixture, client, _plan(expected))

        verified = fixture.store.verify(result.manifest.artifact_id)
        assert result.qualified is True
        assert result.failure_reason is None
        assert (result.pages, result.rows, result.valid_rows, result.malformed_rows) == (
            1,
            2,
            2,
            0,
        )
        assert verified.artifact_kind == "stage-ingestion"
        assert verified.metadata["status"] == "qualified"
        assert verified.metadata["failure_reason"] is None
        assert verified.metadata["stage_artifact_id"] == fixture.evidence.stage_manifest.artifact_id
        assert client.calls == [(2, {">ID": "0", "<=ID": "2"}, "ASC", -1)]
    finally:
        fixture.store.close()


def test_mixed_malformed_capture_seals_and_retains_failed_artifact(tmp_path: Path) -> None:
    valid = _raw(1)
    malformed: JsonValue = {"OWNER_ID": "501", "CREATED_TIME": "2026-08-14T03:30:00Z"}
    expected = (_expected(valid),)
    fixture = _capture_fixture(tmp_path, expected)
    try:
        result = _collect(fixture, _RawClient((_page(valid, malformed),)), _plan(expected))

        verified = fixture.store.verify(result.manifest.artifact_id)
        assert result.qualified is False
        assert result.failure_reason == "malformed_row"
        assert result.valid_rows == 1
        assert result.malformed_rows == 1
        assert verified.artifact_kind == "stage-ingestion-failed"
        assert verified.metadata["status"] == "failed"
        assert verified.metadata["failure_reason"] == "malformed_row"
        assert Path(verified.provenance.artifact_path).is_dir()
        assert Path(verified.backup_path).is_dir()
    finally:
        fixture.store.close()


def test_nonnumeric_history_id_uses_malformed_occurrence_domain(tmp_path: Path) -> None:
    expected = (_expected(_raw(1)),)
    fixture = _capture_fixture(tmp_path, expected)
    invalid = {**_raw(1), "ID": "abc"}
    try:
        result = _collect(fixture, _RawClient((_page(invalid),)), _plan(expected))
        assert result.failure_reason == "malformed_row"
        manifest = fixture.store.verify(result.manifest.artifact_id)
        spool_path = Path(manifest.provenance.artifact_path) / "stage-ingestion.sqlite3"
        with SealedStageHistoryIngestionSpool(
            spool_path,
            expected_artifact_id=manifest.artifact_id,
        ) as spool:
            row = spool.pages()[0].rows[0]
        assert row.row_kind == "malformed"
        assert row.event_identity is None
        assert row.safe_error_code == "invalid_history_id"
    finally:
        fixture.store.close()


@pytest.mark.parametrize(
    ("raw_rows", "expected_ids"),
    [
        ((_raw(2),), (1,)),
        ((_raw(1), _raw(1)), (1,)),
        ((_raw(2), _raw(1)), (1, 2)),
    ],
    ids=["expected-mismatch", "duplicate", "non-increasing"],
)
def test_mismatch_duplicate_and_non_increasing_rows_seal_failed_artifacts(
    tmp_path: Path,
    raw_rows: tuple[dict[str, JsonValue], ...],
    expected_ids: tuple[int, ...],
) -> None:
    expected = tuple(_expected(_raw(history_id)) for history_id in expected_ids)
    fixture = _capture_fixture(tmp_path, expected)
    try:
        result = _collect(
            fixture,
            _RawClient((_page(*raw_rows),)),
            _plan(expected),
        )

        assert result.qualified is False
        assert result.failure_reason == "expected_row_mismatch"
        assert fixture.store.verify(result.manifest.artifact_id).artifact_kind == (
            "stage-ingestion-failed"
        )
    finally:
        fixture.store.close()


def test_page_envelope_failure_abandons_the_preparing_artifact(tmp_path: Path) -> None:
    expected = (_expected(_raw(1)),)
    fixture = _capture_fixture(tmp_path, expected)
    existing_markers = set((tmp_path / "primary" / "sealed").iterdir())
    try:
        with pytest.raises(RuntimeError, match="invalid stage-history envelope"):
            _collect(
                fixture,
                _RawClient((RuntimeError("invalid stage-history envelope"),)),
                _plan(expected),
            )

        assert set((tmp_path / "primary" / "sealed").iterdir()) == existing_markers
        assert list((tmp_path / "primary" / ".sessions").iterdir()) == []
    finally:
        fixture.store.close()


def test_exact_fifty_row_final_page_completes_at_the_approved_upper_bound(
    tmp_path: Path,
) -> None:
    raw_rows = tuple(_raw(index) for index in range(1, 51))
    expected = tuple(_expected(row) for row in raw_rows)
    fixture = _capture_fixture(tmp_path, expected)
    client = _RawClient((_page(*raw_rows),))
    try:
        result = _collect(
            fixture,
            client,
            _plan(expected),
            limits=StageCaptureLimits(1, 50, 10_000_000, 30),
        )
        assert result.qualified is True
        assert len(client.calls) == 1
    finally:
        fixture.store.close()


def test_limits_must_reserve_a_complete_source_page(tmp_path: Path) -> None:
    expected = (_expected(_raw(1)),)
    fixture = _capture_fixture(tmp_path, expected)
    try:
        with pytest.raises(ValueError, match="complete 50-row source page"):
            _collect(
                fixture,
                _RawClient((_page(_raw(1), _raw(2)),)),
                _plan(expected),
                limits=StageCaptureLimits(1, 1, 10_000_000, 30),
            )
    finally:
        fixture.store.close()


def test_spool_limit_is_checked_before_the_first_source_call(tmp_path: Path) -> None:
    expected = (_expected(_raw(1)),)
    fixture = _capture_fixture(tmp_path, expected)
    client = _RawClient((_page(_raw(1)),))
    try:
        with pytest.raises(RuntimeError, match="spool limit"):
            _collect(
                fixture,
                client,
                _plan(expected),
                limits=StageCaptureLimits(1, 50, 1, 30),
            )
        assert client.calls == []
    finally:
        fixture.store.close()


def test_runtime_limit_is_checked_before_the_first_source_call(tmp_path: Path) -> None:
    expected = (_expected(_raw(1)),)
    fixture = _capture_fixture(tmp_path, expected)
    client = _RawClient((_page(_raw(1)),))
    ticks = iter((0.0, 2.0))
    try:
        with pytest.raises(RuntimeError, match="runtime limit"):
            _collect(
                fixture,
                client,
                _plan(expected),
                limits=StageCaptureLimits(1, 50, 10_000_000, 1),
                monotonic=lambda: next(ticks),
            )
        assert client.calls == []
    finally:
        fixture.store.close()


def test_capture_rejects_a_noncanonical_caller_supplied_plan(tmp_path: Path) -> None:
    expected = (_expected(_raw(1)), _expected(_raw(2)))
    fixture = _capture_fixture(tmp_path, expected)
    client = _RawClient((_page(_raw(2)),))
    noncanonical = StageSmokePlan(1, 2, (expected[1],), 1)
    try:
        with pytest.raises(ValueError, match="not deterministically derived"):
            _collect(fixture, client, noncanonical)
        assert client.calls == []
    finally:
        fixture.store.close()


def test_capture_rejects_an_unauthorized_manifest_substitution(tmp_path: Path) -> None:
    expected = (_expected(_raw(1)),)
    fixture = _capture_fixture(tmp_path, expected)
    limits = StageCaptureLimits(1, 50, 10_000_000, 30)
    authorized = StageCaptureAuthorization(
        enabled=True,
        reference="authorization-147-smoke",
        expires_at=_NOW + timedelta(days=1),
        owner_artifact_id=fixture.evidence.owner_manifest.artifact_id,
        owner_manifest_hmac="0" * 64,
        stage_artifact_id=fixture.evidence.stage_manifest.artifact_id,
        stage_manifest_hmac=fixture.evidence.stage_manifest.manifest_hmac,
        qualification_evidence_digest=fixture.evidence.qualification_evidence_digest,
        source_contract_uuid=_SOURCE_CONTRACT,
        entity_type_id=2,
        configuration_digest=_CONFIG_DIGEST,
        limits_digest=stage_capture_limits_digest(limits),
    )
    client = _RawClient((_page(_raw(1)),))
    try:
        with pytest.raises(ValueError, match="manifest identity is not authorized"):
            _collect(
                fixture,
                client,
                _plan(expected),
                limits=limits,
                authorization=authorized,
            )
        assert client.calls == []
    finally:
        fixture.store.close()


def test_authorization_expiry_before_page_write_abandons_capture(tmp_path: Path) -> None:
    expected = (_expected(_raw(1)),)
    fixture = _capture_fixture(tmp_path, expected)
    client = _RawClient((_page(_raw(1)),))
    checks = iter((_NOW, _NOW, _NOW + timedelta(days=2)))
    try:
        with pytest.raises(PermissionError, match="expired"):
            _collect(fixture, client, _plan(expected), now=lambda: next(checks))
        assert len(client.calls) == 1
        assert list((tmp_path / "primary" / ".sessions").iterdir()) == []
    finally:
        fixture.store.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_calls", 0),
        ("max_rows", 0),
        ("max_spool_bytes", 0),
        ("max_runtime_seconds", 0.0),
        ("max_runtime_seconds", float("inf")),
    ],
)
def test_capture_limits_require_finite_positive_budgets(field: str, value: int | float) -> None:
    values: dict[str, int | float] = {
        "max_calls": 1,
        "max_rows": 1,
        "max_spool_bytes": 1,
        "max_runtime_seconds": 1.0,
    }
    values[field] = value

    with pytest.raises(ValueError, match="positive|finite"):
        StageCaptureLimits(
            max_calls=int(values["max_calls"]),
            max_rows=int(values["max_rows"]),
            max_spool_bytes=int(values["max_spool_bytes"]),
            max_runtime_seconds=float(values["max_runtime_seconds"]),
        )


def test_capture_authorization_and_integer_limits_require_exact_runtime_types() -> None:
    authorization = StageCaptureAuthorization(
        enabled=True,
        reference="authorization-147-smoke",
        expires_at=_NOW + timedelta(days=1),
        owner_artifact_id="owner-artifact",
        owner_manifest_hmac="a" * 64,
        stage_artifact_id="stage-artifact",
        stage_manifest_hmac="b" * 64,
        qualification_evidence_digest=_QUALIFICATION_DIGEST,
        source_contract_uuid=_SOURCE_CONTRACT,
        entity_type_id=2,
        configuration_digest=_CONFIG_DIGEST,
        limits_digest=f"sha256:{'f' * 64}",
    )
    with pytest.raises(ValueError, match="enabled must be boolean"):
        replace(authorization, enabled=cast(bool, 1))
    with pytest.raises(ValueError, match="entity_type_id"):
        replace(authorization, entity_type_id=cast(int, 2.5))
    with pytest.raises(ValueError, match="max_calls"):
        StageCaptureLimits(cast(int, 1.5), 1, 1, 1.0)


def test_capture_and_artifact_readers_have_no_graph_dependency() -> None:
    for module in (connector_module, artifact_connector_module):
        source = inspect.getsource(module)
        assert "src.graph" not in source
        assert "Neo4j" not in source
