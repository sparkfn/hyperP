"""Regression tests for atomic sales-record replacement semantics."""

from copy import deepcopy
from typing import cast

import pytest
from neo4j import ManagedTransaction
from src.exclusions import ExclusionContext
from src.graph import queries
from src.models import JsonValue, RecordType, SourceRecordEnvelope
from src.pipeline_sales import (
    _drain_one_pending_sale,
    _finalize_accepted_sale,
    _propose_one_pending_sale,
    _staging_hash,
    drain_pending_customer_sales,
    ingest_sales_record,
    propose_vehicle_matches_for_pending_sales,
)


def test_sales_staging_hash_golden_vector() -> None:
    assert _staging_hash({"a": 1, "b": ["x", None]}) == (
        "18c018603b12c4beed8593acb6ad65cdc9667cce853e8b6dffd035fe3a0fb4de"
    )


def test_sales_stage_serializes_on_source_record_before_first_stage_merge() -> None:
    query = queries.STAGE_SALES_REVIEW
    assert query.index("SET sr.sales_stage_lock_version") < query.index(
        "MERGE (stage:StagedSalesOrder"
    )
    assert "MERGE (stage:StagedSalesOrder {stage_order_key: sr.source_record_pk})" in query


class _Result:
    def __init__(
        self,
        row: dict[str, object] | None = None,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.row = row
        self.rows = rows or []

    def single(self) -> dict[str, object] | None:
        return self.row

    def __iter__(self) -> object:
        return iter(self.rows)


class _Tx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> _Result:
        self.calls.append((query, kwargs))
        if query in {
            queries.ACTIVATE_SOURCE_RECORD_VERSION,
            queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION,
        }:
            return _Result({"source_record_pk": kwargs.get("new_source_record_pk", "new")})
        return _Result()


class _GraphTx(_Tx):
    """Minimal state model routing the actual sales lifecycle query constants."""

    def __init__(self, *, fail_activation: bool = False) -> None:
        super().__init__()
        self.fail_activation = fail_activation
        self.state: dict[str, object] = {
            "versions": {"old": "active", "new": "pending_review"},
            "hashes": {"old": "hash-old", "new": "hash-new"},
            "pending": ["new"],
            "link_status": {"old": "linked", "new": "pending_customer"},
            "latest": "old",
            "order_id": "order-A",
            "contains": {"line-old-1", "line-keep"},
            "of_product": {
                "line-old-1": "product-old-1",
                "line-keep": "product-old-keep",
            },
            "purchase_pks": {"old"},
            "involves_pks": {"old"},
            "bought_pks": {"old"},
            "products": {"shared-product", "product-old-1", "product-old-keep"},
            "vehicles": {"shared-vehicle"},
            "decisions": set(),
            "review_count": 0,
            "activation_count": 0,
            "raw_payloads": {},
            "expected_active": {"new": "old"},
            "identity_resolves": False,
            "candidates": [],
            "source_ids": {"old": "sale-1", "new": "sale-1"},
            "resolvable_pks": set(),
        }

    def run(self, query: str, **kwargs: object) -> _Result:
        result = super().run(query, **kwargs)
        if query == queries.LOCK_AND_GET_SOURCE_STATE:
            versions = cast(dict[str, str], self.state["versions"])
            hashes = cast(dict[str, str], self.state["hashes"])
            source_ids = cast(dict[str, str], self.state["source_ids"])
            rows = [
                {
                    "source_record_pk": pk,
                    "source_record_version": index + 1,
                    "record_hash": hashes[pk],
                    "lifecycle_status": status,
                    "linked_person_ids": [],
                    "max_source_record_version": len(versions),
                }
                for index, (pk, status) in enumerate(versions.items())
                if status in {"active", "pending_review"}
                and source_ids.get(pk) == kwargs["source_record_id"]
            ]
            return _Result(rows=rows)
        if query == queries.FIND_PENDING_CUSTOMER_SALES:
            versions = cast(dict[str, str], self.state["versions"])
            links = cast(dict[str, str], self.state["link_status"])
            payloads = cast(dict[str, dict[str, JsonValue]], self.state["raw_payloads"])
            expected = cast(dict[str, str | None], self.state["expected_active"])
            cursor = cast(str, kwargs["cursor"])
            limit = cast(int, kwargs["limit"])
            rows = sorted(
                [
                    {
                        "source_record_pk": pk,
                        "source_record_id": cast(dict[str, str], self.state["source_ids"])[pk],
                        "expected_active_source_record_pk": expected.get(pk),
                        "source_system_key": "eko_phppos:sales",
                        "raw_payload": payloads[pk],
                    }
                    for pk, status in versions.items()
                    if pk > cursor
                    and status == "pending_review"
                    and links.get(pk) == "pending_customer"
                    and pk in payloads
                ],
                key=lambda row: cast(str, row["source_record_pk"]),
            )
            return _Result(rows=rows[:limit])
        if query == queries.REJECT_PENDING_SOURCE_RECORD:
            pk = cast(str, kwargs["source_record_pk"])
            cast(dict[str, str], self.state["versions"])[pk] = "rejected"
            cast(list[str], self.state["pending"]).remove(pk)
            return _Result({"source_record_pk": pk})
        if query == queries.CREATE_SOURCE_RECORD:
            pk = "incoming"
            cast(dict[str, str], self.state["versions"])[pk] = "pending_review"
            cast(dict[str, str], self.state["hashes"])[pk] = cast(str, kwargs["record_hash"])
            cast(list[str], self.state["pending"]).append(pk)
            cast(dict[str, str], self.state["link_status"])[pk] = cast(str, kwargs["link_status"])
            import json

            cast(dict[str, dict[str, JsonValue]], self.state["raw_payloads"])[pk] = cast(
                dict[str, JsonValue], json.loads(cast(str, kwargs["raw_payload"]))
            )
            cast(dict[str, str | None], self.state["expected_active"])[pk] = cast(
                str | None, kwargs["expected_active_source_record_pk"]
            )
            cast(dict[str, str], self.state["source_ids"])[pk] = cast(
                str, kwargs["source_record_id"]
            )
            return _Result({"source_record_pk": pk})
        if query == queries.RESOLVE_SALES_CUSTOMER:
            resolvable = cast(set[str], self.state["resolvable_pks"])
            if self.state["identity_resolves"] or kwargs["sales_source_record_pk"] in resolvable:
                return _Result({"person_id": "person-1"})
            return _Result()
        if query == queries.FIND_VEHICLE_CANDIDATES_FOR_SALES:
            if cast(set[object], self.state["decisions"]):
                return _Result(rows=[])
            return _Result(rows=cast(list[dict[str, object]], self.state["candidates"]))
        if query == queries.CREATE_MATCH_DECISION:
            decision_id = f"decision-{len(cast(set[object], self.state['decisions'])) + 1}"
            cast(set[object], self.state["decisions"]).add((decision_id, kwargs["decision"]))
            return _Result({"match_decision_id": decision_id})
        if query == queries.CREATE_REVIEW_CASE:
            self.state["review_count"] = cast(int, self.state["review_count"]) + 1
            return _Result({"review_case_id": f"review-{self.state['review_count']}"})
        if query == queries.MARK_SALES_RECORD_LINK_FAILED:
            cast(dict[str, str], self.state["link_status"])[
                cast(str, kwargs["source_record_pk"])
            ] = "link_failed"
        elif query == queries.MARK_SOURCE_RECORD_LINK_FAILED:
            cast(dict[str, str], self.state["versions"])[cast(str, kwargs["source_record_pk"])] = (
                "link_failed"
            )
        elif query == queries.MARK_SALES_RECORD_LINKED:
            cast(dict[str, str], self.state["link_status"])[
                cast(str, kwargs["source_record_pk"])
            ] = "linked"
        elif query == queries.MARK_SALES_RECORD_PENDING_REVIEW:
            cast(dict[str, str], self.state["link_status"])[
                cast(str, kwargs["source_record_pk"])
            ] = "pending_review"
        if query == queries.CLEAR_SUPERSEDED_SALES_LINKS:
            old = kwargs["old_source_record_pk"]
            cast(set[object], self.state["purchase_pks"]).discard(old)
            cast(set[object], self.state["involves_pks"]).discard(old)
            cast(set[object], self.state["bought_pks"]).discard(old)
        elif query == queries.MERGE_ORDER:
            self.state["order_id"] = kwargs["source_order_id"]
        elif query == queries.REPLACE_ORDER_LINES:
            current = set(cast(list[str], kwargs["source_line_item_ids"]))
            old_contains = cast(set[str], self.state["contains"])
            stale = old_contains - current
            self.state["contains"] = old_contains & current
            products = cast(dict[str, str], self.state["of_product"])
            self.state["of_product"] = {
                line_id: product_id
                for line_id, product_id in products.items()
                if line_id not in current and line_id not in stale
            }
        elif query == queries.MERGE_PRODUCT:
            cast(set[object], self.state["products"]).add(kwargs["source_product_id"])
        elif query == queries.STAGE_SALES_REVIEW:
            self.state["staged_sale"] = deepcopy(kwargs)
            return _Result({"source_record_pk": kwargs["source_record_pk"]})
        elif query == queries.MERGE_LINE_ITEM:
            line_id = cast(str, kwargs["source_line_item_id"])
            cast(set[object], self.state["contains"]).add(line_id)
            cast(dict[str, str], self.state["of_product"])[line_id] = cast(
                str, kwargs["source_product_id"]
            )
        elif query == queries.LINK_PERSON_PURCHASED_ORDER:
            cast(set[object], self.state["purchase_pks"]).add(kwargs["source_record_pk"])
        elif query == queries.UPSERT_VEHICLE:
            cast(set[object], self.state["vehicles"]).add("vehicle-new")
            return _Result({"vehicle_id": "vehicle-new", "conflict": False})
        elif query == queries.LINK_ORDER_INVOLVES_VEHICLE:
            cast(set[object], self.state["involves_pks"]).add(kwargs["source_record_pk"])
        elif query == queries.LINK_PERSON_BOUGHT_VEHICLE:
            cast(set[object], self.state["bought_pks"]).add(kwargs["source_record_pk"])
        elif query == queries.ACTIVATE_SOURCE_RECORD_VERSION:
            if self.fail_activation:
                raise RuntimeError("activation failed")
            versions = cast(dict[str, str], self.state["versions"])
            old_pk = cast(str, kwargs["old_source_record_pk"])
            new_pk = cast(str, kwargs["new_source_record_pk"])
            versions[old_pk] = "superseded"
            versions[new_pk] = "active"
            cast(list[str], self.state["pending"]).remove(new_pk)
            self.state["latest"] = new_pk
            self.state["activation_count"] = cast(int, self.state["activation_count"]) + 1
        elif query == queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION:
            versions = cast(dict[str, str], self.state["versions"])
            new_pk = cast(str, kwargs["source_record_pk"])
            versions[new_pk] = "active"
            self.state["pending"] = []
            self.state["latest"] = new_pk
            self.state["activation_count"] = cast(int, self.state["activation_count"]) + 1
        return result

    def atomic(self, callback: object) -> object:
        before = deepcopy(self.state)
        try:
            return callback(cast(ManagedTransaction, self))  # type: ignore[operator]
        except Exception:
            self.state = before
            raise


class _Session:
    def __init__(self, tx: _GraphTx) -> None:
        self.tx = tx

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute_write(self, callback: object) -> object:
        return self.tx.atomic(callback)


class _Client:
    def __init__(self, tx: _GraphTx) -> None:
        self.tx = tx

    def session(self) -> _Session:
        return _Session(self.tx)


def test_sales_customer_link_only_targets_effective_active_identity_versions() -> None:
    for query in (
        queries.LINK_SALES_TO_IDENTITY_RECORD,
        queries.RESOLVE_SALES_CUSTOMER,
    ):
        assert "identity_sr.lifecycle_status = 'active'" in query
        assert "identity_sr.lifecycle_status IS NULL" in query
        assert "identity_sr.is_latest = true" in query
        assert "coalesce(identity_sr.is_latest, true)" not in query
        assert "size(identity_records) = 1" in query

    link_query = queries.LINK_SALES_TO_IDENTITY_RECORD
    assert "OPTIONAL MATCH (sales_sr)-[stale:FOR_CUSTOMER_RECORD]" in link_query
    assert "WHERE stale_identity <> identity_sr" in link_query
    assert "DELETE stale" in link_query
    assert link_query.index("size(identity_records) = 1") < link_query.index("DELETE stale")
    assert "MERGE (sales_sr)-[:FOR_CUSTOMER_RECORD]->(identity_sr)" in link_query


def _envelope(record_hash: str) -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="eko_phppos:sales",
        source_record_id="sale-1",
        record_type=RecordType.SALES,
        observed_at="2026-07-13T00:00:00Z",
        record_hash=record_hash,
        raw_payload=_line_payload(),
    )


@pytest.mark.parametrize(
    ("source_system", "entity_key"),
    [
        ("fundbox:sales", "fundbox"),
        ("eko_phppos:sales", "eko"),
        ("speedzone_phppos:sales", "speedzone"),
        ("onediver:sales", "onediver"),
    ],
)
def test_sales_source_record_creation_supplies_owner_entity_key(
    source_system: str,
    entity_key: str,
) -> None:
    tx = _GraphTx()

    ingest_sales_record(
        cast(object, _Client(tx)),
        _envelope("new-sale").model_copy(update={"source_system": source_system}),
        ingest_run_id=None,
    )

    create_params = next(
        params for query, params in tx.calls if query == queries.CREATE_SOURCE_RECORD
    )
    assert create_params["entity_key"] == entity_key


@pytest.mark.parametrize(
    ("record_hash", "expected_pk"),
    [("hash-old", "old"), ("hash-new", "new")],
)
def test_duplicate_active_or_pending_hash_returns_existing_without_state_change(
    record_hash: str,
    expected_pk: str,
) -> None:
    tx = _GraphTx()
    before = deepcopy(tx.state)
    result = ingest_sales_record(
        cast(object, _Client(tx)),
        _envelope(record_hash),
        ingest_run_id=None,
    )
    assert result.source_record_pk == expected_pk
    assert result.skipped_duplicate
    assert tx.state == before
    assert not any(query == queries.CREATE_SOURCE_RECORD for query, _kwargs in tx.calls)


def _active_only_graph() -> _GraphTx:
    tx = _GraphTx()
    tx.state["versions"] = {"old": "active"}
    tx.state["hashes"] = {"old": "hash-old"}
    tx.state["pending"] = []
    tx.state["link_status"] = {"old": "linked"}
    return tx


def _projection_state(tx: _GraphTx) -> dict[str, object]:
    keys = (
        "latest",
        "order_id",
        "contains",
        "of_product",
        "purchase_pks",
        "involves_pks",
        "bought_pks",
        "products",
        "vehicles",
    )
    return {key: deepcopy(tx.state[key]) for key in keys}


def test_state_model_unresolved_replacement_preserves_exact_active_projection() -> None:
    tx = _active_only_graph()
    before = _projection_state(tx)
    result = ingest_sales_record(
        cast(object, _Client(tx)), _envelope("changed"), ingest_run_id=None
    )
    assert result.source_record_pk == "incoming"
    assert tx.state["versions"] == {"old": "active", "incoming": "pending_review"}
    assert tx.state["pending"] == ["incoming"]
    assert _projection_state(tx) == before


def test_state_model_malformed_replacement_fails_and_preserves_active_projection() -> None:
    tx = _active_only_graph()
    before = _projection_state(tx)
    malformed = _envelope("malformed").model_copy(update={"raw_payload": {"order": None}})
    result = ingest_sales_record(cast(object, _Client(tx)), malformed, ingest_run_id=None)
    assert result.source_record_pk == "incoming"
    assert tx.state["versions"] == {"old": "active", "incoming": "link_failed"}
    assert cast(dict[str, str], tx.state["link_status"])["incoming"] == "link_failed"
    assert tx.state["latest"] == "old"
    assert _projection_state(tx) == before


def test_state_model_newer_update_rejects_pending_without_displacing_active() -> None:
    tx = _GraphTx()
    before = _projection_state(tx)
    ingest_sales_record(cast(object, _Client(tx)), _envelope("newest"), ingest_run_id=None)
    assert cast(dict[str, str], tx.state["versions"])["new"] == "rejected"
    assert cast(dict[str, str], tx.state["versions"])["incoming"] == "pending_review"
    assert tx.state["pending"] == ["incoming"]
    assert _projection_state(tx) == before


def test_sales_customer_resolution_rejects_ambiguous_identity_or_person() -> None:
    query = queries.RESOLVE_SALES_CUSTOMER
    assert "collect(DISTINCT identity_sr)" in query
    assert "size(identity_records) = 1" in query
    assert "collect(DISTINCT p)" in query
    assert "size(persons) = 1" in query


class _IdentityLinkTx(_Tx):
    """State model for the LINK/RESOLVE Cypher lifecycle and cardinality rules."""

    def __init__(self) -> None:
        super().__init__()
        self.identities: dict[str, dict[str, object]] = {}
        self.customer_edges: set[str] = set()

    @staticmethod
    def _is_effective_active(identity: dict[str, object]) -> bool:
        lifecycle = identity.get("lifecycle_status")
        return lifecycle == "active" or (lifecycle is None and identity.get("is_latest") is True)

    def run(self, query: str, **kwargs: object) -> _Result:
        super().run(query, **kwargs)
        if query == queries.LINK_SALES_TO_IDENTITY_RECORD:
            candidates = [
                pk
                for pk, identity in self.identities.items()
                if identity["source_record_id"] == kwargs["identity_source_record_id"]
                and identity["source_system_key"] == kwargs["source_system_key"]
                and self._is_effective_active(identity)
            ]
            if len(candidates) != 1:
                return _Result()
            target = candidates[0]
            self.customer_edges = {target}
            return _Result({"identity_source_record_pk": target})
        if query == queries.RESOLVE_SALES_CUSTOMER:
            effective_edges = [
                pk for pk in self.customer_edges if self._is_effective_active(self.identities[pk])
            ]
            if len(effective_edges) != 1:
                return _Result()
            persons = cast(set[str], self.identities[effective_edges[0]]["person_ids"])
            if len(persons) != 1:
                return _Result()
            return _Result({"person_id": next(iter(persons))})
        return _Result()


def test_sales_customer_relink_replaces_superseded_identity_version() -> None:
    tx = _IdentityLinkTx()
    tx.identities = {
        "v1": {
            "source_record_id": "customer-1",
            "source_system_key": "identity",
            "lifecycle_status": "superseded",
            "is_latest": False,
            "person_ids": {"person-old"},
        },
        "v2": {
            "source_record_id": "customer-1",
            "source_system_key": "identity",
            "lifecycle_status": "active",
            "is_latest": True,
            "person_ids": {"person-new"},
        },
    }
    tx.customer_edges = {"v1"}

    linked = tx.run(
        queries.LINK_SALES_TO_IDENTITY_RECORD,
        sales_source_record_pk="sale-1",
        identity_source_record_id="customer-1",
        source_system_key="identity",
    )
    assert linked.single() == {"identity_source_record_pk": "v2"}
    assert tx.customer_edges == {"v2"}
    assert tx.run(queries.RESOLVE_SALES_CUSTOMER, sales_source_record_pk="sale-1").single() == {
        "person_id": "person-new"
    }


def test_sales_customer_relink_ambiguity_does_not_mutate_existing_edge() -> None:
    tx = _IdentityLinkTx()
    tx.identities = {
        pk: {
            "source_record_id": "customer-1",
            "source_system_key": "identity",
            "lifecycle_status": "active",
            "is_latest": True,
            "person_ids": {f"person-{pk}"},
        }
        for pk in ("v1", "v2")
    }
    tx.customer_edges = {"v1"}
    result = tx.run(
        queries.LINK_SALES_TO_IDENTITY_RECORD,
        sales_source_record_pk="sale-1",
        identity_source_record_id="customer-1",
        source_system_key="identity",
    )
    assert result.single() is None
    assert tx.customer_edges == {"v1"}


@pytest.mark.parametrize("latest_fields", [{}, {"is_latest": None}, {"is_latest": False}])
def test_legacy_identity_requires_explicit_latest_true(
    latest_fields: dict[str, object],
) -> None:
    tx = _IdentityLinkTx()
    legacy: dict[str, object] = {
        "source_record_id": "customer-1",
        "source_system_key": "identity",
        "lifecycle_status": None,
        "person_ids": {"person-legacy"},
    }
    legacy.update(latest_fields)
    tx.identities = {"legacy": legacy}
    tx.customer_edges = {"legacy"}
    link = tx.run(
        queries.LINK_SALES_TO_IDENTITY_RECORD,
        sales_source_record_pk="sale-1",
        identity_source_record_id="customer-1",
        source_system_key="identity",
    )
    assert link.single() is None
    assert tx.run(queries.RESOLVE_SALES_CUSTOMER, sales_source_record_pk="sale-1").single() is None


def test_legacy_identity_with_explicit_latest_true_can_link_and_resolve() -> None:
    tx = _IdentityLinkTx()
    tx.identities = {
        "legacy": {
            "source_record_id": "customer-1",
            "source_system_key": "identity",
            "lifecycle_status": None,
            "is_latest": True,
            "person_ids": {"person-legacy"},
        }
    }
    assert tx.run(
        queries.LINK_SALES_TO_IDENTITY_RECORD,
        sales_source_record_pk="sale-1",
        identity_source_record_id="customer-1",
        source_system_key="identity",
    ).single() == {"identity_source_record_pk": "legacy"}
    assert tx.run(queries.RESOLVE_SALES_CUSTOMER, sales_source_record_pk="sale-1").single() == {
        "person_id": "person-legacy"
    }


def test_accepted_replacement_removes_stale_order_line_and_product_relationships() -> None:
    query = queries.REPLACE_ORDER_LINES
    assert "source_line_item_ids" in query
    assert "DELETE contains" in query
    assert "DELETE old_product" in query
    assert query.count("CALL {") == 2
    assert "WITH DISTINCT o" in query


def test_reused_source_line_id_is_attached_to_only_the_current_order() -> None:
    """A globally keyed line reused upstream must never span two orders."""
    merge_query = " ".join(queries.MERGE_LINE_ITEM.split())

    assert (
        "MERGE (li:LineItem { source_system_key: $source_system_key, "
        "source_line_item_id: $source_line_item_id })" in merge_query
    )
    assert (
        "OPTIONAL MATCH (:Order)-[prior:CONTAINS]->(li) DELETE prior WITH DISTINCT li"
        in merge_query
    )


class _LineProductTx:
    """State model for the source-scoped LineItem-to-Product invariant."""

    def __init__(self) -> None:
        self.product_links: set[tuple[str, str, str]] = set()

    def merge_line(self, *, source: str, line: str, product: str) -> None:
        if "DELETE prior_product" in queries.MERGE_LINE_ITEM:
            self.product_links = {link for link in self.product_links if link[:2] != (source, line)}
        self.product_links.add((source, line, product))


def test_line_item_product_rewire_deletes_old_edge_before_merging_new() -> None:
    query = " ".join(queries.MERGE_LINE_ITEM.split())
    assert (
        "OPTIONAL MATCH (li)-[prior_product:OF_PRODUCT]->(:Product) "
        "DELETE prior_product WITH DISTINCT li" in query
    )
    assert query.index("DELETE prior_product") < query.index("MERGE (li)-[:OF_PRODUCT]->(p)")


def test_line_item_product_rewire_moves_changed_product() -> None:
    tx = _LineProductTx()
    tx.product_links.add(("source-a", "line-1", "product-old"))
    tx.merge_line(source="source-a", line="line-1", product="product-new")
    assert tx.product_links == {("source-a", "line-1", "product-new")}


@pytest.mark.parametrize("existing", [None, "product-1"])
def test_line_item_product_rewire_handles_no_old_or_same_product(
    existing: str | None,
) -> None:
    tx = _LineProductTx()
    if existing is not None:
        tx.product_links.add(("source-a", "line-1", existing))
    tx.merge_line(source="source-a", line="line-1", product="product-1")
    assert tx.product_links == {("source-a", "line-1", "product-1")}


def test_line_item_product_rewire_does_not_touch_same_line_id_from_other_source() -> None:
    tx = _LineProductTx()
    tx.product_links = {
        ("source-a", "line-1", "product-old"),
        ("source-b", "line-1", "product-b"),
    }
    tx.merge_line(source="source-a", line="line-1", product="product-new")
    assert tx.product_links == {
        ("source-a", "line-1", "product-new"),
        ("source-b", "line-1", "product-b"),
    }


def test_sales_retirement_is_strictly_scoped_to_old_source_record() -> None:
    query = queries.CLEAR_SUPERSEDED_SALES_LINKS
    assert query.count("$old_source_record_pk") >= 3
    assert "purchase.source_record_pk = $old_source_record_pk" in query
    assert "unit_rel.source_record_pk = $old_source_record_pk" in query
    assert "bought.source_record_pk = $old_source_record_pk" in query
    assert "source_order_id: $source_order_id" not in query
    assert query.count("CALL {") == 3


def test_pending_sales_scan_has_stable_source_record_cursor() -> None:
    query = queries.FIND_PENDING_CUSTOMER_SALES
    assert "sr.source_record_pk > $cursor" in query
    assert "ORDER BY sr.source_record_pk" in query
    assert query.index("ORDER BY sr.source_record_pk") < query.index("LIMIT $limit")


@pytest.mark.parametrize(
    "customer_link",
    [None, {}, {"identity_source_record_id": 7}, {"identity_source_record_id": "id"}],
)
def test_malformed_deferred_customer_link_fails_without_projection_mutation(
    customer_link: JsonValue,
) -> None:
    tx = _Tx()
    linked = _drain_one_pending_sale(
        cast(ManagedTransaction, tx),
        "new",
        "fundbox:sales",
        {"order": {"source_order_id": "o-1"}, "customer_link": customer_link},
        ExclusionContext(),
        expected_active_source_record_pk="old",
        source_record_id="sale-1",
    )
    assert linked is False
    called = [query for query, _kwargs in tx.calls]
    assert queries.MARK_SOURCE_RECORD_LINK_FAILED in called
    assert queries.MARK_SALES_RECORD_LINK_FAILED in called
    assert queries.CLEAR_SUPERSEDED_SALES_LINKS not in called
    assert queries.MERGE_ORDER not in called


def test_shared_finalizer_retires_then_reconciles_then_activates_last() -> None:
    tx = _Tx()
    accepted = _finalize_accepted_sale(
        cast(ManagedTransaction, tx),
        sales_pk="new",
        person_id="person-1",
        source_system_key="fundbox:sales",
        source_record_id="sale-1",
        raw_payload={
            "order": {"source_order_id": "o-1"},
            "line_items": [],
            "customer_link": {
                "identity_source_record_id": "identity-1",
                "source_system_key": "fundbox",
            },
        },
        observed_at=None,
        exclusion_context=ExclusionContext(),
        expected_active_source_record_pk="old",
    )
    assert accepted is True
    called = [query for query, _kwargs in tx.calls]
    assert called.index(queries.CLEAR_SUPERSEDED_SALES_LINKS) < called.index(queries.MERGE_ORDER)
    assert called.index(queries.MERGE_ORDER) < called.index(queries.REPLACE_ORDER_LINES)
    assert called[-1] == queries.ACTIVATE_SOURCE_RECORD_VERSION


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"order": None},
        {"order": {}},
        {"order": {"source_order_id": ""}},
        {"order": {"source_order_id": "o"}, "line_items": [None]},
        {"order": {"source_order_id": "o"}, "line_items": [{"product": {}}]},
        {
            "order": {"source_order_id": "o"},
            "line_items": [{"source_line_item_id": "li", "product": {}}],
        },
    ],
)
def test_malformed_order_or_lines_terminalize_without_retiring_old(
    payload: dict[str, JsonValue],
) -> None:
    tx = _Tx()
    assert not _drain_one_pending_sale(
        cast(ManagedTransaction, tx),
        "new",
        "eko_phppos:sales",
        payload,
        ExclusionContext(),
        expected_active_source_record_pk="old",
        source_record_id="sale-1",
    )
    called = [query for query, _kwargs in tx.calls]
    assert queries.MARK_SOURCE_RECORD_LINK_FAILED in called
    assert queries.CLEAR_SUPERSEDED_SALES_LINKS not in called
    assert queries.ACTIVATE_SOURCE_RECORD_VERSION not in called


def test_vehicle_proposal_without_lifecycle_context_performs_no_writes() -> None:
    tx = _Tx()
    assert not _propose_one_pending_sale(
        cast(ManagedTransaction, tx),
        source_record_pk="new",
        source_system_key="eko_phppos:sales",
        source_order_id="o",
        customer_nric=None,
        customer_emails=[],
        customer_phones=[],
    )
    assert tx.calls == []


def _corrected_order_payload() -> dict[str, JsonValue]:
    return {
        "order": {"source_order_id": "order-B"},
        "line_items": [],
        "customer_link": {
            "identity_source_record_id": "identity-1",
            "source_system_key": "eko_phppos",
        },
    }


def test_state_model_corrected_order_retires_old_pk_and_activates_new() -> None:
    tx = _GraphTx()
    tx.atomic(
        lambda managed: _finalize_accepted_sale(
            managed,
            sales_pk="new",
            person_id="person-1",
            source_system_key="eko_phppos:sales",
            source_record_id="sale-1",
            raw_payload=_corrected_order_payload(),
            observed_at=None,
            exclusion_context=ExclusionContext(),
            expected_active_source_record_pk="old",
        )
    )
    assert tx.state["versions"] == {"old": "superseded", "new": "active"}
    assert tx.state["latest"] == "new"
    assert tx.state["order_id"] == "order-B"
    assert tx.state["purchase_pks"] == {"new"}
    assert tx.state["involves_pks"] == set()
    assert tx.state["bought_pks"] == set()
    assert tx.state["products"] == {
        "shared-product",
        "product-old-1",
        "product-old-keep",
    }
    assert tx.state["vehicles"] == {"shared-vehicle"}


def test_state_model_activation_failure_rolls_back_projection_replacement() -> None:
    tx = _GraphTx(fail_activation=True)
    before = deepcopy(tx.state)
    with pytest.raises(RuntimeError, match="activation failed"):
        tx.atomic(
            lambda managed: _finalize_accepted_sale(
                managed,
                sales_pk="new",
                person_id="person-1",
                source_system_key="eko_phppos:sales",
                source_record_id="sale-1",
                raw_payload=_corrected_order_payload(),
                observed_at=None,
                exclusion_context=ExclusionContext(),
                expected_active_source_record_pk="old",
            )
        )
    assert tx.state == before


def _line_payload() -> dict[str, JsonValue]:
    return {
        "order": {"source_order_id": "order-B"},
        "line_items": [
            {
                "source_line_item_id": "line-keep",
                "product": {
                    "source_product_id": "product-new-keep",
                    "category": "Accessory",
                },
            },
            {
                "source_line_item_id": "line-new",
                "product": {
                    "source_product_id": "product-new",
                    "sku": "EBIKE-NEW",
                    "name": "New bike",
                    "category": "Electric Bicycles",
                },
                "metadata": {"serial_number": "SERIAL-NEW"},
            },
        ],
        "customer_link": {
            "identity_source_record_id": "identity-1",
            "source_system_key": "eko_phppos",
        },
    }


def test_state_model_reconciles_lines_and_preserves_shared_nodes() -> None:
    tx = _GraphTx()
    _finalize_accepted_sale(
        cast(ManagedTransaction, tx),
        sales_pk="new",
        person_id="person-1",
        source_system_key="eko_phppos:sales",
        source_record_id="sale-1",
        raw_payload=_line_payload(),
        observed_at=None,
        exclusion_context=ExclusionContext(),
        expected_active_source_record_pk="old",
    )
    assert tx.state["contains"] == {"line-keep", "line-new"}
    assert tx.state["of_product"] == {
        "line-keep": "product-new-keep",
        "line-new": "product-new",
    }
    assert cast(set[str], tx.state["products"]) >= {
        "product-old-1",
        "product-old-keep",
        "product-new-keep",
        "product-new",
    }
    assert tx.state["purchase_pks"] == {"new"}
    assert tx.state["involves_pks"] == {"new"}
    assert tx.state["bought_pks"] == {"new"}
    assert cast(set[str], tx.state["vehicles"]) >= {"shared-vehicle", "vehicle-new"}
    assert tx.state["activation_count"] == 1


def test_state_model_first_activation_is_guarded_and_exactly_once() -> None:
    tx = _GraphTx()
    tx.state["versions"] = {"new": "pending_review"}
    tx.state["pending"] = ["new"]
    tx.state["latest"] = None
    tx.state["purchase_pks"] = set()
    tx.state["involves_pks"] = set()
    tx.state["bought_pks"] = set()
    _finalize_accepted_sale(
        cast(ManagedTransaction, tx),
        sales_pk="new",
        person_id="person-1",
        source_system_key="eko_phppos:sales",
        source_record_id="sale-1",
        raw_payload=_line_payload(),
        observed_at=None,
        exclusion_context=ExclusionContext(),
        expected_active_source_record_pk=None,
    )
    assert tx.state["versions"] == {"new": "active"}
    assert tx.state["latest"] == "new"
    assert tx.state["pending"] == []
    assert tx.state["activation_count"] == 1


def _candidate(person_id: str, *, nric_blocked: bool = False) -> dict[str, object]:
    return {
        "person_id": person_id,
        "vehicle_id": "shared-vehicle",
        "rel_type": "OWNS_VEHICLE",
        "is_active": True,
        "conflict_flag": False,
        "last_confirmed_at": "2026-07-13T00:00:00Z",
        "contact_channels": ["email"],
        "nric_blocked": nric_blocked,
    }


def _pending_replacement_graph(candidates: list[dict[str, object]]) -> _GraphTx:
    tx = _GraphTx()
    cast(dict[str, dict[str, JsonValue]], tx.state["raw_payloads"])["new"] = _line_payload()
    tx.state["candidates"] = candidates
    return tx


def test_public_drain_accepts_first_pending_once_and_second_run_is_noop() -> None:
    tx = _active_only_graph()
    tx.state["versions"] = {}
    tx.state["hashes"] = {}
    tx.state["latest"] = None
    tx.state["order_id"] = None
    tx.state["contains"] = set()
    tx.state["of_product"] = {}
    tx.state["purchase_pks"] = set()
    tx.state["involves_pks"] = set()
    tx.state["bought_pks"] = set()
    ingest_sales_record(cast(object, _Client(tx)), _envelope("first"), ingest_run_id=None)
    assert tx.state["pending"] == ["incoming"]
    assert tx.state["latest"] is None
    assert tx.state["order_id"] is None
    tx.state["identity_resolves"] = True
    assert drain_pending_customer_sales(cast(object, _Client(tx))) == 1
    assert tx.state["versions"] == {"incoming": "active"}
    assert tx.state["latest"] == "incoming"
    assert tx.state["order_id"] == "order-B"
    assert tx.state["purchase_pks"] == {"incoming"}
    assert tx.state["involves_pks"] == {"incoming"}
    assert tx.state["bought_pks"] == {"incoming"}
    assert tx.state["activation_count"] == 1
    assert drain_pending_customer_sales(cast(object, _Client(tx))) == 0
    assert tx.state["activation_count"] == 1


def test_public_drain_cursor_reaches_resolvable_and_malformed_later_page() -> None:
    tx = _active_only_graph()
    pks = ("a", "b", "c", "d")
    tx.state["versions"] = {pk: "pending_review" for pk in pks}
    tx.state["hashes"] = {pk: f"hash-{pk}" for pk in pks}
    tx.state["pending"] = list(pks)
    tx.state["latest"] = None
    tx.state["link_status"] = {pk: "pending_customer" for pk in pks}
    tx.state["source_ids"] = {pk: f"sale-{pk}" for pk in pks}
    tx.state["expected_active"] = {pk: None for pk in pks}
    tx.state["raw_payloads"] = {
        "a": _line_payload(),
        "b": _line_payload(),
        "c": _line_payload(),
        "d": {"order": None},
    }
    tx.state["resolvable_pks"] = {"c"}
    assert drain_pending_customer_sales(cast(object, _Client(tx)), batch_size=2) == 1
    assert cast(dict[str, str], tx.state["versions"])["c"] == "active"
    assert cast(dict[str, str], tx.state["versions"])["d"] == "link_failed"
    assert cast(dict[str, str], tx.state["versions"])["a"] == "pending_review"
    assert cast(dict[str, str], tx.state["versions"])["b"] == "pending_review"
    first_cursors = [
        kwargs["cursor"]
        for query, kwargs in tx.calls
        if query == queries.FIND_PENDING_CUSTOMER_SALES
    ]
    assert first_cursors == ["", "b", "d"]
    assert tx.state["activation_count"] == 1
    tx.calls.clear()
    assert drain_pending_customer_sales(cast(object, _Client(tx)), batch_size=2) == 0
    assert tx.state["activation_count"] == 1


def test_public_vehicle_merge_replaces_active_once_and_repeat_is_noop() -> None:
    tx = _pending_replacement_graph([_candidate("person-1")])
    assert propose_vehicle_matches_for_pending_sales(cast(object, _Client(tx))) == 1
    assert tx.state["versions"] == {"old": "superseded", "new": "active"}
    assert tx.state["latest"] == "new"
    assert tx.state["purchase_pks"] == {"new"}
    assert tx.state["involves_pks"] == {"new"}
    assert tx.state["bought_pks"] == {"new"}
    assert len(cast(set[object], tx.state["decisions"])) == 1
    assert tx.state["activation_count"] == 1
    assert propose_vehicle_matches_for_pending_sales(cast(object, _Client(tx))) == 0
    assert len(cast(set[object], tx.state["decisions"])) == 1
    assert tx.state["activation_count"] == 1


def test_public_vehicle_review_preserves_old_and_is_not_duplicated() -> None:
    tx = _pending_replacement_graph([_candidate("person-1"), _candidate("person-2")])
    before = _projection_state(tx)
    assert propose_vehicle_matches_for_pending_sales(cast(object, _Client(tx))) == 1
    after = _projection_state(tx)
    for key in ("purchase_pks", "involves_pks", "bought_pks", "contains", "of_product"):
        assert after[key] == before[key]
    assert tx.state["staged_sale"] is not None
    staged = cast(dict[str, object], tx.state["staged_sale"])
    assert staged["source_record_id"] == "sale-1"
    assert len(cast(list[object], staged["lines"])) == 2
    observations = cast(list[dict[str, object]], staged["observations"])
    assert len(observations) == 1
    assert observations[0]["raw_context"] == "line-new"
    assert observations[0]["confidence"] == 1.0
    assert observations[0]["observation_hash"]
    assert staged["stage_hash"]
    assert tx.state["versions"] == {"old": "active", "new": "pending_review"}
    assert cast(dict[str, str], tx.state["link_status"])["new"] == "pending_review"
    assert len(cast(set[object], tx.state["decisions"])) == 1
    assert tx.state["review_count"] == 1
    assert tx.state["activation_count"] == 0
    assert propose_vehicle_matches_for_pending_sales(cast(object, _Client(tx))) == 0
    assert len(cast(set[object], tx.state["decisions"])) == 1
    assert tx.state["review_count"] == 1


def test_public_vehicle_no_match_preserves_old_and_is_not_duplicated() -> None:
    tx = _pending_replacement_graph([_candidate("person-1", nric_blocked=True)])
    before = _projection_state(tx)
    assert propose_vehicle_matches_for_pending_sales(cast(object, _Client(tx))) == 1
    assert _projection_state(tx) == before
    assert tx.state["versions"] == {"old": "active", "new": "pending_review"}
    assert cast(dict[str, str], tx.state["link_status"])["new"] == "pending_customer"
    assert len(cast(set[object], tx.state["decisions"])) == 1
    assert tx.state["review_count"] == 0
    assert tx.state["activation_count"] == 0
    assert propose_vehicle_matches_for_pending_sales(cast(object, _Client(tx))) == 0
    assert len(cast(set[object], tx.state["decisions"])) == 1
