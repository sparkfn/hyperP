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
        "_phones",
        "_emails",
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
        self._phones: list[RecordDict] | None = None
        self._emails: list[RecordDict] | None = None
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

    def phones(self) -> list[RecordDict]:
        """All candidate phone identifier records (for approximate matching)."""
        if self._phones is None:
            self._phones = [i for i in self.idents if i.get("identifier_type") == "phone"]
        return self._phones

    def emails(self) -> list[RecordDict]:
        """All candidate email identifier records (for approximate matching)."""
        if self._emails is None:
            self._emails = [i for i in self.idents if i.get("identifier_type") == "email"]
        return self._emails

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
                str(f["attribute_value"]) for f in self.facts if f.get("attribute_name") == "dob"
            ]
        return self._dobs


def fetch_candidate_snapshot(
    tx: ManagedTransaction,
    candidate_person_id: str,
) -> CandidateSnapshot:
    """Pull all candidate-side rows the heuristic scorer needs in one shot."""
    idents: list[RecordDict] = [
        dict(r)
        for r in tx.run(
            queries.FETCH_PERSON_MATCH_IDENTIFIERS,
            person_id=candidate_person_id,
        )
    ]
    facts: list[RecordDict] = [
        dict(r)
        for r in tx.run(
            queries.FETCH_PERSON_MATCH_FACTS,
            person_id=candidate_person_id,
        )
    ]
    addrs: list[RecordDict] = [
        dict(r)
        for r in tx.run(
            queries.FETCH_PERSON_MATCH_ADDRESSES,
            person_id=candidate_person_id,
        )
    ]
    return CandidateSnapshot(idents=idents, facts=facts, addrs=addrs)


def fetch_candidate_snapshots(
    tx: ManagedTransaction,
    candidate_person_ids: list[str],
    *,
    include_legacy: bool,
) -> dict[str, CandidateSnapshot]:
    """Load every candidate snapshot in one UNWIND query, keyed by person ID.

    Reads v2 evidence through ``HAS_IDENTIFIER`` and merges the legacy
    ``IDENTIFIED_BY`` projection when ``include_legacy`` is set (shadow mode);
    the v2 projection wins on a duplicate identifier pair.
    """
    candidates = [
        {"input_index": input_index, "person_id": person_id}
        for input_index, person_id in enumerate(candidate_person_ids)
    ]
    snapshots: dict[str, CandidateSnapshot] = {}
    if not candidates:
        return snapshots
    for row in tx.run(
        queries.CANDIDATE_SNAPSHOT_BATCH,
        candidates=candidates,
        include_legacy=include_legacy,
    ):
        person_id = str(row["candidate_person_id"])
        has_identifiers = _record_dicts(row["has_identifiers"])
        legacy_identifiers = _record_dicts(row["legacy_identifiers"])
        snapshots[person_id] = CandidateSnapshot(
            idents=_dedupe_identifiers(has_identifiers, legacy_identifiers),
            facts=_record_dicts(row["facts"]),
            addrs=_record_dicts(row["addresses"]),
        )
    return snapshots


def _record_dicts(value: object) -> list[RecordDict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dedupe_identifiers(
    has_identifiers: list[RecordDict],
    legacy_identifiers: list[RecordDict],
) -> list[RecordDict]:
    merged: dict[tuple[str, str], RecordDict] = {}
    for row in (*has_identifiers, *legacy_identifiers):
        key = (str(row.get("identifier_type", "")), str(row.get("normalized_value", "")))
        merged.setdefault(key, row)
    return list(merged.values())
