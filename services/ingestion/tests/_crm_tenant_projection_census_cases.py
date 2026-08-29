"""Focused source-census admission cases for Issue #305 projection releases."""

from __future__ import annotations

from typing import Literal

import pytest
from _standalone_crm_lane_a_fakes import prepared_mapping_revision, projection_scope
from src.crm_tenant_mapping_identity import mapping_head_id
from src.crm_tenant_mapping_models import CrmTenantMappingExpectedHeadBoundary
from src.crm_tenant_projection_models import (
    CrmTenantProjectionConflictError,
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionMaterializationCommand,
)
from src.graph import crm_tenant_projection as projection_graph
from src.graph.queries import crm_tenant_projection as queries
from src.standalone_crm_census_requests import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
    canonical_request_payload,
)

_DIGEST = "sha256:" + "a" * 64


class _Result:
    def __init__(self, record: dict[str, object]) -> None:
        self._record = record

    def single(self) -> dict[str, object]:
        return self._record


class _CensusTx:
    def __init__(self, record: dict[str, object]) -> None:
        self._record = record
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **parameters: object) -> _Result:
        self.calls.append((query, parameters))
        assert query == queries.READ_CENSUS
        return _Result(self._record)


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


def _request_json(selected: tuple[Literal["contact", "lead", "company"], ...]) -> str:
    return canonical_request_payload(
        SourceSyncCensusRequest(
            "bitrix_chat",
            "portal-a",
            "control-a",
            "occurrence-a",
            selected,
            StandaloneCrmBudget(2, 10, 60, 2, 10, 1, "2026-08-30T00:00:00Z"),
            "policy-a",
            "association-a",
            "configuration-a",
            SourceSyncAuthority(
                "mapping-head", "mapping-digest", "projection-head", "projection-digest"
            ),
        )
    )


def _census_record(
    selected: tuple[Literal["contact", "lead", "company"], ...] = ("contact", "lead"),
) -> dict[str, object]:
    units: list[dict[str, object]] = [
        {
            "stream_kind": "contact",
            "state": "completed",
            "generation": 1,
            "frozen_upper_id": 12,
            "checkpoints": [
                {
                    "generation": 1,
                    "frozen_upper_id": 12,
                    "last_committed_id": 12,
                    "processed_rows": 1,
                    "skipped_rows": 0,
                }
            ],
        },
        {
            "stream_kind": "lead",
            "state": "no_work",
            "generation": 1,
            "frozen_upper_id": 0,
            "checkpoints": [],
        },
    ]
    if "company" in selected:
        units.append(
            {
                "stream_kind": "company",
                "state": "no_work",
                "generation": 1,
                "frozen_upper_id": 0,
                "checkpoints": [],
            }
        )
    completed_units = sum(unit["state"] == "completed" for unit in units)
    return {
        "census": {
            "fingerprint": _DIGEST,
            "source_key": "bitrix_chat",
            "source_instance_id": "portal-a",
            "control_instance_id": "control-a",
            "census_kind": "source_sync",
            "status": "completed",
            "request_json": _request_json(selected),
            "expected_units": len(selected),
            "completed_units": completed_units,
            "failed_units": 0,
            "cancelled_units": 0,
            "no_work_units": len(selected) - completed_units,
            "processed_rows": 1,
            "skipped_rows": 0,
        },
        "units": units,
        "publications": [],
        "fences": [],
        "active_scope_count": 0,
    }


def test_source_census_admission_requires_contact_lead_and_exact_completed_bounds() -> None:
    tx = _CensusTx(_census_record())

    boundary = projection_graph._validate_source_census(tx, _command())

    assert boundary.contact.frozen_upper_id == 12
    assert boundary.lead.frozen_upper_id == 0
    assert tx.calls == [(queries.READ_CENSUS, {"census_id": "census-a"})]

    missing_lead = _census_record(("contact",))
    with pytest.raises(CrmTenantProjectionConflictError, match="select contact and lead"):
        projection_graph._validate_source_census(_CensusTx(missing_lead), _command())

    incomplete = _census_record()
    units = incomplete["units"]
    assert isinstance(units, list)
    lead = units[1]
    assert isinstance(lead, dict)
    lead["state"] = "running"
    with pytest.raises(CrmTenantProjectionConflictError, match="incomplete"):
        projection_graph._validate_source_census(_CensusTx(incomplete), _command())

    generation_drift = _census_record()
    units = generation_drift["units"]
    assert isinstance(units, list)
    lead = units[1]
    assert isinstance(lead, dict)
    lead["generation"] = 0
    with pytest.raises(CrmTenantProjectionIntegrityError, match="generation"):
        projection_graph._validate_source_census(_CensusTx(generation_drift), _command())


def test_source_census_admission_rejects_terminal_company_and_control_drift() -> None:
    company_failure = _census_record(("contact", "lead", "company"))
    units = company_failure["units"]
    assert isinstance(units, list)
    company = units[2]
    assert isinstance(company, dict)
    company["state"] = "failed"
    with pytest.raises(CrmTenantProjectionConflictError, match="incomplete"):
        projection_graph._validate_source_census(_CensusTx(company_failure), _command())

    accounting_drift = _census_record()
    census = accounting_drift["census"]
    assert isinstance(census, dict)
    census["processed_rows"] = 2
    with pytest.raises(CrmTenantProjectionConflictError, match="terminal accounting"):
        projection_graph._validate_source_census(_CensusTx(accounting_drift), _command())

    publication_drift = _census_record()
    publications = publication_drift["publications"]
    assert isinstance(publications, list)
    publications.append({"status": "pending"})
    with pytest.raises(CrmTenantProjectionConflictError, match="publication"):
        projection_graph._validate_source_census(_CensusTx(publication_drift), _command())

    fence_drift = _census_record()
    fences = fence_drift["fences"]
    assert isinstance(fences, list)
    fences.append({"status": "active"})
    with pytest.raises(CrmTenantProjectionConflictError, match="fence"):
        projection_graph._validate_source_census(_CensusTx(fence_drift), _command())
