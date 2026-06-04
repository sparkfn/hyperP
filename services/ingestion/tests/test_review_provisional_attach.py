"""Regression tests for provisional REVIEW-band attach behavior.

A REVIEW-band match against an existing candidate must link the source record
for the reviewer to compare, but must NOT wire the incoming identifiers /
addresses / facts onto the candidate person until a human approves the merge.
"""

from __future__ import annotations

from src.models import (
    NormalizedAddress,
    NormalizedAttribute,
    NormalizedIdentifier,
    QualityFlag,
    RecordType,
    SourceRecordEnvelope,
)
from src.pipeline_writes import link_record_to_graph


class _Result:
    def single(self) -> dict[str, object] | None:
        return None


class _RecordingTx:
    """Captures the Cypher constant each ``run`` was invoked with."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def run(self, query: str, **params: object) -> _Result:
        _ = params
        self.queries.append(query)
        return _Result()


def _envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_record_id="rec-1",
        record_type=RecordType.SYSTEM,
        observed_at="2026-01-01T00:00:00Z",
        record_hash="hash-1",
    )


def _identifiers() -> list[NormalizedIdentifier]:
    return [
        NormalizedIdentifier(
            identifier_type="phone",
            normalized_value="+6512345678",
            quality_flag=QualityFlag.VALID,
        )
    ]


def _addresses() -> list[NormalizedAddress]:
    return [
        NormalizedAddress(
            street_number="1",
            street_name="Main St",
            postal_code="123456",
            quality_flag=QualityFlag.VALID,
        )
    ]


def _attributes() -> list[NormalizedAttribute]:
    return [
        NormalizedAttribute(
            attribute_name="full_name",
            attribute_value="Ada Lovelace",
            quality_flag=QualityFlag.VALID,
        )
    ]


def test_provisional_review_links_record_only() -> None:
    tx = _RecordingTx()
    link_record_to_graph(
        tx,  # type: ignore[arg-type]
        envelope=_envelope(),
        identifiers=_identifiers(),
        addresses=_addresses(),
        attributes=_attributes(),
        person_id="person-1",
        source_record_pk="sr-1",
        attach_evidence=False,
    )

    joined = "\n".join(tx.queries)
    # The provenance edge is created so the reviewer can find the record.
    assert "CREATE (sr)-[:LINKED_TO" in joined
    # But no person-bound evidence is wired onto the candidate.
    assert "MERGE (p)-[rel:IDENTIFIED_BY]" not in joined
    assert "MERGE (p)-[rel:LIVES_AT]" not in joined
    assert "CREATE (p)-[:HAS_FACT" not in joined


def test_confirmed_attach_wires_full_evidence() -> None:
    tx = _RecordingTx()
    link_record_to_graph(
        tx,  # type: ignore[arg-type]
        envelope=_envelope(),
        identifiers=_identifiers(),
        addresses=_addresses(),
        attributes=_attributes(),
        person_id="person-1",
        source_record_pk="sr-1",
        attach_evidence=True,
    )

    joined = "\n".join(tx.queries)
    assert "CREATE (sr)-[:LINKED_TO" in joined
    assert "MERGE (p)-[rel:IDENTIFIED_BY]" in joined
    assert "MERGE (p)-[rel:LIVES_AT]" in joined
    assert "CREATE (p)-[:HAS_FACT" in joined
