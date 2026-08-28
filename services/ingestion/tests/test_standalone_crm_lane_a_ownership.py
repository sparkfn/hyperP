"""Compile-facing ownership boundaries for the four future Lane A components."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from _standalone_crm_lane_a_company_membership_surface import commit_company_membership
from _standalone_crm_lane_a_fakes import (
    active_mapping_head,
    active_projection_head,
    company_description_head,
    company_envelope,
    contact_envelope,
    mapping_manifest,
    membership_head,
)
from _standalone_crm_lane_a_mapping_surface import active_mapping_head_id
from _standalone_crm_lane_a_projection_surface import active_projection_head_id
from _standalone_crm_lane_a_source_fact_surface import commit_source_fact
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint

_INGESTION_SOURCE = Path(__file__).resolve().parents[1] / "src"
_INGESTION_ROOT = _INGESTION_SOURCE.parent
_TESTS = Path(__file__).resolve().parent

_COMPONENT_IMPORTS = {
    "_standalone_crm_lane_a_source_fact_surface.py": {
        "src.standalone_crm_census_lifecycle",
        "src.standalone_crm_child_contracts",
        "src.standalone_crm_unit_repository",
    },
    "_standalone_crm_lane_a_company_membership_surface.py": {
        "src.crm_company_contracts",
        "src.standalone_crm_census_lifecycle",
        "src.standalone_crm_child_contracts",
        "src.standalone_crm_unit_repository",
    },
    "_standalone_crm_lane_a_mapping_surface.py": {"src.crm_tenant_mapping_contracts"},
    "_standalone_crm_lane_a_projection_surface.py": {
        "src.crm_company_contracts",
        "src.crm_tenant_mapping_contracts",
        "src.crm_tenant_projection_contracts",
    },
}


def test_disjoint_component_contract_surfaces_operate_without_future_implementations() -> None:
    source_expected = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 5, 5, 0, 5, 0, 1, 2)
    source_proposed = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 6, 5, 1, 6, 0, 1, 2)
    company_expected = StandaloneCrmCheckpoint(
        "census-a", "company", 10, None, 5, None, None, 5, 0, 1, 2
    )
    company_proposed = StandaloneCrmCheckpoint(
        "census-a", "company", 10, None, 6, None, None, 6, 0, 1, 2
    )
    mapping_head = active_mapping_head()
    projection_head = active_projection_head()

    assert commit_source_fact(contact_envelope(), source_expected, source_proposed) is True
    assert (
        commit_company_membership(
            company_envelope(),
            company_expected,
            company_proposed,
            company_description_head(),
            membership_head(),
        )
        is True
    )
    assert active_mapping_head_id(mapping_head, mapping_manifest()) == "mapping-head-1"
    assert (
        active_projection_head_id(membership_head(), mapping_head, projection_head)
        == "projection-head-1"
    )


def test_component_surfaces_have_disjoint_direct_allowed_import_sets() -> None:
    for file_name, allowed_imports in _COMPONENT_IMPORTS.items():
        imports = _imports_for(_TESTS / file_name)

        assert imports == allowed_imports
        assert _contains_no_runtime_wiring(imports)


def test_projection_public_and_release_import_surfaces_are_order_independent() -> None:
    for module_name in (
        "src.crm_tenant_projection_contracts",
        "src.crm_tenant_projection_release_contracts",
    ):
        _assert_clean_import(module_name)


def test_contract_modules_have_only_their_intended_import_boundaries() -> None:
    source_imports = _imports_for(_INGESTION_SOURCE / "standalone_crm_child_contracts.py")
    unit_imports = _imports_for(_INGESTION_SOURCE / "standalone_crm_unit_repository.py")
    company_primitive_imports = _imports_for(
        _INGESTION_SOURCE / "crm_company_contract_primitives.py"
    )
    company_imports = _imports_for(_INGESTION_SOURCE / "crm_company_contracts.py")
    mapping_imports = _imports_for(_INGESTION_SOURCE / "crm_tenant_mapping_contracts.py")
    projection_imports = _imports_for(_INGESTION_SOURCE / "crm_tenant_projection_contracts.py")
    projection_record_imports = _imports_for(_INGESTION_SOURCE / "crm_tenant_projection_records.py")
    projection_release_imports = _imports_for(
        _INGESTION_SOURCE / "crm_tenant_projection_release_contracts.py"
    )

    assert _contains_no_runtime_wiring(
        source_imports
        | unit_imports
        | company_primitive_imports
        | projection_record_imports
        | projection_release_imports
    )
    assert "src.crm_tenant_mapping_contracts" not in source_imports
    assert "src.crm_tenant_projection_contracts" not in source_imports
    assert "src.crm_tenant_mapping_contracts" not in company_imports
    assert "src.crm_tenant_projection_contracts" not in company_imports
    assert "src.crm_tenant_mapping_contracts" not in company_primitive_imports
    assert "src.crm_tenant_projection_contracts" not in company_primitive_imports
    assert "src.crm_tenant_projection_records" not in company_primitive_imports
    assert "src.crm_tenant_projection_release_contracts" not in company_primitive_imports
    assert "src.crm_company_contracts" not in mapping_imports
    assert "src.crm_tenant_projection_contracts" not in mapping_imports
    assert "src.standalone_crm_child_contracts" not in projection_imports
    assert "src.standalone_crm_unit_repository" not in projection_imports
    assert "src.crm_tenant_projection_contracts" not in projection_record_imports
    assert "src.crm_tenant_projection_contracts" not in projection_release_imports


def _imports_for(path: Path) -> set[str]:
    module = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for statement in module.body:
        if isinstance(statement, ast.ImportFrom) and statement.module is not None:
            if statement.module != "__future__":
                imports.add(statement.module)
        if isinstance(statement, ast.Import):
            imports.update(alias.name for alias in statement.names)
    return {
        imported
        for imported in imports
        if not imported.startswith(("ast", "dataclasses", "pathlib"))
    }


def _contains_no_runtime_wiring(imports: set[str]) -> bool:
    forbidden = (
        "celery",
        "tasks",
        "connectors",
        "graph",
        "main",
        "writer",
        "repository.neo4j",
    )
    return not any(fragment in imported for imported in imports for fragment in forbidden)


def _assert_clean_import(module_name: str) -> None:
    environment = dict(os.environ)
    prior_path = environment.get("PYTHONPATH")
    root = str(_INGESTION_ROOT)
    environment["PYTHONPATH"] = root if not prior_path else root + os.pathsep + prior_path
    result = subprocess.run(
        [sys.executable, "-c", f"import importlib; importlib.import_module({module_name!r})"],
        capture_output=True,
        check=False,
        cwd=_INGESTION_ROOT,
        env=environment,
        text=True,
    )
    assert result.returncode == 0, result.stderr
