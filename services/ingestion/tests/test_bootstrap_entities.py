"""Entity/source-system bootstrap — query + seed data (seed data in Task 2)."""

from __future__ import annotations

from src.graph import queries
from src.graph.bootstrap import _ENTITIES, _SOURCE_SYSTEMS, SOURCE_KEY_TO_ENTITY


def test_upsert_source_system_query_has_no_entity_match() -> None:
    """The entity-less upsert must not reference Entity nodes or OPERATED_BY."""
    q = queries.UPSERT_SOURCE_SYSTEM
    assert "MATCH (e:Entity" not in q
    assert "OPERATED_BY" not in q
    assert "MERGE (ss:SourceSystem {source_key: $source_key})" in q
    assert "RETURN ss.source_system_id AS source_system_id" in q


def test_sggov_entity_is_not_seeded() -> None:
    entity_keys = {entity["entity_key"] for entity in _ENTITIES}
    assert "sggov" not in entity_keys


def test_sg_source_systems_have_no_entity_key() -> None:
    by_key = {source["source_key"]: source for source in _SOURCE_SYSTEMS}
    assert by_key["sgbankruptcy"]["entity_key"] is None
    assert by_key["sgrentalflats"]["entity_key"] is None


def test_non_sg_source_systems_still_have_an_entity_key() -> None:
    by_key = {source["source_key"]: source for source in _SOURCE_SYSTEMS}
    assert by_key["fundbox_consumer_backend"]["entity_key"] == "fundbox"
    assert by_key["onediver"]["entity_key"] == "onediver"


def test_source_key_to_entity_omits_entity_less_sg_sources() -> None:
    assert "sgbankruptcy" not in SOURCE_KEY_TO_ENTITY
    assert "sgrentalflats" not in SOURCE_KEY_TO_ENTITY
    assert SOURCE_KEY_TO_ENTITY["fundbox_consumer_backend"] == "fundbox"
