"""Structural fresh/runtime parity checks for standalone CRM Lane A schema."""

from __future__ import annotations

import re
from pathlib import Path

from src.graph.queries.standalone_crm_lane_a_contracts import (
    CREATE_STANDALONE_CRM_LANE_A_CONSTRAINTS,
)
from src.graph.schema_init import BASE_LIFECYCLE_CONSTRAINTS, _split_statements

_ROOT = Path(__file__).resolve().parents[3]


def _normalize(statement: str) -> str:
    return re.sub(r"\s+", " ", statement).strip().removesuffix(";")


def test_lane_a_ddl_is_an_exact_subset_of_runtime_and_fresh_schema() -> None:
    expected = {_normalize(statement) for statement in CREATE_STANDALONE_CRM_LANE_A_CONSTRAINTS}
    runtime = {_normalize(statement) for statement in BASE_LIFECYCLE_CONSTRAINTS}
    fresh_script = (_ROOT / "infra" / "neo4j" / "init.cypher").read_text(encoding="utf-8")
    fresh = {_normalize(statement) for statement in _split_statements(fresh_script)}

    assert expected <= runtime
    assert expected <= fresh
    assert len(expected) == 30
    assert sum("CREATE CONSTRAINT" in statement for statement in expected) == 24
    assert sum("CREATE INDEX" in statement for statement in expected) == 6
    assert {statement for statement in runtime if "crm_tenant_" in statement} == {
        statement for statement in expected if "crm_tenant_" in statement
    }
    assert {statement for statement in fresh if "crm_tenant_" in statement} == {
        statement for statement in expected if "crm_tenant_" in statement
    }
    assert {statement for statement in runtime if "crm_company_" in statement} == {
        statement for statement in expected if "crm_company_" in statement
    }
    assert {statement for statement in fresh if "crm_company_" in statement} == {
        statement for statement in expected if "crm_company_" in statement
    }
