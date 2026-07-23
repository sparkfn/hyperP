from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

import pytest
from neo4j import AsyncManagedTransaction
from src.graph.queries import (
    ACTIVATE_PENDING_REVIEW_RECORD,
    ASSIGN_REVIEW_CASE,
    CHECK_BOTH_PERSONS_ACTIVE,
    CHECK_NO_MATCH_LOCK,
    CLAIM_PENDING_REVIEW_RESOLUTION,
    CREATE_NO_MATCH_LOCK_FROM_REVIEW,
    EXECUTE_MANUAL_MERGE,
    FINALIZE_STAGED_REVIEW_SALE,
    GET_PENDING_REVIEW_RECORD,
    GET_PERSONS_FOR_REVIEW_MERGE,
    GET_REVIEW_SALES_RECORD,
    LINK_REVIEW_SALES_BOUGHT_VEHICLE,
    LINK_REVIEW_SALES_PURCHASED_ORDER,
    MARK_REVIEW_SALES_RECORD_LINKED,
    MARK_REVIEW_SALES_RECORD_UNRESOLVED,
    PRECHECK_STAGED_REVIEW_SALE,
    PROMOTE_STAGED_REVIEW_SALE,
    REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT,
    REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT,
    REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    REJECT_PENDING_REVIEW_RECORD,
    build_count_review_cases_query,
    build_list_review_cases_query,
    build_review_action_cypher,
)
from src.repositories.neo4j import review as review_module
from src.repositories.neo4j.review import _action_tx
from src.repositories.neo4j.sales_staging import canonical_staging_hash
from src.repositories.protocols.merge import GoldenProfileSelection
from src.types import ApiReviewActionType

type ReviewCaseRecord = dict[str, str | None]
type Record = Mapping[str, object]
type Params = Mapping[str, object]


def test_sales_staging_hash_golden_vector() -> None:
    assert canonical_staging_hash({"a": 1, "b": ["x", None]}) == (
        "18c018603b12c4beed8593acb6ad65cdc9667cce853e8b6dffd035fe3a0fb4de"
    )


def _stage_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _valid_stage_precheck() -> dict[str, object]:
    order: dict[str, object] = {"source_order_id": "order-1", "entity_key": "entity-1"}
    line: dict[str, object] = {
        "line_index": 0,
        "source_line_item_id": "line-1",
        "source_product_id": "product-1",
    }
    line["line_hash"] = _stage_hash(line)
    observation: dict[str, object] = {
        "observation_index": 0,
        "source_system_key": "source-1",
        "source_record_id": "sale-1",
        "normalized_serial_number": "SERIAL-1",
        "product_sku": "SKU-1",
    }
    observation["observation_hash"] = _stage_hash(observation)
    lines = [line]
    observations = [observation]
    return {
        "source_lock_version": 7,
        "lock_version": 4,
        "order": order,
        "order_hash": _stage_hash(order),
        "lines": lines,
        "observations": observations,
        "expected_line_count": 1,
        "expected_observation_count": 1,
        "stage_hash": _stage_hash({"order": order, "lines": lines, "observations": observations}),
    }


class _AsyncResult:
    def __init__(self, record: Record | None) -> None:
        self._record = record

    async def single(self) -> Record | None:
        return self._record

    def __aiter__(self) -> AsyncIterator[Record]:
        self._iter_done = self._record is None
        return self

    async def __anext__(self) -> Record:
        if self._iter_done:
            raise StopAsyncIteration
        self._iter_done = True
        if self._record is None:
            raise StopAsyncIteration
        return self._record


@dataclass(frozen=True)
class _Call:
    query: str
    params: Params


class _Tx:
    def __init__(self, records: Sequence[Record | None]) -> None:
        self._records: list[Record | None] = list(records)
        self.calls: list[_Call] = []

    async def run(self, query: str, **params: object) -> _AsyncResult:
        self.calls.append(_Call(query=query, params=params))
        record = self._records.pop(0) if self._records else None
        return _AsyncResult(record)


@pytest.mark.asyncio
async def test_review_merge_uses_requested_survivor_person() -> None:
    tx = _Tx(
        [
            {"left_person_id": "person-a", "right_person_id": "person-b"},
            {"absorbed": "person-a", "survivor": "person-b"},
            {"is_locked": False},
            {
                "review_case": {
                    "review_case_id": "case-1",
                    "queue_state": "resolved",
                    "resolution": "merge",
                }
            },
            {"merge_event_id": "merge-1"},
        ]
    )

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "case-1",
        ApiReviewActionType.MERGE.value,
        "resolved",
        "merge",
        "same person",
        None,
        "reviewer@example.com",
        "person-b",
        [],
    )

    assert result == {
        "review_case_id": "case-1",
        "queue_state": "resolved",
        "resolution": "merge",
        "survivor_person_id": "person-b",
        "golden_profile_selections": [],
        "redirected_review_case_ids": [],
    }
    assert [call.query for call in tx.calls[:3]] == [
        GET_PERSONS_FOR_REVIEW_MERGE,
        CHECK_BOTH_PERSONS_ACTIVE,
        CHECK_NO_MATCH_LOCK,
    ]
    merge_call = next(c for c in tx.calls if c.query == EXECUTE_MANUAL_MERGE)
    assert merge_call.params == {
        "from_id": "person-a",
        "to_id": "person-b",
        "reason": "same person",
        "actor_id": "reviewer@example.com",
    }
    # Merge side-effects run after the merge, scoped to the merge event.
    assert [c.query for c in tx.calls[-3:]] == [
        REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT,
        REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT,
        REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    ]
    assert tx.calls[-1].params == {
        "absorbed_id": "person-a",
        "survivor_id": "person-b",
        "merge_event_id": "merge-1",
    }


@pytest.mark.asyncio
async def test_review_merge_returns_survivor_and_golden_profile_selections() -> None:
    selections: list[GoldenProfileSelection] = [
        {
            "field_name": "preferred_nric",
            "source_kind": "identifier",
            "selected_value": "S1234567A",
            "source_record_pk": "sr-1",
            "identifier_type": "nric",
        }
    ]
    tx = _Tx(
        [
            {"left_person_id": "person-a", "right_person_id": "person-b"},
            {"absorbed": "person-a", "survivor": "person-b"},
            {"is_locked": False},
            {
                "review_case": {
                    "review_case_id": "case-1",
                    "queue_state": "resolved",
                    "resolution": "merge",
                }
            },
            {"merge_event_id": "merge-1"},
        ]
    )

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "case-1",
        ApiReviewActionType.MERGE.value,
        "resolved",
        "merge",
        "same person",
        None,
        "reviewer@example.com",
        "person-b",
        selections,
    )

    assert result == {
        "review_case_id": "case-1",
        "queue_state": "resolved",
        "resolution": "merge",
        "survivor_person_id": "person-b",
        "golden_profile_selections": selections,
        "redirected_review_case_ids": [],
    }

    tx = _Tx(
        [
            {"left_person_id": "person-a", "right_person_id": "person-b"},
            {"absorbed": "person-a", "survivor": "person-b"},
            {"is_locked": True},
        ]
    )

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "case-1",
        ApiReviewActionType.MERGE.value,
        "resolved",
        "merge",
        "same person",
        None,
        "reviewer@example.com",
        "person-b",
        [],
    )

    assert result == {"merge_blocked": True}
    assert [call.query for call in tx.calls] == [
        GET_PERSONS_FOR_REVIEW_MERGE,
        CHECK_BOTH_PERSONS_ACTIVE,
        CHECK_NO_MATCH_LOCK,
    ]


@pytest.mark.asyncio
async def test_review_merge_rejects_survivor_outside_review_pair() -> None:
    tx = _Tx([{"left_person_id": "person-a", "right_person_id": "person-b"}])

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "case-1",
        ApiReviewActionType.MERGE.value,
        "resolved",
        "merge",
        "same person",
        None,
        "reviewer@example.com",
        "person-c",
        [],
    )

    assert result == {"merge_not_applicable": True}
    assert [call.query for call in tx.calls] == [GET_PERSONS_FOR_REVIEW_MERGE]


@pytest.mark.asyncio
async def test_manual_no_match_creates_review_lock_after_action() -> None:
    tx = _Tx(
        [
            {
                "review_case": {
                    "review_case_id": "case-1",
                    "queue_state": "resolved",
                    "resolution": "manual_no_match",
                }
            },
            None,
        ]
    )

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "case-1",
        ApiReviewActionType.MANUAL_NO_MATCH.value,
        "resolved",
        "manual_no_match",
        "not the same person",
        None,
        "reviewer@example.com",
        None,
        [],
    )

    assert result == {
        "review_case_id": "case-1",
        "queue_state": "resolved",
        "resolution": "manual_no_match",
        "redirected_review_case_ids": [],
    }
    assert tx.calls[-2].query == CREATE_NO_MATCH_LOCK_FROM_REVIEW
    assert tx.calls[-2].params == {
        "review_case_id": "case-1",
        "notes": "not the same person",
        "actor_id": "reviewer@example.com",
    }
    assert tx.calls[-1].query == MARK_REVIEW_SALES_RECORD_UNRESOLVED


@pytest.mark.asyncio
async def test_merge_sales_link_approves_and_links() -> None:
    """MERGE on a sales review case (no person pair) links Order+Units and returns ActionResult."""
    tx = _Tx(
        [
            None,  # GET_PERSONS_FOR_REVIEW_MERGE → no person pair → sales path
            None,  # GET_PENDING_REVIEW_RECORD
            {"source_record_pk": "sr-42"},
            {
                "claim_token": "claim-1",
                "claim_version": 1,
                "claim_status": "open",
                "claimed_by": "reviewer@example.com",
            },
            None,  # LINK_REVIEW_SALES_PURCHASED_ORDER (result not used)
            None,  # LINK_REVIEW_SALES_BOUGHT_VEHICLE (result not used)
            {"source_record_pk": "sr-42"},  # MARK_REVIEW_SALES_RECORD_LINKED → success
            {
                "review_case": {
                    "review_case_id": "rc-sales",
                    "queue_state": "resolved",
                    "resolution": "merge",
                }
            },
        ]
    )

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "rc-sales",
        ApiReviewActionType.MERGE.value,
        "resolved",
        "merge",
        None,
        None,
        "reviewer@example.com",
        None,
        [],
    )

    assert result == {
        "review_case_id": "rc-sales",
        "queue_state": "resolved",
        "resolution": "merge",
    }
    query_seq = [c.query for c in tx.calls]
    assert query_seq[0] == GET_PERSONS_FOR_REVIEW_MERGE
    assert query_seq[1] == GET_PENDING_REVIEW_RECORD
    assert query_seq[2:7] == [
        GET_REVIEW_SALES_RECORD,
        CLAIM_PENDING_REVIEW_RESOLUTION,
        LINK_REVIEW_SALES_PURCHASED_ORDER,
        LINK_REVIEW_SALES_BOUGHT_VEHICLE,
        MARK_REVIEW_SALES_RECORD_LINKED,
    ]


@pytest.mark.asyncio
async def test_merge_pending_sales_promotes_complete_staging_before_close() -> None:
    tx = _Tx(
        [
            None,
            None,
            {
                "source_record_pk": "sales-v2",
                "lifecycle_status": "pending_review",
                "staged_sales_ready": True,
            },
            {
                "claim_token": "claim-1",
                "claim_version": 1,
                "claim_status": "open",
                "claimed_by": "reviewer@example.com",
            },
            _valid_stage_precheck(),
            {
                "source_record_pk": "sales-v2",
                "promoted_line_count": 1,
                "promoted_observation_count": 1,
            },
            {"source_record_pk": "sales-v2"},
            {
                "review_case": {
                    "review_case_id": "rc-sales",
                    "queue_state": "resolved",
                    "resolution": "merge",
                }
            },
        ]
    )
    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "rc-sales",
        ApiReviewActionType.MERGE.value,
        "resolved",
        "merge",
        None,
        None,
        "reviewer@example.com",
        None,
        [],
    )
    assert result is not None
    queries = [call.query for call in tx.calls]
    assert PRECHECK_STAGED_REVIEW_SALE in queries
    assert PROMOTE_STAGED_REVIEW_SALE in queries
    assert FINALIZE_STAGED_REVIEW_SALE in queries
    assert "SET order += properties(stage)" not in PROMOTE_STAGED_REVIEW_SALE
    assert "lock_version: $stage_lock_version" in PROMOTE_STAGED_REVIEW_SALE
    assert "stage.lock_version = coalesce(stage.lock_version, 0) + 1" in (
        PRECHECK_STAGED_REVIEW_SALE
    )
    promote_call = next(call for call in tx.calls if call.query == PROMOTE_STAGED_REVIEW_SALE)
    assert promote_call.params["stage_lock_version"] == 4
    assert promote_call.params["source_lock_version"] == 7
    assert promote_call.params["stage_hash"] == _valid_stage_precheck()["stage_hash"]
    assert "properties(stage)" not in PROMOTE_STAGED_REVIEW_SALE
    assert "stage.injected_property" not in PROMOTE_STAGED_REVIEW_SALE
    assert "MATCH (entity:Entity {entity_key: stage.entity_key})" in PROMOTE_STAGED_REVIEW_SALE
    assert "entity_key: order.entity_key" not in PROMOTE_STAGED_REVIEW_SALE
    assert "OPTIONAL MATCH (:Order)-[prior_contains:CONTAINS]->(canonical)" in (
        PROMOTE_STAGED_REVIEW_SALE
    )
    assert "OPTIONAL MATCH (canonical)-[prior_product:OF_PRODUCT]->(:Product)" in (
        PROMOTE_STAGED_REVIEW_SALE
    )
    assert PROMOTE_STAGED_REVIEW_SALE.index("DELETE prior_contains") < (
        PROMOTE_STAGED_REVIEW_SALE.index(
            "MERGE (order)-[:CONTAINS {source_record_pk: sr.source_record_pk}]->(canonical)"
        )
    )
    assert PROMOTE_STAGED_REVIEW_SALE.index("DELETE prior_product") < (
        PROMOTE_STAGED_REVIEW_SALE.index(
            "MERGE (canonical)-[:OF_PRODUCT {source_record_pk: sr.source_record_pk}]->(product)"
        )
    )
    assert PRECHECK_STAGED_REVIEW_SALE.index("SET sr.sales_stage_lock_version") < (
        PRECHECK_STAGED_REVIEW_SALE.index("MATCH (stage:StagedSalesOrder")
    )
    assert "sr.sales_stage_lock_version = $source_lock_version" in (PROMOTE_STAGED_REVIEW_SALE)
    assert "other.source_line_item_id = item.source_line_item_id" in (PRECHECK_STAGED_REVIEW_SALE)
    assert "count(DISTINCT canonical) AS promoted_line_count" in (PROMOTE_STAGED_REVIEW_SALE)
    assert LINK_REVIEW_SALES_PURCHASED_ORDER not in queries
    assert LINK_REVIEW_SALES_BOUGHT_VEHICLE not in queries
    assert (
        queries.index(PRECHECK_STAGED_REVIEW_SALE)
        < queries.index(PROMOTE_STAGED_REVIEW_SALE)
        < queries.index(FINALIZE_STAGED_REVIEW_SALE)
        < len(queries) - 1
    )


@pytest.mark.asyncio
async def test_merge_pending_sales_tamper_aborts_before_canonical_promotion() -> None:
    precheck = _valid_stage_precheck()
    lines = cast(list[dict[str, object]], precheck["lines"])
    lines[0]["source_product_id"] = "tampered-product"
    tx = _Tx(
        [
            None,
            None,
            {
                "source_record_pk": "sales-v2",
                "lifecycle_status": "pending_review",
                "staged_sales_ready": True,
            },
            {
                "claim_token": "claim-1",
                "claim_version": 1,
                "claim_status": "open",
                "claimed_by": "reviewer@example.com",
            },
            precheck,
        ]
    )
    with pytest.raises(review_module._ReviewResolutionAbortError):
        await _action_tx(
            cast(AsyncManagedTransaction, tx),
            "rc-sales",
            ApiReviewActionType.MERGE.value,
            "resolved",
            "merge",
            None,
            None,
            "reviewer@example.com",
            None,
            [],
        )
    assert PROMOTE_STAGED_REVIEW_SALE not in [call.query for call in tx.calls]


@pytest.mark.asyncio
async def test_merge_pending_sales_duplicate_source_line_id_aborts_before_promotion() -> None:
    precheck = _valid_stage_precheck()
    lines = cast(list[dict[str, object]], precheck["lines"])
    duplicate: dict[str, object] = {
        "line_index": 1,
        "source_line_item_id": "line-1",
        "source_product_id": "product-2",
    }
    duplicate["line_hash"] = _stage_hash(duplicate)
    lines.append(duplicate)
    precheck["expected_line_count"] = 2
    precheck["stage_hash"] = _stage_hash(
        {
            "order": precheck["order"],
            "lines": lines,
            "observations": precheck["observations"],
        }
    )
    tx = _Tx(
        [
            None,
            None,
            {
                "source_record_pk": "sales-v2",
                "lifecycle_status": "pending_review",
                "staged_sales_ready": True,
            },
            {
                "claim_token": "claim-1",
                "claim_version": 1,
                "claim_status": "open",
                "claimed_by": "reviewer@example.com",
            },
            precheck,
        ]
    )
    with pytest.raises(review_module._ReviewResolutionAbortError):
        await _action_tx(
            cast(AsyncManagedTransaction, tx),
            "rc-sales",
            ApiReviewActionType.MERGE.value,
            "resolved",
            "merge",
            None,
            None,
            "reviewer@example.com",
            None,
            [],
        )
    queries = [call.query for call in tx.calls]
    assert PROMOTE_STAGED_REVIEW_SALE not in queries
    assert FINALIZE_STAGED_REVIEW_SALE not in queries


@pytest.mark.asyncio
async def test_merge_returns_not_applicable_when_no_persons_and_no_sales_link() -> None:
    """MERGE with no person pair and no sales SourceRecord yields merge_not_applicable."""
    tx = _Tx(
        [
            None,  # GET_PERSONS_FOR_REVIEW_MERGE
            None,  # GET_PENDING_REVIEW_RECORD
            None,  # GET_REVIEW_SALES_RECORD
        ]
    )

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "rc-1",
        ApiReviewActionType.MERGE.value,
        "resolved",
        "merge",
        None,
        None,
        "reviewer@example.com",
        None,
        [],
    )

    assert result == {"merge_not_applicable": True}
    assert tx.calls[-1].query == GET_REVIEW_SALES_RECORD


@pytest.mark.asyncio
async def test_reject_marks_sales_record_unresolved() -> None:
    """REJECT action marks any attached sales SourceRecord as unresolved."""
    tx = _Tx(
        [
            None,  # no pending lifecycle SourceRecord (legacy sales case)
            {"source_record_pk": "sales-1"},
            {
                "claim_token": "claim-1",
                "claim_version": 1,
                "claim_status": "open",
                "claimed_by": "reviewer@example.com",
            },
            None,  # MARK_REVIEW_SALES_RECORD_UNRESOLVED
            {
                "review_case": {
                    "review_case_id": "rc-2",
                    "queue_state": "resolved",
                    "resolution": "reject",
                }
            },
        ]
    )

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "rc-2",
        ApiReviewActionType.REJECT.value,
        "resolved",
        "reject",
        None,
        None,
        "reviewer@example.com",
        None,
        [],
    )

    assert result == {
        "review_case_id": "rc-2",
        "queue_state": "resolved",
        "resolution": "reject",
        "redirected_review_case_ids": [],
    }
    assert [call.query for call in tx.calls[1:4]] == [
        GET_REVIEW_SALES_RECORD,
        CLAIM_PENDING_REVIEW_RESOLUTION,
        MARK_REVIEW_SALES_RECORD_UNRESOLVED,
    ]


@pytest.mark.asyncio
async def test_sales_reject_lost_claim_has_zero_mutating_side_effects() -> None:
    tx = _Tx([None, {"source_record_pk": "sales-1"}, None])

    result = await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "case-1",
        ApiReviewActionType.REJECT.value,
        "resolved",
        "reject",
        None,
        None,
        "reviewer@example.com",
        None,
        [],
    )

    assert result == {"merge_not_applicable": True}
    assert [call.query for call in tx.calls] == [
        GET_PENDING_REVIEW_RECORD,
        GET_REVIEW_SALES_RECORD,
        CLAIM_PENDING_REVIEW_RESOLUTION,
    ]


@pytest.mark.asyncio
async def test_sales_approval_lost_final_close_aborts_transaction() -> None:
    tx = _Tx(
        [
            None,
            None,
            {"source_record_pk": "sales-1"},
            {
                "claim_token": "claim-1",
                "claim_version": 1,
                "claim_status": "open",
                "claimed_by": "reviewer@example.com",
            },
            None,
            None,
            {"source_record_pk": "sales-1"},
            None,
        ]
    )

    with pytest.raises(RuntimeError, match="close lost after sales"):
        await _action_tx(
            cast(AsyncManagedTransaction, tx),
            "case-1",
            ApiReviewActionType.MERGE.value,
            "resolved",
            "merge",
            None,
            None,
            "reviewer@example.com",
            None,
            [],
        )
    assert tx.calls[-1].params["claim_token"] == "claim-1"


def test_pending_review_queries_guard_lifecycle_and_source_identity() -> None:
    assert "lifecycle_status: 'pending_review'" in GET_PENDING_REVIEW_RECORD
    assert "FROM_SOURCE" in GET_PENDING_REVIEW_RECORD
    assert "proposed_person_id" in GET_PENDING_REVIEW_RECORD
    assert "pending.record_type <> 'sales'" in GET_PENDING_REVIEW_RECORD
    assert "old.lifecycle_status = 'active'" in ACTIVATE_PENDING_REVIEW_RECORD
    assert "old.lifecycle_status IS NULL" in ACTIVATE_PENDING_REVIEW_RECORD
    assert "old.is_latest" in ACTIVATE_PENDING_REVIEW_RECORD
    assert "pending.expected_active_source_record_pk IS NULL" in ACTIVATE_PENDING_REVIEW_RECORD
    assert (
        "old_versions[0].source_record_pk = pending.expected_active_source_record_pk"
        in ACTIVATE_PENDING_REVIEW_RECORD
    )
    assert "PREVIOUS_VERSION_OF" in ACTIVATE_PENDING_REVIEW_RECORD
    assert "old.is_latest = false" in ACTIVATE_PENDING_REVIEW_RECORD
    assert "pending.is_latest = true" in ACTIVATE_PENDING_REVIEW_RECORD
    assert "lifecycle_status: 'pending_review'" in REJECT_PENDING_REVIEW_RECORD
    assert "rejection_reason" in REJECT_PENDING_REVIEW_RECORD
    assert "pending.is_latest = false" in REJECT_PENDING_REVIEW_RECORD
    assert "item.declarer_source_system_key" in ACTIVATE_PENDING_REVIEW_RECORD


def test_pending_review_vehicle_resolution_supports_safe_serial_fallback() -> None:
    assert "item.normalized_lta_tag IS NULL" in ACTIVATE_PENDING_REVIEW_RECORD
    assert "vehicle.normalized_serial_number = item.normalized_serial_number" in (
        ACTIVATE_PENDING_REVIEW_RECORD
    )
    assert "toLower(trim(vehicle.product)) = toLower(trim(item.product))" in (
        ACTIVATE_PENDING_REVIEW_RECORD
    )
    assert "WHERE size(vehicles) = 1" in ACTIVATE_PENDING_REVIEW_RECORD
    assert "SourceRecordIdentityLock" in ACTIVATE_PENDING_REVIEW_RECORD
    assert "source_system: $source_system_key" in ACTIVATE_PENDING_REVIEW_RECORD
    assert "source_record_id: pending.source_record_id" in ACTIVATE_PENDING_REVIEW_RECORD
    assert "observation.normalized_serial_number" in PROMOTE_STAGED_REVIEW_SALE
    assert "serial_match.normalized_serial_number" in PROMOTE_STAGED_REVIEW_SALE


def test_review_queue_queries_allow_pending_or_explicit_latest_legacy_records() -> None:
    list_query = build_list_review_cases_query(None, None, has_q=False)
    count_query = build_count_review_cases_query(has_q=False)

    for query in (list_query, count_query, ASSIGN_REVIEW_CASE, CLAIM_PENDING_REVIEW_RESOLUTION):
        assert "involved:SourceRecord" in query
        assert "involved.lifecycle_status IS NOT NULL" in query
        assert "involved.lifecycle_status <> 'pending_review'" in query
        assert "involved.lifecycle_status IS NULL" in query
        assert "coalesce(involved.is_latest, false) = false" in query


def test_review_action_allows_latest_legacy_and_rejects_stale_source_records() -> None:
    query = build_review_action_cypher("merge", None)

    assert "involved:SourceRecord" in query
    assert "involved.lifecycle_status IS NOT NULL" in query
    assert "involved.lifecycle_status <> 'pending_review'" in query
    assert "involved.lifecycle_status IS NULL" in query
    assert "coalesce(involved.is_latest, false) = false" in query


def test_claimed_review_close_uses_stable_claim_not_lifecycle_eligibility() -> None:
    query = review_module.build_claimed_review_action_cypher("merge", None)

    assert "rc.lifecycle_claim_token = $claim_token" in query
    assert "rc.lifecycle_claim_version = $claim_version" in query
    assert "rc.lifecycle_claim_status = $claim_status" in query
    assert "rc.lifecycle_claimed_by = $actor_id" in query
    assert "involved:SourceRecord" not in query
    assert "rc.lifecycle_claim_token IS NULL" in CLAIM_PENDING_REVIEW_RESOLUTION
    assert "randomUUID()" in CLAIM_PENDING_REVIEW_RESOLUTION
    assert "claim_version" in CLAIM_PENDING_REVIEW_RESOLUTION


def test_review_activation_only_treats_explicit_latest_legacy_record_as_active() -> None:
    query = ACTIVATE_PENDING_REVIEW_RECORD
    assert "old.lifecycle_status IS NULL AND old.is_latest = true" in query
    assert "coalesce(old.is_latest, true)" not in query


def test_review_activation_scopes_knows_declarer_to_payload_source_system() -> None:
    query = ACTIVATE_PENDING_REVIEW_RECORD
    assert "(declarer_sr)-[:FROM_SOURCE]->(declarer_source:SourceSystem)" in query
    assert "declarer_source.source_key = item.declarer_source_system_key" in query
    assert "declarer_sr.lifecycle_status IS NULL" in query
    assert "declarer_sr.is_latest = true" in query
    assert "coalesce(declarer_sr.is_latest, true)" not in query


def test_review_activation_replaces_sourced_projection_lifecycles_atomically() -> None:
    query = ACTIVATE_PENDING_REVIEW_RECORD
    assert "old_knows.source_record_pk = old.source_record_pk" in query
    assert "old_knows.is_active = false" in query
    assert "old_knows.retired_at = datetime()" in query
    assert "new_knows.is_active = true" in query
    assert "old_bankruptcy.retired_at = datetime()" in query
    assert "bankruptcy_rel.activated_at" in query
    assert "bankruptcy_rel.retired_at = null" in query
    assert "rel.activated_at" in query
    assert "rel.retired_at = null" in query
    assert "rel.source_record_pk = pending.source_record_pk" in query
    assert "KNOWS" not in REJECT_PENDING_REVIEW_RECORD


@pytest.mark.asyncio
async def test_defer_does_not_attempt_pending_record_transition() -> None:
    tx = _Tx(
        [
            {
                "review_case": {
                    "review_case_id": "case-1",
                    "queue_state": "deferred",
                    "resolution": None,
                }
            }
        ]
    )
    await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "case-1",
        ApiReviewActionType.DEFER.value,
        "deferred",
        None,
        None,
        "2026-07-20T00:00:00Z",
        "reviewer@example.com",
        None,
        [],
    )
    assert all(call.query != GET_PENDING_REVIEW_RECORD for call in tx.calls)


@pytest.mark.asyncio
async def test_reject_pending_record_requires_conditional_transition_before_closing() -> None:
    tx = _Tx(
        [
            {"pending_source_record_pk": "new-1"},
            {
                "claim_token": "claim-1",
                "claim_version": 1,
                "claim_status": "open",
                "claimed_by": "reviewer@example.com",
            },
            None,
        ]
    )
    with pytest.raises(RuntimeError, match="rejection lost"):
        await _action_tx(
            cast(AsyncManagedTransaction, tx),
            "case-1",
            ApiReviewActionType.REJECT.value,
            "resolved",
            "reject",
            "bad update",
            None,
            "reviewer@example.com",
            None,
            [],
        )
    assert [call.query for call in tx.calls] == [
        GET_PENDING_REVIEW_RECORD,
        CLAIM_PENDING_REVIEW_RESOLUTION,
        REJECT_PENDING_REVIEW_RECORD,
    ]


@pytest.mark.asyncio
async def test_approve_pending_record_lost_race_does_not_close_review() -> None:
    tx = _Tx(
        [
            None,  # not a person-pair review
            {
                "pending_source_record_pk": "new-1",
                "source_system_key": "pos",
                "source_record_id": "contact-1",
                "normalized_payload": '{"identifiers": [], "addresses": [], "attributes": []}',
                "observed_at": "2026-07-13T00:00:00Z",
                "proposed_person_id": "person-b",
            },
            {
                "claim_token": "claim-1",
                "claim_version": 1,
                "claim_status": "open",
                "claimed_by": "reviewer@example.com",
            },
            None,  # conditional lifecycle transition lost the race
        ]
    )
    with pytest.raises(RuntimeError, match="activation lost"):
        await _action_tx(
            cast(AsyncManagedTransaction, tx),
            "case-1",
            ApiReviewActionType.MERGE.value,
            "resolved",
            "merge",
            None,
            None,
            "reviewer@example.com",
            None,
            [],
        )
    assert [call.query for call in tx.calls] == [
        GET_PERSONS_FOR_REVIEW_MERGE,
        GET_PENDING_REVIEW_RECORD,
        CLAIM_PENDING_REVIEW_RESOLUTION,
        ACTIVATE_PENDING_REVIEW_RECORD,
    ]


class _StateResult:
    def __init__(self, row: Mapping[str, object] | None) -> None:
        self.row = row

    async def single(self) -> Mapping[str, object] | None:
        return self.row


class _LifecycleTx:
    def __init__(
        self,
        *,
        expected_old: str | None,
        active_old: str | None,
        proposed: str = "person-a",
        prior: str = "person-a",
        payload: str | Mapping[str, object] | None = None,
        affected: list[str] | None = None,
        reject_succeeds: bool = True,
        claim_succeeds: bool = True,
        close_succeeds: bool = True,
        source_system_key: str = "bitrix",
        source_record_id: str = "contact-1",
    ) -> None:
        self.expected_old = expected_old
        self.active_old = active_old
        self.proposed = proposed
        self.prior = prior
        self.payload = payload or {
            "identifiers": [],
            "addresses": [],
            "attributes": [],
            "vehicle_mentions": [],
        }
        self.affected = affected or [prior, proposed]
        self.reject_succeeds = reject_succeeds
        self.claim_succeeds = claim_succeeds
        self.close_succeeds = close_succeeds
        self.source_system_key = source_system_key
        self.source_record_id = source_record_id
        self.calls: list[tuple[str, Mapping[str, object]]] = []
        self.events: list[str] = []

    async def run(self, query: str, **params: object) -> _StateResult:
        self.calls.append((query, params))
        if query == GET_PERSONS_FOR_REVIEW_MERGE:
            return _StateResult(None)
        if query == GET_PENDING_REVIEW_RECORD:
            return _StateResult(
                {
                    "pending_source_record_pk": "pending-v2",
                    "source_system_key": self.source_system_key,
                    "source_record_id": self.source_record_id,
                    "normalized_payload": self.payload,
                    "observed_at": "2026-07-13T00:00:00Z",
                    "proposed_person_id": self.proposed,
                    "expected_active_source_record_pk": self.expected_old,
                }
            )
        if query == ACTIVATE_PENDING_REVIEW_RECORD:
            self.events.append("activate")
            if self.active_old != self.expected_old:
                return _StateResult(None)
            return _StateResult(
                {
                    "pending_source_record_pk": "pending-v2",
                    "old_source_record_pks": [] if self.active_old is None else [self.active_old],
                    "approved_person_id": self.proposed,
                    "affected_person_ids": self.affected,
                }
            )
        if query == CLAIM_PENDING_REVIEW_RESOLUTION:
            self.events.append("claim")
            return _StateResult(
                {
                    "claim_token": "claim-1",
                    "claim_version": 1,
                    "claim_status": "open",
                    "claimed_by": "reviewer@example.com",
                }
                if self.claim_succeeds
                else None
            )
        if query == REJECT_PENDING_REVIEW_RECORD:
            self.events.append("reject")
            row = {"pending_source_record_pk": "pending-v2"} if self.reject_succeeds else None
            return _StateResult(row)
        if query == MARK_REVIEW_SALES_RECORD_UNRESOLVED:
            return _StateResult(None)
        if "SET rc.queue_state" in query:
            self.events.append("close")
            if not self.close_succeeds:
                return _StateResult(None)
            return _StateResult(
                {
                    "review_case": {
                        "review_case_id": "case-1",
                        "queue_state": params["new_state"],
                        "resolution": params.get("resolution"),
                    }
                }
            )
        raise AssertionError("unexpected query")


def _activation_call(tx: _LifecycleTx) -> Mapping[str, object]:
    return next(params for query, params in tx.calls if query == ACTIVATE_PENDING_REVIEW_RECORD)


async def _submit_lifecycle(tx: _LifecycleTx, action: ApiReviewActionType) -> object:
    return await _action_tx(
        cast(AsyncManagedTransaction, tx),
        "case-1",
        action.value,
        "resolved",
        action.value,
        "reviewed",
        None,
        "reviewer@example.com",
        None,
        [],
    )


@pytest.mark.asyncio
async def test_pending_replacement_activates_payload_recomputes_once_then_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "identifiers": [
            {
                "identifier_type": "email",
                "normalized_value": "a@example.com",
                "quality_flag": "valid",
                "is_verified": True,
            }
        ],
        "addresses": [],
        "attributes": [],
        "vehicle_mentions": [],
    }
    tx = _LifecycleTx(expected_old="active-v1", active_old="active-v1", payload=payload)

    async def recompute(_tx: object, person_id: str) -> None:
        tx.events.append(f"recompute:{person_id}")

    monkeypatch.setattr(review_module, "recompute_golden_profile_tx", recompute)

    result = await _submit_lifecycle(tx, ApiReviewActionType.MERGE)

    assert result == {
        "review_case_id": "case-1",
        "queue_state": "resolved",
        "resolution": "merge",
        "redirected_review_case_ids": [],
    }
    params = _activation_call(tx)
    assert params["expected_active_source_record_pk"] == "active-v1"
    assert params["identifiers"] == payload["identifiers"]
    assert tx.events == ["claim", "activate", "recompute:person-a", "close"]


@pytest.mark.asyncio
async def test_pending_reassignment_recomputes_distinct_people_in_sorted_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tx = _LifecycleTx(
        expected_old="active-v1",
        active_old="active-v1",
        prior="person-z",
        proposed="person-a",
        affected=["person-z", "person-a", "person-z"],
    )

    async def recompute(_tx: object, person_id: str) -> None:
        tx.events.append(f"recompute:{person_id}")

    monkeypatch.setattr(review_module, "recompute_golden_profile_tx", recompute)

    await _submit_lifecycle(tx, ApiReviewActionType.MERGE)

    assert _activation_call(tx)["approved_person_id"] == "person-a"
    assert tx.events == ["claim", "activate", "recompute:person-a", "recompute:person-z", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("expected,active", [(None, None), ("legacy-v1", "legacy-v1")])
async def test_first_or_legacy_activation_uses_exact_expected_old(
    monkeypatch: pytest.MonkeyPatch,
    expected: str | None,
    active: str | None,
) -> None:
    tx = _LifecycleTx(expected_old=expected, active_old=active)

    async def recompute(_tx: object, person_id: str) -> None:
        tx.events.append(f"recompute:{person_id}")

    monkeypatch.setattr(review_module, "recompute_golden_profile_tx", recompute)
    await _submit_lifecycle(tx, ApiReviewActionType.MERGE)
    assert _activation_call(tx)["expected_active_source_record_pk"] == expected
    assert tx.events[-1] == "close"


@pytest.mark.asyncio
@pytest.mark.parametrize("expected,active", [("active-v1", "active-v3"), (None, "active-v1")])
async def test_stale_expected_old_does_not_recompute_or_close(
    monkeypatch: pytest.MonkeyPatch,
    expected: str | None,
    active: str | None,
) -> None:
    tx = _LifecycleTx(expected_old=expected, active_old=active)

    async def recompute(_tx: object, person_id: str) -> None:
        raise AssertionError("must not recompute")

    monkeypatch.setattr(review_module, "recompute_golden_profile_tx", recompute)
    with pytest.raises(RuntimeError, match="activation lost"):
        await _submit_lifecycle(tx, ApiReviewActionType.MERGE)
    assert tx.events == ["claim", "activate"]


@pytest.mark.asyncio
async def test_pending_reject_transitions_then_closes_without_activation() -> None:
    tx = _LifecycleTx(expected_old="active-v1", active_old="active-v1")
    result = await _submit_lifecycle(tx, ApiReviewActionType.REJECT)
    assert result == {
        "review_case_id": "case-1",
        "queue_state": "resolved",
        "resolution": "reject",
        "redirected_review_case_ids": [],
    }
    assert tx.events == ["claim", "reject", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"identifiers": "invalid", "addresses": [], "attributes": []},
        {
            "identifiers": [],
            "addresses": [],
            "attributes": [],
            "bankruptcy_case": {"source_case_id": 7},
        },
    ],
)
async def test_malformed_activation_payload_fails_before_transition_or_close(
    payload: Mapping[str, object],
) -> None:
    tx = _LifecycleTx(expected_old="active-v1", active_old="active-v1", payload=payload)
    assert await _submit_lifecycle(tx, ApiReviewActionType.MERGE) == {"merge_not_applicable": True}
    assert tx.events == []


@pytest.mark.asyncio
async def test_specialized_blueprints_are_validated_and_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mention = {
        "source_system_key": "whatsapp",
        "source_record_id": "chat-1",
        "quality_flag": "partial_parse",
        "normalized_lta_tag": "LTA7",
        "normalized_serial_number": "SN7",
        "product": "Model 7",
        "raw_context": "asked about it",
        "observed_at": "2026-07-13T00:00:00Z",
        "confidence": 0.6,
    }
    payload = {
        "identifiers": [],
        "addresses": [],
        "attributes": [],
        "vehicle_mentions": [mention],
        "knows_relationships": [
            {
                "declarer_source_record_id": "primary-v1",
                "declarer_source_system_key": "bitrix_chat",
                "relationship_label": "sister",
                "relationship_category": "family",
                "status": "pending",
                "approved_at": None,
                "source_system_key": "whatsapp",
            }
        ],
    }
    tx = _LifecycleTx(
        expected_old="active-v1",
        active_old="active-v1",
        payload=payload,
        source_system_key="whatsapp",
        source_record_id="chat-1",
    )

    async def recompute(_tx: object, person_id: str) -> None:
        tx.events.append(f"recompute:{person_id}")

    monkeypatch.setattr(review_module, "recompute_golden_profile_tx", recompute)
    await _submit_lifecycle(tx, ApiReviewActionType.MERGE)
    params = _activation_call(tx)
    assert params["bankruptcy_cases"] == []
    assert params["vehicle_mentions"][0]["normalized_lta_tag"] == "LTA7"  # type: ignore[index]
    assert params["knows_relationships"][0]["declarer_source_record_id"] == "primary-v1"  # type: ignore[index]
    assert params["pending_source_record_pk"] == "pending-v2"
    assert params["expected_active_source_record_pk"] == "active-v1"
    assert "size(vehicles) = 1" in ACTIVATE_PENDING_REVIEW_RECORD
    assert "rel.source_record_pk = old.source_record_pk" in ACTIVATE_PENDING_REVIEW_RECORD
    assert "mention.is_active = false" in ACTIVATE_PENDING_REVIEW_RECORD


@pytest.mark.asyncio
async def test_pending_fundbox_contact_approval_preserves_exact_source_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_key = "fundbox:contacts"
    payload = {
        "identifiers": [],
        "addresses": [],
        "attributes": [],
        "knows_relationships": [
            {
                "declarer_source_record_id": "fundbox-user-1",
                "declarer_source_system_key": "fundbox",
                "relationship_label": "emergency contact",
                "relationship_category": "emergency_contact",
                "status": "declared",
                "approved_at": None,
                "source_system_key": source_key,
            }
        ],
    }
    tx = _LifecycleTx(
        expected_old="active-v1",
        active_old="active-v1",
        payload=payload,
        source_system_key=source_key,
        source_record_id="contact-7",
    )

    async def recompute(_tx: object, person_id: str) -> None:
        tx.events.append(f"recompute:{person_id}")

    monkeypatch.setattr(review_module, "recompute_golden_profile_tx", recompute)

    await _submit_lifecycle(tx, ApiReviewActionType.MERGE)

    params = _activation_call(tx)
    relationships = params["knows_relationships"]
    assert relationships[0]["source_system_key"] == source_key  # type: ignore[index]
    assert tx.events[-1] == "close"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "specialized",
    [
        {
            "bankruptcy_case": {
                "source_system_key": "other",
                "source_case_id": "case-7",
                "observed_at": "2026-07-13T00:00:00Z",
                "raw_payload": "{}",
            }
        },
        {
            "vehicle_mentions": [
                {
                    "source_system_key": "other",
                    "source_record_id": "contact-1",
                    "quality_flag": "valid",
                    "confidence": 1.0,
                }
            ]
        },
        {
            "vehicle_mentions": [
                {
                    "source_system_key": "bitrix",
                    "source_record_id": "other-1",
                    "quality_flag": "valid",
                    "confidence": 1.0,
                }
            ]
        },
    ],
)
async def test_specialized_blueprint_provenance_mismatch_fails_before_claim(
    specialized: Mapping[str, object],
) -> None:
    payload: dict[str, object] = {
        "identifiers": [],
        "addresses": [],
        "attributes": [],
        **specialized,
    }
    tx = _LifecycleTx(expected_old="active-v1", active_old="active-v1", payload=payload)
    assert await _submit_lifecycle(tx, ApiReviewActionType.MERGE) == {"merge_not_applicable": True}
    assert tx.events == []


@pytest.mark.asyncio
async def test_matching_bankruptcy_provenance_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    bankruptcy = {
        "source_system_key": "sgbankruptcy",
        "source_case_id": "case-7",
        "observed_at": "2026-07-13T00:00:00Z",
        "raw_payload": "{}",
    }
    payload = {"identifiers": [], "addresses": [], "attributes": [], "bankruptcy_case": bankruptcy}
    tx = _LifecycleTx(
        expected_old="active-v1",
        active_old="active-v1",
        payload=payload,
        source_system_key="sgbankruptcy",
    )

    async def recompute(_tx: object, person_id: str) -> None:
        tx.events.append(f"recompute:{person_id}")

    monkeypatch.setattr(review_module, "recompute_golden_profile_tx", recompute)
    await _submit_lifecycle(tx, ApiReviewActionType.MERGE)
    assert _activation_call(tx)["bankruptcy_cases"][0]["source_system_key"] == "sgbankruptcy"  # type: ignore[index]


@pytest.mark.asyncio
async def test_recompute_failure_prevents_review_close(monkeypatch: pytest.MonkeyPatch) -> None:
    tx = _LifecycleTx(expected_old="active-v1", active_old="active-v1")

    async def recompute(_tx: object, person_id: str) -> None:
        tx.events.append(f"recompute:{person_id}")
        raise RuntimeError("golden failure")

    monkeypatch.setattr(review_module, "recompute_golden_profile_tx", recompute)
    with pytest.raises(RuntimeError, match="golden failure"):
        await _submit_lifecycle(tx, ApiReviewActionType.MERGE)
    assert "close" not in tx.events


@pytest.mark.asyncio
async def test_concurrent_claim_loss_prevents_lifecycle_mutation() -> None:
    tx = _LifecycleTx(expected_old="active-v1", active_old="active-v1", claim_succeeds=False)
    assert await _submit_lifecycle(tx, ApiReviewActionType.MERGE) == {"merge_not_applicable": True}
    assert tx.events == ["claim"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [ApiReviewActionType.MERGE, ApiReviewActionType.REJECT])
async def test_close_loss_after_lifecycle_mutation_aborts_transaction(
    monkeypatch: pytest.MonkeyPatch,
    action: ApiReviewActionType,
) -> None:
    tx = _LifecycleTx(expected_old="active-v1", active_old="active-v1", close_succeeds=False)

    async def recompute(_tx: object, person_id: str) -> None:
        tx.events.append(f"recompute:{person_id}")

    monkeypatch.setattr(review_module, "recompute_golden_profile_tx", recompute)
    with pytest.raises(RuntimeError, match="review close lost"):
        await _submit_lifecycle(tx, action)
    assert tx.events[0] == "claim"
    assert tx.events[-1] == "close"
