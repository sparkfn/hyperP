from __future__ import annotations

from pathlib import Path

import pytest
from src.connectors.sggov.rental_flats import SGGovernmentRentalFlatsConnector
from src.main import get_connector


def _line(values: list[str]) -> str:
    return "\t".join(values) + "\n"


def _write_dump(path: Path) -> None:
    path.write_text(
        "COPY public.flats "
        "(id, town_id, block_no, street_name, postal_code, flat_type, "
        "first_seen_at, last_seen_at, is_active) FROM stdin;\n"
        + _line(
            [
                "33",
                "9",
                "165A",
                "Teck Whye Cres",
                "681165",
                "1-room & 2-room",
                "2026-05-08 08:24:42.13248+00",
                "2026-05-08 09:47:25.177976+00",
                "t",
            ]
        )
        + "\\.\n"
        + "COPY public.towns (id, name, map_id, map_zone) FROM stdin;\n"
        + _line(["9", "Choa Chu Kang Town", "choa_chu_kang", "CCK"])
        + "\\.\n",
        encoding="utf-8",
    )


def test_rental_flats_connector_yields_address_envelope(tmp_path: Path) -> None:
    dump = tmp_path / "rental.sql"
    _write_dump(dump)

    records = list(SGGovernmentRentalFlatsConnector(dump_path=dump).fetch_records())

    assert len(records) == 1
    record = records[0]
    assert record["source_record_id"] == "rental_flat:33"
    assert record["record_type"] == "rental_flat"
    assert record["observed_at"] == "2026-05-08T09:47:25.177976+00:00"
    assert record["identifiers"] == []
    assert record["attributes"] == {
        "country_code": "SG",
        "postal_code": "681165",
        "block_no": "165A",
        "street_name": "Teck Whye Cres",
        "flat_type": "1-room & 2-room",
        "town_id": "9",
        "town_name": "Choa Chu Kang Town",
        "town_map_id": "choa_chu_kang",
        "town_map_zone": "CCK",
        "is_active": True,
    }
    assert isinstance(record["raw_payload"], dict)
    raw_payload = record["raw_payload"]
    assert isinstance(raw_payload["flat"], dict)
    assert isinstance(raw_payload["town"], dict)
    assert raw_payload["flat"]["postal_code"] == "681165"
    assert raw_payload["town"]["name"] == "Choa Chu Kang Town"
    assert isinstance(record["record_hash"], str)
    assert record["record_hash"].startswith("sha256:")


def test_rental_flats_connector_uses_dumps_root_env_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dump = tmp_path / "sgrentalflats_2026-05-11.sql"
    _write_dump(dump)
    monkeypatch.setenv("DUMPS_ROOT", str(tmp_path))

    records = list(SGGovernmentRentalFlatsConnector().fetch_records())

    assert len(records) == 1
    assert records[0]["source_record_id"] == "rental_flat:33"


def test_rental_flats_connector_is_registered() -> None:
    assert isinstance(get_connector("sgrentalflats"), SGGovernmentRentalFlatsConnector)
