"""Focused terminal membership-snapshot validation cases for Issue #305."""

from __future__ import annotations

import pytest
from _crm_tenant_projection_observation_cases import (
    _DIGEST,
    _SnapshotValidationTx,
    _terminal_snapshot_row,
    _validate_terminal_snapshot,
)
from _standalone_crm_lane_a_fakes import projection_scope
from src.crm_company_contracts import CrmCompanyMembershipSnapshotRecord
from src.crm_identity_associations import normalize_company_membership_snapshot
from src.crm_tenant_projection_models import CrmTenantProjectionIntegrityError
from src.graph import crm_tenant_projection_snapshot_validation as snapshot_validation
from src.standalone_crm_child_contracts import (
    StandaloneCrmSourceAvailability,
    StandaloneCrmSourceChildScope,
)


def test_terminal_snapshot_validation_uses_exclusive_200_row_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = sorted(
        (
            _terminal_snapshot_row(
                str(company_id),
                observation_node_id=f"node-{company_id}",
                is_primary=company_id == 1,
            )
            for company_id in range(1, 201)
        ),
        key=lambda row: str(row["observation_id"]),
    )
    recorded: list[tuple[int, set[str]]] = []

    def validate_contents(
        page_rows: list[dict[str, object]],
        _snapshot_id: str,
        _kind: str,
        _subject_id: str,
        _source_key: str,
        _source_instance_id: str,
        _control_instance_id: str,
        observation_ids: set[str],
    ) -> None:
        recorded.append((len(page_rows), observation_ids))

    monkeypatch.setattr(snapshot_validation, "_validate_snapshot_contents", validate_contents)
    terminal_row = _terminal_snapshot_row(
        observation_id=None,
        observation_node_id=None,
        observation_snapshot_id=None,
        observation_subject_kind=None,
        observation_subject_id=None,
        company_id=None,
        observation_sort=None,
        observation_role_id=None,
        observation_is_primary=None,
        snapshot_reference_count=0,
        observation_owner_count=0,
        company_reference_count=0,
        reference_company_id=None,
        reference_source_key=None,
        reference_source_instance_id=None,
        reference_control_instance_id=None,
    )
    tx = _SnapshotValidationTx([rows, [terminal_row]])

    _validate_terminal_snapshot(tx)

    assert [call["page_limit"] for call in tx.calls] == [200, 200]
    assert tx.calls[0]["cursor"] is None
    assert tx.calls[1]["cursor"] == rows[-1]["observation_id"]
    assert recorded == [(201, {str(row["observation_id"]) for row in rows})]


def test_terminal_snapshot_validation_accepts_empty_snapshot_null_row() -> None:
    scope = projection_scope()
    snapshot = normalize_company_membership_snapshot(
        subject_type="contact", subject_id="101", payloads=()
    )
    record = CrmCompanyMembershipSnapshotRecord(
        StandaloneCrmSourceChildScope(
            scope.source_key, scope.source_instance_id, scope.control_instance_id
        ),
        snapshot,
        "source-record-a",
        "101",
        1,
        _DIGEST,
        None,
        StandaloneCrmSourceAvailability("2026-01-01T00:00:00Z"),
        0,
    )
    row = _terminal_snapshot_row(
        observation_id=None,
        observation_node_id=None,
        observation_snapshot_id=None,
        observation_subject_kind=None,
        observation_subject_id=None,
        company_id=None,
        observation_sort=None,
        observation_role_id=None,
        observation_is_primary=None,
        snapshot_reference_count=0,
        observation_owner_count=0,
        company_reference_count=0,
        reference_company_id=None,
        reference_source_key=None,
        reference_source_instance_id=None,
        reference_control_instance_id=None,
        binding_count=0,
        snapshot_digest=record.snapshot_digest,
        snapshot_source_record_id=record.source_record_id,
        snapshot_source_record_pk=record.source_record_pk,
        snapshot_source_record_version=record.source_record_version,
        snapshot_source_record_hash=record.source_record_hash,
        snapshot_observed_at=record.observed_at,
        snapshot_available_at=record.availability.available_at,
        snapshot_contract_version=record.contract_version,
        input={"input_id": "input-a", "snapshot_id": record.snapshot_id},
        snapshot={
            "snapshot_id": record.snapshot_id,
            "subject_kind": "contact",
            "subject_id": "101",
            "source_instance_id": scope.source_instance_id,
            "control_instance_id": scope.control_instance_id,
        },
    )

    _validate_terminal_snapshot(_SnapshotValidationTx([[row]]), record.snapshot_id)


@pytest.mark.parametrize(
    "overrides",
    (
        {"snapshot_reference_count": 2},
        {"observation_owner_count": 2},
        {"company_reference_count": 2},
        {"observation_id": "malformed"},
    ),
)
def test_terminal_snapshot_validation_rejects_malformed_observation_topology(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(CrmTenantProjectionIntegrityError, match="membership"):
        _validate_terminal_snapshot(_SnapshotValidationTx([[_terminal_snapshot_row(**overrides)]]))
