"""Shared fakes and generated helpers for NRIC plan, verify, and CLI tests."""

from __future__ import annotations

import json
import secrets
import string
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import TypeVar, cast

from neo4j import ManagedTransaction, Record
from src.config import Settings
from src.graph.queries import (
    NRIC_IDENTIFIER_CENSUS,
    NRIC_IDENTIFIER_PAGE,
    NRIC_VERIFY_DUPLICATE_PERSON_KEYS,
    NRIC_VERIFY_DUPLICATE_SCOPED_TARGETS,
    NRIC_VERIFY_IDENTIFIERS,
)
from src.normalizers.nric import nric_checksum_valid
from src.nric_rehash_classify import NricIdentifierRow

KEY_VERSION = "test-key-version"
_TestT = TypeVar("_TestT")


class FakeRecord:
    def __init__(self, values: Mapping[str, object]) -> None:
        self.values = dict(values)

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)


class FakeResult:
    def __init__(self, rows: Sequence[Record]) -> None:
        self.rows = tuple(rows)

    def __iter__(self) -> Iterator[Record]:
        return iter(self.rows)

    def single(self) -> Record | None:
        return self.rows[0] if self.rows else None


class FakeTransaction:
    def __init__(
        self,
        responses: Mapping[str, Sequence[Sequence[Mapping[str, object]]]],
    ) -> None:
        self.responses = {query: list(calls) for query, calls in responses.items()}
        self.parameters: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **parameters: object) -> FakeResult:
        self.parameters.append((query, parameters))
        queued = self.responses.get(query)
        if not queued:
            raise AssertionError("unexpected query execution")
        rows = queued.pop(0)
        return FakeResult(tuple(cast(Record, FakeRecord(row)) for row in rows))


class FakeClient:
    def __init__(
        self,
        responses: Mapping[str, Sequence[Sequence[Mapping[str, object]]]],
        error: BaseException | None = None,
    ) -> None:
        self.transaction = FakeTransaction(responses)
        self.error = error
        self.closed = False

    def execute_read(self, work: Callable[[ManagedTransaction], _TestT]) -> _TestT:
        if self.error is not None:
            raise self.error
        return work(cast(ManagedTransaction, self.transaction))

    def close(self) -> None:
        self.closed = True


def _valid_nric() -> str:
    while True:
        digits = f"{secrets.randbelow(10_000_000):07d}"
        if len(set(digits)) == 1:
            continue
        for letter in string.ascii_uppercase:
            candidate = f"S{digits}{letter}"
            if nric_checksum_valid(candidate):
                return candidate


def _row(
    identifier_id: str | None,
    value: object,
    *,
    scope: str | None = "global",
    hashed_value: str | None = None,
    nric_masked: str | None = None,
    checksum_valid: bool | None = None,
    key_version: str | None = None,
    encrypted: str | None = None,
) -> NricIdentifierRow:
    return NricIdentifierRow(
        identifier_id=identifier_id,
        identifier_scope=scope,
        normalized_value=value if isinstance(value, str) else None,
        hashed_value=hashed_value,
        nric_masked=nric_masked,
        checksum_valid=checksum_valid,
        nric_key_version=key_version,
        nric_encrypted=encrypted,
    )


def _graph_row(row: NricIdentifierRow) -> dict[str, object]:
    return {
        "identifier_id": row.identifier_id,
        "identifier_scope": row.identifier_scope,
        "normalized_value": row.normalized_value,
        "hashed_value": row.hashed_value,
        "nric_masked": row.nric_masked,
        "checksum_valid": row.checksum_valid,
        "nric_key_version": row.nric_key_version,
        "nric_encrypted": row.nric_encrypted,
    }


def _census_row(
    *,
    total: int,
    digest_shaped: int,
    non_digest: int,
    missing_ids: int = 0,
    missing_scopes: int = 0,
    unexpected_scopes: int = 0,
) -> dict[str, object]:
    return {
        "total": total,
        "digest_shaped": digest_shaped,
        "non_digest": non_digest,
        "missing_ids": missing_ids,
        "missing_scopes": missing_scopes,
        "unexpected_scopes": unexpected_scopes,
    }


def _plan_client(
    census: Mapping[str, object], pages: Sequence[Sequence[Mapping[str, object]]]
) -> FakeClient:
    return FakeClient(
        {
            NRIC_IDENTIFIER_CENSUS: [[census]],
            NRIC_IDENTIFIER_PAGE: pages,
        }
    )


def _verification_client(
    *,
    identifier_count: int = 0,
    non_digest_count: int = 0,
    missing_metadata_count: int = 0,
    missing_id_count: int = 0,
    unexpected_scope_count: int = 0,
    duplicate_key_groups: int = 0,
    duplicate_extra_owners: int = 0,
    duplicate_scoped_targets: int = 0,
) -> FakeClient:
    return FakeClient(
        {
            NRIC_VERIFY_IDENTIFIERS: [
                [
                    {
                        "identifier_count": identifier_count,
                        "non_digest_count": non_digest_count,
                        "missing_metadata_count": missing_metadata_count,
                        "missing_id_count": missing_id_count,
                        "unexpected_scope_count": unexpected_scope_count,
                    }
                ]
            ],
            NRIC_VERIFY_DUPLICATE_PERSON_KEYS: [
                [
                    {
                        "duplicate_key_groups": duplicate_key_groups,
                        "duplicate_extra_owners": duplicate_extra_owners,
                    }
                ]
            ],
            NRIC_VERIFY_DUPLICATE_SCOPED_TARGETS: [
                [{"duplicate_scoped_targets": duplicate_scoped_targets}]
            ],
        }
    )


def _settings(key: str, key_version: str = KEY_VERSION) -> Settings:
    return Settings.model_construct(
        nric_hmac_key=key,
        nric_hmac_key_version=key_version,
        neo4j_password="",
    )


def _report(output: str) -> dict[str, object]:
    decoded: object = json.loads(output)
    assert isinstance(decoded, dict)
    return cast(dict[str, object], decoded)
