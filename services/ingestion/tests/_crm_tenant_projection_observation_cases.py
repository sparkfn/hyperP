"""Focused unmapped-observation projection cases for Issue #305."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from _standalone_crm_lane_a_fakes import prepared_mapping_revision, projection_scope
from src.crm_tenant_mapping_identity import mapping_head_id
from src.crm_tenant_mapping_models import CrmTenantMappingExpectedHeadBoundary
from src.crm_tenant_projection_models import (
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionMaterializationCommand,
    CrmTenantProjectionReleaseSummary,
)
from src.crm_tenant_projection_records import _digest as projection_digest
from src.graph import crm_tenant_projection_write as projection_write
from src.graph.queries import crm_tenant_projection_projection as projection_queries

_DIGEST = "sha256:" + "a" * 64


class _ProjectionRowsResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def single(self) -> dict[str, object]:
        return self._rows[0]


class _ProjectionTx:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls: list[str] = []

    def run(self, query: str, **parameters: object) -> _ProjectionRowsResult:
        self.calls.append(query)
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
    observation_id = projection_digest(
        "crm-company-membership-observation-v1",
        ["snapshot-a", "303", None, None, True],
    )
    row: dict[str, object] = {
        "binding_count": 1,
        "observation_id": observation_id,
        "observation_node_id": "node-a",
        "observation_snapshot_id": "snapshot-a",
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


def test_unmapped_observation_is_validated_before_zero_target_decision() -> None:
    tx = _ProjectionTx([_unmapped_observation_row()])

    assert projection_write._project_one_input(
        tx, _projection_release(), "input-a", "contact", "101", "snapshot-a"
    ) == (0, 0)
    assert tx.calls == [projection_queries.READ_INPUT_SUPPORTS, projection_queries.WRITE_DECISION]


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
            tx, _projection_release(), "input-a", "contact", "101", "snapshot-a"
        )

    assert tx.calls == [projection_queries.READ_INPUT_SUPPORTS]
