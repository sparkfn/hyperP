"""Deterministic source-free replay for sealed Bitrix capability artifacts."""

from __future__ import annotations

import hmac
import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path
from typing import cast

from src.connectors.bitrix_stage_history.artifact_manifest import (
    ArtifactManifest,
    canonical_json_bytes,
)
from src.connectors.bitrix_stage_history.artifact_store import ArtifactStore
from src.connectors.bitrix_stage_history.capability_artifacts import (
    owner_summary_qualified,
    stage_summary_qualified,
)
from src.connectors.bitrix_stage_history.reconciliation_spool import digest_rows
from src.models import JsonValue


def qualify_artifacts(
    store: ArtifactStore,
    *,
    owner_artifact_id: str,
    stage_artifact_id: str,
) -> dict[str, JsonValue]:
    """Verify and replay both artifacts twice, requiring byte-identical results."""
    owner = store.verify(owner_artifact_id)
    stage = store.verify(stage_artifact_id)
    _validate_pair(owner, stage)
    first = _replay_once(owner, stage)
    second = _replay_once(owner, stage)
    if not hmac.compare_digest(canonical_json_bytes(first), canonical_json_bytes(second)):
        raise RuntimeError("sealed artifact replay was not deterministic")
    return {
        "qualification_schema_version": "bitrix-artifact-qualification-v1",
        "owner_artifact_id": owner_artifact_id,
        "stage_artifact_id": stage_artifact_id,
        "owner_recommendation": "verified_keyset",
        "stage_recommendation": "bounded_spool_reconcile",
        "deterministic_replay": True,
        "derived": first,
        "source_calls": 0,
        "graph_writes": 0,
        "stage_domain_writes": 0,
    }


def _replay_once(
    owner: ArtifactManifest,
    stage: ArtifactManifest,
) -> dict[str, JsonValue]:
    owner_root = Path(owner.provenance.artifact_path)
    stage_root = Path(stage.provenance.artifact_path)
    owner_db = _single_file(owner, suffix=".sqlite3")
    stage_db = _single_file(stage, suffix=".sqlite3")
    owner_summary = _read_json_object(owner_root / "owner-summary.json")
    stage_summary = _read_json_object(stage_root / "stage-summary.json")
    catalog_summary = _read_json_object(stage_root / "catalog-summary.json")
    if not owner_summary_qualified(owner_summary):
        raise RuntimeError("sealed owner artifact failed replay qualification")
    if not stage_summary_qualified(stage_summary, catalog_summary):
        raise RuntimeError("sealed stage artifact failed replay qualification")
    redaction_key_name = _required_metadata_string(owner, "redaction_key_file")
    redaction_key = (owner_root / redaction_key_name).read_bytes()
    owner_rows = _read_rows(
        owner_root / owner_db,
        "SELECT deal_id, category_id, stage_id, occurrence_count FROM owners ORDER BY deal_id",
    )
    stage_rows = _read_rows(
        stage_root / stage_db,
        "SELECT stable_id, canonical_hash, owner_id, category_id, stage_id, "
        "event_at, occurrence_count FROM events "
        "ORDER BY stable_id, canonical_hash",
    )
    owner_ids = {cast(str, row[0]) for row in owner_rows}
    in_scope = tuple(row for row in stage_rows if row[2] in owner_ids)
    owner_digest = digest_rows(
        owner_rows,
        domain="bitrix-capability-owner-manifest-v1",
        redaction_key=redaction_key,
    )
    stage_digest = digest_rows(
        ((row[0], row[1], row[6]) for row in stage_rows),
        domain="bitrix-capability-stage-global-identity-hash-v1",
        redaction_key=redaction_key,
    )
    in_scope_digest = digest_rows(
        ((row[0], row[1], row[6]) for row in in_scope),
        domain="bitrix-capability-stage-in-scope-identity-hash-v1",
        redaction_key=redaction_key,
    )
    owner_occurrences = _occurrences(owner_rows, 3)
    global_occurrences = _occurrences(stage_rows, 6)
    in_scope_occurrences = _occurrences(in_scope, 6)
    expected_owner_digest = _required_metadata_string(owner, "owner_manifest_digest")
    expected_stage_digest = _required_metadata_string(stage, "global_identity_hash_digest")
    expected_in_scope_digest = _required_metadata_string(stage, "in_scope_identity_hash_digest")
    for actual, expected, label in (
        (owner_digest, expected_owner_digest, "owner"),
        (stage_digest, expected_stage_digest, "global stage"),
        (in_scope_digest, expected_in_scope_digest, "in-scope stage"),
    ):
        if not hmac.compare_digest(actual, expected):
            raise RuntimeError(f"sealed {label} replay digest does not match collection")
    return {
        "owner_manifest_digest": owner_digest,
        "global_identity_hash_digest": stage_digest,
        "in_scope_identity_hash_digest": in_scope_digest,
        "owner_rows": len(owner_rows),
        "owner_occurrences": owner_occurrences,
        "global_stage_rows": len(stage_rows),
        "global_stage_occurrences": global_occurrences,
        "in_scope_stage_rows": len(in_scope),
        "in_scope_stage_occurrences": in_scope_occurrences,
        "out_of_scope_stage_occurrences": global_occurrences - in_scope_occurrences,
    }


def _validate_pair(owner: ArtifactManifest, stage: ArtifactManifest) -> None:
    if owner.artifact_kind not in {"owner-capability", "owner-export"}:
        raise ValueError("owner artifact has an incompatible kind")
    if stage.artifact_kind != "stage-capability":
        raise ValueError("stage artifact has an incompatible kind")
    if _required_metadata_string(stage, "owner_artifact_id") != owner.artifact_id:
        raise ValueError("stage artifact is not bound to the supplied owner artifact")
    if owner.provenance.source_contract_uuid != stage.provenance.source_contract_uuid:
        raise ValueError("owner and stage artifacts use different source contracts")


def _single_file(manifest: ArtifactManifest, *, suffix: str) -> str:
    matches = [item.relative_path for item in manifest.files if item.relative_path.endswith(suffix)]
    if len(matches) != 1:
        raise RuntimeError(f"sealed artifact must contain exactly one {suffix} file")
    return matches[0]


def _required_metadata_string(manifest: ArtifactManifest, key: str) -> str:
    value = manifest.metadata.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"sealed artifact metadata omitted {key}")
    return value


def _read_rows(path: Path, query: str) -> tuple[tuple[object, ...], ...]:
    uri = f"file:{path}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        return tuple(tuple(row) for row in connection.execute(query))


def _read_json_object(path: Path) -> dict[str, JsonValue]:
    try:
        parsed = cast(JsonValue, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("sealed capability summary is unreadable") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("sealed capability summary must be an object")
    return parsed


def _occurrences(rows: Iterable[tuple[object, ...]], index: int) -> int:
    total = 0
    for row in rows:
        value = row[index]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RuntimeError("sealed capability artifact contains an invalid occurrence count")
        total += value
    return total
