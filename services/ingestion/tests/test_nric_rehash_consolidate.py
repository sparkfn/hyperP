"""Unit coverage for pure consolidation edge policy and transaction order."""

from __future__ import annotations

import secrets
from collections.abc import Iterable, Iterator
from typing import cast

import pytest
from _nric_rehash_neo4j_seed import _valid_canonical
from neo4j import ManagedTransaction
from src import nric_rehash_consolidate as consolidate_module
from src.graph.queries import nric_rehash_consolidate as consolidate_queries
from src.identity_links import ParticipantKind
from src.normalizers.nric import mask_nric, nric_checksum_valid, nric_digest
from src.nric_rehash_consolidate import (
    SUPPORTED_EDGES,
    consolidate_identifier,
    is_supported_edge,
)


class _Result:
    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self.rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)

    def single(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None


class _FakeTx:
    def __init__(self, events: list[str], plaintext: str, digest: str) -> None:
        self.events = events
        self.plaintext = plaintext
        self.digest = digest

    def run(self, query: str, **parameters: object) -> _Result:
        if query == consolidate_queries.NRIC_REHASH_LOCK_CONSOLIDATION_IDENTIFIERS:
            self.events.append("lock")
            return _Result(
                (
                    {
                        "element_id": "loser",
                        "identifier_type": "nric",
                        "identifier_scope": "global",
                        "normalized_value": self.plaintext,
                    },
                    {
                        "element_id": "survivor",
                        "identifier_type": "nric",
                        "identifier_scope": "global",
                        "normalized_value": self.digest,
                    },
                )
            )
        if query == consolidate_queries.NRIC_REHASH_INVENTORY_CONSOLIDATION_RELATIONSHIPS:
            self.events.append("inventory")
            return _Result(
                (
                    {
                        "relationship_type": "ASSERTS",
                        "loser_is_start": False,
                        "other_labels": ["SourceRecord"],
                        "relationship_count": 2,
                    },
                    {
                        "relationship_type": "IDENTIFIED_BY",
                        "loser_is_start": False,
                        "other_labels": ["Person"],
                        "relationship_count": 1,
                    },
                    {
                        "relationship_type": "ANCHORED_BY",
                        "loser_is_start": False,
                        "other_labels": ["Person"],
                        "relationship_count": 1,
                    },
                    {
                        "relationship_type": "MOVED_RELATIONSHIP",
                        "loser_is_start": False,
                        "other_labels": ["MergeEvent"],
                        "relationship_count": 1,
                    },
                    {
                        "relationship_type": "HAS_IDENTIFIER",
                        "loser_is_start": False,
                        "other_labels": ["Person"],
                        "relationship_count": 1,
                    },
                )
            )
        if query == consolidate_queries.NRIC_REHASH_CONSOLIDATION_PARTICIPANTS:
            self.events.append("participants")
            return _Result(
                (
                    {
                        "participant_labels": ["Profile"],
                        "participant_kind": "profile",
                        "participant_id": "profile",
                    },
                    {
                        "participant_labels": ["Person"],
                        "participant_kind": "person",
                        "participant_id": "person",
                    },
                )
            )
        move_queries = {
            consolidate_queries.NRIC_REHASH_MOVE_ASSERTS: ("move:ASSERTS", 2),
            consolidate_queries.NRIC_REHASH_MOVE_IDENTIFIED_BY: ("move:IDENTIFIED_BY", 1),
            consolidate_queries.NRIC_REHASH_MOVE_ANCHORED_BY: ("move:ANCHORED_BY", 1),
            consolidate_queries.NRIC_REHASH_MOVE_MOVED_RELATIONSHIP: ("move:MOVED_RELATIONSHIP", 1),
        }
        if query in move_queries:
            event, count = move_queries[query]
            self.events.append(event)
            return _Result(({"moved": count},))
        if query == consolidate_queries.NRIC_REHASH_DELETE_CONSOLIDATED_IDENTIFIER:
            self.events.append("delete")
            return _Result(({"deleted": 1},))
        if query == consolidate_queries.NRIC_REHASH_UPDATE_SURVIVOR_METADATA:
            self.events.append("metadata")
            return _Result(({"updated": 1},))
        raise AssertionError("unexpected consolidation statement")


def test_supported_edge_policy_accepts_only_declared_incoming_shapes() -> None:
    expected = frozenset(
        {
            ("ASSERTS", False, "SourceRecord"),
            ("IDENTIFIED_BY", False, "Person"),
            ("ANCHORED_BY", False, "Person"),
            ("MOVED_RELATIONSHIP", False, "MergeEvent"),
            ("HAS_IDENTIFIER", False, "Person"),
            ("HAS_IDENTIFIER", False, "Profile"),
        }
    )
    assert SUPPORTED_EDGES == expected
    for relationship_type, loser_is_start, label in expected:
        assert is_supported_edge(relationship_type, loser_is_start, label)
        assert not is_supported_edge(relationship_type, not loser_is_start, label)
        assert not is_supported_edge(relationship_type, loser_is_start, "UnsupportedLabel")
    assert not is_supported_edge("UNKNOWN", False, "Person")


def test_consolidation_runs_lock_move_recompute_delete_and_metadata_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _valid_canonical(set())
    key = secrets.token_hex(32)
    digest = nric_digest(canonical, key)
    events: list[str] = []
    tx = _FakeTx(events, canonical, digest)

    def record_link(_tx: ManagedTransaction, kind: ParticipantKind, participant_id: str) -> None:
        events.append(f"recompute:{kind}:{participant_id}")

    def record_counts(_tx: ManagedTransaction, keys: Iterable[tuple[str, str, str]]) -> None:
        if set(keys) != {("nric", "global", digest)}:
            pytest.fail("participant count key mismatch")
        events.append("counts")

    monkeypatch.setattr(consolidate_module, "recompute_participant_links", record_link)
    monkeypatch.setattr(consolidate_module, "recompute_participant_counts", record_counts)
    consolidate_identifier(
        cast(ManagedTransaction, tx),
        loser_element_id="loser",
        survivor_element_id="survivor",
        plaintext_value=canonical,
        canonical=canonical,
        digest=digest,
        key=key,
        key_version="generated-version",
        masked=mask_nric(canonical),
        checksum_valid=nric_checksum_valid(canonical),
    )

    assert events == [
        "lock",
        "inventory",
        "participants",
        "move:ASSERTS",
        "move:IDENTIFIED_BY",
        "move:ANCHORED_BY",
        "move:MOVED_RELATIONSHIP",
        "recompute:person:person",
        "recompute:profile:profile",
        "counts",
        "recompute:person:person",
        "recompute:profile:profile",
        "delete",
        "metadata",
    ]


def test_supported_edge_values_are_unique() -> None:
    assert len(SUPPORTED_EDGES) == 6
