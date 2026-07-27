from __future__ import annotations

from src.models import RawAddress
from src.normalizers.address import normalize_raw_addresses


def test_normalize_raw_addresses_uses_deterministic_parser() -> None:
    results = normalize_raw_addresses(
        [
            RawAddress(raw="#05-123 10 Orchard Road Singapore 238863"),
            RawAddress(raw="20 Second Street Singapore 654321"),
        ]
    )

    assert len(results) == 2
    first, first_flag = results[0]
    assert first_flag == "valid"
    assert first.unit_number == "05-123"
    assert first.street_name == "orchard road"
    assert first.building_name is None
    assert first.postal_code == "238863"
    second, second_flag = results[1]
    assert second_flag == "valid"
    assert second.street_number == "20"
    assert second.postal_code == "654321"


def test_normalize_raw_addresses_uses_partial_parse_for_nonmatching_input() -> None:
    results = normalize_raw_addresses(
        [RawAddress(raw="Lucky Plaza #05-123, 10 Orchard Road Singapore 238863")]
    )

    assert len(results) == 1
    address, flag = results[0]
    assert flag == "partial_parse"
    assert address.street_number == ""
    assert address.street_name == "lucky plaza #05-123, 10 orchard road singapore 238863"
    assert address.postal_code == "238863"


def test_normalize_raw_addresses_deduplicates_equivalent_inputs() -> None:
    results = normalize_raw_addresses(
        [
            RawAddress(raw="#04-242 163A Rivervale Crescent Singapore 541163"),
            RawAddress(raw="#04-242 163A Rivervale Crescent Singapore 541163"),
        ]
    )

    assert len(results) == 1
    address, flag = results[0]
    assert flag == "valid"
    assert address.street_number == "163a"
    assert address.postal_code == "541163"


def test_normalize_raw_addresses_prefers_structured_fields_over_raw() -> None:
    results = normalize_raw_addresses(
        [
            RawAddress(
                raw="163A RIVERVALE CRESCENT, #04-242, RIVERVALE CRESCENT, "
                "163A 04 242, SINGAPORE, SINGAPORE, 541163, SINGAPORE",
                unit_number="#04-242",
                street_number="163A",
                street_name="RIVERVALE CRESCENT",
                city="SINGAPORE",
                postal_code="541163",
                country_code="SINGAPORE",
            )
        ]
    )

    assert len(results) == 1
    address, flag = results[0]
    assert flag == "partial_parse"
    assert "163a, rivervale crescent" in address.normalized_full
    assert "04 242" not in address.normalized_full
