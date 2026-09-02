"""Verification of the separately approved, HMAC-sealed #310 overlay."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.execution_models import RepairQualificationRun
from src.models import JsonValue

APPROVAL_OVERLAY_VERSION = "crm_deal_identity_repair_approval_v1"
APPROVAL_OVERLAY_HMAC_DOMAIN = b"crm-deal-identity-repair-approval-v1\x00"
ApprovalDisposition = Literal["executable", "blocked", "investigate"]


@dataclass(frozen=True)
class ApprovalRow:
    inventory_key: str
    source_record_pk: str
    graph_fingerprint: str
    stored_payload_fingerprint: str
    disposition: ApprovalDisposition

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.inventory_key,
                self.source_record_pk,
                self.graph_fingerprint,
                self.stored_payload_fingerprint,
            )
        ):
            raise ValueError("approval row identity is incomplete")
        if self.disposition not in {"executable", "blocked", "investigate"}:
            raise ValueError("approval row disposition is invalid")


@dataclass(frozen=True)
class ApprovalOverlay:
    approval_id: str
    repair_id: str
    run_id: str
    qualification_identity: str
    artifact_id: str
    artifact_manifest_hmac: str
    inventory_digest: str
    inventory_row_count: int
    boundary_digest: str
    repository_sha: str
    image_digest: str
    configuration_digest: str
    source_contract_uuid: str
    approval_reference: str
    unit_ceiling: int
    rows: tuple[ApprovalRow, ...]
    key_id: str
    overlay_digest: str

    def __post_init__(self) -> None:
        if self.inventory_row_count < 1 or self.unit_ceiling < 0:
            raise ValueError("approval overlay counts are invalid")
        if len(self.rows) != self.inventory_row_count:
            raise ValueError("approval overlay does not cover the qualified inventory")
        keys = tuple(row.inventory_key for row in self.rows)
        if len(keys) != len(set(keys)):
            raise ValueError("approval overlay has duplicate row identities")
        if sum(row.disposition == "executable" for row in self.rows) > self.unit_ceiling:
            raise ValueError("approval overlay executable ceiling is exceeded")


def verify_approval_overlay(
    path: Path,
    *,
    secret: bytes,
    expected: ApprovalOverlay | None = None,
) -> ApprovalOverlay:
    """Verify canonical bytes, HMAC, and every immutable qualification binding."""
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("repair approval overlay is not JSON") from exc
    if not isinstance(document, dict):
        raise RuntimeError("repair approval overlay must be an object")
    if canonical_json_bytes(cast(dict[str, JsonValue], document)) != raw:
        raise RuntimeError("repair approval overlay is not canonical")
    transport = cast(dict[str, JsonValue], document)
    supplied_hmac = _string(transport.get("hmac"), "approval HMAC")
    payload = {key: value for key, value in transport.items() if key != "hmac"}
    computed = hmac.new(
        secret, APPROVAL_OVERLAY_HMAC_DOMAIN + canonical_json_bytes(payload), hashlib.sha256
    )
    if not hmac.compare_digest(supplied_hmac, computed.hexdigest()):
        raise RuntimeError("repair approval overlay HMAC is invalid")
    overlay = _overlay_from_payload(payload)
    if expected is not None and overlay != expected:
        raise RuntimeError("repair approval overlay does not bind the qualified run exactly")
    return overlay


def assert_overlay_binds_qualification(
    overlay: ApprovalOverlay, *, run: RepairQualificationRun, expected_key_id: str
) -> None:
    """Reject every overlay that is not an exact #300 approval binding."""
    manifest = run.manifest
    expected = (
        run.repair_id,
        run.run_id,
        run.qualification_identity,
        run.artifact_id,
        run.artifact_manifest_hmac,
        run.inventory_digest,
        run.inventory_row_count,
        run.boundary_digest,
        manifest.repository_sha,
        manifest.image_digest,
        manifest.configuration_digest,
        manifest.source_contract_uuid,
        manifest.approval_reference,
        manifest.unit_ceiling,
        expected_key_id,
    )
    actual = (
        overlay.repair_id,
        overlay.run_id,
        overlay.qualification_identity,
        overlay.artifact_id,
        overlay.artifact_manifest_hmac,
        overlay.inventory_digest,
        overlay.inventory_row_count,
        overlay.boundary_digest,
        overlay.repository_sha,
        overlay.image_digest,
        overlay.configuration_digest,
        overlay.source_contract_uuid,
        overlay.approval_reference,
        overlay.unit_ceiling,
        overlay.key_id,
    )
    if actual != expected:
        raise RuntimeError("repair approval overlay does not bind the qualified manifest exactly")


def approval_overlay_digest(payload: dict[str, JsonValue]) -> str:
    """Digest an overlay payload excluding its transport HMAC."""
    return object_digest(APPROVAL_OVERLAY_HMAC_DOMAIN, payload)


def _overlay_from_payload(payload: dict[str, JsonValue]) -> ApprovalOverlay:
    required = {
        "version",
        "approval_id",
        "repair_id",
        "run_id",
        "qualification_identity",
        "artifact_id",
        "artifact_manifest_hmac",
        "inventory_digest",
        "inventory_row_count",
        "boundary_digest",
        "repository_sha",
        "image_digest",
        "configuration_digest",
        "source_contract_uuid",
        "approval_reference",
        "unit_ceiling",
        "rows",
        "key_id",
    }
    if set(payload) != required or payload.get("version") != APPROVAL_OVERLAY_VERSION:
        raise RuntimeError("repair approval overlay schema is invalid")
    raw_rows = payload["rows"]
    if not isinstance(raw_rows, list):
        raise RuntimeError("repair approval rows are invalid")
    rows: list[ApprovalRow] = []
    for raw in raw_rows:
        if not isinstance(raw, dict) or set(raw) != {
            "inventory_key",
            "source_record_pk",
            "graph_fingerprint",
            "stored_payload_fingerprint",
            "disposition",
        }:
            raise RuntimeError("repair approval row schema is invalid")
        disposition = raw["disposition"]
        if disposition not in {"executable", "blocked", "investigate"}:
            raise RuntimeError("repair approval disposition is invalid")
        rows.append(
            ApprovalRow(
                _string(raw["inventory_key"], "inventory key"),
                _string(raw["source_record_pk"], "source PK"),
                _string(raw["graph_fingerprint"], "graph fingerprint"),
                _string(raw["stored_payload_fingerprint"], "payload fingerprint"),
                cast(ApprovalDisposition, disposition),
            )
        )
    canonical = dict(payload)
    return ApprovalOverlay(
        approval_id=_string(payload["approval_id"], "approval ID"),
        repair_id=_string(payload["repair_id"], "repair ID"),
        run_id=_string(payload["run_id"], "run ID"),
        qualification_identity=_string(payload["qualification_identity"], "qualification identity"),
        artifact_id=_string(payload["artifact_id"], "artifact ID"),
        artifact_manifest_hmac=_string(payload["artifact_manifest_hmac"], "artifact HMAC"),
        inventory_digest=_string(payload["inventory_digest"], "inventory digest"),
        inventory_row_count=_integer(payload["inventory_row_count"], "inventory row count"),
        boundary_digest=_string(payload["boundary_digest"], "boundary digest"),
        repository_sha=_string(payload["repository_sha"], "repository SHA"),
        image_digest=_string(payload["image_digest"], "image digest"),
        configuration_digest=_string(payload["configuration_digest"], "configuration digest"),
        source_contract_uuid=_string(payload["source_contract_uuid"], "source contract UUID"),
        approval_reference=_string(payload["approval_reference"], "approval reference"),
        unit_ceiling=_integer(payload["unit_ceiling"], "unit ceiling"),
        rows=tuple(rows),
        key_id=_string(payload["key_id"], "key ID"),
        overlay_digest=approval_overlay_digest(canonical),
    )


def _string(value: JsonValue | object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"repair approval {label} is invalid")
    return value


def _integer(value: JsonValue | object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"repair approval {label} is invalid")
    return value
