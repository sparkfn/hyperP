from __future__ import annotations

from src.graph import queries


def test_machine_unit_queries_are_exported() -> None:
    assert "MachineUnit" in queries.UPSERT_MACHINE_UNIT
    assert "INVOLVES_UNIT" in queries.LINK_ORDER_INVOLVES_UNIT
    assert "BOUGHT_UNIT" in queries.LINK_PERSON_BOUGHT_UNIT
    assert "OWNS_UNIT" in queries.LINK_PERSON_OWNS_UNIT
    assert "conflict_flag" in queries.FLAG_MACHINE_UNIT_OWNER_CONFLICTS


def test_machine_unit_upsert_handles_lta_or_serial_matches() -> None:
    query = queries.UPSERT_MACHINE_UNIT

    assert "normalized_lta_tag" in query
    assert "normalized_serial_number" in query
    assert "machine_unit_identifier_conflict" in query
