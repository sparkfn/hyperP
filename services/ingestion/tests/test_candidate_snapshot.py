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
