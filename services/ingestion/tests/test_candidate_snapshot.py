"""CandidateSnapshot list accessors used by approximate-match scoring."""

from __future__ import annotations

from src.matching.snapshot import CandidateSnapshot


def _snapshot() -> CandidateSnapshot:
    return CandidateSnapshot(
        idents=[
            {"identifier_type": "phone", "normalized_value": "+6591234567", "is_verified": True},
            {"identifier_type": "phone", "normalized_value": "+6598765432", "is_verified": False},
            {
                "identifier_type": "email",
                "normalized_value": "ada@example.com",
                "is_verified": True,
            },
            {"identifier_type": "nric", "normalized_value": "S1234567A", "is_verified": True},
        ],
        facts=[],
        addrs=[],
    )


def test_phones_returns_all_phone_records() -> None:
    snapshot = _snapshot()
    values = {str(r["normalized_value"]) for r in snapshot.phones()}
    assert values == {"+6591234567", "+6598765432"}


def test_emails_returns_all_email_records() -> None:
    snapshot = _snapshot()
    values = {str(r["normalized_value"]) for r in snapshot.emails()}
    assert values == {"ada@example.com"}


def test_phones_excludes_other_identifier_types() -> None:
    snapshot = _snapshot()
    assert all(r["identifier_type"] == "phone" for r in snapshot.phones())


def test_emails_is_empty_when_no_emails() -> None:
    snapshot = CandidateSnapshot(idents=[], facts=[], addrs=[])
    assert snapshot.emails() == []


def test_fetch_candidate_snapshot_uses_matching_only_compacted_queries() -> None:
    from src.graph import queries
    from src.matching.snapshot import fetch_candidate_snapshot

    class Tx:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def run(self, query: str, **_kwargs: object) -> list[dict[str, object]]:
            self.calls.append(query)
            if query == queries.FETCH_PERSON_MATCH_IDENTIFIERS:
                return [
                    {
                        "identifier_type": "phone",
                        "normalized_value": "+6591234567",
                        "is_verified": True,
                        "is_system_sourced": True,
                    }
                ]
            if query == queries.FETCH_PERSON_MATCH_FACTS:
                return [{"attribute_name": "full_name", "attribute_value": "Ada"}]
            if query == queries.FETCH_PERSON_MATCH_ADDRESSES:
                return [{"normalized_full": "1 ada street"}]
            raise AssertionError("unexpected query")

    tx = Tx()
    snapshot = fetch_candidate_snapshot(tx, "person-1")  # type: ignore[arg-type]

    assert tx.calls == [
        queries.FETCH_PERSON_MATCH_IDENTIFIERS,
        queries.FETCH_PERSON_MATCH_FACTS,
        queries.FETCH_PERSON_MATCH_ADDRESSES,
    ]
    assert snapshot.phones_by_value()["+6591234567"]["is_system_sourced"] is True
    assert snapshot.names() == ["Ada"]
    assert snapshot.addrs == [{"normalized_full": "1 ada street"}]
