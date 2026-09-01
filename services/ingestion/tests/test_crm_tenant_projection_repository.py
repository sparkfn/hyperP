"""Focused repository-boundary checks for immutable CRM tenant projection persistence."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from _crm_tenant_projection_census_cases import (
    test_source_census_admission_rejects_terminal_company_and_control_drift,
    test_source_census_admission_requires_contact_lead_and_exact_completed_bounds,
)
from _crm_tenant_projection_mapping_guard_cases import (
    test_mapping_proof_guard_rejects_bad_target_entity_topology,
    test_mapping_proof_scan_classifies_malformed_canonical_values_as_integrity,
    test_mapping_target_proof_scan_accepts_canonical_target_identity,
)
from _crm_tenant_projection_neo4j_helpers import (
    _command as _neo4j_command,
)
from _crm_tenant_projection_neo4j_helpers import (
    _mapping_active_head_drift_parameters,
)
from _crm_tenant_projection_neo4j_seed import (
    _mapping_manifest,
    _mapping_properties,
    _mapping_revision_id,
    _snapshot_record,
)
from _crm_tenant_projection_neo4j_seed import (
    _scope as neo4j_projection_scope,
)
from _crm_tenant_projection_observation_cases import (
    test_projection_support_fan_out_limit_fails_before_writes,
    test_projection_support_preflight_uses_actual_rows_not_global_mapping_targets,
    test_snapshot_contents_uses_canonical_multibinding_order_for_digest_and_identity,
    test_unmapped_observation_is_validated_before_zero_target_decision,
    test_unmapped_observation_topology_fails_closed_before_decision,
)
from _crm_tenant_projection_query_contract_cases import (
    test_capture_cursor_filter_is_applied_after_optional_checkpoint_matching,
    test_completion_boundary_retains_census_for_checkpoint_uniqueness_checks,
    test_completion_query_authorizes_ledger_integrity_atomically,
    test_failure_code_is_rejected_before_any_graph_write,
    test_projection_queries_do_not_write_active_heads_or_source_membership_state,
    test_projection_support_read_is_hard_limited_after_deterministic_ordering,
)
from _crm_tenant_projection_terminal_snapshot_cases import (
    test_terminal_snapshot_bounded_reader_uses_one_transaction_per_page,
    test_terminal_snapshot_guard_rejects_hidden_observation_rows,
    test_terminal_snapshot_validation_accepts_empty_snapshot_null_row,
    test_terminal_snapshot_validation_rejects_malformed_observation_topology,
    test_terminal_snapshot_validation_uses_exclusive_200_row_pages,
)
from _standalone_crm_lane_a_fakes import prepared_mapping_revision, projection_scope
from src.connectors.bitrix_openlines.models import CrmCompanyBindingPayload
from src.crm_tenant_mapping_contracts import CrmTenantMappingCompanyEntry, CrmTenantMappingTarget
from src.crm_tenant_mapping_identity import mapping_head_id, mapping_revision_id
from src.crm_tenant_mapping_models import CrmTenantMappingExpectedHeadBoundary
from src.crm_tenant_projection_identity import (
    empty_capture_boundary_digest,
    extend_capture_boundary_digest,
    projection_release_id,
)
from src.crm_tenant_projection_models import (
    CrmTenantProjectionIntegrityError,
    CrmTenantProjectionMaterializationCommand,
    CrmTenantProjectionReleaseSummary,
)
from src.graph import crm_tenant_projection as projection_graph
from src.graph import crm_tenant_projection_mapping_guard as mapping_guard
from src.graph.crm_tenant_projection_values import _materialized_fingerprint_from_values
from src.graph.queries import crm_tenant_projection as queries
from src.graph.queries import crm_tenant_projection_mapping_guard as mapping_guard_queries

_DIGEST = "sha256:" + "a" * 64

__all__ = (
    "test_source_census_admission_rejects_terminal_company_and_control_drift",
    "test_source_census_admission_requires_contact_lead_and_exact_completed_bounds",
    "test_mapping_proof_guard_rejects_bad_target_entity_topology",
    "test_mapping_proof_scan_classifies_malformed_canonical_values_as_integrity",
    "test_mapping_target_proof_scan_accepts_canonical_target_identity",
    "test_capture_cursor_filter_is_applied_after_optional_checkpoint_matching",
    "test_completion_query_authorizes_ledger_integrity_atomically",
    "test_completion_boundary_retains_census_for_checkpoint_uniqueness_checks",
    "test_failure_code_is_rejected_before_any_graph_write",
    "test_projection_queries_do_not_write_active_heads_or_source_membership_state",
    "test_projection_support_read_is_hard_limited_after_deterministic_ordering",
    "test_projection_support_fan_out_limit_fails_before_writes",
    "test_projection_support_preflight_uses_actual_rows_not_global_mapping_targets",
    "test_snapshot_contents_uses_canonical_multibinding_order_for_digest_and_identity",
    "test_terminal_snapshot_bounded_reader_uses_one_transaction_per_page",
    "test_terminal_snapshot_validation_accepts_empty_snapshot_null_row",
    "test_terminal_snapshot_guard_rejects_hidden_observation_rows",
    "test_terminal_snapshot_validation_rejects_malformed_observation_topology",
    "test_terminal_snapshot_validation_uses_exclusive_200_row_pages",
    "test_unmapped_observation_is_validated_before_zero_target_decision",
    "test_unmapped_observation_topology_fails_closed_before_decision",
)


class _Result:
    def __init__(self, record: dict[str, object]) -> None:
        self._record = record

    def single(self) -> dict[str, object]:
        return self._record


class _AllocationTx:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, query: str, **_parameters: object) -> _Result:
        self.calls.append(query)
        assert query == queries.LOCK_SCOPE
        return _Result({"next_release_number": 1})


class _AllocationClient:
    def __init__(self, tx: _AllocationTx) -> None:
        self._tx = tx

    def execute_write(self, work: object) -> object:
        assert callable(work)
        return work(self._tx)


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


def test_mapping_active_head_drift_setup_uses_command_property_without_neo4j() -> None:
    parameters = _mapping_active_head_drift_parameters()

    assert parameters.head_id == mapping_head_id(neo4j_projection_scope().mapping_scope)
    assert parameters.active_revision_id == "other"
    assert parameters.active_revision_number == 1


def test_neo4j_mapping_fixture_uses_canonical_revision_and_distinct_targets() -> None:
    manifest = _mapping_manifest(
        (
            CrmTenantMappingCompanyEntry("303", (CrmTenantMappingTarget("issue-305-entity"),)),
            CrmTenantMappingCompanyEntry("404", (CrmTenantMappingTarget("issue-305-entity"),)),
        )
    )

    properties, entries, targets = _mapping_properties(manifest)

    assert _mapping_revision_id() == mapping_revision_id(
        neo4j_projection_scope().mapping_scope,
        1,
    )
    assert properties["revision_id"] == _mapping_revision_id()
    assert {entry["entry_id"] for entry in entries} == {target["entry_id"] for target in targets}
    assert len({entry["entry_id"] for entry in entries}) == 2
    assert len({target["target_id"] for target in targets}) == 2


def test_neo4j_fixture_command_and_multicompany_snapshot_are_canonical() -> None:
    manifest = _mapping_manifest(
        (CrmTenantMappingCompanyEntry("303", (CrmTenantMappingTarget("issue-305-entity"),)),)
    )
    bindings = (
        CrmCompanyBindingPayload("404", 5, None, False),
        CrmCompanyBindingPayload("303", None, None, True),
    )
    record = _snapshot_record("contact", "101", "issue-305-contact-source", bindings)

    assert _neo4j_command(manifest=manifest).mapping_manifest_digest == manifest.digest
    assert record.binding_count == 2
    assert tuple(binding.company_id for binding in record.membership_snapshot.bindings) == (
        "303",
        "404",
    )
    assert record.snapshot_digest == record.membership_snapshot.digest
    assert (
        record.snapshot_id
        == _snapshot_record("contact", "101", "issue-305-contact-source", bindings).snapshot_id
    )


def test_allocation_rechecks_request_after_scope_lock_before_any_boundary_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = replace(_projection_release(), state="failed", failure_code="boundary_conflict")
    found = iter((None, terminal))
    tx = _AllocationTx()
    monkeypatch.setattr(
        projection_graph, "assert_standalone_crm_lane_a_ready", lambda _client: None
    )
    monkeypatch.setattr(projection_graph, "_find_by_request", lambda _tx, _command: next(found))
    monkeypatch.setattr(
        projection_graph,
        "_validate_source_census",
        lambda _tx, _command: pytest.fail("source census read before locked replay check"),
    )
    repository = projection_graph.Neo4jCrmTenantProjectionRepository(_AllocationClient(tx))

    assert repository.allocate_or_replay(_command()) == terminal
    assert tx.calls == [queries.LOCK_SCOPE]


def test_release_summary_rejects_deterministic_identity_corruption() -> None:
    scope = projection_scope()
    record = {
        "release": {
            "source_key": scope.source_key,
            "source_instance_id": scope.source_instance_id,
            "control_instance_id": scope.control_instance_id,
            "release_id": "not-the-deterministic-id",
            "release_number": 1,
            "request_id": "projection-request",
            "request_fingerprint": _DIGEST,
            "release_fingerprint": _DIGEST,
            "source_census_id": "census-a",
            "source_census_fingerprint": _DIGEST,
            "contact_unit_state": "completed",
            "contact_unit_generation": 1,
            "contact_checkpoint_present": True,
            "contact_checkpoint_generation": 1,
            "contact_processed_rows": 0,
            "contact_skipped_rows": 0,
            "contact_expected_input_count": 0,
            "contact_frozen_upper_id": 0,
            "lead_unit_state": "no_work",
            "lead_unit_generation": 1,
            "lead_checkpoint_present": False,
            "lead_checkpoint_generation": None,
            "lead_processed_rows": 0,
            "lead_skipped_rows": 0,
            "lead_expected_input_count": 0,
            "lead_frozen_upper_id": 0,
            "mapping_revision_id": prepared_mapping_revision().revision_id,
            "mapping_revision_number": 1,
            "mapping_entry_count": 0,
            "mapping_target_count": 0,
            "mapping_topology_fingerprint": empty_capture_boundary_digest(),
            "mapping_manifest_digest": prepared_mapping_revision().manifest_digest,
            "projection_head_id": _DIGEST,
            "expected_mapping_head_id": _DIGEST,
            "expected_mapping_head_digest": "absent",
            "expected_mapping_head_present": False,
            "expected_mapping_active_revision_id": None,
            "expected_mapping_active_revision_number": None,
            "expected_prior_head_present": False,
            "expected_prior_head_id": None,
            "expected_prior_release_id": None,
            "expected_prior_release_number": None,
            "expected_prior_release_fingerprint": None,
            "contract_version": "crm-tenant-projection-v1",
            "state": "building",
            "phase": "capture",
            "capture_cursor_kind": None,
            "capture_cursor_subject_id": None,
            "projection_cursor_kind": None,
            "projection_cursor_subject_id": None,
            "input_count": 0,
            "decision_count": 0,
            "association_count": 0,
            "support_count": 0,
            "capture_boundary_digest": empty_capture_boundary_digest(),
            "failure_code": None,
        }
    }

    with pytest.raises(CrmTenantProjectionIntegrityError, match="deterministic identity"):
        projection_graph._summary_from_record(record)

    release = record["release"]
    assert isinstance(release, dict)
    release["release_id"] = projection_release_id(scope, 1)
    release["projection_head_id"] = _command().projection_head_id
    release["expected_mapping_head_id"] = mapping_head_id(scope.mapping_scope)
    release["release_fingerprint"] = _materialized_fingerprint_from_values(release)
    assert projection_graph._summary_from_record(record).release_number == 1
    original_fingerprint = release["release_fingerprint"]
    assert isinstance(original_fingerprint, str)
    for key, value in (
        ("mapping_entry_count", 1),
        ("mapping_target_count", 1),
        ("mapping_topology_fingerprint", mapping_guard._empty_mapping_topology_fingerprint()),
    ):
        changed = dict(release)
        changed[key] = value
        assert _materialized_fingerprint_from_values(changed) != original_fingerprint
    release["contact_processed_rows"] = 1
    with pytest.raises(CrmTenantProjectionIntegrityError, match="release fingerprint"):
        projection_graph._summary_from_record(record)


def test_capture_digest_is_stable_for_zero_and_ordered_input_boundaries() -> None:
    empty = empty_capture_boundary_digest()
    contact_then_lead = extend_capture_boundary_digest(
        extend_capture_boundary_digest(empty, "contact-input", _DIGEST),
        "lead-input",
        "sha256:" + "b" * 64,
    )
    lead_then_contact = extend_capture_boundary_digest(
        extend_capture_boundary_digest(empty, "lead-input", "sha256:" + "b" * 64),
        "contact-input",
        _DIGEST,
    )

    assert empty.startswith("sha256:")
    assert contact_then_lead != lead_then_contact


def test_mapping_proof_guard_uses_complete_parameters_and_bounded_empty_pages() -> None:
    class _Rows:
        def __init__(self, rows: list[dict[str, object]]) -> None:
            self._rows = rows

        def __iter__(self) -> object:
            return iter(self._rows)

        def single(self) -> dict[str, object] | None:
            return self._rows[0] if self._rows else None

    class _Tx:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def run(self, query: str, **parameters: object) -> _Rows:
            self.calls.append((query, parameters))
            if query in {
                mapping_guard_queries.READ_MAPPING_ENTRY_PROOF_PAGE,
                mapping_guard_queries.READ_MAPPING_TARGET_PROOF_PAGE,
            }:
                assert parameters["page_limit"] == 200
                return _Rows([])
            return _Rows(
                [
                    {
                        "stored_entry_count": 0,
                        "stored_target_count": 0,
                        "stored_topology_fingerprint": (
                            mapping_guard._empty_mapping_topology_fingerprint()
                        ),
                        "bad_revision_links": 0,
                        "bad_entry_links": 0,
                        "bad_target_links": 0,
                        "orphan_entries": 0,
                        "orphan_targets": 0,
                        "bad_entry_owners": 0,
                        "bad_target_owners": 0,
                        "bad_target_entities": 0,
                        "entry_count": 0,
                        "target_count": 0,
                    }
                ]
            )

    tx = _Tx()
    mapping_guard._validate_mapping_topology_fingerprint(tx, "release", _DIGEST)
    guard_parameters = next(
        parameters
        for query, parameters in tx.calls
        if query == mapping_guard_queries.VALIDATE_MAPPING_PROOF_GUARD
    )
    assert guard_parameters["mapping_entry_link"] == "HAS_MAPPING_ENTRY"
    assert guard_parameters["mapping_target_link"] == "HAS_MAPPING_TARGET"
    assert guard_parameters["targets_entity_link"] == "TARGETS_ENTITY"


def test_projection_graph_modules_do_not_import_connector_runtime() -> None:
    observations = __import__("src.graph.crm_tenant_projection_observations", fromlist=["__name__"])
    assert observations.__file__ is not None
    source = Path(observations.__file__).read_text(encoding="utf-8")
    assert "src.connectors." not in source
