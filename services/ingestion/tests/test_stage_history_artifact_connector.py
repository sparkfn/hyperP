"""Focused source-free tests for sealed stage-history artifact replay."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

import pytest
from src.connectors.bitrix_stage_history import artifact_connector as artifact_connector_module
from src.connectors.bitrix_stage_history.artifact_connector import (
    StageArtifactReplayAuthorization,
    StageExpectedRow,
    StageQualificationEvidence,
    derive_smoke_plan,
    read_stage_ingestion_artifact,
)
from src.connectors.bitrix_stage_history.artifact_manifest import (
    ArtifactManifest,
    canonical_json_bytes,
    canonical_metadata_json,
)
from src.connectors.bitrix_stage_history.artifact_provenance import ArtifactProvenance
from src.connectors.bitrix_stage_history.artifact_store import (
    ArtifactStore,
    RestrictedArtifactSession,
)
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
    decode_stage_history_item,
)
from src.models import JsonValue
from src.stage_history_ingestion_models import (
    StageHistoryMalformedObservation,
    StageHistoryValidObservation,
)

_SOURCE_CONTRACT = "12345678-1234-5678-9234-567812345678"
_CONFIG_DIGEST = f"sha256:{'c' * 64}"
_OBSERVED_AT = datetime(2026, 8, 14, 4, 5, 6, 123456, tzinfo=UTC)
_REDACTION_KEY = b"r" * 32
_QUALIFIED_RESULT: dict[str, JsonValue] = {
    "qualification_schema_version": "bitrix-artifact-qualification-v1",
    "deterministic_replay": True,
}
_QUALIFICATION_DIGEST = (
    "sha256:" + hashlib.sha256(canonical_json_bytes(_QUALIFIED_RESULT)).hexdigest()
)


def _authorization(
    kind: str = "stage-ingestion",
) -> StageArtifactReplayAuthorization:
    return StageArtifactReplayAuthorization(
        reference="authorization-1",
        actor="reviewer-1",
        artifact_id="ingestion-artifact",
        manifest_hmac="f" * 64,
        artifact_kind=kind,
        manifest_schema_version=1,
        repository_sha="a" * 40,
        image_digest=f"sha256:{'b' * 64}",
        source_contract_uuid=_SOURCE_CONTRACT,
        entity_type_id="2",
        owner_artifact_id="owner-artifact",
        owner_manifest_hmac="a" * 64,
        stage_artifact_id="stage-artifact",
        stage_manifest_hmac="b" * 64,
        qualification_evidence_digest=_QUALIFICATION_DIGEST,
        configuration_digest=_CONFIG_DIGEST,
        limits_digest=f"sha256:{'e' * 64}",
        canonical_hash_version="bitrix-stage-history-v1",
        traversal_contract="bounded_spool_reconcile",
    )


@pytest.fixture(autouse=True)
def _stub_qualification_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    def qualify(
        store: ArtifactStore,
        *,
        owner_artifact_id: str,
        stage_artifact_id: str,
    ) -> dict[str, JsonValue]:
        _ = store, owner_artifact_id, stage_artifact_id
        return _QUALIFIED_RESULT

    monkeypatch.setattr(artifact_connector_module, "qualify_artifacts", qualify)


def test_smoke_plan_selects_a_deterministic_largest_bounded_suffix(tmp_path: Path) -> None:
    evidence = _planning_evidence(tmp_path, row_count=120)

    first = derive_smoke_plan(evidence, max_calls=2, max_rows=75)
    second = derive_smoke_plan(evidence, max_calls=2, max_rows=75)

    assert first == second
    assert first.lower_history_id == 70
    assert first.upper_history_id == 120
    assert tuple(row.history_id for row in first.expected_rows) == tuple(range(71, 121))
    assert first.maximum_calls == 1


def test_smoke_plan_uses_the_tighter_call_or_row_budget(tmp_path: Path) -> None:
    evidence = _planning_evidence(tmp_path, row_count=120)

    call_limited = derive_smoke_plan(evidence, max_calls=1, max_rows=100)
    with pytest.raises(ValueError, match="complete 50-row source page"):
        derive_smoke_plan(evidence, max_calls=10, max_rows=7)

    assert tuple(row.history_id for row in call_limited.expected_rows) == tuple(range(71, 121))
    assert call_limited.maximum_calls == 1


class _FakeStore(ArtifactStore):
    def __init__(self, manifests: dict[str, ArtifactManifest]) -> None:
        self._manifests = manifests
        self.verify_calls: list[str] = []
        self.source_calls = 0

    @contextmanager
    def begin(self, *, artifact_kind: str) -> Iterator[RestrictedArtifactSession]:
        raise AssertionError(f"artifact replay attempted a producer operation: {artifact_kind}")
        yield  # pragma: no cover

    def verify(self, artifact_id: str) -> ArtifactManifest:
        self.verify_calls.append(artifact_id)
        return self._manifests[artifact_id]

    def close(self) -> None:
        pass

    def __enter__(self) -> _FakeStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def test_reader_is_source_free_and_preserves_occurrence_identity_and_time(
    tmp_path: Path,
) -> None:
    raw_in_scope = _raw_row("101", owner_id="501")
    raw_out_of_scope = _raw_row("102", owner_id="999")
    store, artifact_id = _artifact_store(
        tmp_path,
        kind="stage-ingestion",
        pages=((raw_in_scope, raw_out_of_scope),),
        owner_ids=("501",),
    )

    first = read_stage_ingestion_artifact(
        store,
        artifact_id=artifact_id,
        authorization=_authorization(),
    )
    second = read_stage_ingestion_artifact(
        store,
        artifact_id=artifact_id,
        authorization=_authorization(),
    )

    assert store.source_calls == 0
    assert store.verify_calls == [
        artifact_id,
        "owner-artifact",
        "stage-artifact",
        artifact_id,
        "owner-artifact",
        "stage-artifact",
    ]
    assert first.pages == second.pages
    assert len(first.pages) == 1
    in_scope, out_of_scope = first.pages[0].rows
    assert in_scope.in_scope is True
    assert out_of_scope.in_scope is False
    assert isinstance(in_scope.observation, StageHistoryValidObservation)
    assert isinstance(out_of_scope.observation, StageHistoryValidObservation)
    assert in_scope.observation.source_observed_at == _OBSERVED_AT
    assert out_of_scope.observation.source_observed_at == _OBSERVED_AT
    assert in_scope.observation.row_sequence == 1
    assert out_of_scope.observation.row_sequence == 2
    assert in_scope.observation.occurrence_id == _valid_occurrence_id(
        artifact_id,
        1,
        in_scope.observation.event_identity,
        in_scope.observation.canonical_hash,
    )


def test_failed_reader_builds_keyed_malformed_occurrence_without_identity(
    tmp_path: Path,
) -> None:
    malformed: JsonValue = {"ID": None, "OWNER_ID": ["bad"]}
    store, artifact_id = _artifact_store(
        tmp_path,
        kind="stage-ingestion-failed",
        pages=((malformed,),),
        owner_ids=("501",),
        malformed=True,
    )

    artifact = read_stage_ingestion_artifact(
        store,
        artifact_id=artifact_id,
        authorization=_authorization("stage-ingestion-failed"),
    )

    row = artifact.pages[0].rows[0]
    assert row.in_scope is False
    assert isinstance(row.observation, StageHistoryMalformedObservation)
    assert row.observation.safe_error_code == "missing_history_id"
    assert row.observation.source_observed_at == _OBSERVED_AT
    assert row.observation.occurrence_id.startswith("hmac-sha256:")
    assert row.observation.occurrence_id == _malformed_occurrence_id(
        artifact.manifest,
        row.observation.row_sequence,
        row.observation.canonical_raw_row_digest,
        redaction_key=_REDACTION_KEY,
    )


def test_reader_accepts_the_inherited_fifty_row_page_limit(tmp_path: Path) -> None:
    rows = tuple(_raw_row(str(index), owner_id="501") for index in range(1, 51))
    store, artifact_id = _artifact_store(
        tmp_path,
        kind="stage-ingestion",
        pages=(rows,),
        owner_ids=("501",),
    )

    artifact = read_stage_ingestion_artifact(
        store,
        artifact_id=artifact_id,
        authorization=_authorization(),
    )

    assert len(artifact.pages) == 1
    assert len(artifact.pages[0].rows) == 50
    assert artifact.pages[0].rows[-1].observation.row_sequence == 50


def test_reader_uses_immutable_spool_validation(tmp_path: Path) -> None:
    store, artifact_id = _artifact_store(
        tmp_path,
        kind="stage-ingestion",
        pages=((_raw_row("101", owner_id="501"),),),
        owner_ids=("501",),
    )
    manifest = store.verify(artifact_id)
    spool_path = Path(manifest.provenance.artifact_path) / "stage-ingestion.sqlite3"
    spool_path.chmod(0o600)

    with pytest.raises(ValueError, match="must not be writable"):
        read_stage_ingestion_artifact(
            store,
            artifact_id=artifact_id,
            authorization=_authorization(),
        )


def test_reader_rejects_changed_qualification_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, artifact_id = _artifact_store(
        tmp_path,
        kind="stage-ingestion",
        pages=((_raw_row("101", owner_id="501"),),),
        owner_ids=("501",),
    )

    def changed(
        store: ArtifactStore,
        *,
        owner_artifact_id: str,
        stage_artifact_id: str,
    ) -> dict[str, JsonValue]:
        _ = store, owner_artifact_id, stage_artifact_id
        return {**_QUALIFIED_RESULT, "deterministic_replay": False}

    monkeypatch.setattr(artifact_connector_module, "qualify_artifacts", changed)
    with pytest.raises(ValueError, match="qualification evidence changed"):
        read_stage_ingestion_artifact(
            store,
            artifact_id=artifact_id,
            authorization=_authorization(),
        )


@pytest.mark.parametrize(
    "authorization",
    (
        replace(_authorization(), artifact_id="substitute-artifact"),
        replace(_authorization(), manifest_hmac="0" * 64),
        replace(_authorization(), actor="substitute-reviewer"),
        replace(_authorization(), owner_manifest_hmac="0" * 64),
        replace(_authorization(), stage_manifest_hmac="0" * 64),
        replace(_authorization(), repository_sha="0" * 40),
        replace(_authorization(), image_digest=f"sha256:{'0' * 64}"),
        replace(_authorization(), limits_digest=f"sha256:{'0' * 64}"),
        replace(_authorization(), configuration_digest=f"sha256:{'0' * 64}"),
    ),
)
def test_reader_rejects_authorization_descriptor_substitution(
    tmp_path: Path,
    authorization: StageArtifactReplayAuthorization,
) -> None:
    store, artifact_id = _artifact_store(
        tmp_path,
        kind="stage-ingestion",
        pages=((_raw_row("101", owner_id="501"),),),
        owner_ids=("501",),
    )
    with pytest.raises(ValueError, match="not authorized|changed"):
        read_stage_ingestion_artifact(
            store,
            artifact_id=artifact_id,
            authorization=authorization,
        )


def test_replay_authorization_rejects_boolean_schema_version() -> None:
    with pytest.raises(ValueError, match="schema version"):
        replace(_authorization(), manifest_schema_version=True)


def _artifact_store(
    tmp_path: Path,
    *,
    kind: str,
    pages: tuple[tuple[JsonValue, ...], ...],
    owner_ids: tuple[str, ...],
    malformed: bool = False,
) -> tuple[_FakeStore, str]:
    owner_root = tmp_path / "owner"
    stage_root = tmp_path / "stage"
    ingestion_root = tmp_path / "ingestion"
    for path in (owner_root, stage_root, ingestion_root):
        path.mkdir(mode=0o700)
    owner_db = owner_root / "owners.sqlite3"
    redaction_key_file = owner_root / "redaction-key.bin"
    redaction_key_file.write_bytes(_REDACTION_KEY)
    connection = sqlite3.connect(owner_db)
    try:
        connection.execute("CREATE TABLE owners (deal_id TEXT PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO owners(deal_id) VALUES (?)",
            tuple((owner_id,) for owner_id in owner_ids),
        )
        connection.commit()
    finally:
        connection.close()
    owner_digest = "hmac-sha256:owner-digest"
    owner = _manifest(
        "owner-artifact",
        "owner-export",
        owner_root,
        {
            "owner_manifest_file": owner_db.name,
            "owner_manifest_digest": owner_digest,
            "redaction_key_file": redaction_key_file.name,
        },
        manifest_hmac="a" * 64,
    )
    stage = _manifest(
        "stage-artifact",
        "stage-capability",
        stage_root,
        {
            "owner_artifact_id": owner.artifact_id,
            "recommendation": "bounded_spool_reconcile",
        },
        manifest_hmac="b" * 64,
    )
    artifact_id = "ingestion-artifact"
    spool = StageHistoryIngestionSpool(ingestion_root, artifact_id=artifact_id)
    for page in pages:
        captured: list[ValidCapturedRow | MalformedCapturedRow] = []
        for raw in page:
            if malformed:
                captured.append(MalformedCapturedRow(raw, safe_error_code="missing_history_id"))
            else:
                decoded = decode_stage_history_item(raw, entity_type_id="2")
                assert isinstance(decoded, DecodedStageHistoryRow)
                identity = encode_stage_source_record_id(
                    _SOURCE_CONTRACT,
                    "2",
                    decoded.item.history_id,
                )
                captured.append(
                    ValidCapturedRow(
                        raw,
                        event_identity=identity,
                        canonical_hash=canonical_stage_hash_v1(
                            _SOURCE_CONTRACT,
                            decoded.item,
                        ),
                    )
                )
        spool.append_page(captured, source_observed_at=_OBSERVED_AT)
    sealed = spool.seal()
    sealed.rename(ingestion_root / "stage-ingestion.sqlite3")
    row_count = sum(len(page) for page in pages)
    malformed_count = row_count if malformed else 0
    counts = {
        "pages": len(pages),
        "rows": row_count,
        "valid_rows": row_count - malformed_count,
        "malformed_rows": malformed_count,
    }
    ingestion = _manifest(
        artifact_id,
        kind,
        ingestion_root,
        {
            "status": "qualified" if kind == "stage-ingestion" else "failed",
            "failure_reason": (
                None
                if kind == "stage-ingestion"
                else "malformed_row"
                if malformed_count
                else "expected_row_mismatch"
            ),
            "authorization_reference": "authorization-1",
            "authorization_actor_digest": "sha256:" + hashlib.sha256(b"reviewer-1").hexdigest(),
            "owner_artifact_id": owner.artifact_id,
            "stage_artifact_id": stage.artifact_id,
            "owner_manifest_digest": owner_digest,
            "canonical_hash_version": "bitrix-stage-history-v1",
            "traversal_contract": "bounded_spool_reconcile",
            "entity_type_id": "2",
            "ingestion_spool_file": "stage-ingestion.sqlite3",
            "qualification_evidence_digest": _QUALIFICATION_DIGEST,
            "limits_digest": f"sha256:{'e' * 64}",
            "configuration_digest": _CONFIG_DIGEST,
            **counts,
        },
        counts=counts,
        manifest_hmac="f" * 64,
    )
    return _FakeStore(
        {
            ingestion.artifact_id: ingestion,
            owner.artifact_id: owner,
            stage.artifact_id: stage,
        }
    ), artifact_id


def _manifest(
    artifact_id: str,
    artifact_kind: str,
    root: Path,
    metadata: dict[str, JsonValue],
    *,
    manifest_hmac: str,
    counts: dict[str, int] | None = None,
) -> ArtifactManifest:
    return ArtifactManifest(
        schema_version=1,
        artifact_id=artifact_id,
        artifact_kind=artifact_kind,
        created_at="2026-08-14T03:00:00Z",
        retention_expires_at="2026-09-14T03:00:00Z",
        metadata_json=canonical_metadata_json(metadata),
        files=(),
        provenance=ArtifactProvenance(
            artifact_path=str(root.absolute()),
            primary_device=1,
            primary_inode=1,
            backup_device=1,
            backup_inode=2,
            owner_uid=1,
            group_gid=1,
            directory_mode=0o500,
            source_contract_uuid=_SOURCE_CONTRACT,
            repository_sha="a" * 40,
            image_digest=f"sha256:{'b' * 64}",
            configuration_digest=_CONFIG_DIGEST,
            restricted_boundaries_json='{"upper_history_id":"200"}',
            counts_json=json.dumps(counts or {"rows": 1}, sort_keys=True, separators=(",", ":")),
            total_bytes=0,
        ),
        backup_path=str((root.parent / "backup" / artifact_id).absolute()),
        backup_verified=True,
        signing_key_id="key-1",
        manifest_hmac=manifest_hmac,
    )


def _planning_evidence(tmp_path: Path, *, row_count: int) -> StageQualificationEvidence:
    owner_root = tmp_path / "planning-owner"
    stage_root = tmp_path / "planning-stage"
    owner_root.mkdir(mode=0o700)
    stage_root.mkdir(mode=0o700)
    owner = _manifest(
        "planning-owner-artifact",
        "owner-export",
        owner_root,
        {"owner_manifest_digest": "hmac-sha256:owner"},
        manifest_hmac="a" * 64,
    )
    stage = _manifest(
        "planning-stage-artifact",
        "stage-capability",
        stage_root,
        {"owner_artifact_id": owner.artifact_id},
        manifest_hmac="b" * 64,
    )
    return StageQualificationEvidence(
        owner_manifest=owner,
        stage_manifest=stage,
        qualification_evidence_digest=f"sha256:{'d' * 64}",
        entity_type_id=2,
        owner_ids=frozenset({"501"}),
        expected_rows=tuple(
            StageExpectedRow(
                history_id=index,
                event_identity=f"event-{index}",
                canonical_hash=f"sha256:{index:064x}",
            )
            for index in range(1, row_count + 1)
        ),
    )


def _raw_row(history_id: str, *, owner_id: str) -> dict[str, JsonValue]:
    return {
        "ID": history_id,
        "OWNER_ID": owner_id,
        "TYPE_ID": "1",
        "CREATED_TIME": "2026-08-14T02:30:00+00:00",
        "CATEGORY_ID": "2",
        "STAGE_SEMANTIC_ID": "P",
        "STAGE_ID": "C2:NEW",
    }


def _valid_occurrence_id(
    artifact_id: str,
    row_sequence: int,
    event_identity: str,
    canonical_hash: str,
) -> str:
    digest = hashlib.sha256()
    for value in (
        "bitrix-stage-history-valid-occurrence-v1",
        artifact_id,
        str(row_sequence),
        event_identity,
        canonical_hash,
    ):
        digest.update(value.encode("utf-8"))
        digest.update(b"\x00")
    return "sha256:" + digest.hexdigest()


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
