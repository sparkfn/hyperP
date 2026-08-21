"""Regression tests for provisional REVIEW-band attach behavior.

A REVIEW-band match against an existing candidate must link the source record
for the reviewer to compare, but must NOT wire the incoming identifiers /
addresses / facts onto the candidate person until a human approves the merge.
"""

from __future__ import annotations

from collections.abc import Iterator

from _txmock import _RecordingTx
from src.graph import queries
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

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(())


class _Tx(_RecordingTx):
    """Captures the Cypher constant each ``run`` was invoked with.

    Inherits the ``(query, kwargs)`` call recording from the shared
    ``_txmock._RecordingTx``; exposes a ``queries`` view (query strings only) for
    the assertions below.
    """

    @property
    def queries(self) -> list[str]:
        return [q for q, _ in self.calls]

    def run(self, query: str, **params: object) -> _Result:
        self._record(query, params)
        return _Result()


def _envelope(record_type: RecordType = RecordType.IDENTITY) -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_record_id="rec-1",
        record_type=record_type,
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
    tx = _Tx()
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
    tx = _Tx()
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
    assert "MERGE (p)-[rel:IDENTIFIED_BY {" in joined
    assert "MERGE (p)-[rel:LIVES_AT {" in joined
    assert "CREATE (p)-[:HAS_FACT" in joined


def test_crm_deal_attach_recomputes_projection_but_identity_attach_does_not() -> None:
    crm_tx = _Tx()
    link_record_to_graph(
        crm_tx,  # type: ignore[arg-type]
        envelope=_envelope(RecordType.CRM_DEAL),
        identifiers=[],
        addresses=[],
        attributes=[],
        person_id="person-1",
        source_record_pk="deal-1",
    )
    identity_tx = _Tx()
    link_record_to_graph(
        identity_tx,  # type: ignore[arg-type]
        envelope=_envelope(),
        identifiers=[],
        addresses=[],
        attributes=[],
        person_id="person-1",
        source_record_pk="identity-1",
    )

    assert queries.RECOMPUTE_PERSON_CRM_DEAL_COUNTS in crm_tx.queries
    assert queries.RECOMPUTE_PERSON_CRM_DEAL_COUNTS not in identity_tx.queries
