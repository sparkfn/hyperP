"""Parity checks for API and ingestion #272 control-constraint inventories."""

from __future__ import annotations

import ast
from pathlib import Path

from src.repositories.neo4j.ingestion_control_schema import (
    _REQUIRED_SPECS,
    _RETIRED_SPECS,
    _ConstraintSpec,
)


def _canonical_spec_inventory(assignment_name: str) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    path = (
        Path(__file__).resolve().parents[2]
        / "ingestion"
        / "src"
        / "graph"
        / "queries"
        / "ingestion_control_instance_migration.py"
    )
    module = ast.parse(path.read_text(encoding="utf-8"))
    for statement in module.body:
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == assignment_name
        ):
            return _parse_spec_tuple(statement.value)
    raise AssertionError(f"canonical migration inventory {assignment_name} is missing")


def _parse_spec_tuple(node: ast.expr | None) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    if not isinstance(node, ast.Tuple):
        raise AssertionError("canonical migration inventory must be a tuple")
    return tuple(_parse_spec(node_element) for node_element in node.elts)


def _parse_spec(node: ast.expr) -> tuple[str, str, tuple[str, ...]]:
    if not isinstance(node, ast.Tuple) or len(node.elts) != 3:
        raise AssertionError("canonical migration constraint spec is malformed")
    return (
        _string(node.elts[0]),
        _string(node.elts[1]),
        _string_tuple(node.elts[2]),
    )


def _string(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise AssertionError("canonical migration constraint value must be a string")


def _string_tuple(node: ast.expr) -> tuple[str, ...]:
    if not isinstance(node, ast.Tuple):
        raise AssertionError("canonical migration constraint properties must be a tuple")
    return tuple(_string(property_name) for property_name in node.elts)


def _api_specs(
    specs: tuple[_ConstraintSpec, ...],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    return tuple((spec.name, spec.label, spec.properties) for spec in specs)


def test_api_control_constraint_inventories_match_canonical_ingestion_migration() -> None:
    assert _api_specs(_REQUIRED_SPECS) == _canonical_spec_inventory("NEW_CONSTRAINT_SPECS")
    assert _api_specs(_RETIRED_SPECS) == _canonical_spec_inventory("LEGACY_CONSTRAINT_SPECS")
