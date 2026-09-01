"""Focused unmapped-observation projection cases for Issue #305."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from _standalone_crm_lane_a_fakes import prepared_mapping_revision, projection_scope
from src.connectors.bitrix_openlines.models import CrmCompanyBindingPayload
from src.crm_company_contracts import CrmCompanyMembershipSnapshotRecord
from src.crm_identity_associations import CrmCompanyBinding, normalize_company_membership_snapshot
from src.crm_tenant_mapping_identity import mapping_head_id
from src.crm_tenant_mapping_models import CrmTenantMappingExpectedHeadBoundary
from src.crm_tenant_projection_models import (
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionMaterializationCommand,
    CrmTenantProjectionReleaseSummary,
)
from src.crm_tenant_projection_records import _digest as projection_digest
from src.graph import crm_tenant_projection_snapshot_validation as snapshot_validation
from src.graph import crm_tenant_projection_write as projection_write
from src.graph.queries import crm_tenant_projection_projection as projection_queries
from src.graph.queries.crm_tenant_projection_release_pages import (
    READ_SNAPSHOT_OBSERVATION_GUARD,
    READ_SNAPSHOT_OBSERVATION_PAGE,
)
from src.standalone_crm_child_contracts import (
    StandaloneCrmSourceAvailability,
    StandaloneCrmSourceChildScope,
)

_DIGEST = "sha256:" + "a" * 64


def _snapshot_record() -> CrmCompanyMembershipSnapshotRecord:
    scope = projection_scope()
    snapshot = normalize_company_membership_snapshot(
        subject_type="contact",
        subject_id="101",
        payloads=(CrmCompanyBindingPayload("303", None, None, True),),
    )
    return CrmCompanyMembershipSnapshotRecord(
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
        1,
    )


class _ProjectionRowsResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def single(self) -> dict[str, object]:
        return self._rows[0]


class _ProjectionTx:
    def __init__(
        self,
        rows: list[dict[str, object]],
        support_bound: dict[str, object] | None = None,
    ) -> None:
        self.rows = rows
        self._support_bound = support_bound or {
            "binding_count": _row_binding_count(rows),
            "support_row_count": len(rows),
        }
        self.calls: list[str] = []
        self.parameters: list[dict[str, object]] = []

    def run(self, query: str, **parameters: object) -> _ProjectionRowsResult:
        self.calls.append(query)
        self.parameters.append(parameters)
        if query == projection_queries.READ_INPUT_SUPPORT_BOUND:
            return _ProjectionRowsResult([self._support_bound])
        if query == projection_queries.READ_INPUT_SUPPORTS:
            return _ProjectionRowsResult(self.rows)
        if query == projection_queries.WRITE_DECISION:
            return _ProjectionRowsResult([{"input_id": parameters["input_id"]}])
        raise AssertionError("unexpected projection query")


def _command() -> CrmTenantProjectionMaterializationCommand:
    scope = projection_scope()
    prepared = prepared_mapping_revision()
    return CrmTenantProjectionMaterializationCommand(
        scope,
        "projection-request",
        "census-a",
        _DIGEST,
        prepared.revision_id,
        prepared.manifest_digest,
        CrmTenantMappingExpectedHeadBoundary(
            scope.mapping_scope, mapping_head_id(scope.mapping_scope), None
        ),
        None,
        2,
    )


def _projection_release() -> CrmTenantProjectionReleaseSummary:
    command = _command()
    return CrmTenantProjectionReleaseSummary(
        command.scope,
        "release-a",
        1,
        command.request_id,
        command.release_fingerprint,
        command.source_census_id,
        command.mapping_revision_id,
        command.mapping_manifest_digest,
        "building",
        "projection",
        None,
        None,
        1,
        0,
        0,
        0,
    )


def _unmapped_observation_row(**overrides: object) -> dict[str, object]:
    record = _snapshot_record()
    observation_id = projection_digest(
        "crm-company-membership-observation-v1",
        [record.snapshot_id, "303", None, None, True],
    )
    row: dict[str, object] = {
        "binding_count": 1,
        "observation_id": observation_id,
        "observation_node_id": "node-a",
        "snapshot_digest": record.snapshot_digest,
        "snapshot_source_record_id": record.source_record_id,
        "snapshot_source_record_pk": record.source_record_pk,
        "snapshot_source_record_version": record.source_record_version,
        "snapshot_source_record_hash": record.source_record_hash,
        "snapshot_observed_at": record.observed_at,
        "snapshot_available_at": record.availability.available_at,
        "snapshot_contract_version": record.contract_version,
        "observation_snapshot_id": record.snapshot_id,
        "observation_subject_kind": "contact",
        "observation_subject_id": "101",
        "company_id": "303",
        "observation_sort": None,
        "observation_role_id": None,
        "observation_is_primary": True,
        "snapshot_reference_count": 1,
        "observation_owner_count": 1,
        "company_reference_count": 1,
        "reference_company_id": "303",
        "reference_source_key": "bitrix_chat",
        "reference_source_instance_id": "portal-a",
        "reference_control_instance_id": "control-a",
        "mapping_target_id": None,
        "entity_key": None,
        "relationship_kind": None,
    }
    row.update(overrides)
    return row


def _row_binding_count(rows: list[dict[str, object]]) -> int:
    if not rows:
        return 0
    binding_count = rows[0].get("binding_count")
    if not isinstance(binding_count, int):
        raise AssertionError("projection fake rows must include an integer binding_count")
    return binding_count


def test_projection_support_fan_out_limit_fails_before_writes() -> None:
    tx = _ProjectionTx(
        [_unmapped_observation_row()],
        {"binding_count": 501, "support_row_count": 501},
    )

    with pytest.raises(CrmTenantProjectionIntegrityError, match="support fan-out exceeds"):
        projection_write._project_one_input(
            tx, _projection_release(), "input-a", "contact", "101", "x"
        )

    assert tx.calls == [projection_queries.READ_INPUT_SUPPORT_BOUND]
    assert tx.parameters == [
        {
            "release_id": "release-a",
            "mapping_revision_id": _projection_release().mapping_revision_id,
            "input_id": "input-a",
            "snapshot_id": "x",
            "support_row_limit": 501,
        }
    ]


def test_projection_support_preflight_uses_actual_rows_not_global_mapping_targets() -> None:
    tx = _ProjectionTx(
        [_unmapped_observation_row()],
        {"binding_count": 1, "support_row_count": 1},
    )

    assert projection_write._project_one_input(
        tx, _projection_release(), "input-a", "contact", "101", _snapshot_record().snapshot_id
    ) == (0, 0)
    assert tx.calls == [
        projection_queries.READ_INPUT_SUPPORT_BOUND,
        projection_queries.READ_INPUT_SUPPORTS,
        projection_queries.WRITE_DECISION,
    ]


def _replacement_observation_row() -> dict[str, object]:
    record = _snapshot_record()
    return _unmapped_observation_row(
        observation_id=projection_digest(
            "crm-company-membership-observation-v1",
            [record.snapshot_id, "404", None, None, True],
        ),
        observation_node_id="node-b",
        company_id="404",
        reference_company_id="404",
    )


def _terminal_snapshot_row(
    company_id: str = "303", is_primary: bool = True, **overrides: object
) -> dict[str, object]:
    record = _snapshot_record()
    observation_id = projection_digest(
        "crm-company-membership-observation-v1",
        [record.snapshot_id, company_id, None, None, is_primary],
    )
    row = _unmapped_observation_row(
        observation_id=observation_id,
        company_id=company_id,
        reference_company_id=company_id,
        observation_is_primary=is_primary,
    )
    row.update(
        {
            "input": {
                "input_id": "input-a",
                "snapshot_id": record.snapshot_id,
            },
            "snapshot": {
                "snapshot_id": record.snapshot_id,
                "subject_kind": "contact",
                "subject_id": "101",
                "source_instance_id": projection_scope().source_instance_id,
                "control_instance_id": projection_scope().control_instance_id,
            },
            "input_owner_links": 1,
            "input_owner_count": 1,
            "snapshot_links": 1,
        }
    )
    row.update(overrides)
    return row


class _SnapshotValidationRows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def single(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None


class _SnapshotValidationTx:
    def __init__(
        self,
        pages: list[list[dict[str, object]]],
        guard_overrides: dict[str, object] | None = None,
    ) -> None:
        self._pages = pages
        self._guard_overrides = {} if guard_overrides is None else guard_overrides
        self.calls: list[dict[str, object]] = []

    def run(self, query: str, **parameters: object) -> _SnapshotValidationRows:
        self.calls.append(parameters)
        if query == READ_SNAPSHOT_OBSERVATION_GUARD:
            rows = [row for page in self._pages for row in page]
            source = rows[0]
            snapshot = dict(source["snapshot"])
            observation_rows = [row for row in rows if row["observation_id"] is not None]
            snapshot["binding_count"] = len(observation_rows)
            guard: dict[str, object] = {
                "input": source["input"],
                "snapshot": snapshot,
                "input_owner_links": 1,
                "input_owner_count": 1,
                "snapshot_links": 1,
                "observation_links": len(observation_rows),
                "observation_nodes": len(observation_rows),
                "observation_id_count": len(observation_rows),
                "distinct_observation_ids": len(observation_rows),
            }
            guard.update(self._guard_overrides)
            return _SnapshotValidationRows([guard])
        assert query == READ_SNAPSHOT_OBSERVATION_PAGE
        return _SnapshotValidationRows(self._pages.pop(0))


def _validate_terminal_snapshot(tx: _SnapshotValidationTx, snapshot_id: str | None = None) -> None:
    snapshot_validation._validate_input_snapshot_contents(
        tx,
        _projection_release(),
        "input-a",
        _snapshot_record().snapshot_id if snapshot_id is None else snapshot_id,
        "contact",
        "101",
    )


def test_unmapped_observation_is_validated_before_zero_target_decision() -> None:
    tx = _ProjectionTx([_unmapped_observation_row()])

    assert projection_write._project_one_input(
        tx, _projection_release(), "input-a", "contact", "101", _snapshot_record().snapshot_id
    ) == (0, 0)
    assert tx.calls == [
        projection_queries.READ_INPUT_SUPPORT_BOUND,
        projection_queries.READ_INPUT_SUPPORTS,
        projection_queries.WRITE_DECISION,
    ]


@pytest.mark.parametrize(
    "rows",
    (
        (_replacement_observation_row(),),
        (_unmapped_observation_row(), _replacement_observation_row()),
        (_unmapped_observation_row(binding_count=2),),
    ),
)
def test_snapshot_content_corruption_fails_before_decision(
    rows: tuple[dict[str, object], ...],
) -> None:
    tx = _ProjectionTx(list(rows))

    with pytest.raises(CrmTenantProjectionIntegrityError, match="membership snapshot"):
        projection_write._project_one_input(
            tx, _projection_release(), "input-a", "contact", "101", _snapshot_record().snapshot_id
        )

    assert tx.calls == [
        projection_queries.READ_INPUT_SUPPORT_BOUND,
        projection_queries.READ_INPUT_SUPPORTS,
    ]


@pytest.mark.parametrize(
    "overrides",
    (
        {"observation_subject_id": "102"},
        {"observation_snapshot_id": "other-snapshot"},
        {"snapshot_reference_count": 2},
        {"observation_owner_count": 2},
        {"company_reference_count": 0},
        {"reference_company_id": "other"},
        {"reference_source_key": "other-source"},
        {"observation_id": "malformed"},
    ),
)
def test_unmapped_observation_topology_fails_closed_before_decision(
    overrides: dict[str, object],
) -> None:
    tx = _ProjectionTx([_unmapped_observation_row(**overrides)])

    with pytest.raises(CrmTenantProjectionIntegrityError, match="membership"):
        projection_write._project_one_input(
            tx, _projection_release(), "input-a", "contact", "101", _snapshot_record().snapshot_id
        )

    assert tx.calls == [
        projection_queries.READ_INPUT_SUPPORT_BOUND,
        projection_queries.READ_INPUT_SUPPORTS,
    ]


def test_snapshot_contents_uses_canonical_multibinding_order_for_digest_and_identity() -> None:
    scope = projection_scope()
    snapshot = normalize_company_membership_snapshot(
        subject_type="contact",
        subject_id="101",
        payloads=(
            CrmCompanyBindingPayload("100", 0, None, False),
            CrmCompanyBindingPayload("900", None, None, True),
        ),
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
        2,
    )
    bindings = (
        CrmCompanyBinding("100", 0, None, False),
        CrmCompanyBinding("900", None, None, True),
    )
    assert tuple(sorted(bindings)) != snapshot.bindings
    rows = [
        {
            "binding_count": 2,
            "observation_id": projection_digest(
                "crm-company-membership-observation-v1",
                [
                    record.snapshot_id,
                    binding.company_id,
                    binding.sort,
                    binding.role_id,
                    binding.is_primary,
                ],
            ),
            "company_id": binding.company_id,
            "observation_sort": binding.sort,
            "observation_role_id": binding.role_id,
            "observation_is_primary": binding.is_primary,
            "snapshot_digest": record.snapshot_digest,
            "snapshot_source_record_id": record.source_record_id,
            "snapshot_source_record_pk": record.source_record_pk,
            "snapshot_source_record_version": record.source_record_version,
            "snapshot_source_record_hash": record.source_record_hash,
            "snapshot_observed_at": record.observed_at,
            "snapshot_available_at": record.availability.available_at,
            "snapshot_contract_version": record.contract_version,
        }
        for binding in reversed(bindings)
    ]

    projection_write._validate_snapshot_contents(
        rows,
        record.snapshot_id,
        "contact",
        "101",
        scope.source_key,
        scope.source_instance_id,
        scope.control_instance_id,
        {str(row["observation_id"]) for row in rows},
    )

    assert record.snapshot_digest == snapshot.digest
    assert (
        record.snapshot_id
        == CrmCompanyMembershipSnapshotRecord(
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
            2,
        ).snapshot_id
    )
