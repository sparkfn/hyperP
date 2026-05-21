from __future__ import annotations

from pathlib import Path

import pytest
from src.exclusion_config import load_exclusion_file


def test_load_exclusion_file_returns_arrays(tmp_path: Path) -> None:
    path = tmp_path / "exclusions.json"
    path.write_text(
        "{"
        '"phones":["+6512345678"],'
        '"emails":["ops@example.com"],'
        '"email_domains":["ada.asia"],'
        '"names":["Ada Ops"],'
        '"source_ids":["staff-1"],'
        '"machine_unit_identifiers":['
        '{"machine_product":"Servicing Labour","serial_number":"1186#1506"}'
        "]"
        "}",
        encoding="utf-8",
    )

    loaded = load_exclusion_file(str(path))

    assert loaded.phones == ["+6512345678"]
    assert loaded.emails == ["ops@example.com"]
    assert loaded.email_domains == ["ada.asia"]
    assert loaded.names == ["Ada Ops"]
    assert loaded.source_ids == ["staff-1"]
    assert loaded.machine_unit_identifiers == [
        {"machine_product": "Servicing Labour", "serial_number": "1186#1506"}
    ]


def test_load_exclusion_file_blank_path_returns_empty() -> None:
    loaded = load_exclusion_file("")

    assert loaded.phones == []
    assert loaded.emails == []
    assert loaded.email_domains == []
    assert loaded.names == []
    assert loaded.source_ids == []
    assert loaded.machine_unit_identifiers == []


def test_load_exclusion_file_missing_configured_path_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_exclusion_file(str(tmp_path / "missing.json"))


def test_load_exclusion_file_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{bad", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid ingestion exclusions JSON"):
        load_exclusion_file(str(path))
