"""Authenticated artifact readers for bounded stage-history capture and replay."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from src.connectors.bitrix_stage_history.artifact_manifest import (
    ArtifactManifest,
    canonical_json_bytes,
)
from src.connectors.bitrix_stage_history.artifact_store import ArtifactStore
from src.connectors.bitrix_stage_history.canonical import decode_stage_source_record_id
from src.connectors.bitrix_stage_history.capability_artifacts import (
    owner_summary_qualified,
    stage_summary_qualified,
)
from src.connectors.bitrix_stage_history.models import (
    parse_positive_history_id,
    parse_positive_numeric_id,
)
from src.connectors.bitrix_stage_history.replay import qualify_artifacts
from src.models import JsonValue
from src.stage_history_ingestion_models import (
    StageHistoryMalformedObservation,
    StageHistoryValidObservation,
)

_BITRIX_PAGE_SIZE = 50


@dataclass(frozen=True, slots=True)
class StageExpectedRow:
    history_id: int
    event_identity: str
    canonical_hash: str


@dataclass(frozen=True, slots=True)
class StageQualificationEvidence:
    owner_manifest: ArtifactManifest
    stage_manifest: ArtifactManifest
    qualification_evidence_digest: str
    entity_type_id: int
    owner_ids: frozenset[str]
    expected_rows: tuple[StageExpectedRow, ...]


@dataclass(frozen=True, slots=True)
class StageSmokePlan:
    lower_history_id: int
    upper_history_id: int
    expected_rows: tuple[StageExpectedRow, ...]
    maximum_calls: int

    def __post_init__(self) -> None:
        if self.lower_history_id >= self.upper_history_id:
            raise ValueError("smoke history-ID range must be non-empty")
        if not self.expected_rows:
            raise ValueError("smoke plan requires expected stage-history rows")
        if len(self.expected_rows) > self.maximum_calls * _BITRIX_PAGE_SIZE:
            raise ValueError("smoke plan rows exceed its finite call budget")
        ids = tuple(row.history_id for row in self.expected_rows)
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise ValueError("smoke plan requires strictly increasing history IDs")
        if ids[0] <= self.lower_history_id or ids[-1] > self.upper_history_id:
            raise ValueError("smoke plan rows must remain inside the approved range")


def load_qualification_evidence(
    store: ArtifactStore,
    *,
    owner_artifact_id: str,
    stage_artifact_id: str,
    expected_qualification_evidence_digest: str,
    expected_source_contract_uuid: str,
    expected_configuration_digest: str,
    entity_type_id: int,
) -> StageQualificationEvidence:
    """Authenticate accepted capability artifacts and their deterministic replay."""
    if (
        isinstance(entity_type_id, bool)
        or not isinstance(entity_type_id, int)
        or entity_type_id < 1
    ):
        raise ValueError("entity_type_id must be positive")
    owner = store.verify(owner_artifact_id)
    stage = store.verify(stage_artifact_id)
    _validate_accepted_manifests(
        owner,
        stage,
        expected_source_contract_uuid=expected_source_contract_uuid,
        expected_configuration_digest=expected_configuration_digest,
    )
    qualified = qualify_artifacts(
        store,
        owner_artifact_id=owner_artifact_id,
        stage_artifact_id=stage_artifact_id,
    )
    qualification_digest = _qualification_digest(qualified)
    if not hmac.compare_digest(qualification_digest, expected_qualification_evidence_digest):
        raise ValueError("qualification evidence digest does not match the accepted replay")
    owner_root = Path(owner.provenance.artifact_path)
    stage_root = Path(stage.provenance.artifact_path)
    owner_summary = _read_json_object(owner_root / "owner-summary.json")
    stage_summary = _read_json_object(stage_root / "stage-summary.json")
    catalog_summary = _read_json_object(stage_root / "catalog-summary.json")
    if not owner_summary_qualified(owner_summary):
        raise RuntimeError("accepted owner artifact is no longer qualified")
    if not stage_summary_qualified(stage_summary, catalog_summary):
        raise RuntimeError("accepted stage artifact is no longer qualified")
    owner_ids = _read_owner_ids(owner, owner_root)
    expected_rows = _read_expected_stage_rows(
        stage,
        stage_root,
        expected_source_contract_uuid=expected_source_contract_uuid,
        entity_type_id=entity_type_id,
    )
    return StageQualificationEvidence(
        owner_manifest=owner,
        stage_manifest=stage,
        qualification_evidence_digest=qualification_digest,
        entity_type_id=entity_type_id,
        owner_ids=owner_ids,
        expected_rows=expected_rows,
    )


def derive_smoke_plan(
    evidence: StageQualificationEvidence,
    *,
    max_calls: int,
    max_rows: int,
) -> StageSmokePlan:
    """Choose a deterministic suffix range that fits mandatory finite limits."""
    if isinstance(max_calls, bool) or max_calls < 1:
        raise ValueError("max_calls must be positive")
    if isinstance(max_rows, bool) or max_rows < 1:
        raise ValueError("max_rows must be positive")
    page_budget = min(max_calls, max_rows // _BITRIX_PAGE_SIZE)
    if page_budget < 1:
        raise ValueError("smoke limits must reserve one complete 50-row source page")
    capacity = page_budget * _BITRIX_PAGE_SIZE
    selected = evidence.expected_rows[-capacity:]
    if not selected:
        raise RuntimeError("accepted stage artifact contains no smoke-test rows")
    lower = selected[0].history_id - 1
    upper = selected[-1].history_id
    required_calls = (len(selected) + _BITRIX_PAGE_SIZE - 1) // _BITRIX_PAGE_SIZE
    return StageSmokePlan(lower, upper, selected, required_calls)


def _validate_accepted_manifests(
    owner: ArtifactManifest,
    stage: ArtifactManifest,
    *,
    expected_source_contract_uuid: str,
    expected_configuration_digest: str,
) -> None:
    if owner.artifact_kind not in {"owner-capability", "owner-export"}:
        raise ValueError("accepted owner artifact has an incompatible kind")
    if stage.artifact_kind != "stage-capability":
        raise ValueError("accepted stage artifact has an incompatible kind")
    if _metadata_string(stage, "owner_artifact_id") != owner.artifact_id:
        raise ValueError("accepted stage artifact is not bound to the owner artifact")
    if _metadata_string(stage, "recommendation") != "bounded_spool_reconcile":
        raise ValueError("accepted stage artifact lacks the bounded replay recommendation")
    for manifest in (owner, stage):
        provenance = manifest.provenance
        if provenance.source_contract_uuid != expected_source_contract_uuid:
            raise ValueError("accepted artifact source contract changed")
        if provenance.configuration_digest != expected_configuration_digest:
            raise ValueError("accepted artifact configuration digest changed")


def _read_owner_ids(manifest: ArtifactManifest, root: Path) -> frozenset[str]:
    file_name = _metadata_string(manifest, "owner_manifest_file")
    rows = _read_rows(root / file_name, "SELECT deal_id FROM owners ORDER BY deal_id")
    owner_ids: set[str] = set()
    for row in rows:
        if len(row) != 1 or not isinstance(row[0], str) or not row[0]:
            raise RuntimeError("sealed owner artifact contains an invalid owner identity")
        try:
            parse_positive_numeric_id(row[0], field_name="owner ID")
        except ValueError as exc:
            raise RuntimeError(
                "sealed owner artifact contains a noncanonical owner identity"
            ) from exc
        if row[0] in owner_ids:
            raise RuntimeError("sealed owner artifact contains duplicate owner identities")
        owner_ids.add(row[0])
    if not owner_ids:
        raise RuntimeError("sealed owner artifact contains no owner identities")
    return frozenset(owner_ids)


def _read_expected_stage_rows(
    manifest: ArtifactManifest,
    root: Path,
    *,
    expected_source_contract_uuid: str,
    entity_type_id: int,
) -> tuple[StageExpectedRow, ...]:
    file_name = _metadata_string(manifest, "stage_reconciliation_file")
    rows = _read_rows(
        root / file_name,
        "SELECT stable_id, canonical_hash, occurrence_count FROM events ORDER BY stable_id",
    )
    parsed: list[StageExpectedRow] = []
    for row in rows:
        if (
            len(row) != 3
            or not isinstance(row[0], str)
            or not isinstance(row[1], str)
            or isinstance(row[2], bool)
            or not isinstance(row[2], int)
            or row[2] != 1
        ):
            raise RuntimeError("sealed stage artifact cannot derive an exact smoke range")
        contract, row_entity_type, history_text = decode_stage_source_record_id(row[0])
        if contract != expected_source_contract_uuid or row_entity_type != str(entity_type_id):
            raise RuntimeError("sealed stage artifact identity domain changed")
        try:
            history_id = parse_positive_history_id(history_text)
        except ValueError as exc:
            raise RuntimeError("sealed stage artifact contains a noncanonical history ID") from exc
        parsed.append(StageExpectedRow(history_id, row[0], row[1]))
    ordered = tuple(sorted(parsed, key=lambda item: item.history_id))
    ids = tuple(item.history_id for item in ordered)
    if len(ids) != len(set(ids)):
        raise RuntimeError("sealed stage artifact contains duplicate history IDs")
    return ordered


def _metadata_string(manifest: ArtifactManifest, key: str) -> str:
    value = manifest.metadata.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"sealed artifact metadata omitted {key}")
    return value


def _metadata_non_negative_int(manifest: ArtifactManifest, key: str) -> int:
    value = manifest.metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"sealed artifact metadata omitted {key}")
    return value


def _read_rows(path: Path, query: str) -> tuple[tuple[object, ...], ...]:
    uri = f"file:{path}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        return tuple(tuple(row) for row in connection.execute(query))


def _read_json_object(path: Path) -> dict[str, JsonValue]:
    try:
        raw = cast(JsonValue, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("sealed capability summary is unreadable") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("sealed capability summary must be an object")
    return raw


@dataclass(frozen=True, slots=True)
class StageArtifactReplayRow:
    observation: StageHistoryValidObservation | StageHistoryMalformedObservation
    in_scope: bool


@dataclass(frozen=True, slots=True)
class StageArtifactReplayPage:
    page_sequence: int
    page_digest: str
    rows: tuple[StageArtifactReplayRow, ...]


@dataclass(frozen=True, slots=True)
class VerifiedStageIngestionArtifact:
    manifest: ArtifactManifest
    owner_manifest: ArtifactManifest
    stage_manifest: ArtifactManifest
    pages: tuple[StageArtifactReplayPage, ...]


@dataclass(frozen=True, slots=True)
class StageArtifactReplayAuthorization:
    reference: str
    actor: str
    artifact_id: str
    manifest_hmac: str
    artifact_kind: str
    manifest_schema_version: int
    repository_sha: str
    image_digest: str
    source_contract_uuid: str
    entity_type_id: str
    owner_artifact_id: str
    owner_manifest_hmac: str
    stage_artifact_id: str
    stage_manifest_hmac: str
    qualification_evidence_digest: str
    configuration_digest: str
    limits_digest: str
    canonical_hash_version: str
    traversal_contract: str

    def __post_init__(self) -> None:
        values = (
            self.reference,
            self.actor,
            self.artifact_id,
            self.manifest_hmac,
            self.artifact_kind,
            self.repository_sha,
            self.image_digest,
            self.source_contract_uuid,
            self.entity_type_id,
            self.owner_artifact_id,
            self.owner_manifest_hmac,
            self.stage_artifact_id,
            self.stage_manifest_hmac,
            self.qualification_evidence_digest,
            self.configuration_digest,
            self.limits_digest,
            self.canonical_hash_version,
            self.traversal_contract,
        )
        if any(not value.strip() for value in values):
            raise ValueError("stage replay authorization fields must be non-empty")
        if self.artifact_kind not in {"stage-ingestion", "stage-ingestion-failed"}:
            raise ValueError("stage replay authorization artifact kind is invalid")
        if (
            isinstance(self.manifest_schema_version, bool)
            or not isinstance(self.manifest_schema_version, int)
            or self.manifest_schema_version != 1
        ):
            raise ValueError("stage replay authorization schema version is invalid")
        if self.canonical_hash_version != "bitrix-stage-history-v1":
            raise ValueError("stage replay authorization hash version is invalid")
        if self.traversal_contract != "bounded_spool_reconcile":
            raise ValueError("stage replay authorization traversal contract is invalid")
        parse_positive_numeric_id(self.entity_type_id, field_name="entity_type_id")


def read_stage_ingestion_artifact(
    store: ArtifactStore,
    *,
    artifact_id: str,
    authorization: StageArtifactReplayAuthorization,
) -> VerifiedStageIngestionArtifact:
    """Verify one sealed capture artifact and decode its immutable pages source-free."""
    from src.connectors.bitrix_stage_history.canonical import (
        canonical_stage_hash_v1,
        encode_stage_source_record_id,
    )
    from src.connectors.bitrix_stage_history.ingestion_spool import (
        SealedStageHistoryIngestionSpool,
    )
    from src.connectors.bitrix_stage_history.models import (
        DecodedStageHistoryRow,
        decode_stage_history_item,
    )

    if not hmac.compare_digest(artifact_id, authorization.artifact_id):
        raise ValueError("stage ingestion artifact ID is not authorized")
    manifest = store.verify(artifact_id)
    if manifest.artifact_kind != authorization.artifact_kind:
        raise ValueError("sealed stage ingestion artifact has an incompatible kind")
    expected_status = "qualified" if authorization.artifact_kind == "stage-ingestion" else "failed"
    if _metadata_string(manifest, "status") != expected_status:
        raise RuntimeError("sealed stage ingestion artifact status disagrees with its kind")
    _validate_replay_authorization(manifest, authorization)
    owner = store.verify(_metadata_string(manifest, "owner_artifact_id"))
    stage = store.verify(_metadata_string(manifest, "stage_artifact_id"))
    if not hmac.compare_digest(owner.manifest_hmac, authorization.owner_manifest_hmac):
        raise ValueError("stage ingestion owner manifest HMAC changed")
    if not hmac.compare_digest(stage.manifest_hmac, authorization.stage_manifest_hmac):
        raise ValueError("stage ingestion stage manifest HMAC changed")
    _validate_ingestion_bindings(manifest, owner, stage)
    qualified = qualify_artifacts(
        store,
        owner_artifact_id=owner.artifact_id,
        stage_artifact_id=stage.artifact_id,
    )
    if not hmac.compare_digest(
        _qualification_digest(qualified),
        _metadata_string(manifest, "qualification_evidence_digest"),
    ):
        raise ValueError("stage ingestion qualification evidence changed")
    owner_ids = _read_owner_ids(owner, Path(owner.provenance.artifact_path))
    redaction_key = _read_redaction_key(owner)
    source_contract_uuid = manifest.provenance.source_contract_uuid
    entity_type_id = _metadata_string(manifest, "entity_type_id")
    spool_name = _metadata_string(manifest, "ingestion_spool_file")
    spool_path = Path(manifest.provenance.artifact_path) / spool_name
    pages: list[StageArtifactReplayPage] = []
    with SealedStageHistoryIngestionSpool(spool_path, expected_artifact_id=artifact_id) as spool:
        for page in spool.pages():
            replay_rows: list[StageArtifactReplayRow] = []
            for row in page.rows:
                observed_at = _parse_timestamp(row.source_observed_at)
                if row.row_kind == "valid":
                    decoded = decode_stage_history_item(
                        row.raw_payload,
                        entity_type_id=entity_type_id,
                    )
                    if not isinstance(decoded, DecodedStageHistoryRow):
                        raise RuntimeError("sealed valid row no longer decodes as valid")
                    event_identity = encode_stage_source_record_id(
                        source_contract_uuid,
                        entity_type_id,
                        decoded.item.history_id,
                    )
                    canonical_hash = canonical_stage_hash_v1(
                        source_contract_uuid,
                        decoded.item,
                    )
                    if row.event_identity != event_identity or row.canonical_hash != canonical_hash:
                        raise RuntimeError("sealed valid row identity/hash evidence changed")
                    observation: StageHistoryValidObservation | StageHistoryMalformedObservation
                    observation = StageHistoryValidObservation(
                        occurrence_id=_valid_occurrence_id(
                            artifact_id,
                            row.artifact_row_sequence,
                            event_identity,
                            canonical_hash,
                        ),
                        artifact_id=artifact_id,
                        page_sequence=page.page_sequence,
                        row_sequence=row.artifact_row_sequence,
                        event_identity=event_identity,
                        canonical_hash=canonical_hash,
                        item=decoded.item,
                        logical_parent_source_system="bitrix_chat",
                        logical_parent_source_record_id=(
                            f"bitrix-crm-deal-{decoded.item.owner_id}"
                        ),
                        source_observed_at=observed_at,
                    )
                    in_scope = decoded.item.owner_id in owner_ids
                else:
                    if row.safe_error_code is None:
                        raise RuntimeError("sealed malformed row omitted its safe error code")
                    raw_digest = _raw_row_digest(row.raw_payload, redaction_key=redaction_key)
                    observation = StageHistoryMalformedObservation(
                        occurrence_id=_malformed_occurrence_id(
                            manifest,
                            row.artifact_row_sequence,
                            raw_digest,
                            redaction_key=redaction_key,
                        ),
                        artifact_id=artifact_id,
                        page_sequence=page.page_sequence,
                        row_sequence=row.artifact_row_sequence,
                        canonical_raw_row_digest=raw_digest,
                        safe_error_code=row.safe_error_code,
                        source_observed_at=observed_at,
                    )
                    in_scope = False
                replay_rows.append(StageArtifactReplayRow(observation, in_scope))
            pages.append(
                StageArtifactReplayPage(
                    page_sequence=page.page_sequence,
                    page_digest=page.page_digest,
                    rows=tuple(replay_rows),
                )
            )
    page_count = len(pages)
    rows = tuple(row for page in pages for row in page.rows)
    row_count = len(rows)
    malformed_count = sum(
        isinstance(row.observation, StageHistoryMalformedObservation) for row in rows
    )
    valid_count = row_count - malformed_count
    actual_counts = {
        "pages": page_count,
        "rows": row_count,
        "valid_rows": valid_count,
        "malformed_rows": malformed_count,
    }
    metadata_counts = {key: _metadata_non_negative_int(manifest, key) for key in actual_counts}
    provenance_counts = _ingestion_provenance_counts(manifest)
    if metadata_counts != actual_counts or provenance_counts != actual_counts:
        raise RuntimeError("sealed stage ingestion metadata accounting changed")
    failure_reason = manifest.metadata.get("failure_reason")
    if authorization.artifact_kind == "stage-ingestion" and failure_reason is not None:
        raise RuntimeError("qualified stage ingestion artifact has a failure reason")
    if authorization.artifact_kind == "stage-ingestion-failed":
        expected_failure_reason = "malformed_row" if malformed_count else "expected_row_mismatch"
        if failure_reason != expected_failure_reason:
            raise RuntimeError("failed stage ingestion artifact has an invalid failure reason")
    if authorization.artifact_kind == "stage-ingestion" and malformed_count:
        raise RuntimeError("qualified stage ingestion artifact contains malformed rows")
    return VerifiedStageIngestionArtifact(manifest, owner, stage, tuple(pages))


def _ingestion_provenance_counts(manifest: ArtifactManifest) -> dict[str, int]:
    try:
        decoded = cast(JsonValue, json.loads(manifest.provenance.counts_json))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("sealed stage ingestion provenance counts are invalid") from exc
    expected = {"pages", "rows", "valid_rows", "malformed_rows"}
    if not isinstance(decoded, dict) or set(decoded) != expected:
        raise RuntimeError("sealed stage ingestion provenance accounting changed")
    counts: dict[str, int] = {}
    for key in expected:
        value = decoded[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("sealed stage ingestion provenance accounting changed")
        counts[key] = value
    return counts


def _validate_ingestion_bindings(
    ingestion: ArtifactManifest,
    owner: ArtifactManifest,
    stage: ArtifactManifest,
) -> None:
    if owner.artifact_kind not in {"owner-capability", "owner-export"}:
        raise ValueError("stage ingestion owner artifact has an incompatible kind")
    if stage.artifact_kind != "stage-capability":
        raise ValueError("stage ingestion qualification artifact has an incompatible kind")
    if _metadata_string(stage, "recommendation") != "bounded_spool_reconcile":
        raise ValueError("stage ingestion qualification recommendation changed")
    if _metadata_string(stage, "owner_artifact_id") != owner.artifact_id:
        raise ValueError("stage qualification is not bound to the ingestion owner")
    if _metadata_string(ingestion, "owner_manifest_digest") != _metadata_string(
        owner, "owner_manifest_digest"
    ):
        raise ValueError("stage ingestion owner digest changed")
    if _metadata_string(ingestion, "canonical_hash_version") != "bitrix-stage-history-v1":
        raise ValueError("stage ingestion canonical hash version changed")
    if _metadata_string(ingestion, "traversal_contract") != "bounded_spool_reconcile":
        raise ValueError("stage ingestion traversal contract changed")
    for qualified in (owner, stage):
        if qualified.provenance.source_contract_uuid != ingestion.provenance.source_contract_uuid:
            raise ValueError("stage ingestion source contract changed")
        if qualified.provenance.configuration_digest != (ingestion.provenance.configuration_digest):
            raise ValueError("stage ingestion configuration digest changed")


def _validate_replay_authorization(
    manifest: ArtifactManifest,
    authorization: StageArtifactReplayAuthorization,
) -> None:
    if manifest.schema_version != authorization.manifest_schema_version:
        raise ValueError("sealed stage ingestion manifest schema changed")
    if not hmac.compare_digest(manifest.manifest_hmac, authorization.manifest_hmac):
        raise ValueError("sealed stage ingestion manifest HMAC changed")
    expected_metadata = {
        "authorization_reference": authorization.reference,
        "authorization_actor_digest": "sha256:"
        + hashlib.sha256(authorization.actor.encode("utf-8")).hexdigest(),
        "entity_type_id": authorization.entity_type_id,
        "owner_artifact_id": authorization.owner_artifact_id,
        "stage_artifact_id": authorization.stage_artifact_id,
        "qualification_evidence_digest": authorization.qualification_evidence_digest,
        "configuration_digest": authorization.configuration_digest,
        "limits_digest": authorization.limits_digest,
        "canonical_hash_version": authorization.canonical_hash_version,
        "traversal_contract": authorization.traversal_contract,
    }
    for key, expected in expected_metadata.items():
        if not hmac.compare_digest(_metadata_string(manifest, key), expected):
            raise ValueError(f"sealed stage ingestion {key} changed")
    if not hmac.compare_digest(
        manifest.provenance.source_contract_uuid,
        authorization.source_contract_uuid,
    ):
        raise ValueError("sealed stage ingestion source contract changed")
    if not hmac.compare_digest(manifest.provenance.repository_sha, authorization.repository_sha):
        raise ValueError("sealed stage ingestion repository provenance changed")
    if not hmac.compare_digest(manifest.provenance.image_digest, authorization.image_digest):
        raise ValueError("sealed stage ingestion image provenance changed")
    if not hmac.compare_digest(
        manifest.provenance.configuration_digest,
        authorization.configuration_digest,
    ):
        raise ValueError("sealed stage ingestion configuration provenance changed")


def _valid_occurrence_id(
    artifact_id: str,
    row_sequence: int,
    event_identity: str,
    canonical_hash: str,
) -> str:
    return _domain_digest(
        "bitrix-stage-history-valid-occurrence-v1",
        artifact_id,
        str(row_sequence),
        event_identity,
        canonical_hash,
    )


def _malformed_occurrence_id(
    manifest: ArtifactManifest,
    row_sequence: int,
    raw_digest: str,
    *,
    redaction_key: bytes,
) -> str:
    digest = hmac.new(redaction_key, digestmod=hashlib.sha256)
    for value in (
        "bitrix-stage-history-malformed-occurrence-v1",
        manifest.artifact_id,
        str(row_sequence),
        raw_digest,
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\x00")
    return "hmac-sha256:" + digest.hexdigest()


def _read_redaction_key(owner: ArtifactManifest) -> bytes:
    file_name = _metadata_string(owner, "redaction_key_file")
    key = (Path(owner.provenance.artifact_path) / file_name).read_bytes()
    if len(key) < 32:
        raise RuntimeError("sealed owner artifact redaction key is invalid")
    return key


def _raw_row_digest(raw: JsonValue, *, redaction_key: bytes) -> str:
    encoded = canonical_json_bytes(
        {
            "domain": "bitrix-stage-history-raw-row-v1",
            "raw": raw,
        }
    )
    protected = hmac.new(redaction_key, encoded, hashlib.sha256).digest()
    return "sha256:" + hashlib.sha256(protected).hexdigest()


def _qualification_digest(qualified: dict[str, JsonValue]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(qualified)).hexdigest()


def _domain_digest(domain: str, *values: str) -> str:
    digest = hashlib.sha256()
    digest.update(domain.encode("utf-8"))
    digest.update(b"\x00")
    for value in values:
        digest.update(value.encode("utf-8"))
        digest.update(b"\x00")
    return "sha256:" + digest.hexdigest()


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("sealed stage ingestion timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("sealed stage ingestion timestamp must be timezone-aware")
    return parsed
