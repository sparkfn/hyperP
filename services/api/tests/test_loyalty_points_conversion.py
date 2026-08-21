from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from src.graph.loyalty_points import convert_integral

_FIXTURE = (
    Path(__file__).resolve().parents[3] / "testdata" / "issue-241-loyalty-integral-vectors.json"
)


def _value(kind: str, raw: object) -> object:
    if kind == "none":
        return None
    if kind == "bool":
        return bool(raw)
    if kind == "int":
        return int(str(raw))
    if kind == "decimal":
        return Decimal(str(raw))
    if kind == "float":
        return float(str(raw))
    if kind == "str":
        return str(raw)
    if kind == "list":
        return []
    if kind == "object":
        return {}
    raise AssertionError(f"unexpected fixture kind: {kind}")


@pytest.mark.parametrize(
    "vector", json.loads(_FIXTURE.read_text(encoding="utf-8")), ids=lambda v: v["name"]
)
def test_convert_integral_vectors(vector: dict[str, object]) -> None:
    result = convert_integral(_value(str(vector["kind"]), vector["input"]))
    expected = vector["value"]
    assert result.value == (int(str(expected)) if expected is not None else None)
    assert result.error_code == vector["error"]
