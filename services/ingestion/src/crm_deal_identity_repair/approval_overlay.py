"""Verification and deterministic allocation of an externally approved repair overlay."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.control_models import RepairOverlayRow
from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.execution_models import (
    RepairExecutionBoundaryManifest,
    RepairUnit,
)
from src.models import JsonValue

_OVERLAY_DOMAIN = b"crm-deal-identity-repair-approval-overlay-v1\x00"
_UNIT_DOMAIN = b"crm-deal-identity-repair-allocation-unit-v1\x00"
_COMPLETION_DOMAIN = b"crm-deal-identity-repair-allocation-completion-v1\x00"


@dataclass(frozen=True)
class VerifiedApprovalOverlay:
    """Immutable approval overlay bound to exactly one qualified boundary."""

    approval_reference: str
    overlay_digest: str
    rows: tuple[RepairOverlayRow, ...]


def verify_approval_overlay(
    payload: Mapping[str, JsonValue], *, manifest: RepairExecutionBoundaryManifest
) -> VerifiedApprovalOverlay:
    """Validate caller-supplied overlay metadata without creating an approval."""
    required = {
        "approval_reference",
        "artifact_id",
        "artifact_manifest_hmac",
        "inventory_digest",
        "manifest_digest",
        "repository_sha",
        "image_digest",
        "configuration_digest",
        "source_contract_uuid",
        "unit_ceiling",
        "rows",
    }
    if set(payload) != required:
        raise ValueError("repair approval overlay schema is invalid")
    bindings = {
        "artifact_id": manifest.artifact_id,
        "artifact_manifest_hmac": manifest.artifact_manifest_hmac,
        "inventory_digest": manifest.inventory_digest,
        "manifest_digest": manifest.manifest_digest,
        "repository_sha": manifest.repository_sha,
        "image_digest": manifest.image_digest,
        "configuration_digest": manifest.configuration_digest,
        "source_contract_uuid": manifest.source_contract_uuid,
        "unit_ceiling": manifest.unit_ceiling,
    }
    for key, expected in bindings.items():
        if payload.get(key) != expected:
            raise ValueError(f"repair approval overlay {key} does not bind the qualified run")
    reference = payload["approval_reference"]
    if reference != manifest.approval_reference or not isinstance(reference, str) or not reference:
        raise ValueError("repair approval overlay reference is invalid")
    raw_rows = payload["rows"]
    if not isinstance(raw_rows, list):
        raise ValueError("repair approval overlay rows are invalid")
    rows = tuple(_overlay_row(row) for row in raw_rows)
    keys = tuple(row.inventory_key for row in rows)
    if len(keys) != len(set(keys)):
        raise ValueError("repair approval overlay rows must be unique")
    source_record_pks = tuple(row.source_record_pk for row in rows)
    if len(source_record_pks) != len(set(source_record_pks)):
        raise ValueError("repair approval overlay source-record identities must be unique")
    fingerprints = tuple(row.inventory_fingerprint for row in rows)
    if len(fingerprints) != len(set(fingerprints)):
        raise ValueError("repair approval overlay row fingerprints must be unique")
    if len(rows) != manifest.inventory_row_count:
        raise ValueError("repair approval overlay does not cover the qualified inventory")
    if any(row.inventory_key != row.source_record_pk for row in rows):
        raise ValueError("repair approval overlay inventory keys must equal qualified identities")
    return VerifiedApprovalOverlay(
        reference,
        object_digest(_OVERLAY_DOMAIN, dict(payload)),
        rows,
    )


def allocate_units(
    overlay: VerifiedApprovalOverlay,
    *,
    run_id: str,
    manifest: RepairExecutionBoundaryManifest,
    generation: int,
) -> tuple[RepairUnit, ...]:
    """Return canonical executable units only; blocked/investigate rows never allocate."""
    executable = tuple(
        sorted(
            (row for row in overlay.rows if row.disposition == "executable"),
            key=lambda row: row.inventory_key,
        )
    )
    if len(executable) > manifest.unit_ceiling:
        raise ValueError("repair executable overlay rows exceed the qualified ceiling")
    return tuple(
        RepairUnit(
            run_id=run_id,
            unit_id="unit-"
            + object_digest(
                _UNIT_DOMAIN,
                {"run_id": run_id, "inventory_key": row.inventory_key},
            ).removeprefix("sha256:")[:32],
            generation=generation,
            sequence=index,
            attempt=1,
            boundary_digest=manifest.graph_boundary_digest,
            inventory_fingerprint=row.inventory_fingerprint,
            state="allocated",
        )
        for index, row in enumerate(executable)
    )


def allocation_digest(overlay: VerifiedApprovalOverlay, units: tuple[RepairUnit, ...]) -> str:
    """Seal allocation enumeration, including a valid empty executable selection."""
    payload: dict[str, JsonValue] = {
        "overlay_digest": overlay.overlay_digest,
        "units": [
            {
                "unit_id": unit.unit_id,
                "sequence": unit.sequence,
                "inventory_fingerprint": unit.inventory_fingerprint,
            }
            for unit in units
        ],
    }
    return object_digest(_COMPLETION_DOMAIN, payload)


def _overlay_row(value: JsonValue) -> RepairOverlayRow:
    if not isinstance(value, dict) or set(value) != {
        "inventory_key",
        "source_record_pk",
        "inventory_fingerprint",
        "disposition",
    }:
        raise ValueError("repair approval overlay row schema is invalid")
    return RepairOverlayRow(
        inventory_key=_string(value["inventory_key"]),
        source_record_pk=_string(value["source_record_pk"]),
        inventory_fingerprint=_string(value["inventory_fingerprint"]),
        disposition=_disposition(value["disposition"]),
    )


def _string(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise ValueError("repair approval overlay value must be a string")
    return value


def _disposition(value: JsonValue) -> str:
    if value not in {"executable", "blocked", "investigate"}:
        raise ValueError("repair approval overlay disposition is invalid")
    return str(value)


def verify_sealed_approval_overlay(
    encoded: bytes,
    *,
    manifest: RepairExecutionBoundaryManifest,
    signing_secret: bytes,
) -> VerifiedApprovalOverlay:
    """Verify a detached HMAC envelope before accepting an externally approved overlay."""
    import hashlib
    import hmac
    import json

    try:
        envelope = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError("repair approval overlay is not JSON") from exc
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "hmac"}:
        raise ValueError("repair approval overlay envelope is invalid")
    payload = envelope["payload"]
    supplied = envelope["hmac"]
    if not isinstance(payload, dict) or not isinstance(supplied, str):
        raise ValueError("repair approval overlay envelope values are invalid")
    expected = hmac.new(
        signing_secret,
        _OVERLAY_DOMAIN + canonical_json_bytes(payload),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied.removeprefix("sha256:"), expected):
        raise ValueError("repair approval overlay HMAC is invalid")
    typed: dict[str, JsonValue] = {str(key): value for key, value in payload.items()}
    return verify_approval_overlay(typed, manifest=manifest)
