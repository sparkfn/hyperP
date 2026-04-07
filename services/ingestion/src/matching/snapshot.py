"""Pre-fetched view of a candidate Person's identifiers, facts, and addresses.

Bundling these into one object lets the heuristic scorer iterate them
multiple times without re-querying Neo4j, and keeps method signatures short.
"""

from __future__ import annotations

from neo4j import ManagedTransaction

from src.graph import queries

# A Neo4j Record dict — heterogeneous query result. Kept as ``dict[str, object]``
# rather than ``dict[str, Any]`` so callers can't silently propagate untyped data.
RecordDict = dict[str, object]


class CandidateSnapshot:
    """Lazy index of a candidate Person's identifiers, facts, and addresses."""

    __slots__ = (
        "idents",
        "facts",
        "addrs",
        "_phones_by_value",
        "_emails_by_value",
        "_names",
        "_dobs",
    )

    def __init__(
        self,
        *,
        idents: list[RecordDict],
        facts: list[RecordDict],
        addrs: list[RecordDict],
    ) -> None:
        self.idents = idents
        self.facts = facts
        self.addrs = addrs
        self._phones_by_value: dict[str, RecordDict] | None = None
        self._emails_by_value: dict[str, RecordDict] | None = None
        self._names: list[str] | None = None
        self._dobs: list[str] | None = None

    def phones_by_value(self) -> dict[str, RecordDict]:
        if self._phones_by_value is None:
            self._phones_by_value = {
                str(i["normalized_value"]): i
                for i in self.idents
                if i.get("identifier_type") == "phone"
            }
        return self._phones_by_value

    def emails_by_value(self) -> dict[str, RecordDict]:
        if self._emails_by_value is None:
            self._emails_by_value = {
                str(i["normalized_value"]): i
                for i in self.idents
                if i.get("identifier_type") == "email"
            }
        return self._emails_by_value

    def names(self) -> list[str]:
        if self._names is None:
            self._names = [
                str(f["attribute_value"])
                for f in self.facts
                if f.get("attribute_name") in ("full_name", "preferred_name", "legal_name")
            ]
        return self._names

    def dobs(self) -> list[str]:
        if self._dobs is None:
            self._dobs = [
                str(f["attribute_value"])
                for f in self.facts
                if f.get("attribute_name") == "dob"
            ]
        return self._dobs


def fetch_candidate_snapshot(
    tx: ManagedTransaction, candidate_person_id: str,
) -> CandidateSnapshot:
    """Pull all candidate-side rows the heuristic scorer needs in one shot."""
    idents: list[RecordDict] = [
        dict(r) for r in tx.run(
            queries.FETCH_PERSON_IDENTIFIERS,
            person_id=candidate_person_id,
        )
    ]
    facts: list[RecordDict] = [
        dict(r) for r in tx.run(
            queries.FETCH_PERSON_FACTS,
            person_id=candidate_person_id,
        )
    ]
    addrs: list[RecordDict] = [
        dict(r) for r in tx.run(
            queries.FETCH_PERSON_ADDRESSES,
            person_id=candidate_person_id,
        )
    ]
    return CandidateSnapshot(idents=idents, facts=facts, addrs=addrs)
