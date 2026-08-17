"""Bounded live capture into a restricted stage-history ingestion artifact."""

from __future__ import annotations

import hashlib
import hmac
import math
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from src.connectors.bitrix_stage_history.artifact_connector import (
    StageQualificationEvidence,
    StageSmokePlan,
    derive_backfill_plan,
    derive_catch_up_plan,
    derive_smoke_plan,
    load_qualification_evidence,
)
from src.connectors.bitrix_stage_history.artifact_manifest import (
    ArtifactManifest,
    canonical_json_bytes,
)
from src.connectors.bitrix_stage_history.artifact_provenance import ArtifactProvenanceInput
from src.connectors.bitrix_stage_history.artifact_store import LocalRestrictedArtifactStore
from src.connectors.bitrix_stage_history.canonical import (
    canonical_stage_hash_v1,
    encode_stage_source_record_id,
)
from src.connectors.bitrix_stage_history.ingestion_spool import (
    MalformedCapturedRow,
    StageHistoryIngestionSpool,
    ValidCapturedRow,
)
from src.connectors.bitrix_stage_history.models import (
    DecodedStageHistoryRow,
    StageHistoryRawPage,
    decode_stage_history_item,
    parse_positive_history_id,
)
from src.models import JsonValue

CaptureFailureReason = Literal["malformed_row", "expected_row_mismatch"]
_BITRIX_PAGE_SIZE = 50
_INGESTION_DB_FILE = "stage-ingestion.sqlite3"


class RawStageHistoryClient(Protocol):
    @property
    def request_count(self) -> int: ...

    def constrain_request_budget(
        self, *, max_request_count: int, deadline_monotonic: float
    ) -> None: ...

    def list_stage_history_raw_page(
        self,
        *,
        entity_type_id: int,
        filters: Mapping[str, JsonValue] | None = None,
        order_direction: str = "ASC",
        start: int = -1,
    ) -> StageHistoryRawPage: ...


@dataclass(frozen=True, slots=True)
class StageCaptureAuthorization:
    enabled: bool
    reference: str
    actor: str
    expires_at: datetime
    owner_artifact_id: str
    owner_manifest_hmac: str
    stage_artifact_id: str
    stage_manifest_hmac: str
    qualification_evidence_digest: str
    source_contract_uuid: str
    entity_type_id: int
    configuration_digest: str
    limits_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("capture authorization enabled must be boolean")
        strings = (
            self.reference,
            self.actor,
            self.owner_artifact_id,
            self.owner_manifest_hmac,
            self.stage_artifact_id,
            self.stage_manifest_hmac,
            self.qualification_evidence_digest,
            self.source_contract_uuid,
            self.configuration_digest,
            self.limits_digest,
        )
        if any(not value.strip() for value in strings):
            raise ValueError("capture authorization fields must be non-empty")
        if (
            isinstance(self.entity_type_id, bool)
            or not isinstance(self.entity_type_id, int)
            or self.entity_type_id < 1
        ):
            raise ValueError("capture authorization entity_type_id must be positive")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("capture authorization expiry must be timezone-aware")

    def assert_active(self, *, now: datetime) -> None:
        if not self.enabled:
            raise PermissionError("stage-history capture is disabled")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("capture authorization check time must be timezone-aware")
        if now >= self.expires_at:
            raise PermissionError("stage-history capture authorization has expired")


@dataclass(frozen=True, slots=True)
class StageCaptureLimits:
    max_calls: int
    max_rows: int
    max_spool_bytes: int
    max_runtime_seconds: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_calls, bool)
            or not isinstance(self.max_calls, int)
            or self.max_calls < 1
        ):
            raise ValueError("max_calls must be positive")
        if (
            isinstance(self.max_rows, bool)
            or not isinstance(self.max_rows, int)
            or self.max_rows < 1
        ):
            raise ValueError("max_rows must be positive")
        if (
            isinstance(self.max_spool_bytes, bool)
            or not isinstance(self.max_spool_bytes, int)
            or self.max_spool_bytes < 1
        ):
            raise ValueError("max_spool_bytes must be positive")
        if (
            isinstance(self.max_runtime_seconds, bool)
            or not isinstance(self.max_runtime_seconds, (int, float))
            or not math.isfinite(self.max_runtime_seconds)
            or self.max_runtime_seconds <= 0
        ):
            raise ValueError("max_runtime_seconds must be positive and finite")


@dataclass(frozen=True, slots=True)
class StageCaptureResult:
    manifest: ArtifactManifest
    qualified: bool
    failure_reason: CaptureFailureReason | None
    pages: int
    rows: int
    valid_rows: int
    malformed_rows: int


@dataclass(slots=True)
class _CaptureState:
    started: float
    calls: int = 0
    pages: int = 0
    rows: int = 0
    valid_rows: int = 0
    malformed_rows: int = 0
    last_history_id: int | None = None


def collect_stage_history_smoke(
    client: RawStageHistoryClient,
    store: LocalRestrictedArtifactStore,
    *,
    evidence: StageQualificationEvidence,
    plan: StageSmokePlan,
    authorization: StageCaptureAuthorization,
    limits: StageCaptureLimits,
    repository_sha: str,
    image_digest: str,
    configuration_digest: str,
    limits_digest: str,
    retention_days: int,
    ownership_guard: Callable[[], None] = lambda: None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
) -> StageCaptureResult:
    """Capture only the artifact-derived range; never write graph/control state."""
    ownership_guard()
    checked_at = now()
    authorization.assert_active(now=checked_at)
    _validate_repository_sha(repository_sha)
    _validate_sha256_digest(image_digest, "image_digest")
    expected_limits_digest = stage_capture_limits_digest(limits)
    if not hmac.compare_digest(limits_digest, expected_limits_digest) or not hmac.compare_digest(
        authorization.limits_digest,
        expected_limits_digest,
    ):
        raise ValueError("capture limits digest does not match the concrete limits")
    if not hmac.compare_digest(authorization.configuration_digest, configuration_digest):
        raise ValueError("capture configuration digest is not authorized")
    evidence = load_qualification_evidence(
        store,
        owner_artifact_id=authorization.owner_artifact_id,
        stage_artifact_id=authorization.stage_artifact_id,
        expected_qualification_evidence_digest=(authorization.qualification_evidence_digest),
        expected_source_contract_uuid=authorization.source_contract_uuid,
        expected_configuration_digest=configuration_digest,
        entity_type_id=authorization.entity_type_id,
    )
    if not hmac.compare_digest(
        evidence.owner_manifest.manifest_hmac,
        authorization.owner_manifest_hmac,
    ) or not hmac.compare_digest(
        evidence.stage_manifest.manifest_hmac,
        authorization.stage_manifest_hmac,
    ):
        raise ValueError("capture artifact manifest identity is not authorized")
    _validate_capture_inputs(evidence, plan, limits, configuration_digest, retention_days)
    state = _CaptureState(started=monotonic())
    client.constrain_request_budget(
        max_request_count=limits.max_calls,
        deadline_monotonic=state.started + limits.max_runtime_seconds,
    )
    with store.begin(artifact_kind="stage-ingestion") as artifact:
        spool = StageHistoryIngestionSpool(artifact.path, artifact_id=artifact.artifact_id)
        try:
            failure = _capture_pages(
                client,
                spool,
                evidence=evidence,
                plan=plan,
                authorization=authorization,
                limits=limits,
                state=state,
                now=now,
                monotonic=monotonic,
                ownership_guard=ownership_guard,
            )
            if failure is None and not _matches_expected(spool, plan):
                failure = "expected_row_mismatch"

            def write_guard() -> None:
                ownership_guard()
                _capture_write_guard(
                    authorization,
                    state,
                    spool,
                    limits,
                    now=now,
                    monotonic=monotonic,
                )

            write_guard()
            sealed_path = spool.seal(guard=write_guard)
            write_guard()
            sealed_path.rename(artifact.path / _INGESTION_DB_FILE)
            kind = "stage-ingestion" if failure is None else "stage-ingestion-failed"
            artifact.artifact_kind = kind
            manifest = artifact.seal(
                metadata=_metadata(
                    evidence,
                    authorization,
                    configuration_digest,
                    expected_limits_digest,
                    state,
                    failure,
                    plan.capture_mode,
                ),
                provenance=ArtifactProvenanceInput.create(
                    source_contract_uuid=evidence.stage_manifest.provenance.source_contract_uuid,
                    repository_sha=repository_sha,
                    image_digest=image_digest,
                    configuration_digest=configuration_digest,
                    restricted_boundaries={
                        "lower_history_id": plan.lower_history_id,
                        "upper_history_id": plan.upper_history_id,
                        "stage_artifact_id": evidence.stage_manifest.artifact_id,
                    },
                    counts={
                        "pages": state.pages,
                        "rows": state.rows,
                        "valid_rows": state.valid_rows,
                        "malformed_rows": state.malformed_rows,
                    },
                ),
                retention_expires_at=_retention_expiry(checked_at, retention_days),
                guard=write_guard,
            )
        except BaseException:
            spool.cleanup()
            raise
    return StageCaptureResult(
        manifest=manifest,
        qualified=failure is None,
        failure_reason=failure,
        pages=state.pages,
        rows=state.rows,
        valid_rows=state.valid_rows,
        malformed_rows=state.malformed_rows,
    )


def _capture_pages(
    client: RawStageHistoryClient,
    spool: StageHistoryIngestionSpool,
    *,
    evidence: StageQualificationEvidence,
    plan: StageSmokePlan,
    authorization: StageCaptureAuthorization,
    limits: StageCaptureLimits,
    state: _CaptureState,
    now: Callable[[], datetime],
    monotonic: Callable[[], float],
    ownership_guard: Callable[[], None],
) -> CaptureFailureReason | None:
    filters: dict[str, JsonValue] = {
        ">ID": str(plan.lower_history_id),
        "<=ID": str(plan.upper_history_id),
    }
    while True:
        ownership_guard()
        authorization.assert_active(now=now())
        _check_limits(state, spool, limits, monotonic)
        state.calls = client.request_count
        if state.calls >= limits.max_calls:
            raise RuntimeError("stage-history smoke call limit exceeded before range completion")
        ownership_guard()
        page = client.list_stage_history_raw_page(
            entity_type_id=evidence.entity_type_id,
            filters=filters,
            order_direction="ASC",
            start=-1,
        )
        state.calls = client.request_count
        if len(page.items) > _BITRIX_PAGE_SIZE:
            raise RuntimeError("Bitrix stage-history page exceeded 50 rows")
        decoded_rows: list[ValidCapturedRow | MalformedCapturedRow] = []
        page_failed = False
        ordering_failed = False
        for raw in page.items:
            state.rows += 1
            _check_limits(state, spool, limits, monotonic)
            decoded = decode_stage_history_item(raw, entity_type_id=str(evidence.entity_type_id))
            if isinstance(decoded, DecodedStageHistoryRow):
                item = decoded.item
                numeric_id = _numeric_history_id(item.history_id)
                if (
                    numeric_id is None
                    or numeric_id <= plan.lower_history_id
                    or numeric_id > plan.upper_history_id
                    or (state.last_history_id is not None and numeric_id <= state.last_history_id)
                ):
                    ordering_failed = True
                elif not ordering_failed:
                    state.last_history_id = numeric_id
                event_identity = encode_stage_source_record_id(
                    evidence.stage_manifest.provenance.source_contract_uuid,
                    item.entity_type_id,
                    item.history_id,
                )
                decoded_rows.append(
                    ValidCapturedRow(
                        raw_payload=decoded.raw,
                        event_identity=event_identity,
                        canonical_hash=canonical_stage_hash_v1(
                            evidence.stage_manifest.provenance.source_contract_uuid,
                            item,
                        ),
                    )
                )
                state.valid_rows += 1
            else:
                decoded_rows.append(
                    MalformedCapturedRow(
                        raw_payload=decoded.raw,
                        safe_error_code=decoded.error_code,
                    )
                )
                state.malformed_rows += 1
                page_failed = True
        authorization.assert_active(now=now())
        ownership_guard()
        _check_limits(state, spool, limits, monotonic)

        def page_write_guard() -> None:
            ownership_guard()
            _capture_write_guard(
                authorization,
                state,
                spool,
                limits,
                now=now,
                monotonic=monotonic,
            )

        spool.append_page(
            decoded_rows,
            source_observed_at=now(),
            max_storage_bytes=limits.max_spool_bytes,
            guard=page_write_guard,
        )
        state.pages += 1
        _check_limits(state, spool, limits, monotonic)
        if page_failed:
            return "malformed_row"
        if ordering_failed:
            return "expected_row_mismatch"
        if len(page.items) < _BITRIX_PAGE_SIZE:
            return None
        if state.last_history_id is None:
            raise RuntimeError("full stage-history page did not provide a keyset cursor")
        if state.last_history_id == plan.upper_history_id:
            return None
        filters[">ID"] = str(state.last_history_id)


def _matches_expected(spool: StageHistoryIngestionSpool, plan: StageSmokePlan) -> bool:
    spool.flush()
    path = spool.path
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = tuple(
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT event_identity, canonical_hash FROM rows "
                "WHERE row_kind = 'valid' ORDER BY artifact_row_sequence"
            )
        )
    finally:
        connection.close()
    expected = tuple((row.event_identity, row.canonical_hash) for row in plan.expected_rows)
    return rows == expected


def _numeric_history_id(value: str) -> int | None:
    try:
        return parse_positive_history_id(value)
    except ValueError:
        return None


def _check_limits(
    state: _CaptureState,
    spool: StageHistoryIngestionSpool,
    limits: StageCaptureLimits,
    monotonic: Callable[[], float],
) -> None:
    if state.calls > limits.max_calls:
        raise RuntimeError("stage-history smoke call limit exceeded")
    if state.rows > limits.max_rows:
        raise RuntimeError("stage-history smoke row limit exceeded")
    if monotonic() - state.started > limits.max_runtime_seconds:
        raise RuntimeError("stage-history smoke runtime limit exceeded")
    if spool.total_bytes > limits.max_spool_bytes:
        raise RuntimeError("stage-history smoke spool limit exceeded")


def _validate_capture_inputs(
    evidence: StageQualificationEvidence,
    plan: StageSmokePlan,
    limits: StageCaptureLimits,
    configuration_digest: str,
    retention_days: int,
) -> None:
    if plan.maximum_calls > limits.max_calls or len(plan.expected_rows) > limits.max_rows:
        raise ValueError("derived smoke plan exceeds capture limits")
    if plan.capture_mode == "backfill":
        canonical_plan = derive_backfill_plan(
            evidence, max_calls=limits.max_calls, max_rows=limits.max_rows
        )
    elif plan.capture_mode == "catch_up":
        canonical_plan = derive_catch_up_plan(
            evidence, max_calls=limits.max_calls, max_rows=limits.max_rows
        )
    else:
        canonical_plan = derive_smoke_plan(
            evidence, max_calls=limits.max_calls, max_rows=limits.max_rows
        )
    if plan != canonical_plan:
        raise ValueError("capture plan was not deterministically derived from qualification")
    if evidence.stage_manifest.provenance.configuration_digest != configuration_digest:
        raise ValueError("capture configuration differs from accepted qualification")
    if (
        isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or retention_days < 1
    ):
        raise ValueError("retention_days must be positive")


def _metadata(
    evidence: StageQualificationEvidence,
    authorization: StageCaptureAuthorization,
    configuration_digest: str,
    limits_digest: str,
    state: _CaptureState,
    failure: CaptureFailureReason | None,
    capture_mode: Literal["smoke", "backfill", "catch_up"],
) -> dict[str, JsonValue]:
    return {
        "mode": f"collect-{capture_mode.replace('_', '-')}",
        "status": "qualified" if failure is None else "failed",
        "failure_reason": failure,
        "ingestion_spool_file": _INGESTION_DB_FILE,
        "entity_type_id": str(evidence.entity_type_id),
        "owner_artifact_id": evidence.owner_manifest.artifact_id,
        "owner_manifest_digest": _required_metadata(
            evidence.owner_manifest, "owner_manifest_digest"
        ),
        "stage_artifact_id": evidence.stage_manifest.artifact_id,
        "qualification_evidence_digest": evidence.qualification_evidence_digest,
        "canonical_hash_version": "bitrix-stage-history-v1",
        "traversal_contract": "bounded_spool_reconcile",
        "configuration_digest": configuration_digest,
        "limits_digest": limits_digest,
        "authorization_reference": authorization.reference,
        "authorization_actor_digest": "sha256:"
        + hashlib.sha256(authorization.actor.encode("utf-8")).hexdigest(),
        "pages": state.pages,
        "rows": state.rows,
        "valid_rows": state.valid_rows,
        "malformed_rows": state.malformed_rows,
    }


def stage_capture_limits_digest(limits: StageCaptureLimits) -> str:
    encoded = canonical_json_bytes(
        {
            "domain": "bitrix-stage-history-capture-limits-v1",
            "max_calls": limits.max_calls,
            "max_rows": limits.max_rows,
            "max_spool_bytes": limits.max_spool_bytes,
            "max_runtime_seconds": limits.max_runtime_seconds,
        }
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _capture_write_guard(
    authorization: StageCaptureAuthorization,
    state: _CaptureState,
    spool: StageHistoryIngestionSpool,
    limits: StageCaptureLimits,
    *,
    now: Callable[[], datetime],
    monotonic: Callable[[], float],
) -> None:
    authorization.assert_active(now=now())
    _check_limits(state, spool, limits, monotonic)


def _required_metadata(manifest: ArtifactManifest, key: str) -> str:
    value = manifest.metadata.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"accepted artifact metadata omitted {key}")
    return value


def _retention_expiry(now: datetime, retention_days: int) -> datetime:
    return now.astimezone(UTC) + timedelta(days=retention_days)


def _validate_repository_sha(value: str) -> None:
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("repository_sha must be a full lowercase Git SHA")


def _validate_sha256_digest(value: str, field_name: str) -> None:
    prefix = "sha256:"
    payload = value.removeprefix(prefix)
    if (
        not value.startswith(prefix)
        or len(payload) != 64
        or any(character not in "0123456789abcdef" for character in payload)
    ):
        raise ValueError(f"{field_name} must be a canonical SHA-256 digest")
