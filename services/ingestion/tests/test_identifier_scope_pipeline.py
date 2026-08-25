"""Identifier scope propagation through graph write and matching boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

from neo4j import ManagedTransaction
from src.graph import queries
from src.identifier_scopes import identifier_scope
from src.models import NormalizedIdentifier, QualityFlag
from src.pipeline_writes import find_candidates, upsert_nodes


class _Rows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)


class _CaptureTx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **params: object) -> _Rows:
        self.calls.append((query, params))
        if query == queries.FIND_CANDIDATES_BY_IDENTIFIERS_BATCH:
            return _Rows(
                [
                    {"input_index": 0, "fanout": 1, "person_ids": ["person-primary"]},
                    {"input_index": 1, "fanout": 1, "person_ids": ["person-email"]},
                ]
            )
        return _Rows([])


def _identifiers() -> list[NormalizedIdentifier]:
    return [
        NormalizedIdentifier(
            identifier_type="crm_contact_id",
            normalized_value="123",
            source_instance_id="bitrix-primary",
            quality_flag=QualityFlag.VALID,
        ),
        NormalizedIdentifier(
            identifier_type="email",
            normalized_value="ada@example.com",
            quality_flag=QualityFlag.VALID,
        ),
    ]


def test_upsert_uses_instance_scope_for_crm_and_global_scope_for_email() -> None:
    tx = _CaptureTx()

    upsert_nodes(cast(ManagedTransaction, tx), _identifiers(), [])

    assert tx.calls == [
        (
            queries.UPSERT_IDENTIFIERS_BATCH,
            {
                "identifiers": [
                    {
                        "identifier_type": "crm_contact_id",
                        "identifier_scope": "bitrix-primary",
                        "source_instance_id": "bitrix-primary",
                        "normalized_value": "123",
                    },
                    {
                        "identifier_type": "email",
                        "identifier_scope": "global",
                        "source_instance_id": None,
                        "normalized_value": "ada@example.com",
                    },
                ]
            },
        )
    ]


def test_candidate_lookup_carries_identifier_scope_to_the_graph() -> None:
    tx = _CaptureTx()

    candidates = find_candidates(cast(ManagedTransaction, tx), _identifiers(), [])

    assert [candidate.person_id for candidate in candidates] == ["person-primary", "person-email"]
    query, params = tx.calls[0]
    assert query == queries.FIND_CANDIDATES_BY_IDENTIFIERS_BATCH
    assert params["identifiers"] == [
        {
            "input_index": 0,
            "identifier_type": "crm_contact_id",
            "identifier_scope": "bitrix-primary",
            "normalized_value": "123",
        },
        {
            "input_index": 1,
            "identifier_type": "email",
            "identifier_scope": "global",
            "normalized_value": "ada@example.com",
        },
    ]


def test_scope_helper_cannot_instance_scope_generic_identifiers() -> None:
    assert identifier_scope("email", "bitrix-primary") == "global"
    assert identifier_scope("crm_contact_id", None) == "legacy-default"


def test_upsert_strips_accidental_instance_from_generic_identifier() -> None:
    tx = _CaptureTx()
    email = NormalizedIdentifier(
        identifier_type="email",
        normalized_value="ada@example.com",
        source_instance_id="bitrix-primary",
    )

    upsert_nodes(cast(ManagedTransaction, tx), [email], [])

    identifiers = tx.calls[0][1]["identifiers"]
    assert identifiers == [
        {
            "identifier_type": "email",
            "identifier_scope": "global",
            "source_instance_id": None,
            "normalized_value": "ada@example.com",
        }
    ]
