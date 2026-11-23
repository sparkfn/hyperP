"""Fake Neo4j client and result objects for NRIC apply unit tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from copy import deepcopy
from typing import TypeVar, cast

from neo4j import ManagedTransaction, Record
from src.graph.queries import nric_rehash_consolidate as consolidate_queries
from src.graph.queries.nric_rehash import (
    NRIC_IDENTIFIER_CENSUS,
    NRIC_IDENTIFIER_PAGE,
    NRIC_REHASH_ACQUIRE_LEASE,
    NRIC_REHASH_ADVANCE_BATCH,
    NRIC_REHASH_COMPLETE_AND_RELEASE,
    NRIC_REHASH_DATABASE_NAME,
    NRIC_REHASH_FENCE_BATCH,
    NRIC_REHASH_FIND_DIGEST_TARGET,
    NRIC_REHASH_PAGE_AND_LOCK_IDENTIFIERS,
    NRIC_REHASH_READ_REVISION,
    NRIC_REHASH_READ_STATE,
    NRIC_REHASH_RELEASE_LEASE,
    NRIC_REHASH_UPDATE_IDENTIFIER,
    NRIC_VERIFY_DUPLICATE_PERSON_KEYS,
    NRIC_VERIFY_DUPLICATE_SCOPED_TARGETS,
    NRIC_VERIFY_IDENTIFIERS,
)
from src.normalizers.nric import is_nric_digest

_T = TypeVar("_T")


class _Result:
    def __init__(self, rows: Sequence[Record]) -> None:
        self.rows = tuple(rows)

    def __iter__(self) -> Iterator[Record]:
        return iter(self.rows)

    def single(self) -> Record | None:
        return self.rows[0] if self.rows else None


def _result(*rows: Mapping[str, object]) -> _Result:
    return _Result(tuple(cast(Record, dict(row)) for row in rows))


class _FakeTx:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client

    def run(self, query: str, **parameters: object) -> _Result:
        self.client.query_parameters.append((query, parameters))
        if query == NRIC_IDENTIFIER_CENSUS:
            return _result(self._census())
        if query == NRIC_IDENTIFIER_PAGE:
            return self._page(parameters.get("after_id"), parameters.get("batch_size"))
        if query == NRIC_REHASH_DATABASE_NAME:
            return _result({"name": self.client.database_name})
        if query == NRIC_REHASH_READ_STATE:
            return self._read_state()
        if query == NRIC_REHASH_READ_REVISION:
            return _result({"current_revision": self.client.revision})
        if query == NRIC_VERIFY_IDENTIFIERS:
            return _result(self._verify_identifiers(parameters.get("key_version")))
        if query == NRIC_VERIFY_DUPLICATE_PERSON_KEYS:
            return _result({"duplicate_key_groups": 0, "duplicate_extra_owners": 0})
        if query == NRIC_VERIFY_DUPLICATE_SCOPED_TARGETS:
            return _result({"duplicate_scoped_targets": self._duplicate_targets()})
        if query == consolidate_queries.NRIC_REHASH_EVIDENCE_COUNTS:
            return _result(
                {
                    "asserts": 0,
                    "identified_by": 0,
                    "anchored_by": 0,
                    "moved": 0,
                    "has_identifier": 0,
                    "active_links": 0,
                }
            )
        if query == consolidate_queries.NRIC_REHASH_LOCK_CONSOLIDATION_IDENTIFIERS:
            return self._lock_consolidation(parameters)
        if query == consolidate_queries.NRIC_REHASH_INVENTORY_CONSOLIDATION_RELATIONSHIPS:
            return _result(*self.client.relationship_inventory)
        if query == consolidate_queries.NRIC_REHASH_CONSOLIDATION_PARTICIPANTS:
            return _result()
        if query in {
            consolidate_queries.NRIC_REHASH_MOVE_ASSERTS,
            consolidate_queries.NRIC_REHASH_MOVE_IDENTIFIED_BY,
            consolidate_queries.NRIC_REHASH_MOVE_ANCHORED_BY,
            consolidate_queries.NRIC_REHASH_MOVE_MOVED_RELATIONSHIP,
        }:
            return _result({"moved": 0})
        if query == NRIC_REHASH_ACQUIRE_LEASE:
            return self._acquire(parameters)
        if query == NRIC_REHASH_FENCE_BATCH:
            return self._fence(parameters)
        if query == NRIC_REHASH_PAGE_AND_LOCK_IDENTIFIERS:
            return self._page(parameters.get("cursor"), parameters.get("batch_size"))
        if query == NRIC_REHASH_FIND_DIGEST_TARGET:
            return self._find_digest(parameters.get("digest"))
        if query == NRIC_REHASH_UPDATE_IDENTIFIER:
            return self._update_identifier(parameters)
        if query == NRIC_REHASH_ADVANCE_BATCH:
            return self._advance(parameters)
        if query == NRIC_REHASH_COMPLETE_AND_RELEASE:
            return self._complete(parameters)
        if query == NRIC_REHASH_RELEASE_LEASE:
            if self.client.release_error is not None:
                raise self.client.release_error
            return self._release(parameters)
        raise AssertionError("unexpected Cypher statement")

    def _census(self) -> dict[str, object]:
        rows = self.client.identifiers
        values = [row.get("normalized_value") for row in rows]
        digest_flags = [isinstance(value, str) and is_nric_digest(value) for value in values]
        scopes = [row.get("identifier_scope") for row in rows]
        return {
            "total": len(rows),
            "digest_shaped": sum(digest_flags),
            "non_digest": len(rows) - sum(digest_flags),
            "missing_ids": sum(row.get("identifier_id") is None for row in rows),
            "missing_scopes": sum(scope is None for scope in scopes),
            "unexpected_scopes": sum(scope is not None and scope != "global" for scope in scopes),
        }

    def _page(self, cursor: object, batch_size: object) -> _Result:
        if not isinstance(batch_size, int):
            raise AssertionError("page size was not an integer")
        rows = sorted(
            self.client.identifiers,
            key=lambda row: str(row.get("identifier_id") or ""),
        )
        after = cursor if isinstance(cursor, str) else None
        selected = [
            row for row in rows if after is None or str(row.get("identifier_id") or "") > after
        ][:batch_size]
        return _result(
            *[
                {
                    **row,
                    "element_id": f"test-element-{row.get('identifier_id')}",
                }
                for row in selected
            ]
        )

    def _read_state(self) -> _Result:
        state = self.client.state
        if state is None:
            return _result({"exists": False})
        return _result({"exists": True, **state})

    def _verify_identifiers(self, key_version: object) -> dict[str, object]:
        rows = self.client.identifiers
        values = [row.get("normalized_value") for row in rows]
        metadata_gaps = 0
        for row, value in zip(rows, values, strict=True):
            if not isinstance(value, str) or not is_nric_digest(value):
                continue
            if (
                any(
                    row.get(key) is None
                    for key in (
                        "nric_masked",
                        "checksum_valid",
                        "nric_key_version",
                        "nric_encrypted",
                    )
                )
                or row.get("nric_key_version") != key_version
            ):
                metadata_gaps += 1
        digest_flags = [isinstance(value, str) and is_nric_digest(value) for value in values]
        non_digest = len(rows) - sum(digest_flags)
        return {
            "identifier_count": len(rows),
            "non_digest_count": non_digest,
            "missing_metadata_count": metadata_gaps,
            "missing_id_count": sum(row.get("identifier_id") is None for row in rows),
            "unexpected_scope_count": sum(row.get("identifier_scope") != "global" for row in rows),
        }

    def _duplicate_targets(self) -> int:
        values = [
            row.get("normalized_value")
            for row in self.client.identifiers
            if row.get("identifier_scope") in (None, "global")
        ]
        return len({value for value in values if values.count(value) > 1})

    def _acquire(self, parameters: Mapping[str, object]) -> _Result:
        state = self.client.state
        if state is None:
            state = {
                "phase": "convert",
                "cursor": None,
                "key_version": parameters["key_version"],
                "key_check": parameters["key_check"],
                "baseline_total": parameters["baseline_total"],
                "baseline_non_digest": parameters["baseline_non_digest"],
                "baseline_digest_shaped": parameters["baseline_digest_shaped"],
                "baseline_asserts": parameters["baseline_asserts"],
                "baseline_identified_by": parameters["baseline_identified_by"],
                "baseline_anchored_by": parameters["baseline_anchored_by"],
                "baseline_moved": parameters["baseline_moved"],
                "baseline_has_identifier": parameters["baseline_has_identifier"],
                "baseline_active_links": parameters["baseline_active_links"],
                "baseline_revision": parameters["baseline_revision"],
                "batches": 0,
                "converted_in_place": 0,
                "converted_consolidated": 0,
                "removed_nodes": 0,
                "lock_version": 0,
            }
            self.client.state = state
        state["lock_version"] = int(state.get("lock_version", 0)) + 1
        owner = parameters["owner"]
        if state.get("lease_owner") not in (None, owner):
            return _result()
        if (
            state.get("key_version") != parameters["key_version"]
            or state.get("key_check") != parameters["key_check"]
        ):
            return _result(
                {
                    "acquired": True,
                    "key_matches": False,
                    **state,
                }
            )
        if parameters.get("restart_completed") is True and state.get("phase") == "convert_complete":
            state["phase"] = "convert"
            state["cursor"] = None
        state["lease_owner"] = owner
        state["offline_acknowledged_at"] = state.get("offline_acknowledged_at", "ack")
        return _result({"acquired": True, "key_matches": True, **state})

    def _fence(self, parameters: Mapping[str, object]) -> _Result:
        if self.client.state is None or self.client.state.get("lease_owner") != parameters.get(
            "owner"
        ):
            return _result()
        self.client.state["lock_version"] = int(self.client.state.get("lock_version", 0)) + 1
        return _result({"fenced": True})

    def _find_digest(self, digest: object) -> _Result:
        matches = [
            {"element_id": f"test-element-{row.get('identifier_id')}"}
            for row in self.client.identifiers
            if row.get("identifier_type") == "nric"
            and row.get("identifier_scope") == "global"
            and row.get("normalized_value") == digest
        ]
        return _result(*matches[:1])

    def _lock_consolidation(self, parameters: Mapping[str, object]) -> _Result:
        element_ids = parameters.get("element_ids")
        if not isinstance(element_ids, list):
            raise AssertionError("consolidation lock ids were malformed")
        wanted = set(element_ids)
        rows = [
            {**row, "element_id": f"test-element-{row.get('identifier_id')}"}
            for row in self.client.identifiers
            if f"test-element-{row.get('identifier_id')}" in wanted
        ]
        return _result(*rows)

    def _update_identifier(self, parameters: Mapping[str, object]) -> _Result:
        element_id = parameters.get("element_id")
        digest = parameters.get("digest")
        for row in self.client.identifiers:
            row_id = row.get("identifier_id")
            row_element_id = f"test-element-{row_id}" if isinstance(row_id, str) else None
            if (
                row_element_id == element_id
                and row.get("identifier_type") == "nric"
                and row.get("identifier_scope") == "global"
                and not is_nric_digest(str(row.get("normalized_value") or ""))
                and not any(
                    other is not row
                    and other.get("identifier_type") == "nric"
                    and other.get("identifier_scope") == "global"
                    and other.get("normalized_value") == digest
                    for other in self.client.identifiers
                )
            ):
                row.update(
                    normalized_value=digest,
                    hashed_value=parameters.get("hashed_value"),
                    nric_key_version=parameters.get("key_version"),
                    nric_masked=parameters.get("masked"),
                    checksum_valid=parameters.get("checksum_valid"),
                    nric_encrypted=parameters.get("ciphertext"),
                )
                return _result({"updated": 1})
        return _result({"updated": 0})

    def _advance(self, parameters: Mapping[str, object]) -> _Result:
        state = self.client.state
        if state is None or state.get("lease_owner") != parameters.get("owner"):
            return _result()
        state["cursor"] = parameters["cursor"]
        state["batches"] = int(state["batches"]) + 1
        state["converted_in_place"] = int(state["converted_in_place"]) + int(
            parameters["converted_in_place"]
        )
        state["converted_consolidated"] = int(state["converted_consolidated"]) + int(
            parameters["converted_consolidated"]
        )
        state["removed_nodes"] = int(state["removed_nodes"]) + int(parameters["removed_nodes"])
        return _result(
            {
                "batches": state["batches"],
                "converted_in_place": state["converted_in_place"],
                "converted_consolidated": state["converted_consolidated"],
                "removed_nodes": state["removed_nodes"],
            }
        )

    def _complete(self, parameters: Mapping[str, object]) -> _Result:
        state = self.client.state
        if state is None or state.get("lease_owner") != parameters.get("owner"):
            return _result()
        state["phase"] = "convert_complete"
        state["cursor"] = None
        state.pop("lease_owner", None)
        return _result({"completed": True})

    def _release(self, parameters: Mapping[str, object]) -> _Result:
        state = self.client.state
        if state is None or state.get("lease_owner") != parameters.get("owner"):
            return _result()
        state.pop("lease_owner", None)
        return _result({"released": True})


class _FakeClient:
    def __init__(
        self,
        identifiers: Sequence[Mapping[str, object]] = (),
        *,
        database_name: str = "neo4j",
        revision: int | None = 29,
        state: dict[str, object] | None = None,
        relationship_inventory: Sequence[Mapping[str, object]] = (),
        release_error: BaseException | None = None,
    ) -> None:
        self.identifiers = [dict(row) for row in identifiers]
        self.database_name = database_name
        self.revision = revision
        self.state = deepcopy(state)
        self.relationship_inventory = [dict(row) for row in relationship_inventory]
        self.release_error = release_error
        self.write_calls = 0
        self.query_parameters: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def execute_read(self, work: Callable[[ManagedTransaction], _T]) -> _T:
        return work(cast(ManagedTransaction, _FakeTx(self)))

    def execute_write(self, work: Callable[[ManagedTransaction], _T]) -> _T:
        self.write_calls += 1
        identifiers = deepcopy(self.identifiers)
        state = deepcopy(self.state)
        try:
            return work(cast(ManagedTransaction, _FakeTx(self)))
        except BaseException:
            self.identifiers = identifiers
            self.state = state
            raise

    def close(self) -> None:
        self.closed = True
